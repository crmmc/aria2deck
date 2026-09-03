"""任务 HTTP 端点适配层。

业务实现已下沉到 app.services.task_orchestration；本模块保留 REST/RPC
端点适配函数及错误映射，并 re-export orchestration 符号保持既有
import/patch 路径兼容。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from app.domain.errors import (
    BadGatewayError,
    BadRequestError,
    ConflictError,
    DomainError,
    NotFoundError,
)
from app.modules.task_core.unref import (
    ERROR_ALREADY_TERMINAL,
    ERROR_FORBIDDEN,
    ERROR_NOT_FOUND,
    UnrefError,
    unref,
)
from app.repositories.task.user_tasks import (
    clear_terminal_user_tasks,
    get_user_task_by_id,
    list_user_tasks_page,
)
from app.services.settings_service import get_min_free_disk

# Re-export orchestration 符号：保持 `app.services.task_service.<name>`
# 的 import 与 patch 路径兼容（orchestration 内部对可替换依赖一律经
# task_service 模块属性查找，patch 本模块即生效）。
from app.services.task_orchestration import (
    MAGNET_MIN_SPACE,
    MAX_TORRENT_BASE64_LENGTH,
    SUBMISSION_FAILED_MESSAGE,
    Aria2BackendAdapter,
    _get_backend,
    _impl_create_task,
    _impl_create_torrent_task,
    _impl_preview_torrent_task,
    _TolerantBackend,
    _validate_options,  # noqa: F401  # 保留 task_service 私有 patch 兼容路径
    check_disk_space,
    check_torrent_network_safety,
    check_url_safety,
    create_task,
    create_torrent_task,
    extract_info_hash_from_magnet,
    get_max_task_size,
    get_uri_hash,
    get_usage,
    http_resource_identity,
    is_http_url,
    is_magnet_link,
    mask_url_credentials,
    parse_torrent_or_error,
    preview_torrent_task,
    probe_url_with_get_fallback,
    raise_register_error,
    register,
    register_and_submit,
    set_task_backend_override,
    source_request_options,
    submit_tid,
    torrent_preview_response,
)
from app.services.task_projection import (
    InvalidTaskStatusFilter,
    build_rest_task_response,
    filter_rows_for_status,
)
from app.services.task_projection_rows import (
    attach_snapshots_to_rows,
    list_user_task_projections,
)

__all__ = ("ERROR_ALREADY_TERMINAL", "ERROR_FORBIDDEN", "ERROR_NOT_FOUND", "MAGNET_MIN_SPACE", "MAX_TORRENT_BASE64_LENGTH", "SUBMISSION_FAILED_MESSAGE", "Any", "Aria2BackendAdapter", "BadGatewayError", "BadRequestError", "ConflictError", "DomainError", "InvalidTaskStatusFilter", "NotFoundError", "UnrefError", "attach_snapshots_to_rows", "build_rest_task_response", "bulk_cancel_tasks", "cancel_task", "check_disk_space", "check_torrent_network_safety", "check_url_safety", "clear_history", "clear_terminal_user_tasks", "create_task", "create_task_response", "create_tasks_batch", "create_torrent_task", "extract_info_hash_from_magnet", "filter_rows_for_status", "get_max_task_size", "get_min_free_disk", "get_uri_hash", "get_usage", "get_user_task_by_id", "http_resource_identity", "is_http_url", "is_magnet_link", "list_task_response", "list_tasks", "list_tasks_page", "list_user_task_projections", "list_user_tasks_page", "logger", "logging", "mask_url_credentials", "parse_torrent_or_error", "preview_torrent_task", "probe_url_with_get_fallback", "raise_register_error", "register", "register_and_submit", "set_task_backend_override", "source_request_options", "submit_tid", "torrent_preview_response", "unref")


# orchestration 公共 create/preview 转发到 via 函数，可替换依赖经本模块 patch 注入点查找。
async def _create_task_via(
    *,
    user_id: int,
    quota_bytes: int,
    uri: str,
    options: dict | None,
) -> dict:
    return await _impl_create_task(
        user_id=user_id, quota_bytes=quota_bytes, uri=uri, options=options
    )

async def _preview_torrent_task_via(*, user_id: int, torrent: str) -> dict:
    return await _impl_preview_torrent_task(user_id=user_id, torrent=torrent)
async def _create_torrent_task_via(
    *,
    user_id: int,
    quota_bytes: int,
    torrent: str,
    selected_file_indexes: Sequence[object] | None,
    options: dict | None,
) -> dict:
    return await _impl_create_torrent_task(
        user_id=user_id,
        quota_bytes=quota_bytes,
        torrent=torrent,
        selected_file_indexes=selected_file_indexes,
        options=options,
    )

logger = logging.getLogger(__name__)

# 以下为 HTTP 端点适配层的 client 注入点：既有测试 patch
# ``app.services.task_service._get_client``。
async def create_tasks_batch(**kwargs: Any) -> Any:
    """批量创建薄 wrapper（M24）：复用 _get_client 注入 aria2 client。"""
    from app.services.task_batch_submission import batch_create_tasks
    return await batch_create_tasks(client=_get_client(), **kwargs)


def _get_client() -> Any:
    from app.services.task_orchestration import _default_client

    return _default_client()


def create_task_response(
    *,
    task_row: dict,
    global_download: dict | None,
    fallback_uri: str,
    fallback_name: str | None,
    fallback_total_length: int,
) -> dict:
    row = {
        **task_row,
        "source_uri": (global_download.get("source_uri") if global_download else None)
        or fallback_uri,
        "global_display_name": (
            global_download.get("display_name") if global_download else None
        )
        or fallback_name,
        "global_status": global_download.get("status") if global_download else None,
        "total_bytes": (
            global_download.get("total_bytes") if global_download else None
        )
        or fallback_total_length,
        "completed_bytes": (
            global_download.get("completed_bytes") if global_download else None
        )
        or 0,
        "global_error_message": (
            global_download.get("error_message") if global_download else None
        ),
    }
    return build_rest_task_response(row)


def list_task_response(row: dict, live: dict | None = None) -> dict:
    return build_rest_task_response(row, live)


async def list_tasks(
    *,
    user_id: int,
    status_filter: str | None,
) -> list[dict]:
    rows = await list_user_task_projections(user_id)
    if status_filter is not None:
        try:
            rows = filter_rows_for_status(rows, status_filter)
        except InvalidTaskStatusFilter as exc:
            raise BadRequestError(
                f"Unsupported status_filter: {exc.args[0]}"
            ) from exc

    logger.debug(
        "查询任务列表 user_id=%s status_filter=%s count=%s",
        user_id,
        status_filter,
        len(rows),
    )

    return [list_task_response(row) for row in rows]


async def list_tasks_page(
    *,
    user_id: int,
    status_filter: str | None,
    page: int,
    page_size: int,
) -> dict:
    try:
        rows, total = await list_user_tasks_page(
            user_id,
            page=page,
            page_size=page_size,
            status_filter=status_filter,
        )
    except ValueError as exc:
        raise BadRequestError(f"Unsupported status_filter: {exc.args[0]}") from exc
    rows = await attach_snapshots_to_rows(rows)
    return {
        "items": [list_task_response(row) for row in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


async def cancel_task(
    *,
    user_id: int,
    user_task_id: int,
    quota_bytes: int,
    tolerate_backend_failure: bool = False,
) -> dict:
    _ = quota_bytes  # budget release is handled inside unref's claim path
    # Legacy v0 submissions bind the aria2 gid up front; fencing on it keeps
    # the terminal CAS consistent with the old lifecycle claim path.
    # include_pending_user 允许 deletion_cleanup 取消待删除用户的任务。
    existing = await get_user_task_by_id(
        user_id, user_task_id, include_pending_user=True
    )
    expected_gid = str(existing["aria2_gid"]) if existing and existing.get("aria2_gid") else None
    backend = _get_backend()
    if tolerate_backend_failure:
        # 清理路径（如用户持久删除）要求终态化不被 backend RPC 失败阻塞；
        # 残余 aria2 清理由 fencing/启动修复兜底。
        backend = _TolerantBackend(backend)
    try:
        await unref(
            user_id=user_id,
            pid=user_task_id,
            backend=backend,
            expected_gid=expected_gid,
        )
    except UnrefError as exc:
        logger.warning(
            "取消任务失败 user_id=%s task_id=%s reason=%s",
            user_id,
            user_task_id,
            exc.code,
        )
        if exc.code == ERROR_NOT_FOUND:
            raise NotFoundError("任务不存在") from exc
        if exc.code == ERROR_FORBIDDEN:
            raise NotFoundError("任务不存在") from exc
        if exc.code == ERROR_ALREADY_TERMINAL:
            raise ConflictError(str(exc)) from exc
        raise BadGatewayError("取消下载任务失败") from exc
    except Exception as exc:
        logger.warning("取消任务失败 user_id=%s task_id=%s error=%s", user_id, user_task_id, exc)
        raise BadGatewayError("取消下载任务失败") from exc

    logger.info("取消任务成功 user_id=%s task_id=%s", user_id, user_task_id)
    return {"ok": True}


async def bulk_cancel_tasks(*, user_id: int, task_ids: list[int], quota_bytes: int) -> dict:
    """批量取消：逐条复用 cancel_task（记录保留、终态化，不触发文件删除）。"""
    results: list[dict] = []
    for task_id in dict.fromkeys(task_ids):
        try:
            await cancel_task(
                user_id=user_id,
                user_task_id=task_id,
                quota_bytes=quota_bytes,
                tolerate_backend_failure=False,
            )
            results.append({"task_id": task_id, "ok": True, "state": "cancelled", "accepted": True, "error": None})
        except DomainError as exc:
            results.append({"task_id": task_id, "ok": False, "state": "failed", "accepted": False, "error": exc.detail})
        except Exception:
            results.append({"task_id": task_id, "ok": False, "state": "failed", "accepted": False, "error": "取消下载任务失败"})
            logger.exception("批量取消任务失败 user_id=%s task_id=%s", user_id, task_id)
    accepted_count = sum(1 for item in results if item["ok"])
    logger.info(
        "批量取消任务完成 user_id=%s requested=%s accepted=%s failed=%s",
        user_id, len(task_ids), accepted_count, len(results) - accepted_count,
    )
    return {"accepted_count": accepted_count, "failed_count": len(results) - accepted_count, "results": results}


async def clear_history(user_id: int) -> dict:
    tids = await clear_terminal_user_tasks(user_id)
    from app.services.history_retention import reclaim_zero_pid_tid

    for tid in set(tids):
        await reclaim_zero_pid_tid(tid)
    count = len(tids)
    logger.info("清空任务记录成功 user_id=%s count=%s", user_id, count)
    return {"ok": True, "count": count}
