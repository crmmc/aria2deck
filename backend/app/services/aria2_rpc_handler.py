"""aria2 RPC 方法处理器

为外部 aria2 兼容客户端（如 AriaNg、Motrix）提供 RPC 方法实现。
实现用户隔离、数据脱敏、配额检查等安全机制。

基于共享下载架构（DownloadTask + UserTaskSubscription）。
"""
from __future__ import annotations
import asyncio
from datetime import datetime, timezone
import hashlib
import logging
import shutil
from pathlib import Path
from typing import Any, Protocol, Sequence
from urllib.parse import unquote, urlsplit

from sqlalchemy.exc import IntegrityError
from sqlmodel import select, func, col, update

from app.aria2.failed_task_cleanup import cleanup_failed_task_artifacts
from app.core.config import settings
from app.core.security import mask_url_credentials
from app.core.state import AppState
from app.database import get_session
from app.models import DownloadTask, TaskHistory, User, UserTaskSubscription, utc_now_str
from app.routers.config import get_min_free_disk
from app.services.hash import extract_info_hash_from_torrent_base64, get_uri_hash
from app.services.storage import cleanup_task_download_dir, get_user_space_info


logger = logging.getLogger(__name__)

DEFAULT_STATUS_DOWNLOADS = "0"
DEFAULT_STATUS_WAITING = "waiting"
DEFAULT_STATUS_ERROR = "error"
DEFAULT_STATUS_COMPLETE = "complete"
DEFAULT_BOOL_FALSE = "false"
DEFAULT_ERROR_CODE = "0"
BT_INFO_HASH_LENGTH = 40
SPECIAL_GID_PREFIXES = ("hist-", "task-")

