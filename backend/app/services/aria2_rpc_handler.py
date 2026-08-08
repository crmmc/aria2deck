"""aria2 RPC 方法处理器

为外部 aria2 兼容客户端（如 AriaNg、Motrix）提供 RPC 方法实现。
实现用户隔离、数据脱敏、配额检查等安全机制。

基于 v0 shared download tables（global_downloads + user_tasks）。
"""

from __future__ import annotations

import hashlib
import logging
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from app.core.config import get_internal_base_url
from app.domain.errors import (
    BadGatewayError,
    ConflictError,
    DomainError,
    ForbiddenError,
    NotFoundError,
)
from app.http.safe_client import UnsafeTargetError, normalize_public_http_url
from app.core.config import settings
from app.core.security import (
    MAX_DOWNLOAD_URI_COUNT,
    WEBSEED_URI_SCHEMES,
    check_torrent_network_endpoints,
    check_url_ssrf,
)
from app.domain.status import ACTIVE_USER_TASK_STATUSES
from app.domain.task_policy import stat_counts
from app.domain.torrent_metadata import (
    TorrentMetadata,
    TorrentMetadataError,
    build_select_file_option,
    build_selection_resource_key,
    parse_torrent_base64_async,
    selected_total_size,
    validate_selected_indexes,
)
from app.repositories import auth as auth_repo
from app.repositories.downloads import (
    delete_all_terminal_user_tasks,
    delete_terminal_user_task,
    delete_terminal_user_task_by_gid,
    get_user_task_by_gid,
    get_user_task_by_id,
    list_user_tasks,
)
from app.services import aria2_snapshot_sanitize, rpc_view_service, task_service
from app.modules.task_core.register import ResourceSpec
from app.services.hash import extract_info_hash_from_magnet, get_uri_hash
from app.services.internal_fetch import (
    http_resource_identity,
    source_request_options,
)
from app.services.settings_service import get_min_free_disk
from app.services.task_projection import (
    has_real_file_path,
    speed_totals,
)
from app.services.task_projection_rows import attach_snapshots_to_rows
from app.services.usage_service import get_usage

SAFE_INTERNAL_ERROR_MESSAGE = "Internal error"
RPC_ADD_URI_SCHEMES = frozenset({"http", "https", "magnet"})

