"""aria2 RPC 方法处理器

为外部 aria2 兼容客户端（如 AriaNg、Motrix）提供 RPC 方法实现。
实现用户隔离、数据脱敏、配额检查等安全机制。

基于 v0 shared download tables（global_downloads + user_tasks）。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import shutil
from pathlib import Path
from typing import Any, Protocol, Sequence
from urllib.parse import unquote, urlsplit

from app.core.config import settings
from app.core.security import mask_url_credentials
from app.core.state import AppState
from app.routers.config import get_min_free_disk
from app.repositories import auth as auth_repo
from app.repositories.downloads import (
    ACTIVE_USER_TASK_STATUSES,
    delete_all_terminal_user_tasks,
    delete_terminal_user_task,
    delete_terminal_user_task_by_gid,
    get_user_task_by_gid,
    get_user_task_by_id,
    list_user_tasks,
)
from app.services import rpc_view_service
from app.services.download_service import (
    cancel_user_task,
    create_user_download,
    create_user_torrent_download,
)
from app.services.hash import extract_info_hash_from_torrent_base64, get_uri_hash
from app.services.task_projection import (
    ACTIVE_LIKE_STATUSES,
    has_real_file_path,
    is_current,
    speed_totals,
    stat_counts,
)
from app.services.usage_service import get_usage


logger = logging.getLogger(__name__)

DEFAULT_STATUS_DOWNLOADS = "0"
DEFAULT_STATUS_WAITING = "waiting"
DEFAULT_STATUS_ERROR = "error"
DEFAULT_BOOL_FALSE = "false"
DEFAULT_ERROR_CODE = "0"

ALLOWED_STATUS_VALUES = {
    "active",
    "waiting",
    "paused",
    "error",
    "complete",
    "removed",
}


class RpcBackendClient(Protocol):
    async def add_uri(self, uris: list[str], options: dict | None = None) -> str: ...
    async def add_torrent(
        self,
        torrent: str,
        uris: list[str] | None = None,
        options: dict | None = None,
    ) -> str: ...
    async def tell_status(self, gid: str) -> dict: ...
    async def tell_active(self) -> list[dict]: ...
    async def tell_waiting(self, offset: int = 0, num: int = 1000) -> list[dict]: ...
    async def remove_download_result(self, gid: str) -> str: ...
    async def force_remove(self, gid: str) -> str: ...
    async def get_global_stat(self) -> dict: ...
    async def get_files(self, gid: str) -> list[dict]: ...
    async def get_uris(self, gid: str) -> list[dict]: ...
    async def get_peers(self, gid: str) -> list[dict]: ...
    async def get_servers(self, gid: str) -> list[dict]: ...
    async def get_version(self) -> dict: ...


# JSON-RPC 2.0 错误码
class RpcErrorCode:
    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603
    # 自定义错误码
    TASK_NOT_FOUND = 1
    PERMISSION_DENIED = 2
    QUOTA_EXCEEDED = 3


class RpcError(Exception):
    """JSON-RPC 错误"""

    def __init__(self, code: int, message: str, data: Any = None):
        self.code = code
        self.message = message
        self.data = data
        super().__init__(message)

    def to_dict(self) -> dict:
        error = {"code": self.code, "message": self.message}
        if self.data is not None:
            error["data"] = self.data
        return error


class Aria2RpcHandler:
    """aria2 RPC 方法处理器

    为每个用户提供隔离的 aria2 RPC 接口。
    所有操作只能访问当前用户的任务和文件。
    """

    # 支持的 RPC 方法列表
    SUPPORTED_METHODS = [
        "aria2.addUri",
        "aria2.addTorrent",
        "aria2.remove",
        "aria2.forceRemove",
        "aria2.pause",
        "aria2.forcePause",
        "aria2.unpause",
        "aria2.tellStatus",
        "aria2.tellActive",
        "aria2.tellWaiting",
        "aria2.tellStopped",
        "aria2.getFiles",
        "aria2.getUris",
        "aria2.getGlobalStat",
        "aria2.getVersion",
        "aria2.changePosition",
        "aria2.getOption",
        "aria2.getGlobalOption",
        "aria2.saveSession",
        "aria2.purgeDownloadResult",
        "aria2.removeDownloadResult",
        "aria2.pauseAll",
        "aria2.forcePauseAll",
        "aria2.unpauseAll",
        "aria2.getSessionInfo",
        "aria2.getPeers",
        "aria2.getServers",
        "aria2.changeUri",
        "system.listMethods",
        "system.multicall",
    ]

    def __init__(
        self, user_id: int, aria2_client: RpcBackendClient, app_state: AppState
    ):
        self.user_id = user_id
        self.client = aria2_client
        if app_state is None:
            raise RuntimeError("AppState is required for Aria2RpcHandler")
        self.app_state: AppState = app_state
        self._multicall_depth: int = 0

    async def handle(self, method: str, params: list) -> Any:
        """路由到具体方法处理"""
        handler_name = self._get_handler_name(method)
        handler = getattr(self, handler_name, None)

        if handler is None:
            raise RpcError(RpcErrorCode.METHOD_NOT_FOUND, f"Method not found: {method}")

        return await handler(params)

    def _get_handler_name(self, method: str) -> str:
        """将 RPC 方法名转换为处理器方法名

        aria2.addUri -> _handle_add_uri
        system.listMethods -> _handle_system_list_methods
        """
        if method.startswith("aria2."):
            name = method[6:]
        elif method.startswith("system."):
            name = "system_" + method[7:]
        else:
            name = method

        result = []
        for i, char in enumerate(name):
            if char.isupper() and i > 0:
                result.append("_")
            result.append(char.lower())

        return "_handle_" + "".join(result)

    def _sanitize_file_path(self, path: str) -> str:
        if not path:
            return path
        return Path(path).name

    async def _verify_task_owner(self, gid: str) -> dict[str, Any] | None:
        """Return the current user's v0 task row for an aria2 gid."""
        return await get_user_task_by_gid(self.user_id, gid)

    async def _resolve_owned_row(self, gid: str) -> dict[str, Any] | None:
        """Resolve a client-facing gid to the current user's task row.

        The only identity exposed to clients is ``task-{id}``; a raw aria2 gid
        is still accepted for backward compatibility. ``hist-`` gids never map
        to a live task.
        """
        _, task_id, history_id = self._parse_history_gid(gid)
        if history_id is not None:
            return None
        if task_id is not None:
            return await self._get_task_pair_by_task_id(task_id)
        return await self._verify_task_owner(gid)

    async def _fetch_live_status(self, row: dict[str, Any]) -> dict[str, Any] | None:
        """Fetch sanitized live aria2 status via the real backend gid."""
        real_gid = str(row.get("aria2_gid") or "")
        if not real_gid or not is_current(row):
            return None
        try:
            return self._sanitize_status(await self.client.tell_status(real_gid))
        except Exception as exc:
            logger.debug(
                "live tellStatus failed for task=%s user_id=%s",
                row.get("id"),
                self.user_id,
                exc_info=exc,
            )
            return None

    async def _get_user_gids(
        self,
        sub_statuses: list[str] | None = None,
        task_statuses: list[str] | None = None,
    ) -> set[str]:
        """获取用户指定状态的任务 gid 集合"""
        rows = await list_user_tasks(self.user_id, sub_statuses)
        gids: set[str] = set()
        for row in rows:
            gid = row.get("aria2_gid")
            if not gid:
                continue
            if task_statuses and row.get("global_status") not in task_statuses:
                continue
            gids.add(str(gid))
        return gids

    async def _get_current_rows_by_gid(self) -> dict[str, dict[str, Any]]:
        rows = await list_user_tasks(self.user_id, ACTIVE_LIKE_STATUSES)
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            gid = row.get("aria2_gid")
            if gid and is_current(row):
                result[str(gid)] = row
        return result

    @staticmethod
    def _extract_scalar_value(value: Any) -> Any:
        scalar: Any = value
        if isinstance(scalar, (tuple, list)):
            if not scalar:
                return None
            return scalar[0]

        mapping = getattr(scalar, "_mapping", None)
        if mapping:
            try:
                return next(iter(mapping.values()))
            except StopIteration:
                return None
        return scalar

    @classmethod
    def _to_int_scalar(cls, value: Any, default: int = 0) -> int:
        scalar = cls._extract_scalar_value(value)
        if scalar is None:
            return default
        try:
            return int(scalar)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _normalize_gid_collection(values: Any) -> set[str]:
        gids: set[str] = set()
        for raw in values:
            value = Aria2RpcHandler._extract_scalar_value(raw)

            if value is None:
                continue

            gid = str(value)
            if gid:
                gids.add(gid)
        return gids

    @staticmethod
    def _extract_name_from_uri(uri: str) -> str:
        if not uri:
            return ""
        parsed = urlsplit(uri)
        if not parsed.path:
            return ""
        decoded_path = unquote(parsed.path)
        return Path(decoded_path).name

    @staticmethod
    def _status_str(value: Any, default: str = DEFAULT_STATUS_DOWNLOADS) -> str:
        if value is None:
            return default
        return str(value)

    @staticmethod
    def _status_bool(value: Any, default: str = DEFAULT_BOOL_FALSE) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "false"}:
                return normalized
        return default

    @staticmethod
    def _normalize_status_value(
        value: Any, default: str = DEFAULT_STATUS_WAITING
    ) -> str:
        status = str(value).strip().lower() if value is not None else default
        if status in ALLOWED_STATUS_VALUES:
            return status
        return default

    @staticmethod
    def _new_file_payload() -> dict[str, Any]:
        return {
            "index": "1",
            "path": "",
            "length": "0",
            "completedLength": "0",
            "selected": "true",
            "uris": [],
        }

    def _new_status_payload(self) -> dict[str, Any]:
        return {
            "gid": "",
            "status": DEFAULT_STATUS_WAITING,
            "totalLength": DEFAULT_STATUS_DOWNLOADS,
            "completedLength": DEFAULT_STATUS_DOWNLOADS,
            "uploadLength": DEFAULT_STATUS_DOWNLOADS,
            "downloadSpeed": DEFAULT_STATUS_DOWNLOADS,
            "uploadSpeed": DEFAULT_STATUS_DOWNLOADS,
            "pieceLength": DEFAULT_STATUS_DOWNLOADS,
            "numPieces": DEFAULT_STATUS_DOWNLOADS,
            "connections": DEFAULT_STATUS_DOWNLOADS,
            "errorCode": DEFAULT_ERROR_CODE,
            "errorMessage": "",
            "dir": "",
            "files": [],
            "infoHash": "",
            "numSeeders": DEFAULT_STATUS_DOWNLOADS,
            "seeder": DEFAULT_BOOL_FALSE,
            "bittorrent": {
                "announceList": [],
                "comment": "",
                "creationDate": "0",
                "mode": "single",
                "info": {"name": ""},
            },
        }

    @staticmethod
    def _extract_status_keys(params: list, index: int) -> list[str] | None:
        if len(params) <= index:
            return None
        keys = params[index]
        if keys is None:
            return None
        if not isinstance(keys, list):
            raise RpcError(RpcErrorCode.INVALID_PARAMS, "keys must be an array")
        normalized = [
            str(item) for item in keys if isinstance(item, str) and item.strip()
        ]
        if not normalized:
            return None
        return normalized

    @staticmethod
    def _apply_status_keys(status: dict, keys: list[str] | None) -> dict:
        if not keys:
            return status
        return {key: status[key] for key in keys if key in status}

    def _sanitize_files(self, files: Any) -> list[dict]:
        if not isinstance(files, list):
            return []

        sanitized_files: list[dict] = []
        for item in files:
            if not isinstance(item, dict):
                continue
            file_data = self._new_file_payload()
            file_data["index"] = self._status_str(item.get("index"), file_data["index"])
            file_data["length"] = self._status_str(
                item.get("length"), file_data["length"]
            )
            file_data["completedLength"] = self._status_str(
                item.get("completedLength"), file_data["completedLength"]
            )
            file_data["selected"] = self._status_bool(
                item.get("selected"), file_data["selected"]
            )
            file_data["path"] = self._sanitize_file_path(
                self._status_str(item.get("path"), "")
            )
            sanitized_files.append(file_data)
        return sanitized_files

    def _sanitize_bittorrent(self, bt_info: Any) -> dict:
        sanitized = {
            "announceList": [],
            "info": {"name": ""},
        }
        if not isinstance(bt_info, dict):
            return sanitized

        info = bt_info.get("info")
        if isinstance(info, dict):
            name = info.get("name")
            if isinstance(name, str):
                sanitized["info"]["name"] = name

        return sanitized

    @staticmethod
    def _new_peer_payload() -> dict[str, str]:
        return {
            "peerId": "masked-peer",
            "ip": "0.0.0.0",
            "port": "0",
            "bitfield": "",
            "amChoking": DEFAULT_BOOL_FALSE,
            "peerChoking": DEFAULT_BOOL_FALSE,
            "downloadSpeed": DEFAULT_STATUS_DOWNLOADS,
            "uploadSpeed": DEFAULT_STATUS_DOWNLOADS,
            "seeder": DEFAULT_BOOL_FALSE,
        }

    @staticmethod
    def _new_server_payload() -> dict[str, str]:
        return {
            "uri": "",
            "currentUri": "",
            "downloadSpeed": DEFAULT_STATUS_DOWNLOADS,
        }

    def _sanitize_peers(self, peers: Any) -> list[dict]:
        if not isinstance(peers, list):
            return []
        sanitized_peers: list[dict] = []
        for item in peers:
            if not isinstance(item, dict):
                continue
            peer = self._new_peer_payload()
            peer["bitfield"] = self._status_str(item.get("bitfield"), "")
            peer["amChoking"] = self._status_bool(
                item.get("amChoking"), DEFAULT_BOOL_FALSE
            )
            peer["peerChoking"] = self._status_bool(
                item.get("peerChoking"), DEFAULT_BOOL_FALSE
            )
            peer["downloadSpeed"] = self._status_str(
                item.get("downloadSpeed"), DEFAULT_STATUS_DOWNLOADS
            )
            peer["uploadSpeed"] = self._status_str(
                item.get("uploadSpeed"), DEFAULT_STATUS_DOWNLOADS
            )
            peer["seeder"] = self._status_bool(item.get("seeder"), DEFAULT_BOOL_FALSE)
            sanitized_peers.append(peer)
        return sanitized_peers

    def _sanitize_servers(self, server_groups: Any) -> list[dict]:
        if not isinstance(server_groups, list):
            return []

        sanitized_groups: list[dict] = []
        for group in server_groups:
            if not isinstance(group, dict):
                continue
            index = self._status_str(group.get("index"), "1")
            servers = group.get("servers")
            sanitized_servers: list[dict] = []
            if isinstance(servers, list):
                for server in servers:
                    if not isinstance(server, dict):
                        continue
                    payload = self._new_server_payload()
                    payload["downloadSpeed"] = self._status_str(
                        server.get("downloadSpeed"), DEFAULT_STATUS_DOWNLOADS
                    )
                    sanitized_servers.append(payload)
            sanitized_groups.append({"index": index, "servers": sanitized_servers})
        return sanitized_groups

    def _sanitize_uris(self, uris: Any) -> list[dict]:
        if not isinstance(uris, list):
            return []
        sanitized_uris: list[dict] = []
        for item in uris:
            if not isinstance(item, dict):
                continue
            uri = mask_url_credentials(self._status_str(item.get("uri"), ""))
            status = self._status_str(item.get("status"), "waiting")
            if status not in {"used", "waiting"}:
                status = "waiting"
            sanitized_uris.append({"uri": uri, "status": status})
        return sanitized_uris

    def _sanitize_version(self, version_info: Any) -> dict:
        if not isinstance(version_info, dict):
            return {"version": "aria2deck-proxy", "enabledFeatures": []}

        version = self._status_str(version_info.get("version"), "aria2deck-proxy")
        features = version_info.get("enabledFeatures")
        if isinstance(features, list):
            enabled_features = [
                str(feature) for feature in features if isinstance(feature, str)
            ]
        else:
            enabled_features = []
        return {"version": version, "enabledFeatures": enabled_features}

    @staticmethod
    def _is_bittorrent_uri(uri: str) -> bool:
        return uri.startswith("magnet:") or uri.startswith("torrent:")

    def _path_is_uri_like(self, path: str) -> bool:
        return self._is_bittorrent_uri(path.strip().lower())

    def _status_needs_tell_status_refresh(self, status: dict[str, Any]) -> bool:
        if not status:
            return False

        bt_name = (
            status.get("bittorrent", {}).get("info", {}).get("name")
            if isinstance(status.get("bittorrent"), dict)
            else None
        )
        if isinstance(bt_name, str) and self._path_is_uri_like(bt_name):
            return True

        files = status.get("files")
        if not isinstance(files, list) or not files:
            return False
        return any(
            isinstance(item, dict)
            and isinstance(item.get("path"), str)
            and self._path_is_uri_like(item["path"])
            for item in files
        )

    def _sanitize_status(self, status: dict) -> dict:
        """对 tellStatus 返回的数据进行脱敏处理"""
        result = self._new_status_payload()

        result["gid"] = self._status_str(status.get("gid"), "")
        result["status"] = self._normalize_status_value(status.get("status"))
        result["totalLength"] = self._status_str(
            status.get("totalLength"), DEFAULT_STATUS_DOWNLOADS
        )
        result["completedLength"] = self._status_str(
            status.get("completedLength"), DEFAULT_STATUS_DOWNLOADS
        )
        result["uploadLength"] = self._status_str(
            status.get("uploadLength"), DEFAULT_STATUS_DOWNLOADS
        )
        result["downloadSpeed"] = self._status_str(
            status.get("downloadSpeed"), DEFAULT_STATUS_DOWNLOADS
        )
        result["uploadSpeed"] = self._status_str(
            status.get("uploadSpeed"), DEFAULT_STATUS_DOWNLOADS
        )
        result["pieceLength"] = self._status_str(
            status.get("pieceLength"), DEFAULT_STATUS_DOWNLOADS
        )
        result["numPieces"] = self._status_str(
            status.get("numPieces"), DEFAULT_STATUS_DOWNLOADS
        )
        result["connections"] = self._status_str(
            status.get("connections"), DEFAULT_STATUS_DOWNLOADS
        )
        result["errorCode"] = self._status_str(
            status.get("errorCode"), DEFAULT_ERROR_CODE
        )
        result["errorMessage"] = self._status_str(status.get("errorMessage"), "")
        result["dir"] = ""
        result["files"] = self._sanitize_files(status.get("files"))
        result["infoHash"] = self._status_str(status.get("infoHash"), "")
        result["numSeeders"] = self._status_str(
            status.get("numSeeders"), DEFAULT_STATUS_DOWNLOADS
        )
        result["seeder"] = self._status_bool(status.get("seeder"), DEFAULT_BOOL_FALSE)
        result["bittorrent"] = self._sanitize_bittorrent(status.get("bittorrent"))

        bitfield = status.get("bitfield")
        if isinstance(bitfield, str):
            result["bitfield"] = bitfield

        followed_by = status.get("followedBy")
        if isinstance(followed_by, list):
            gids = [str(gid) for gid in followed_by if isinstance(gid, (str, int))]
            result["followedBy"] = gids

        following = status.get("following")
        if following is not None:
            result["following"] = self._status_str(following, "")

        belongs_to = status.get("belongsTo")
        if belongs_to is not None:
            result["belongsTo"] = self._status_str(belongs_to, "")

        verified_length = status.get("verifiedLength")
        if verified_length is not None:
            result["verifiedLength"] = self._status_str(
                verified_length, DEFAULT_STATUS_DOWNLOADS
            )

        verify_integrity_pending = status.get("verifyIntegrityPending")
        if verify_integrity_pending is not None:
            result["verifyIntegrityPending"] = self._status_bool(
                verify_integrity_pending
            )

        return result

    @staticmethod
    def _parse_history_gid(gid: str) -> tuple[str | None, int | None, int | None]:
        if gid.startswith("hist-"):
            suffix = gid[5:]
            if suffix.isdigit():
                return None, None, int(suffix)
        if gid.startswith("task-"):
            suffix = gid[5:]
            if suffix.isdigit():
                return None, int(suffix), None
        return gid, None, None

    async def _get_user_available_space(self) -> int:
        """获取用户实际可用空间"""
        user = await auth_repo.get_user_by_id(self.user_id)
        if user is None:
            return 0
        quota_bytes = int(user["quota_bytes"])
        if quota_bytes <= 0:
            return 0
        usage = await get_usage(self.user_id, quota_bytes)
        download_path = Path(settings.download_dir)
        download_path.mkdir(parents=True, exist_ok=True)
        disk_free = shutil.disk_usage(download_path).free
        return min(int(usage["available_bytes"]), disk_free)

    async def _get_user_quota(self) -> int:
        user = await auth_repo.get_user_by_id(self.user_id)
        return int(user["quota_bytes"]) if user else 0

    def _check_disk_space(self) -> tuple[bool, int]:
        """检查磁盘空间是否足够"""
        download_path = Path(settings.download_dir)
        download_path.mkdir(parents=True, exist_ok=True)
        disk = shutil.disk_usage(download_path)
        min_free = get_min_free_disk()
        return disk.free > min_free, disk.free

    @staticmethod
    def _normalize_pagination(params: list, default_num: int = 1000) -> tuple[int, int]:
        offset = params[0] if params else 0
        num = params[1] if len(params) > 1 else default_num
        if type(offset) is not int or type(num) is not int:
            raise RpcError(
                RpcErrorCode.INVALID_PARAMS, "offset and num must be integers"
            )
        if num < 0:
            raise RpcError(RpcErrorCode.INVALID_PARAMS, "num must be non-negative")
        return offset, num

    @staticmethod
    def _slice_with_offset(items: Sequence[Any], offset: int, num: int) -> list[Any]:
        items_list = list(items)
        if num == 0:
            return []
        if offset >= 0:
            return items_list[offset : offset + num]

        start = len(items_list) + offset
        if start < 0:
            return []

        result: list[Any] = []
        idx = start
        while idx >= 0 and len(result) < num:
            result.append(items_list[idx])
            idx -= 1
        return result

    async def _get_task_pair_by_task_id(self, task_id: int) -> dict[str, Any] | None:
        return await get_user_task_by_id(self.user_id, task_id)

    @staticmethod
    def _status_has_file_name(status: dict[str, Any]) -> bool:
        return has_real_file_path(status)

    @staticmethod
    def _extract_gids_from_statuses(statuses: list[dict[str, Any]]) -> set[str]:
        gids: set[str] = set()
        for status in statuses:
            gid = status.get("gid")
            if isinstance(gid, str) and gid:
                gids.add(gid)
        return gids

    def _apply_status_keys_to_list(
        self, statuses: list[dict], keys: list[str] | None
    ) -> list[dict]:
        return [self._apply_status_keys(item, keys) for item in statuses]

    async def _fetch_waiting_tasks(
        self,
        user_gids: set[str],
        need_count: int,
        max_items: int = 1000,
        page_size: int = 200,
    ) -> list[dict]:
        offset = 0
        all_waiting: list[dict] = []
        matched = 0
        while offset < max_items:
            batch_limit = min(page_size, max_items - offset)
            batch = await self.client.tell_waiting(offset, batch_limit)
            if not batch:
                break
            all_waiting.extend(batch)
            matched += sum(
                1 for t in batch if str(t.get("gid") or "") in user_gids
            )
            if matched >= need_count:
                break
            if len(batch) < batch_limit:
                break
            offset += len(batch)
        return all_waiting

    @staticmethod
    def _strip_rpc_token(params: Any) -> list:
        if not isinstance(params, list):
            return []
        if params and isinstance(params[0], str) and params[0].startswith("token:"):
            return params[1:]
        return params

    async def _check_quota_and_disk(self) -> None:
        """检查配额和磁盘空间，不足则抛异常"""
        disk_ok, disk_free = self._check_disk_space()
        if not disk_ok:
            raise RpcError(
                RpcErrorCode.QUOTA_EXCEEDED,
                f"Disk space not enough, free: {disk_free / 1024 / 1024 / 1024:.2f} GB",
            )
        user_available = await self._get_user_available_space()
        if user_available <= 0:
            raise RpcError(RpcErrorCode.QUOTA_EXCEEDED, "Your quota has been exceeded")

    @staticmethod
    def _resource_kind_for_uri(uri: str) -> str:
        lower = uri.lower()
        if lower.startswith("magnet:"):
            return "magnet"
        if lower.startswith(("http://", "https://")):
            return "http"
        return "other"

    async def _gid_for_created_task(
        self,
        task: dict[str, Any],
        resource_key: str,
    ) -> str:
        return f"task-{task['id']}"

    @staticmethod
    def _raise_create_download_error(exc: Exception) -> None:
        if isinstance(exc, RpcError):
            raise exc
        if isinstance(exc, ValueError):
            message = str(exc)
            if message == "quota exceeded":
                raise RpcError(
                    RpcErrorCode.QUOTA_EXCEEDED, "Your quota has been exceeded"
                ) from exc
            raise RpcError(RpcErrorCode.INVALID_PARAMS, message) from exc
        if isinstance(exc, LookupError):
            raise RpcError(RpcErrorCode.INTERNAL_ERROR, str(exc)) from exc
        raise RpcError(RpcErrorCode.INTERNAL_ERROR, str(exc)) from exc

    # ========== 完整实现的方法 ==========
    async def _handle_add_uri(self, params: list) -> str:
        """aria2.addUri(uris[, options[, position]])"""
        if not params or not isinstance(params[0], list):
            raise RpcError(RpcErrorCode.INVALID_PARAMS, "uris is required")
        uris = params[0]
        if not uris:
            raise RpcError(RpcErrorCode.INVALID_PARAMS, "uris list is empty")
        options = (
            dict(params[1]) if len(params) > 1 and isinstance(params[1], dict) else {}
        )
        await self._check_quota_and_disk()
        submit_uris = [str(item) for item in uris]
        uri = submit_uris[0]
        resource_key = get_uri_hash(uri) or hashlib.sha256(uri.encode()).hexdigest()
        task_name = self._extract_name_from_uri(uri) or uri

        try:
            task = await create_user_download(
                user_id=self.user_id,
                quota_bytes=await self._get_user_quota(),
                uri=uri,
                resource_key=resource_key,
                resource_kind=self._resource_kind_for_uri(uri),
                display_name=task_name,
                total_bytes=0,
                aria2_client=self.client,
                options=options,
                submit_uris=submit_uris,
            )
        except Exception as exc:
            self._raise_create_download_error(exc)

        return await self._gid_for_created_task(task, resource_key)

    async def _handle_add_torrent(self, params: list) -> str:
        """aria2.addTorrent(torrent[, uris[, options[, position]]])"""
        if not params or not isinstance(params[0], str):
            raise RpcError(RpcErrorCode.INVALID_PARAMS, "torrent data is required")
        torrent_data = params[0]
        # 限制 torrent 文件大小（10MB）
        if len(torrent_data) > 10 * 1024 * 1024:
            raise RpcError(RpcErrorCode.INVALID_PARAMS, "Torrent data too large")
        options = (
            dict(params[2]) if len(params) > 2 and isinstance(params[2], dict) else {}
        )
        webseed_uris = (
            [str(item) for item in params[1]]
            if len(params) > 1 and isinstance(params[1], list)
            else []
        )
        await self._check_quota_and_disk()
        info_hash = extract_info_hash_from_torrent_base64(torrent_data)
        resource_key = info_hash or hashlib.sha256(torrent_data.encode()).hexdigest()
        task_uri = f"magnet:?xt=urn:btih:{resource_key}"
        task_name = f"torrent-{resource_key[:12]}"

        try:
            task = await create_user_torrent_download(
                user_id=self.user_id,
                quota_bytes=await self._get_user_quota(),
                torrent_data=torrent_data,
                resource_key=resource_key,
                source_uri=task_uri,
                display_name=task_name,
                total_bytes=0,
                aria2_client=self.client,
                options=options,
                uris=webseed_uris,
            )
        except Exception as exc:
            self._raise_create_download_error(exc)

        return await self._gid_for_created_task(task, resource_key)

    async def _handle_remove(self, params: list) -> str:
        """aria2.remove(gid)"""
        if not params:
            raise RpcError(RpcErrorCode.INVALID_PARAMS, "gid is required")
        gid = str(params[0])
        row = await self._resolve_owned_row(gid)
        if row is None or row["status"] not in ACTIVE_USER_TASK_STATUSES:
            raise RpcError(RpcErrorCode.TASK_NOT_FOUND, f"Task not found: {gid}")
        await cancel_user_task(
            user_id=self.user_id,
            user_task_id=int(row["id"]),
            quota_bytes=await self._get_user_quota(),
            aria2_client=self.client,
        )
        return gid

    async def _handle_force_remove(self, params: list) -> str:
        """aria2.forceRemove(gid) - 同 remove"""
        return await self._handle_remove(params)

    async def _handle_tell_status(self, params: list) -> dict:
        """aria2.tellStatus(gid[, keys])"""
        if not params:
            raise RpcError(RpcErrorCode.INVALID_PARAMS, "gid is required")
        gid = str(params[0])
        keys = self._extract_status_keys(params, 1)

        row = await self._resolve_owned_row(gid)
        if row is None:
            raise RpcError(RpcErrorCode.TASK_NOT_FOUND, f"Task not found: {gid}")

        live = await self._fetch_live_status(row)
        response = rpc_view_service.status_from_task(row, live)
        return self._apply_status_keys(response, keys)

    async def _handle_tell_active(self, params: list) -> list:
        """aria2.tellActive([keys])"""
        keys = self._extract_status_keys(params, 0)
        try:
            all_active = await self.client.tell_active()
        except Exception as exc:
            logger.warning(
                "aria2.tellActive failed for user_id=%s",
                self.user_id,
                exc_info=exc,
            )
            statuses = await rpc_view_service.list_active_statuses(self.user_id)
            return self._apply_status_keys_to_list(statuses, keys)

        rows_by_gid = await self._get_current_rows_by_gid()
        statuses: list[tuple[dict, dict]] = []
        need_refresh: list[tuple[int, str]] = []
        MAX_REFRESH_COUNT = 10

        for active in all_active:
            gid = str(active.get("gid") or "")
            row = rows_by_gid.get(gid)
            if row is None:
                continue
            live = self._sanitize_status(active)
            idx = len(statuses)
            statuses.append((row, live))
            if (
                self._status_needs_tell_status_refresh(live)
                and len(need_refresh) < MAX_REFRESH_COUNT
            ):
                need_refresh.append((idx, gid))

        if need_refresh:
            refresh_results = await asyncio.gather(
                *[self.client.tell_status(gid) for _, gid in need_refresh],
                return_exceptions=True,
            )
            for (idx, _gid), result in zip(need_refresh, refresh_results):
                if not isinstance(result, BaseException):
                    statuses[idx] = (statuses[idx][0], self._sanitize_status(result))

        result_list = [
            rpc_view_service.status_from_task(row, live) for row, live in statuses
        ]
        return self._apply_status_keys_to_list(result_list, keys)

    async def _handle_tell_waiting(self, params: list) -> list:
        """aria2.tellWaiting(offset, num[, keys])"""
        offset, num = self._normalize_pagination(params)
        keys = self._extract_status_keys(params, 2)
        rows_by_gid = await self._get_current_rows_by_gid()
        try:
            all_waiting = await self._fetch_waiting_tasks(
                user_gids=set(rows_by_gid.keys()),
                need_count=offset + num,
            )
        except Exception as exc:
            logger.warning(
                "aria2.tellWaiting failed for user_id=%s",
                self.user_id,
                exc_info=exc,
            )
            statuses = await rpc_view_service.list_waiting_statuses(self.user_id)
            sliced = self._slice_with_offset(statuses, offset, num)
            return self._apply_status_keys_to_list(sliced, keys)

        statuses: list[dict[str, Any]] = []
        for waiting in all_waiting:
            gid = str(waiting.get("gid") or "")
            row = rows_by_gid.get(gid)
            if row is None:
                continue
            statuses.append(
                rpc_view_service.status_from_task(row, self._sanitize_status(waiting))
            )
        sliced = self._slice_with_offset(statuses, offset, num)
        return self._apply_status_keys_to_list(sliced, keys)

    async def _handle_tell_stopped(self, params: list) -> list:
        """aria2.tellStopped(offset, num[, keys])"""
        offset, num = self._normalize_pagination(params)
        keys = self._extract_status_keys(params, 2)
        statuses = await rpc_view_service.list_stopped_statuses(self.user_id)
        sliced = self._slice_with_offset(statuses, offset, num)
        return self._apply_status_keys_to_list(sliced, keys)

    async def _handle_get_global_stat(self, params: list) -> dict:
        """aria2.getGlobalStat()"""
        rows = await list_user_tasks(self.user_id)
        counts = stat_counts(rows)
        num_active = counts["active"]
        num_waiting = counts["waiting"]
        num_stopped = counts["stopped"]

        rows_by_gid = await self._get_current_rows_by_gid()
        live_by_gid: dict[str, dict[str, Any]] = {}
        try:
            all_active = await self.client.tell_active()
        except Exception as exc:
            logger.warning(
                "aria2.tellActive failed for getGlobalStat user_id=%s, fallback to zero speed",
                self.user_id,
                exc_info=exc,
            )
        else:
            if isinstance(all_active, list):
                for active in all_active:
                    if not isinstance(active, dict):
                        continue
                    gid = str(active.get("gid") or "")
                    if gid in rows_by_gid:
                        live_by_gid[gid] = self._sanitize_status(active)

        speeds = speed_totals(rows, live_by_gid)
        return {
            "downloadSpeed": str(speeds["download_speed"]),
            "uploadSpeed": str(speeds["upload_speed"]),
            "numActive": str(num_active),
            "numWaiting": str(num_waiting),
            "numStopped": str(num_stopped),
            "numStoppedTotal": str(num_stopped),
        }

    async def _handle_get_files(self, params: list) -> list:
        """aria2.getFiles(gid)"""
        if not params:
            raise RpcError(RpcErrorCode.INVALID_PARAMS, "gid is required")
        gid = str(params[0])
        row = await self._resolve_owned_row(gid)
        if row is None:
            raise RpcError(RpcErrorCode.TASK_NOT_FOUND, f"Task not found: {gid}")
        real_gid = str(row.get("aria2_gid") or "")
        if real_gid and is_current(row):
            try:
                sanitized = self._sanitize_files(await self.client.get_files(real_gid))
                if sanitized and self._status_has_file_name({"files": sanitized}):
                    return sanitized
            except Exception as exc:
                logger.warning(
                    "aria2.getFiles failed for task=%s user_id=%s",
                    row.get("id"),
                    self.user_id,
                    exc_info=exc,
                )
        return self._sanitize_files(rpc_view_service.status_from_task(row).get("files"))

    async def _handle_get_uris(self, params: list) -> list:
        """aria2.getUris(gid)"""
        if not params:
            raise RpcError(RpcErrorCode.INVALID_PARAMS, "gid is required")
        gid = str(params[0])
        row = await self._resolve_owned_row(gid)
        if row is None:
            raise RpcError(RpcErrorCode.TASK_NOT_FOUND, f"Task not found: {gid}")
        real_gid = str(row.get("aria2_gid") or "")
        if real_gid and is_current(row):
            try:
                return self._sanitize_uris(await self.client.get_uris(real_gid))
            except Exception as exc:
                logger.warning(
                    "aria2.getUris failed for task=%s user_id=%s",
                    row.get("id"),
                    self.user_id,
                    exc_info=exc,
                )
        source_uri = row.get("source_uri")
        return (
            self._sanitize_uris([{"uri": source_uri, "status": "used"}])
            if source_uri
            else []
        )

    async def _handle_get_version(self, params: list) -> dict:
        """aria2.getVersion()"""
        try:
            version_info = await self.client.get_version()
        except Exception as exc:
            logger.warning(
                "aria2.getVersion failed for user_id=%s",
                self.user_id,
                exc_info=exc,
            )
            version_info = {}
        return self._sanitize_version(version_info)

    async def _handle_remove_download_result(self, params: list) -> str:
        """aria2.removeDownloadResult(gid) - 删除用户的 stopped 订阅（历史记录）"""
        if not params:
            raise RpcError(RpcErrorCode.INVALID_PARAMS, "gid is required")
        gid_param = params[0]
        if not isinstance(gid_param, str):
            raise RpcError(RpcErrorCode.INVALID_PARAMS, "gid must be a string")
        gid, task_id, history_id = self._parse_history_gid(gid_param)
        if history_id is not None:
            raise RpcError(RpcErrorCode.TASK_NOT_FOUND, f"Task not found: {gid_param}")
        deleted = (
            await delete_terminal_user_task(self.user_id, task_id)
            if task_id is not None
            else await delete_terminal_user_task_by_gid(self.user_id, str(gid))
        )
        if not deleted:
            raise RpcError(RpcErrorCode.TASK_NOT_FOUND, f"Task not found: {gid_param}")
        return "OK"

    async def _handle_purge_download_result(self, params: list) -> str:
        """aria2.purgeDownloadResult() - 删除用户所有 stopped 订阅"""
        await delete_all_terminal_user_tasks(self.user_id)
        return "OK"

    # ========== 静默返回的方法（不支持但不报错） ==========
    async def _handle_pause(self, params: list) -> str:
        """aria2.pause - 不支持，静默返回 gid"""
        return params[0] if params else "0"

    async def _handle_force_pause(self, params: list) -> str:
        return params[0] if params else "0"

    async def _handle_unpause(self, params: list) -> str:
        return params[0] if params else "0"

    async def _handle_pause_all(self, params: list) -> str:
        return "OK"

    async def _handle_force_pause_all(self, params: list) -> str:
        return "OK"

    async def _handle_unpause_all(self, params: list) -> str:
        return "OK"

    async def _handle_get_option(self, params: list) -> dict:
        return {}

    async def _handle_get_global_option(self, params: list) -> dict:
        return {}

    async def _handle_change_position(self, params: list) -> int:
        return 0

    async def _handle_change_uri(self, params: list) -> list:
        return [0, 0]

    async def _handle_get_peers(self, params: list) -> list:
        if not params:
            raise RpcError(RpcErrorCode.INVALID_PARAMS, "gid is required")
        gid = str(params[0])
        row = await self._resolve_owned_row(gid)
        if row is None:
            raise RpcError(RpcErrorCode.TASK_NOT_FOUND, f"Task not found: {gid}")
        return []

    async def _handle_get_servers(self, params: list) -> list:
        if not params:
            raise RpcError(RpcErrorCode.INVALID_PARAMS, "gid is required")
        gid = str(params[0])
        row = await self._resolve_owned_row(gid)
        if row is None:
            raise RpcError(RpcErrorCode.TASK_NOT_FOUND, f"Task not found: {gid}")
        real_gid = str(row.get("aria2_gid") or "")
        indexes = ["1"]
        if real_gid and is_current(row):
            try:
                files = await self.client.get_files(real_gid)
                if isinstance(files, list):
                    indexes = [
                        str(item.get("index"))
                        for item in files
                        if isinstance(item, dict) and item.get("index") is not None
                    ] or indexes
            except Exception:
                pass
        return [{"index": index, "servers": []} for index in indexes]

    async def _handle_save_session(self, params: list) -> str:
        return "OK"

    async def _handle_get_session_info(self, params: list) -> dict:
        return {"sessionId": "aria2deck-proxy-session"}

    # ========== system 方法 ==========
    async def _handle_system_list_methods(self, params: list) -> list:
        """system.listMethods()"""
        return self.SUPPORTED_METHODS

    async def _handle_system_multicall(self, params: list) -> list:
        """system.multicall(methods)"""
        if not params or not isinstance(params[0], list):
            raise RpcError(RpcErrorCode.INVALID_PARAMS, "methods is required")
        if self._multicall_depth >= 1:
            raise RpcError(RpcErrorCode.INVALID_REQUEST, "Nested multicall is not allowed")
        methods = params[0]
        MAX_MULTICALL_SIZE = 20
        if len(methods) > MAX_MULTICALL_SIZE:
            raise RpcError(
                RpcErrorCode.INVALID_REQUEST,
                f"Too many methods in multicall, max {MAX_MULTICALL_SIZE}",
            )
        self._multicall_depth += 1
        try:
            results = []
            for call in methods:
                if not isinstance(call, dict):
                    results.append(
                        {
                            "faultCode": RpcErrorCode.INVALID_PARAMS,
                            "faultString": "Invalid method call",
                        }
                    )
                    continue
                method_name = call.get("methodName", "")
                method_params = self._strip_rpc_token(call.get("params", []))
                try:
                    result = await self.handle(method_name, method_params)
                    results.append([result])
                except RpcError as e:
                    results.append({"faultCode": e.code, "faultString": e.message})
                except Exception as e:
                    results.append(
                        {"faultCode": RpcErrorCode.INTERNAL_ERROR, "faultString": str(e)}
                    )
            return results
        finally:
            self._multicall_depth -= 1
