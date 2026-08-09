"""Shared private helpers for the rpc package.

Extracted from ``aria2_rpc_handler.py`` (M4 T13) so that the upcoming
rpc submodules (write / read / system) share a single definition.

``aria2_rpc_handler.py`` (deleted in M4 T16); behaviour is unchanged.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from app.core.config import settings
from app.core.security import (
    MAX_DOWNLOAD_URI_COUNT,
    check_url_ssrf,
)
from app.domain.errors import (
    ConflictError,
    DomainError,
    ForbiddenError,
)
from app.domain.torrent_metadata import (
    TorrentMetadata,
    TorrentMetadataError,
    validate_selected_indexes,
)
from app.repositories import auth as auth_repo
from app.repositories.task.user_tasks import (
    get_user_task_by_gid,
    get_user_task_by_id,
)
from app.services.hash import extract_info_hash_from_magnet, get_uri_hash
from app.services.settings_service import get_min_free_disk
from app.services.usage_service import get_usage

logger = logging.getLogger(__name__)

SAFE_INTERNAL_ERROR_MESSAGE = "Internal error"


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


async def _resolve_owned_row(user_id: int, gid: str) -> dict[str, Any] | None:
    """Resolve a client-facing gid to the current user's task row.

    The only identity exposed to clients is ``task-{id}``; a raw aria2 gid
    is still accepted for backward compatibility. ``hist-`` gids never map
    to a live task.
    """
    _, task_id, history_id = _parse_history_gid(gid)
    if history_id is not None:
        return None
    if task_id is not None:
        return await get_user_task_by_id(user_id, task_id)
    return await get_user_task_by_gid(user_id, gid)


async def _check_quota_and_disk(user_id: int) -> None:
    """检查配额和磁盘空间，不足则抛异常"""
    download_path = Path(settings.download_dir)
    download_path.mkdir(parents=True, exist_ok=True)
    disk = shutil.disk_usage(download_path)
    min_free = get_min_free_disk()
    if disk.free <= min_free:
        raise RpcError(
            RpcErrorCode.QUOTA_EXCEEDED,
            f"Disk space not enough, free: {disk.free / 1024 / 1024 / 1024:.2f} GB",
        )
    user_available = await _get_user_available_space(user_id)
    if user_available <= 0:
        raise RpcError(RpcErrorCode.QUOTA_EXCEEDED, "Your quota has been exceeded")


async def _get_user_available_space(user_id: int) -> int:
    """获取用户实际可用空间"""
    user = await auth_repo.get_user_by_id(user_id)
    if user is None:
        return 0
    quota_bytes = int(user["quota_bytes"])
    if quota_bytes <= 0:
        return 0
    usage = await get_usage(user_id, quota_bytes)
    download_path = Path(settings.download_dir)
    download_path.mkdir(parents=True, exist_ok=True)
    disk_free = shutil.disk_usage(download_path).free
    return min(int(usage["available_bytes"]), disk_free)


async def _get_user_quota(user_id: int) -> int:
    user = await auth_repo.get_user_by_id(user_id)
    return int(user["quota_bytes"]) if user else 0


def _raise_create_download_error(user_id: int, exc: Exception) -> None:
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
        user_id,
        type(exc).__name__,
    )
    raise RpcError(
        RpcErrorCode.INTERNAL_ERROR,
        SAFE_INTERNAL_ERROR_MESSAGE,
    ) from exc


async def _gid_for_created_task(
    task: dict[str, Any],
    resource_key: str,
) -> str:
    return f"task-{task['id']}"


async def _validate_uri_list(
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


def _validate_submit_options(options: Mapping[str, Any] | None) -> None:
    if not options or "out" not in options:
        return
    out = str(options["out"])
    if not out or out in {".", ".."} or "/" in out or "\\" in out:
        raise RpcError(
            RpcErrorCode.INVALID_PARAMS,
            "invalid out option: must be a filename without path separators",
        )


def _with_rpc_mirrors(
    options: Mapping[str, Any], submit_uris: list[str]
) -> dict[str, Any]:
    """把 addUri 的备用 URI 存入 mirrors，供 Task Core 提交使用。"""
    result = dict(options)
    if len(submit_uris) > 1:
        result["mirrors"] = submit_uris[1:]
    return result


def _resource_kind_for_uri(uri: str) -> str:
    lower = uri.lower()
    if lower.startswith("magnet:"):
        return "magnet"
    if lower.startswith(("http://", "https://")):
        return "http"
    return "other"


def _resource_key_for_uri(uri: str) -> str:
    resource_key = get_uri_hash(uri)
    if resource_key:
        return resource_key
    if uri.lower().startswith("magnet:"):
        raise RpcError(RpcErrorCode.INVALID_PARAMS, "无效的磁力链接")
    return hashlib.sha256(uri.encode()).hexdigest()


def _extract_name_from_uri(uri: str) -> str:
    if not uri:
        return ""
    parsed = urlsplit(uri)
    if not parsed.path:
        return ""
    decoded_path = unquote(parsed.path)
    return Path(decoded_path).name


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


def _apply_status_keys(status: dict, keys: list[str] | None) -> dict:
    if not keys:
        return status
    return {key: status[key] for key in keys if key in status}


def _apply_status_keys_to_list(
    statuses: list[dict], keys: list[str] | None
) -> list[dict]:
    return [_apply_status_keys(item, keys) for item in statuses]