logger = logging.getLogger(__name__)


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
    TASK_EXISTS = 4


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

    def __init__(self, user_id: int):
        self.user_id = user_id
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

    async def _get_projection_row(self, row: dict[str, Any]) -> dict[str, Any]:
        """Attach the backend snapshot/files projection to a task row."""
        return (await attach_snapshots_to_rows([row]))[0]

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
            row = await self._get_task_pair_by_task_id(task_id)
        else:
            row = await self._verify_task_owner(gid)
        return row

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
        return aria2_snapshot_sanitize.sanitize_files(files)

    def _sanitize_uris(self, uris: Any) -> list[dict]:
        return aria2_snapshot_sanitize.sanitize_uris(uris)

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

    def _apply_status_keys_to_list(
        self, statuses: list[dict], keys: list[str] | None
    ) -> list[dict]:
        return [self._apply_status_keys(item, keys) for item in statuses]

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

    @staticmethod
    def _resource_key_for_uri(uri: str) -> str:
        resource_key = get_uri_hash(uri)
        if resource_key:
            return resource_key
        if uri.lower().startswith("magnet:"):
            raise RpcError(RpcErrorCode.INVALID_PARAMS, "无效的磁力链接")
        return hashlib.sha256(uri.encode()).hexdigest()

    @staticmethod
    def _selected_torrent_indexes(
        metadata: TorrentMetadata, value: Any
    ) -> tuple[int, ...]:
        if value is None or value == "":
            return validate_selected_indexes(metadata, None)
        if not isinstance(value, str):
            raise RpcError(
                RpcErrorCode.INVALID_PARAMS, "select-file 必须是字符串"
            )
        indexes: list[int] = []
        file_count = metadata.file_count
        try:
            for part in value.split(","):
                token = part.strip()
                if not token:
                    raise ValueError
                if "-" not in token:
                    index = int(token)
                    if not 1 <= index <= file_count or len(indexes) >= file_count:
                        raise ValueError
                    indexes.append(index)
                    continue
                start_text, end_text = token.split("-", 1)
                start, end = int(start_text), int(end_text)
                if not 1 <= start <= end <= file_count:
                    raise ValueError
                if len(indexes) + end - start + 1 > file_count:
                    raise ValueError
                indexes.extend(range(start, end + 1))
            selected_values: list[object] = list(indexes)
            return validate_selected_indexes(metadata, selected_values)
        except (ValueError, TorrentMetadataError) as exc:
            raise RpcError(
                RpcErrorCode.INVALID_PARAMS, "select-file 参数无效"
            ) from exc

    @staticmethod
    def _validate_submit_options(options: Mapping[str, Any] | None) -> None:
        if not options or "out" not in options:
            return
        out = str(options["out"])
        if not out or out in {".", ".."} or "/" in out or "\\" in out:
            raise RpcError(
                RpcErrorCode.INVALID_PARAMS,
                "invalid out option: must be a filename without path separators",
            )

    @staticmethod
    def _with_rpc_mirrors(
        options: Mapping[str, Any], submit_uris: list[str]
    ) -> dict[str, Any]:
        """把 addUri 的备用 URI 存入 mirrors，供 Task Core 提交使用。"""
        result = dict(options)
        if len(submit_uris) > 1:
            result["mirrors"] = submit_uris[1:]
        return result

    async def _gid_for_created_task(
        self,
        task: dict[str, Any],
        resource_key: str,
    ) -> str:
        return f"task-{task['id']}"

    async def _validate_uri_list(
        self,
        value: Any,
        *,
        name: str,
        allowed_schemes: frozenset[str],
        allow_empty: bool,
    ) -> list[str]:
        if not isinstance(value, list):
            raise RpcError(RpcErrorCode.INVALID_PARAMS, f"{name} must be an array")
        if not value and not allow_empty:
            raise RpcError(RpcErrorCode.INVALID_PARAMS, f"{name} list is empty")
        if len(value) > MAX_DOWNLOAD_URI_COUNT:
            raise RpcError(
                RpcErrorCode.INVALID_PARAMS,
                f"Too many {name}, max {MAX_DOWNLOAD_URI_COUNT}",
            )

        uris: list[str] = []
        for index, item in enumerate(value):
            if not isinstance(item, str):
                raise RpcError(
                    RpcErrorCode.INVALID_PARAMS,
                    f"{name}[{index}] must be a string",
                )
            normalized_item = item
            if urlsplit(item).scheme.lower() == "magnet":
                info_hash = extract_info_hash_from_magnet(item)
                if not info_hash:
                    raise RpcError(
                        RpcErrorCode.INVALID_PARAMS,
                        f"{name}[{index}]: 无效的磁力链接",
                    )
                normalized_item = f"magnet:?xt=urn:btih:{info_hash}"
            error = await check_url_ssrf(
                normalized_item,
                allowed_schemes=allowed_schemes,
            )
            if error:
                raise RpcError(
                    RpcErrorCode.INVALID_PARAMS,
                    f"{name}[{index}]: {error}",
                )
            uris.append(normalized_item)
        return uris

    def _raise_create_download_error(self, exc: Exception) -> None:
        """把 register/submit 的 DomainError 映射为 JSON-RPC 错误码。"""
        if isinstance(exc, RpcError):
            raise exc
        if isinstance(exc, ConflictError):
            raise RpcError(RpcErrorCode.TASK_EXISTS, str(exc)) from exc
        if isinstance(exc, ForbiddenError):
            raise RpcError(RpcErrorCode.QUOTA_EXCEEDED, str(exc)) from exc
        if isinstance(exc, DomainError):
            raise RpcError(RpcErrorCode.INVALID_PARAMS, str(exc)) from exc
        if isinstance(exc, ValueError):
            raise RpcError(RpcErrorCode.INVALID_PARAMS, str(exc)) from exc
        logger.warning(
            "RPC创建下载任务内部异常 user_id=%s error_type=%s",
            self.user_id,
            type(exc).__name__,
        )
        raise RpcError(
            RpcErrorCode.INTERNAL_ERROR,
            SAFE_INTERNAL_ERROR_MESSAGE,
        ) from exc

    # ========== 完整实现的方法 ==========
    async def _handle_add_uri(self, params: list) -> str:
        """aria2.addUri(uris[, options[, position]])"""
        if not params:
            raise RpcError(RpcErrorCode.INVALID_PARAMS, "uris is required")
        submit_uris = await self._validate_uri_list(
            params[0],
            name="uris",
            allowed_schemes=RPC_ADD_URI_SCHEMES,
            allow_empty=False,
        )
        if len(submit_uris) > 1 and any(
            urlsplit(item).scheme.lower() == "magnet" for item in submit_uris
        ):
            raise RpcError(
                RpcErrorCode.INVALID_PARAMS,
                "magnet URI does not support mirrors",
            )
        options = (
            dict(params[1]) if len(params) > 1 and isinstance(params[1], dict) else {}
        )
        self._validate_submit_options(options)
        if "bt-tracker" in options:
            raise RpcError(
                RpcErrorCode.INVALID_PARAMS, "bt-tracker option is not allowed"
            )
        options = self._with_rpc_mirrors(options, submit_uris)
        await self._check_quota_and_disk()
        uri = submit_uris[0]

        try:
            if self._resource_kind_for_uri(uri) == "http":
                try:
                    uri = normalize_public_http_url(uri)
                except UnsafeTargetError as exc:
                    raise RpcError(RpcErrorCode.INVALID_PARAMS, str(exc)) from exc
                get_internal_base_url()
                source_opts = source_request_options(
                    options, mirrors=submit_uris[1:]
                )
                resource_key = http_resource_identity(
                    self._resource_key_for_uri(uri), source_opts
                )
            else:
                resource_key = self._resource_key_for_uri(uri)

            resource = ResourceSpec(
                resource_key=resource_key,
                source_uri=uri,
                resource_kind=self._resource_kind_for_uri(uri),
                display_name=self._extract_name_from_uri(uri) or uri,
                size_bytes=0,
                size_known=False,
            )
            task = await task_service.register_and_submit(
                user_id=self.user_id,
                quota_bytes=await self._get_user_quota(),
                resource=resource,
                options=options,
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
        webseed_uris = await self._validate_uri_list(
            params[1] if len(params) > 1 else [],
            name="uris",
            allowed_schemes=WEBSEED_URI_SCHEMES,
            allow_empty=True,
        )
        if webseed_uris:
            raise RpcError(
                RpcErrorCode.INVALID_PARAMS,
                "Torrent webseed URIs are not allowed",
            )
        try:
            metadata = await parse_torrent_base64_async(torrent_data)
        except TorrentMetadataError as exc:
            raise RpcError(
                RpcErrorCode.INVALID_PARAMS,
                f"无效的种子文件: {exc}",
            ) from exc
        endpoint_error = await check_torrent_network_endpoints(
            metadata.tracker_urls,
            metadata.webseed_urls,
        )
        if endpoint_error:
            raise RpcError(RpcErrorCode.INVALID_PARAMS, endpoint_error)
        options = (
            dict(params[2]) if len(params) > 2 and isinstance(params[2], dict) else {}
        )
        if "bt-tracker" in options:
            raise RpcError(RpcErrorCode.INVALID_PARAMS, "bt-tracker option is not allowed")
        selected_indexes = self._selected_torrent_indexes(
            metadata, options.pop("select-file", None)
        )
        selected_size = selected_total_size(metadata, selected_indexes)
        select_file = build_select_file_option(
            selected_indexes, metadata.file_count
        )
        submit_options = dict(options)
        if select_file:
            submit_options["select-file"] = select_file
        await self._check_quota_and_disk()
        resource_key = build_selection_resource_key(
            metadata.info_hash,
            selected_indexes,
            total_file_count=metadata.file_count,
        )
        magnet_uri = f"magnet:?xt=urn:btih:{metadata.info_hash}"

        try:
            resource = ResourceSpec(
                resource_key=resource_key,
                source_uri=f"base64:{torrent_data}",
                resource_kind="torrent",
                display_name=metadata.name,
                size_bytes=selected_size,
                size_known=True,
                display_uri=magnet_uri,
            )
            task = await task_service.register_and_submit(
                user_id=self.user_id,
                quota_bytes=await self._get_user_quota(),
                resource=resource,
                options=submit_options,
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
        try:
            await task_service.cancel_task(
                user_id=self.user_id,
                user_task_id=int(row["id"]),
                quota_bytes=await self._get_user_quota(),
            )
        except (NotFoundError, ConflictError):
            raise RpcError(
                RpcErrorCode.TASK_NOT_FOUND, f"Task not found: {gid}"
            ) from None
        except BadGatewayError as exc:
            raise RpcError(
                RpcErrorCode.INTERNAL_ERROR, SAFE_INTERNAL_ERROR_MESSAGE
            ) from exc
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

        projected = await self._get_projection_row(row)
        response = rpc_view_service.status_from_task(projected)
        return self._apply_status_keys(response, keys)

    async def _handle_tell_active(self, params: list) -> list:
        """aria2.tellActive([keys])"""
        keys = self._extract_status_keys(params, 0)
        statuses = await rpc_view_service.list_active_statuses(self.user_id)
        return self._apply_status_keys_to_list(statuses, keys)

    async def _handle_tell_waiting(self, params: list) -> list:
        """aria2.tellWaiting(offset, num[, keys])"""
        offset, num = self._normalize_pagination(params)
        keys = self._extract_status_keys(params, 2)
        waiting_statuses = await rpc_view_service.list_waiting_statuses(self.user_id)
        sliced = self._slice_with_offset(waiting_statuses, offset, num)
        return self._apply_status_keys_to_list(sliced, keys)

    async def _handle_tell_stopped(self, params: list) -> list:
        """aria2.tellStopped(offset, num[, keys])"""
        offset, num = self._normalize_pagination(params)
        keys = self._extract_status_keys(params, 2)
        stopped_statuses = await rpc_view_service.list_stopped_statuses(self.user_id)
        sliced = self._slice_with_offset(stopped_statuses, offset, num)
        return self._apply_status_keys_to_list(sliced, keys)

    async def _handle_get_global_stat(self, params: list) -> dict:
        """aria2.getGlobalStat()"""
        rows = await attach_snapshots_to_rows(await list_user_tasks(self.user_id))
        counts = stat_counts(rows)
        num_active = counts["active"]
        num_waiting = counts["waiting"]
        num_stopped = counts["stopped"]

        speeds = speed_totals(rows)
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
        projected = await self._get_projection_row(row)
        snapshot_files = projected.get("backend_files") or []
        if snapshot_files and self._status_has_file_name({"files": snapshot_files}):
            return snapshot_files
        return self._sanitize_files(
            rpc_view_service.status_from_task(projected).get("files")
        )

    async def _handle_get_uris(self, params: list) -> list:
        """aria2.getUris(gid)"""
        if not params:
            raise RpcError(RpcErrorCode.INVALID_PARAMS, "gid is required")
        gid = str(params[0])
        row = await self._resolve_owned_row(gid)
        if row is None:
            raise RpcError(RpcErrorCode.TASK_NOT_FOUND, f"Task not found: {gid}")
        source_uri = row.get("source_uri")
        return (
            self._sanitize_uris([{"uri": source_uri, "status": "used"}])
            if source_uri
            else []
        )

    async def _handle_get_version(self, params: list) -> dict:
        """aria2.getVersion()"""
        return {"version": "aria2deck-proxy", "enabledFeatures": []}

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

    # ========== 明确拒绝暂停（aria2deck 不支持暂停，只支持取消） ==========
    async def _handle_pause(self, params: list) -> str:
        raise RpcError(1, "Pause is not supported, use aria2.remove to cancel")

    async def _handle_force_pause(self, params: list) -> str:
        raise RpcError(1, "Pause is not supported, use aria2.remove to cancel")

    async def _handle_unpause(self, params: list) -> str:
        raise RpcError(1, "Unpause is not supported")

    async def _handle_pause_all(self, params: list) -> str:
        raise RpcError(1, "Pause is not supported")

    async def _handle_force_pause_all(self, params: list) -> str:
        raise RpcError(1, "Pause is not supported")

    async def _handle_unpause_all(self, params: list) -> str:
        raise RpcError(1, "Unpause is not supported")

    async def _handle_get_option(self, params: list) -> dict:
        if not params:
            raise RpcError(RpcErrorCode.INVALID_PARAMS, "gid is required")
        gid = str(params[0])
        if await self._resolve_owned_row(gid) is None:
            raise RpcError(RpcErrorCode.TASK_NOT_FOUND, "Task not found")
        return {}

    async def _handle_get_global_option(self, params: list) -> dict:
        return {}

    async def _handle_change_position(self, params: list) -> int:
        return 0

    async def _handle_change_uri(self, params: list) -> list:
        raise RpcError(
            RpcErrorCode.PERMISSION_DENIED,
            "URI mutation is not supported",
        )

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
        return []

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
            results: list[Any] = []
            for index, call in enumerate(methods):
                if not isinstance(call, dict):
                    results.append(
                        {
                            "faultCode": RpcErrorCode.INVALID_PARAMS,
                            "faultString": "Invalid method call",
                        }
                    )
                    continue
                method_name = call.get("methodName", "")
                raw_method_params = call.get("params", [])
                method_params = self._strip_rpc_token(raw_method_params)
                try:
                    result = await self.handle(method_name, method_params)
                    results.append([result])
                except RpcError as exc:
                    fault_string = (
                        SAFE_INTERNAL_ERROR_MESSAGE
                        if exc.code == RpcErrorCode.INTERNAL_ERROR
                        else exc.message
                    )
                    results.append(
                        {"faultCode": exc.code, "faultString": fault_string}
                    )
                except Exception as exc:
                    logger.warning(
                        "system.multicall method failed user_id=%s index=%s error_type=%s",
                        self.user_id,
                        index,
                        type(exc).__name__,
                    )
                    results.append(
                        {
                            "faultCode": RpcErrorCode.INTERNAL_ERROR,
                            "faultString": SAFE_INTERNAL_ERROR_MESSAGE,
                        }
                    )
            return results
        finally:
            self._multicall_depth -= 1