ALLOWED_STATUS_VALUES = {
    "active",
    "waiting",
    "paused",
    "error",
    "complete",
    "removed",
}
RUNNABLE_TASK_STATUSES = ("active", "queued", "waiting", "paused")
CANCELABLE_TASK_STATUSES = ("queued", "active", "waiting", "paused", "error")


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
        "aria2.changeOption",
        "aria2.getGlobalOption",
        "aria2.changeGlobalOption",
        "aria2.shutdown",
        "aria2.forceShutdown",
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

    def __init__(self, user_id: int, aria2_client: RpcBackendClient, app_state: AppState):
        self.user_id = user_id
        self.client = aria2_client
        if app_state is None:
            raise RuntimeError("AppState is required for Aria2RpcHandler")
        self.app_state: AppState = app_state

    async def handle(self, method: str, params: list) -> Any:
        """路由到具体方法处理"""
        handler_name = self._get_handler_name(method)
        handler = getattr(self, handler_name, None)

        if handler is None:
            raise RpcError(
                RpcErrorCode.METHOD_NOT_FOUND,
                f"Method not found: {method}"
            )

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

    async def _verify_task_owner(self, gid: str) -> tuple[DownloadTask, UserTaskSubscription] | None:
        """检查 gid 对应的任务是否属于当前用户
        Returns: (DownloadTask, UserTaskSubscription) 或 None
        """
        async with get_session() as db:
            result = await db.exec(
                select(DownloadTask, UserTaskSubscription)
                .join(UserTaskSubscription, UserTaskSubscription.task_id == DownloadTask.id)  # type: ignore[arg-type]
                .where(
                    DownloadTask.gid == gid,
                    UserTaskSubscription.owner_id == self.user_id,
                )
            )
            return result.first()
    async def _get_user_gids(self, sub_statuses: list[str] | None = None, task_statuses: list[str] | None = None) -> set[str]:
        """获取用户指定状态的任务 gid 集合"""
        async with get_session() as db:
            stmt = (
                select(DownloadTask.gid)
                .join(UserTaskSubscription, UserTaskSubscription.task_id == DownloadTask.id)  # type: ignore[arg-type]
                .where(
                    UserTaskSubscription.owner_id == self.user_id,
                    col(DownloadTask.gid).is_not(None),
                )
            )
            if sub_statuses:
                stmt = stmt.where(col(UserTaskSubscription.status).in_(sub_statuses))
            if task_statuses:
                stmt = stmt.where(col(DownloadTask.status).in_(task_statuses))
            result = await db.exec(stmt)
            return self._normalize_gid_collection(result.all())

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
    def _extract_magnet_display_name(uri: str) -> str:
        """从磁力链接提取显示名称（dn 参数或完整 URI）"""
        if not uri or not uri.startswith("magnet:"):
            return ""
        # 尝试提取 dn 参数
        import re
        dn_match = re.search(r'[?&]dn=([^&]+)', uri)
        if dn_match:
            return unquote(dn_match.group(1))
        # 没有 dn，返回完整磁力链接
        return uri

    def _build_status_files(
        self,
        task_name: str | None,
        uri: str | None,
        total_length: int,
        completed_length: int,
        fallback_name: str = "",
    ) -> list[dict[str, Any]]:
        file_data = self._new_file_payload()
        candidate_name = (task_name or "").strip()
        if not candidate_name and uri and not self._is_bittorrent_uri(uri):
            candidate_name = self._extract_name_from_uri(uri)
        if not candidate_name:
            candidate_name = fallback_name
        file_data["path"] = self._sanitize_file_path(candidate_name)
        file_data["length"] = str(total_length)
        file_data["completedLength"] = str(completed_length)
        return [file_data]

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
    def _normalize_status_value(value: Any, default: str = DEFAULT_STATUS_WAITING) -> str:
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
            "bittorrent": {"announceList": [], "comment": "", "creationDate": "0", "mode": "single", "info": {"name": ""}},
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
        normalized = [str(item) for item in keys if isinstance(item, str) and item.strip()]
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
            file_data["length"] = self._status_str(item.get("length"), file_data["length"])
            file_data["completedLength"] = self._status_str(item.get("completedLength"), file_data["completedLength"])
            file_data["selected"] = self._status_bool(item.get("selected"), file_data["selected"])
            file_data["path"] = self._sanitize_file_path(self._status_str(item.get("path"), ""))
            sanitized_files.append(file_data)
        return sanitized_files

    def _sanitize_bittorrent(self, bt_info: Any) -> dict:
        sanitized = {"announceList": [], "comment": "", "creationDate": "0", "mode": "single", "info": {"name": ""}}
        if not isinstance(bt_info, dict):
            return sanitized

        mode = bt_info.get("mode")
        if isinstance(mode, str) and mode in {"single", "multi"}:
            sanitized["mode"] = mode

        comment = bt_info.get("comment")
        if isinstance(comment, str):
            sanitized["comment"] = comment

        creation_date = bt_info.get("creationDate")
        if creation_date is not None:
            sanitized["creationDate"] = self._status_str(creation_date, "0")

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
            peer["amChoking"] = self._status_bool(item.get("amChoking"), DEFAULT_BOOL_FALSE)
            peer["peerChoking"] = self._status_bool(item.get("peerChoking"), DEFAULT_BOOL_FALSE)
            peer["downloadSpeed"] = self._status_str(item.get("downloadSpeed"), DEFAULT_STATUS_DOWNLOADS)
            peer["uploadSpeed"] = self._status_str(item.get("uploadSpeed"), DEFAULT_STATUS_DOWNLOADS)
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
                    payload["downloadSpeed"] = self._status_str(server.get("downloadSpeed"), DEFAULT_STATUS_DOWNLOADS)
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
            enabled_features = [str(feature) for feature in features if isinstance(feature, str)]
        else:
            enabled_features = []
        result: dict[str, Any] = {"version": version, "enabledFeatures": enabled_features}
        for key in ("rpc_url", "secret", "sessionId"):
            value = version_info.get(key)
            if isinstance(value, str):
                result[key] = value
        return result

    @staticmethod
    def _build_bt_info_hash(uri_hash: str) -> str:
        value = uri_hash.strip()
        if len(value) != BT_INFO_HASH_LENGTH:
            return ""
        if not all(char in "0123456789abcdefABCDEF" for char in value):
            return ""
        return value.lower()

    @staticmethod
    def _is_bittorrent_uri(uri: str) -> bool:
        return uri.startswith("magnet:") or uri.startswith("torrent:")

    def _sanitize_status(self, status: dict) -> dict:
        """对 tellStatus 返回的数据进行脱敏处理"""
        result = self._new_status_payload()

        result["gid"] = self._status_str(status.get("gid"), "")
        result["status"] = self._normalize_status_value(status.get("status"))
        result["totalLength"] = self._status_str(status.get("totalLength"), DEFAULT_STATUS_DOWNLOADS)
        result["completedLength"] = self._status_str(status.get("completedLength"), DEFAULT_STATUS_DOWNLOADS)
        result["uploadLength"] = self._status_str(status.get("uploadLength"), DEFAULT_STATUS_DOWNLOADS)
        result["downloadSpeed"] = self._status_str(status.get("downloadSpeed"), DEFAULT_STATUS_DOWNLOADS)
        result["uploadSpeed"] = self._status_str(status.get("uploadSpeed"), DEFAULT_STATUS_DOWNLOADS)
        result["pieceLength"] = self._status_str(status.get("pieceLength"), DEFAULT_STATUS_DOWNLOADS)
        result["numPieces"] = self._status_str(status.get("numPieces"), DEFAULT_STATUS_DOWNLOADS)
        result["connections"] = self._status_str(status.get("connections"), DEFAULT_STATUS_DOWNLOADS)
        result["errorCode"] = self._status_str(status.get("errorCode"), DEFAULT_ERROR_CODE)
        result["errorMessage"] = self._status_str(status.get("errorMessage"), "")
        result["dir"] = ""
        result["files"] = self._sanitize_files(status.get("files"))
        result["infoHash"] = self._status_str(status.get("infoHash"), "")
        result["numSeeders"] = self._status_str(status.get("numSeeders"), DEFAULT_STATUS_DOWNLOADS)
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
            result["verifiedLength"] = self._status_str(verified_length, DEFAULT_STATUS_DOWNLOADS)

        verify_integrity_pending = status.get("verifyIntegrityPending")
        if verify_integrity_pending is not None:
            result["verifyIntegrityPending"] = self._status_bool(verify_integrity_pending)

        return result

    @staticmethod
    def _build_history_gid(task: DownloadTask) -> str:
        if task.gid:
            return task.gid
        if task.id is not None:
            return f"task-{task.id}"
        return ""

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

    def _build_status_from_history(self, history: TaskHistory) -> dict:
        if history.result == "completed":
            status = DEFAULT_STATUS_COMPLETE
            error_message = ""
        elif history.result == "cancelled":
            status = "removed"
            error_message = ""
        else:
            status = DEFAULT_STATUS_ERROR
            error_message = history.reason or ""

        gid = f"hist-{history.id}" if history.id is not None else ""
        total_length = history.total_length or 0
        completed_length = total_length if status == DEFAULT_STATUS_COMPLETE else 0
        result = self._new_status_payload()
        result["gid"] = gid
        result["status"] = status
        result["totalLength"] = str(total_length)
        result["completedLength"] = str(completed_length)
        result["errorCode"] = "1" if status == DEFAULT_STATUS_ERROR else DEFAULT_ERROR_CODE
        result["errorMessage"] = error_message
        result["files"] = self._build_status_files(
            task_name=history.task_name,
            uri=history.uri,
            total_length=total_length,
            completed_length=completed_length,
            fallback_name=gid,
        )
        result["bittorrent"]["info"]["name"] = result["files"][0]["path"] if result["files"] else ""
        try:
            created_dt = datetime.fromisoformat(history.created_at)
            if created_dt.tzinfo is None:
                created_dt = created_dt.replace(tzinfo=timezone.utc)
            result["bittorrent"]["creationDate"] = str(int(created_dt.timestamp()))
        except (TypeError, ValueError):
            result["bittorrent"]["creationDate"] = "0"
        return result

    async def _get_user_available_space(self) -> int:
        """获取用户实际可用空间"""
        async with get_session() as db:
            result = await db.exec(select(User.quota).where(User.id == self.user_id))
            user_quota = self._to_int_scalar(result.first(), default=0)
        if user_quota <= 0:
            return 0
        space_info = await get_user_space_info(self.user_id, user_quota)
        return space_info["available"]
    def _check_disk_space(self) -> tuple[bool, int]:
        """检查磁盘空间是否足够"""
        download_path = Path(settings.download_dir)
        download_path.mkdir(parents=True, exist_ok=True)
        disk = shutil.disk_usage(download_path)
        min_free = get_min_free_disk()
        return disk.free > min_free, disk.free

    async def _get_task_submit_lock(self, task_id: int) -> asyncio.Lock:
        async with self.app_state.lock:
            lock = self.app_state.task_submit_locks.get(task_id)
            if lock is None:
                lock = asyncio.Lock()
                self.app_state.task_submit_locks[task_id] = lock
            return lock

    @staticmethod
    def _normalize_pagination(params: list, default_num: int = 1000) -> tuple[int, int]:
        offset = params[0] if params else 0
        num = params[1] if len(params) > 1 else default_num
        if type(offset) is not int or type(num) is not int:
            raise RpcError(RpcErrorCode.INVALID_PARAMS, "offset and num must be integers")
        if num < 0:
            raise RpcError(RpcErrorCode.INVALID_PARAMS, "num must be non-negative")
        return offset, num

    @staticmethod
    def _slice_with_offset(items: Sequence[Any], offset: int, num: int) -> list[Any]:
        items_list = list(items)
        if num == 0:
            return []
        if offset >= 0:
            return items_list[offset: offset + num]

        start = len(items_list) + offset
        if start < 0:
            return []

        result: list[Any] = []
        idx = start
        while idx >= 0 and len(result) < num:
            result.append(items_list[idx])
            idx -= 1
        return result

    async def _get_task_pair_by_task_id(self, task_id: int) -> tuple[DownloadTask, UserTaskSubscription] | None:
        async with get_session() as db:
            stmt = (
                select(DownloadTask, UserTaskSubscription)
                .join(UserTaskSubscription, UserTaskSubscription.task_id == DownloadTask.id)  # type: ignore[arg-type]
                .where(
                    DownloadTask.id == task_id,
                    UserTaskSubscription.owner_id == self.user_id,
                )
                .order_by(col(UserTaskSubscription.id).desc())
            )
            result = await db.exec(stmt)
            return result.first()

    async def _get_history_status(self, history_id: int) -> dict | None:
        async with get_session() as db:
            history = await db.get(TaskHistory, history_id)
        if history is None or history.owner_id != self.user_id:
            return None
        return self._build_status_from_history(history)

    async def _get_special_gid_status(self, gid: str) -> dict | None:
        _, task_id, history_id = self._parse_history_gid(gid)
        if history_id is not None:
            return await self._get_history_status(history_id)
        if task_id is None:
            return None
        pair = await self._get_task_pair_by_task_id(task_id)
        if pair is None:
            return None
        task, sub = pair
        return self._build_status_from_db(task, sub)

    async def _resolve_special_gid_status(self, gid: str) -> dict | None:
        if not gid.startswith(SPECIAL_GID_PREFIXES):
            return None
        return await self._get_special_gid_status(gid)

    async def _get_special_gid_source_uri(self, gid: str) -> str | None:
        _, task_id, history_id = self._parse_history_gid(gid)
        if history_id is not None:
            async with get_session() as db:
                history = await db.get(TaskHistory, history_id)
            if history is None or history.owner_id != self.user_id:
                return None
            return history.uri

        if task_id is None:
            return None
        pair = await self._get_task_pair_by_task_id(task_id)
        if pair is None:
            return None
        task, _ = pair
        return task.uri

    async def _get_pending_task_pairs(self, task_statuses: list[str]) -> list[tuple[DownloadTask, UserTaskSubscription]]:
        async with get_session() as db:
            stmt = (
                select(DownloadTask, UserTaskSubscription)
                .join(UserTaskSubscription, UserTaskSubscription.task_id == DownloadTask.id)  # type: ignore[arg-type]
                .where(
                    UserTaskSubscription.owner_id == self.user_id,
                    UserTaskSubscription.status == "pending",
                    col(DownloadTask.status).in_(task_statuses),
                )
                .order_by(col(UserTaskSubscription.id).desc())
            )
            result = await db.exec(stmt)
            return result.all()

    async def _get_pending_task_map(self, gids: set[str]) -> dict[str, DownloadTask]:
        if not gids:
            return {}

        async with get_session() as db:
            stmt = (
                select(DownloadTask, UserTaskSubscription)
                .join(UserTaskSubscription, UserTaskSubscription.task_id == DownloadTask.id)  # type: ignore[arg-type]
                .where(
                    UserTaskSubscription.owner_id == self.user_id,
                    UserTaskSubscription.status == "pending",
                    col(DownloadTask.gid).in_(sorted(gids)),
                )
                .order_by(col(UserTaskSubscription.id).desc())
            )
            result = await db.exec(stmt)
            pairs = result.all()

        task_map: dict[str, DownloadTask] = {}
        for task, _ in pairs:
            if task.gid:
                task_map[task.gid] = task
        return task_map

    @staticmethod
    def _status_has_file_name(status: dict[str, Any]) -> bool:
        files = status.get("files")
        if not isinstance(files, list) or not files:
            return False
        for file_item in files:
            if not isinstance(file_item, dict):
                continue
            path = file_item.get("path")
            if isinstance(path, str) and path.strip():
                return True
        return False

    @staticmethod
    def _extract_gids_from_statuses(statuses: list[dict[str, Any]]) -> set[str]:
        gids: set[str] = set()
        for status in statuses:
            gid = status.get("gid")
            if isinstance(gid, str) and gid:
                gids.add(gid)
        return gids

    def _enrich_status_files_from_task(self, status: dict[str, Any], task: DownloadTask | None) -> dict[str, Any]:
        if task is None:
            return status

        enriched = dict(status)

        # 补充 files（如果需要）
        if not self._status_has_file_name(status):
            total_length = self._to_int_scalar(status.get("totalLength"), task.total_length or 0)
            completed_length = self._to_int_scalar(status.get("completedLength"), task.completed_length or 0)
            enriched["files"] = self._build_status_files(
                task_name=task.name,
                uri=task.uri,
                total_length=total_length,
                completed_length=completed_length,
                fallback_name=self._status_str(enriched.get("gid"), "task"),
            )

        # 补充 bittorrent.info.name（对所有 BT 任务）
        if self._is_bittorrent_uri(task.uri):
            bittorrent = enriched.get("bittorrent")
            if not isinstance(bittorrent, dict):
                bittorrent = self._new_status_payload()["bittorrent"]
            else:
                bittorrent = dict(bittorrent)
            info = bittorrent.get("info")
            if not isinstance(info, dict):
                info = {}
            else:
                info = dict(info)

            # 优先用 task.name，但跳过 [METADATA] 占位符
            if task.name and not task.name.startswith("[METADATA]"):
                info["name"] = task.name
            else:
                # [METADATA] 占位符：从磁力链接提取显示名
                magnet_name = self._extract_magnet_display_name(task.uri)
                if magnet_name:
                    info["name"] = magnet_name
                elif enriched.get("files") and enriched["files"]:
                    info["name"] = enriched["files"][0].get("path", "")

            bittorrent["info"] = info
            enriched["bittorrent"] = bittorrent

        return enriched

    def _enrich_statuses_with_task_map(
        self,
        statuses: list[dict[str, Any]],
        task_map: dict[str, DownloadTask],
    ) -> list[dict[str, Any]]:
        enriched_statuses: list[dict[str, Any]] = []
        for status in statuses:
            gid_value = status.get("gid")
            task = task_map.get(str(gid_value)) if gid_value is not None else None
            enriched_statuses.append(self._enrich_status_files_from_task(status, task))
        return enriched_statuses

    def _apply_status_keys_to_list(self, statuses: list[dict], keys: list[str] | None) -> list[dict]:
        return [self._apply_status_keys(item, keys) for item in statuses]

    async def _fetch_waiting_tasks(self, max_items: int = 10000, page_size: int = 1000) -> list[dict]:
        offset = 0
        all_waiting: list[dict] = []
        while offset < max_items:
            batch_limit = min(page_size, max_items - offset)
            batch = await self.client.tell_waiting(offset, batch_limit)
            if not batch:
                break
            all_waiting.extend(batch)
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

    async def _find_or_create_task(
        self,
        uri_hash: str,
        uri: str,
        name: str,
    ) -> tuple[DownloadTask, bool]:
        async with get_session() as db:
            result = await db.exec(select(DownloadTask).where(DownloadTask.uri_hash == uri_hash))
            existing = result.first()
            if existing:
                return existing, False

            task = DownloadTask(
                uri_hash=uri_hash,
                uri=uri,
                name=name,
                status="queued",
            )
            db.add(task)
            try:
                await db.commit()
                await db.refresh(task)
                return task, True
            except IntegrityError:
                await db.rollback()
                result = await db.exec(select(DownloadTask).where(DownloadTask.uri_hash == uri_hash))
                existing = result.first()
                if existing:
                    return existing, False
                raise RpcError(RpcErrorCode.INTERNAL_ERROR, "Failed to create task")

    async def _create_or_get_subscription(self, task_id: int) -> UserTaskSubscription:
        async with get_session() as db:
            result = await db.exec(
                select(UserTaskSubscription).where(
                    UserTaskSubscription.owner_id == self.user_id,
                    UserTaskSubscription.task_id == task_id,
                )
            )
            existing = result.first()
            if existing:
                if existing.status != "pending":
                    existing.status = "pending"
                    existing.error_display = None
                    db.add(existing)
                    await db.commit()
                    await db.refresh(existing)
                return existing

            sub = UserTaskSubscription(owner_id=self.user_id, task_id=task_id, status="pending")
            db.add(sub)
            try:
                await db.commit()
                await db.refresh(sub)
                return sub
            except IntegrityError:
                await db.rollback()
                result = await db.exec(
                    select(UserTaskSubscription).where(
                        UserTaskSubscription.owner_id == self.user_id,
                        UserTaskSubscription.task_id == task_id,
                    )
                )
                existing = result.first()
                if existing:
                    if existing.status != "pending":
                        existing.status = "pending"
                        existing.error_display = None
                        db.add(existing)
                        await db.commit()
                        await db.refresh(existing)
                    return existing
                raise RpcError(RpcErrorCode.INTERNAL_ERROR, "Failed to create subscription")

    async def _cleanup_aria2_gid(self, gid: str) -> None:
        try:
            await self.client.force_remove(gid)
        except Exception:
            logger.warning("Failed to force remove aria2 task %s during compensation", gid)
        try:
            await self.client.remove_download_result(gid)
        except Exception:
            logger.warning("Failed to remove aria2 result %s during compensation", gid)

    async def _mark_submit_failed_state(
        self,
        task_id: int,
        message: str,
        *,
        raw_error: str | None = None,
        gid_hint: str | None = None,
    ) -> tuple[str | None, str, str | None, int, list[tuple[int, str]]]:
        """Persist failure state and return context for history/cleanup."""
        gid_to_cleanup = gid_hint
        task_name = "未知任务"
        task_uri: str | None = None
        task_total_length = 0
        history_inputs: list[tuple[int, str]] = []

        async with get_session() as db:
            task = await db.get(DownloadTask, task_id)
            if task and task.status in ("queued", "active", "waiting"):
                gid_to_cleanup = gid_to_cleanup or task.gid
                task_name = task.name or task.uri or "未知任务"
                task_uri = task.uri
                task_total_length = task.total_length or 0
                task.status = "error"
                task.error = raw_error
                task.error_display = message
                task.gid = None
                task.updated_at = utc_now_str()
                db.add(task)

            result = await db.exec(
                select(UserTaskSubscription).where(
                    UserTaskSubscription.task_id == task_id,
                    UserTaskSubscription.status == "pending",
                )
            )
            subscriptions = result.all()
            for sub in subscriptions:
                sub.status = "failed"
                sub.error_display = message
                sub.frozen_space = 0
                history_inputs.append((sub.owner_id, sub.created_at))
                db.add(sub)

        return gid_to_cleanup, task_name, task_uri, task_total_length, history_inputs

    async def _mark_submit_failed(
        self,
        task_id: int,
        message: str,
        *,
        raw_error: str | None = None,
        gid_hint: str | None = None,
    ) -> None:
        from app.services.history import add_task_history

        gid_to_cleanup, task_name, task_uri, task_total_length, history_inputs = (
            await self._mark_submit_failed_state(
                task_id=task_id,
                message=message,
                raw_error=raw_error,
                gid_hint=gid_hint,
            )
        )

        for owner_id, created_at in history_inputs:
            await add_task_history(
                owner_id=owner_id,
                task_name=task_name,
                result="failed",
                reason=message,
                uri=task_uri,
                total_length=task_total_length,
                created_at=created_at,
            )

        await cleanup_failed_task_artifacts(
            client=self.client,
            task_id=task_id,
            gid=gid_to_cleanup,
            owner_id=self.user_id,
            log_prefix="[RPC]",
        )
    async def _check_quota_and_disk(self) -> None:
        """检查配额和磁盘空间，不足则抛异常"""
        disk_ok, disk_free = self._check_disk_space()
        if not disk_ok:
            raise RpcError(
                RpcErrorCode.QUOTA_EXCEEDED,
                f"Disk space not enough, free: {disk_free / 1024 / 1024 / 1024:.2f} GB"
            )
        user_available = await self._get_user_available_space()
        if user_available <= 0:
            raise RpcError(RpcErrorCode.QUOTA_EXCEEDED, "Your quota has been exceeded")
    # ========== 完整实现的方法 ==========
    async def _handle_add_uri(self, params: list) -> str:
        """aria2.addUri(uris[, options[, position]])"""
        if not params or not isinstance(params[0], list):
            raise RpcError(RpcErrorCode.INVALID_PARAMS, "uris is required")
        uris = params[0]
        if not uris:
            raise RpcError(RpcErrorCode.INVALID_PARAMS, "uris list is empty")
        options = dict(params[1]) if len(params) > 1 and isinstance(params[1], dict) else {}
        # 获取用户空间锁
        from app.core.state import get_user_space_lock
        from app.services.storage import get_task_download_dir
        user_lock = await get_user_space_lock(self.app_state, self.user_id)
        async with user_lock:
            await self._check_quota_and_disk()
            uri = uris[0] if uris else ""
            uri_hash = get_uri_hash(uri) or hashlib.sha256(uri.encode()).hexdigest()
            task_name = uri.split("/")[-1] or uri

            task, _ = await self._find_or_create_task(uri_hash=uri_hash, uri=uri, name=task_name)
            task_id = task.id
            if task_id is None:
                raise RpcError(RpcErrorCode.INTERNAL_ERROR, "Task id missing")

            await self._create_or_get_subscription(task_id)

            if task.gid and task.status in RUNNABLE_TASK_STATUSES:
                return task.gid
            options["dir"] = str(get_task_download_dir(task_id))

            task_lock = await self._get_task_submit_lock(task_id)
            async with task_lock:
                async with get_session() as db:
                    db_task = await db.get(DownloadTask, task_id)
                    if not db_task:
                        raise RpcError(RpcErrorCode.INTERNAL_ERROR, "Task not found after add")
                    if db_task.gid and db_task.status in RUNNABLE_TASK_STATUSES:
                        return db_task.gid

                try:
                    gid = await self.client.add_uri(uris, options)
                except Exception as exc:
                    await self._mark_submit_failed(
                        task_id,
                        "添加下载任务失败",
                        raw_error=str(exc),
                    )
                    raise RpcError(RpcErrorCode.INTERNAL_ERROR, str(exc))

                try:
                    async with get_session() as db:
                        db_task = await db.get(DownloadTask, task_id)
                        if not db_task:
                            raise RpcError(RpcErrorCode.INTERNAL_ERROR, "Task not found after add")
                        db_task.gid = gid
                        db_task.uri = uri
                        db_task.name = task_name
                        db_task.status = "active"
                        db.add(db_task)
                except Exception as exc:
                    await self._mark_submit_failed(
                        task_id,
                        "添加下载任务失败",
                        raw_error=str(exc),
                        gid_hint=gid,
                    )
                    raise RpcError(RpcErrorCode.INTERNAL_ERROR, str(exc))

                return gid
    async def _handle_add_torrent(self, params: list) -> str:
        """aria2.addTorrent(torrent[, uris[, options[, position]]])"""
        if not params or not isinstance(params[0], str):
            raise RpcError(RpcErrorCode.INVALID_PARAMS, "torrent data is required")
        torrent_data = params[0]
        # 限制 torrent 文件大小（10MB）
        if len(torrent_data) > 10 * 1024 * 1024:
            raise RpcError(RpcErrorCode.INVALID_PARAMS, "Torrent data too large")
        uris = params[1] if len(params) > 1 and isinstance(params[1], list) else []
        options = dict(params[2]) if len(params) > 2 and isinstance(params[2], dict) else {}
        from app.core.state import get_user_space_lock
        from app.services.storage import get_task_download_dir
        user_lock = await get_user_space_lock(self.app_state, self.user_id)
        async with user_lock:
            await self._check_quota_and_disk()
            info_hash = extract_info_hash_from_torrent_base64(torrent_data)
            uri_hash = info_hash or hashlib.sha256(torrent_data.encode()).hexdigest()
            task_uri = f"torrent:{uri_hash}"
            task_name = f"torrent-{uri_hash[:12]}"

            task, _ = await self._find_or_create_task(uri_hash=uri_hash, uri=task_uri, name=task_name)
            task_id = task.id
            if task_id is None:
                raise RpcError(RpcErrorCode.INTERNAL_ERROR, "Task id missing")

            await self._create_or_get_subscription(task_id)

            if task.gid and task.status in RUNNABLE_TASK_STATUSES:
                return task.gid
            options["dir"] = str(get_task_download_dir(task_id))

            task_lock = await self._get_task_submit_lock(task_id)
            async with task_lock:
                async with get_session() as db:
                    db_task = await db.get(DownloadTask, task_id)
                    if not db_task:
                        raise RpcError(RpcErrorCode.INTERNAL_ERROR, "Task not found after add")
                    if db_task.gid and db_task.status in RUNNABLE_TASK_STATUSES:
                        return db_task.gid

                try:
                    gid = await self.client.add_torrent(torrent_data, uris, options)
                except Exception as exc:
                    await self._mark_submit_failed(
                        task_id,
                        "添加种子任务失败",
                        raw_error=str(exc),
                    )
                    raise RpcError(RpcErrorCode.INTERNAL_ERROR, str(exc))

                try:
                    name = task_name
                    try:
                        status = await self.client.tell_status(gid)
                        if isinstance(status, dict):
                            bt_info = status.get("bittorrent", {})
                            if isinstance(bt_info, dict):
                                info = bt_info.get("info", {})
                                if isinstance(info, dict):
                                    name = self._status_str(info.get("name"), "") or name
                    except Exception as exc:
                        logger.debug(
                            "Failed to fetch bittorrent metadata for gid=%s user_id=%s",
                            gid,
                            self.user_id,
                            exc_info=exc,
                        )

                    async with get_session() as db:
                        db_task = await db.get(DownloadTask, task_id)
                        if not db_task:
                            raise RpcError(RpcErrorCode.INTERNAL_ERROR, "Task not found after add")
                        db_task.gid = gid
                        db_task.uri = task_uri
                        db_task.name = name
                        db_task.status = "active"
                        db.add(db_task)
                except Exception as exc:
                    await self._mark_submit_failed(
                        task_id,
                        "添加种子任务失败",
                        raw_error=str(exc),
                        gid_hint=gid,
                    )
                    raise RpcError(RpcErrorCode.INTERNAL_ERROR, str(exc))

                return gid
    async def _handle_remove(self, params: list) -> str:
        """aria2.remove(gid)"""
        if not params:
            raise RpcError(RpcErrorCode.INVALID_PARAMS, "gid is required")
        gid = params[0]
        pair = await self._verify_task_owner(gid)
        if not pair:
            raise RpcError(RpcErrorCode.TASK_NOT_FOUND, f"Task not found: {gid}")
        task, sub = pair
        if sub.status != "pending":
            raise RpcError(RpcErrorCode.TASK_NOT_FOUND, f"Task not found: {gid}")
        is_active_cancel = task.status in ("queued", "active")
        if sub.id is None:
            raise RpcError(RpcErrorCode.INTERNAL_ERROR, "Subscription id missing")
        if task.id is None:
            raise RpcError(RpcErrorCode.INTERNAL_ERROR, "Task id missing")

        task_id = task.id
        remaining_count = 0
        async with get_session() as db:
            db_sub = await db.get(UserTaskSubscription, sub.id)
            if db_sub:
                await db.delete(db_sub)

            result = await db.exec(
                select(func.count()).select_from(UserTaskSubscription).where(
                    UserTaskSubscription.task_id == task_id,
                    UserTaskSubscription.status == "pending",
                )
            )
            remaining_count = self._to_int_scalar(result.one(), default=0)

        # 写历史记录
        if is_active_cancel:
            from app.services.history import add_task_history
            await add_task_history(
                owner_id=self.user_id,
                task_name=task.name or task.uri or "",
                result="cancelled",
                reason="用户取消",
                uri=task.uri,
                total_length=task.total_length,
                created_at=sub.created_at,
            )

        if remaining_count == 0:
            lock = await self._get_task_submit_lock(task_id)
            async with lock:
                async with get_session() as db:
                    result = await db.exec(
                        select(func.count()).select_from(UserTaskSubscription).where(
                            UserTaskSubscription.task_id == task_id,
                            UserTaskSubscription.status == "pending",
                        )
                    )
                    still_pending = self._to_int_scalar(result.one(), default=0)
                    if still_pending != 0:
                        return gid

                    db_task = await db.get(DownloadTask, task_id)

                async with get_session() as db:
                    latest_task = await db.get(DownloadTask, task_id)
                    if latest_task is not None and latest_task.status in CANCELABLE_TASK_STATUSES:
                        latest_task.gid = None
                        latest_task.status = "error"
                        latest_task.error_display = "已取消"
                        latest_task.updated_at = utc_now_str()
                        db.add(latest_task)

                # Clean up via unified entry point
                from app.aria2.failed_task_cleanup import cleanup_failed_task_artifacts
                await cleanup_failed_task_artifacts(
                    client=self.client,
                    task_id=task_id,
                    gid=db_task.gid if db_task else None,
                    owner_id=self.user_id,
                    log_prefix="[RPC]",
                    skip_status_check=True,
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

        special_status = await self._resolve_special_gid_status(gid)
        if special_status is not None:
            return self._apply_status_keys(special_status, keys)

        pair = await self._verify_task_owner(gid)
        if not pair:
            raise RpcError(RpcErrorCode.TASK_NOT_FOUND, f"Task not found: {gid}")
        task, sub = pair
        # 尝试从 aria2 获取实时数据
        try:
            status = await self.client.tell_status(gid)
            response = self._sanitize_status(status)
            response = self._enrich_status_files_from_task(response, task)
        except Exception as exc:
            # aria2 中已不存在（已完成/失败），从 DB 构造
            logger.debug(
                "Fallback to DB status for gid=%s user_id=%s",
                gid,
                self.user_id,
                exc_info=exc,
            )
            response = self._build_status_from_db(task, sub)
        return self._apply_status_keys(response, keys)

    def _build_status_from_db(self, task: DownloadTask, sub: UserTaskSubscription) -> dict:
        """从数据库记录构造 aria2 tellStatus 格式的响应"""
        # 状态映射
        status_map = {
            "success": DEFAULT_STATUS_COMPLETE,
            "failed": DEFAULT_STATUS_ERROR,
            "active": "active",
            "queued": "waiting",
            "waiting": "waiting",
            "paused": "paused",
            "complete": DEFAULT_STATUS_COMPLETE,
            "error": DEFAULT_STATUS_ERROR,
            "removed": "removed",
        }
        aria2_status = status_map.get(sub.status, status_map.get(task.status, DEFAULT_STATUS_ERROR))
        total_length = task.total_length or 0
        completed_length = task.completed_length or 0
        error_message = task.error or task.error_display or ""
        result = self._new_status_payload()
        result["gid"] = self._build_history_gid(task)
        result["status"] = aria2_status
        result["totalLength"] = str(total_length)
        result["completedLength"] = str(completed_length)
        result["downloadSpeed"] = str(task.download_speed or 0)
        result["uploadSpeed"] = str(task.upload_speed or 0)
        result["connections"] = str(task.peak_connections or 0)
        result["errorCode"] = "1" if aria2_status == DEFAULT_STATUS_ERROR else DEFAULT_ERROR_CODE
        result["errorMessage"] = error_message if aria2_status == DEFAULT_STATUS_ERROR else ""
        result["files"] = self._build_status_files(
            task_name=task.name,
            uri=task.uri,
            total_length=total_length,
            completed_length=completed_length,
            fallback_name=result["gid"] or "task",
        )

        if self._is_bittorrent_uri(task.uri):
            result["infoHash"] = self._build_bt_info_hash(task.uri_hash)
            result["bittorrent"]["info"]["name"] = task.name or result["files"][0]["path"]

        return result

    async def _handle_tell_active(self, params: list) -> list:
        """aria2.tellActive([keys])"""
        keys = self._extract_status_keys(params, 0)
        user_gids = await self._get_user_gids(sub_statuses=["pending"])
        if not user_gids:
            return []
        try:
            all_active = await self.client.tell_active()
        except Exception as exc:
            logger.warning(
                "aria2.tellActive failed for user_id=%s",
                self.user_id,
                exc_info=exc,
            )
        else:
            active_statuses = [self._sanitize_status(t) for t in all_active if t.get("gid") in user_gids]
            if active_statuses:
                status_gids = self._extract_gids_from_statuses(active_statuses)
                task_map = await self._get_pending_task_map(status_gids)
                enriched_statuses = self._enrich_statuses_with_task_map(active_statuses, task_map)
                return self._apply_status_keys_to_list(enriched_statuses, keys)

        fallback_pairs = await self._get_pending_task_pairs(["active"])
        fallback_statuses = [self._build_status_from_db(task, sub) for task, sub in fallback_pairs]
        return self._apply_status_keys_to_list(fallback_statuses, keys)

    async def _handle_tell_waiting(self, params: list) -> list:
        """aria2.tellWaiting(offset, num[, keys])"""
        offset, num = self._normalize_pagination(params)
        keys = self._extract_status_keys(params, 2)
        user_gids = await self._get_user_gids(sub_statuses=["pending"])
        if not user_gids:
            return []
        try:
            all_waiting = await self._fetch_waiting_tasks()
        except Exception as exc:
            logger.warning(
                "aria2.tellWaiting failed for user_id=%s",
                self.user_id,
                exc_info=exc,
            )
        else:
            filtered = [self._sanitize_status(t) for t in all_waiting if t.get("gid") in user_gids]
            if filtered:
                sliced = self._slice_with_offset(filtered, offset, num)
                status_gids = self._extract_gids_from_statuses(sliced)
                task_map = await self._get_pending_task_map(status_gids)
                enriched_statuses = self._enrich_statuses_with_task_map(sliced, task_map)
                return self._apply_status_keys_to_list(enriched_statuses, keys)

        fallback_pairs = await self._get_pending_task_pairs(["queued", "waiting", "paused"])
        fallback_statuses = [self._build_status_from_db(task, sub) for task, sub in fallback_pairs]
        sliced_fallback = self._slice_with_offset(fallback_statuses, offset, num)
        return self._apply_status_keys_to_list(sliced_fallback, keys)

    async def _handle_tell_stopped(self, params: list) -> list:
        """aria2.tellStopped(offset, num[, keys])"""
        offset, num = self._normalize_pagination(params)
        keys = self._extract_status_keys(params, 2)

        async with get_session() as db:
            stmt = (
                select(TaskHistory)
                .where(
                    TaskHistory.owner_id == self.user_id,
                )
                .order_by(col(TaskHistory.id).asc())
            )
            result = await db.exec(stmt)
            rows = result.all()

        sliced_rows = self._slice_with_offset(rows, offset, num)
        stopped_statuses = [self._build_status_from_history(history) for history in sliced_rows]
        return self._apply_status_keys_to_list(stopped_statuses, keys)
    async def _handle_get_global_stat(self, params: list) -> dict:
        """aria2.getGlobalStat()"""
        # 用户级别统计
        async with get_session() as db:
            # active: subscription pending + task active
            r_active = await db.exec(
                select(func.count()).select_from(UserTaskSubscription)
                .join(DownloadTask, UserTaskSubscription.task_id == DownloadTask.id)  # type: ignore[arg-type]
                .where(
                    UserTaskSubscription.owner_id == self.user_id,
                    UserTaskSubscription.status == "pending",
                    DownloadTask.status == "active",
                )
            )
            num_active = self._to_int_scalar(r_active.one(), default=0)
            # waiting: subscription pending + task queued/waiting
            r_waiting = await db.exec(
                select(func.count()).select_from(UserTaskSubscription)
                .join(DownloadTask, UserTaskSubscription.task_id == DownloadTask.id)  # type: ignore[arg-type]
                .where(
                    UserTaskSubscription.owner_id == self.user_id,
                    UserTaskSubscription.status == "pending",
                    col(DownloadTask.status).in_(["queued", "waiting"]),
                )
            )
            num_waiting = self._to_int_scalar(r_waiting.one(), default=0)
            # stopped
            r_stopped = await db.exec(
                select(func.count()).select_from(TaskHistory).where(
                    TaskHistory.owner_id == self.user_id,
                )
            )
            num_stopped = self._to_int_scalar(r_stopped.one(), default=0)
        # 获取全局速度
        try:
            global_stat = await self.client.get_global_stat()
            download_speed = global_stat.get("downloadSpeed", "0")
            upload_speed = global_stat.get("uploadSpeed", "0")
        except Exception as exc:
            logger.warning(
                "aria2.getGlobalStat failed for user_id=%s, fallback to zero speed",
                self.user_id,
                exc_info=exc,
            )
            download_speed = "0"
            upload_speed = "0"
        return {
            "downloadSpeed": download_speed,
            "uploadSpeed": upload_speed,
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
        special_status = await self._resolve_special_gid_status(gid)
        if gid.startswith(SPECIAL_GID_PREFIXES):
            if special_status is None:
                raise RpcError(RpcErrorCode.TASK_NOT_FOUND, f"Task not found: {gid}")
            return self._sanitize_files(special_status.get("files"))
        pair = await self._verify_task_owner(gid)
        if not pair:
            raise RpcError(RpcErrorCode.TASK_NOT_FOUND, f"Task not found: {gid}")
        try:
            files = await self.client.get_files(gid)
            return self._sanitize_files(files)
        except Exception as exc:
            logger.warning(
                "aria2.getFiles failed for gid=%s user_id=%s",
                gid,
                self.user_id,
                exc_info=exc,
            )
            return []
    async def _handle_get_uris(self, params: list) -> list:
        """aria2.getUris(gid)"""
        if not params:
            raise RpcError(RpcErrorCode.INVALID_PARAMS, "gid is required")
        gid = str(params[0])
        special_status = await self._resolve_special_gid_status(gid)
        if gid.startswith(SPECIAL_GID_PREFIXES):
            if special_status is None:
                raise RpcError(RpcErrorCode.TASK_NOT_FOUND, f"Task not found: {gid}")
            source_uri = await self._get_special_gid_source_uri(gid)
            if not source_uri:
                return []
            return self._sanitize_uris([{"uri": source_uri, "status": "used"}])
        pair = await self._verify_task_owner(gid)
        if not pair:
            raise RpcError(RpcErrorCode.TASK_NOT_FOUND, f"Task not found: {gid}")
        try:
            uris = await self.client.get_uris(gid)
            return self._sanitize_uris(uris)
        except Exception as exc:
            logger.warning(
                "aria2.getUris failed for gid=%s user_id=%s",
                gid,
                self.user_id,
                exc_info=exc,
            )
            return []
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
        async with get_session() as db:
            if history_id is not None:
                history = await db.get(TaskHistory, history_id)
                if history is None or history.owner_id != self.user_id:
                    raise RpcError(RpcErrorCode.TASK_NOT_FOUND, f"Task not found: {gid_param}")
                await db.delete(history)
                await db.commit()
                return "OK"

            stmt = (
                select(UserTaskSubscription)
                .join(DownloadTask, UserTaskSubscription.task_id == DownloadTask.id)  # type: ignore[arg-type]
                .where(
                    UserTaskSubscription.owner_id == self.user_id,
                    col(UserTaskSubscription.status).in_(["success", "failed"]),
                    col(DownloadTask.status).in_(["complete", "error"]),
                )
            )
            if task_id is not None:
                stmt = stmt.where(DownloadTask.id == task_id)
            else:
                stmt = stmt.where(DownloadTask.gid == gid)

            result = await db.exec(stmt)
            sub = result.first()
            if not sub:
                raise RpcError(RpcErrorCode.TASK_NOT_FOUND, f"Task not found: {gid_param}")

            task = await db.get(DownloadTask, sub.task_id)
            await db.delete(sub)

            if task is not None:
                history_stmt = (
                    select(TaskHistory)
                    .where(
                        TaskHistory.owner_id == self.user_id,
                        TaskHistory.uri == task.uri,
                    )
                    .order_by(col(TaskHistory.id).desc())
                )
                history = (await db.exec(history_stmt)).first()
                if history is not None:
                    await db.delete(history)

            await db.commit()
        return "OK"
    async def _handle_purge_download_result(self, params: list) -> str:
        """aria2.purgeDownloadResult() - 删除用户所有 stopped 订阅"""
        async with get_session() as db:
            result = await db.exec(
                select(UserTaskSubscription).where(
                    UserTaskSubscription.owner_id == self.user_id,
                    col(UserTaskSubscription.status).in_(["success", "failed"]),
                )
            )
            subs = result.all()
            for sub in subs:
                await db.delete(sub)

            history_result = await db.exec(
                select(TaskHistory).where(TaskHistory.owner_id == self.user_id)
            )
            for history in history_result.all():
                await db.delete(history)

            await db.commit()
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
    async def _handle_change_option(self, params: list) -> str:
        return "OK"
    async def _handle_get_global_option(self, params: list) -> dict:
        return {}
    async def _handle_change_global_option(self, params: list) -> str:
        return "OK"
    async def _handle_change_position(self, params: list) -> int:
        return 0
    async def _handle_change_uri(self, params: list) -> list:
        return [0, 0]
    async def _handle_get_peers(self, params: list) -> list:
        if not params:
            raise RpcError(RpcErrorCode.INVALID_PARAMS, "gid is required")
        gid = str(params[0])
        special_status = await self._resolve_special_gid_status(gid)
        if gid.startswith(SPECIAL_GID_PREFIXES):
            if special_status is None:
                raise RpcError(RpcErrorCode.TASK_NOT_FOUND, f"Task not found: {gid}")
            return []
        pair = await self._verify_task_owner(gid)
        if not pair:
            raise RpcError(RpcErrorCode.TASK_NOT_FOUND, f"Task not found: {gid}")
        try:
            peers = await self.client.get_peers(gid)
            return self._sanitize_peers(peers)
        except Exception as exc:
            logger.warning(
                "aria2.getPeers failed for gid=%s user_id=%s",
                gid,
                self.user_id,
                exc_info=exc,
            )
            return []
    async def _handle_get_servers(self, params: list) -> list:
        if not params:
            raise RpcError(RpcErrorCode.INVALID_PARAMS, "gid is required")
        gid = str(params[0])
        special_status = await self._resolve_special_gid_status(gid)
        if gid.startswith(SPECIAL_GID_PREFIXES):
            if special_status is None:
                raise RpcError(RpcErrorCode.TASK_NOT_FOUND, f"Task not found: {gid}")
            return []
        pair = await self._verify_task_owner(gid)
        if not pair:
            raise RpcError(RpcErrorCode.TASK_NOT_FOUND, f"Task not found: {gid}")
        try:
            server_groups = await self.client.get_servers(gid)
            return self._sanitize_servers(server_groups)
        except Exception as exc:
            logger.warning(
                "aria2.getServers failed for gid=%s user_id=%s",
                gid,
                self.user_id,
                exc_info=exc,
            )
            return []
    async def _handle_shutdown(self, params: list) -> str:
        return "OK"
    async def _handle_force_shutdown(self, params: list) -> str:
        return "OK"
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
        methods = params[0]
        results = []
        for call in methods:
            if not isinstance(call, dict):
                results.append({"faultCode": RpcErrorCode.INVALID_PARAMS, "faultString": "Invalid method call"})
                continue
            method_name = call.get("methodName", "")
            method_params = self._strip_rpc_token(call.get("params", []))
            try:
                result = await self.handle(method_name, method_params)
                results.append([result])
            except RpcError as e:
                results.append({"faultCode": e.code, "faultString": e.message})
            except Exception as e:
                results.append({"faultCode": RpcErrorCode.INTERNAL_ERROR, "faultString": str(e)})
        return results
