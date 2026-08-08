from __future__ import annotations

import logging
import shutil
import threading
from pathlib import Path
from typing import Any

from app.modules.backend.port import BackendPort

from app.core.config import get_internal_base_url, settings
from app.core.security import (
    check_torrent_network_endpoints,
    check_url_ssrf,
    mask_url_credentials,
)
from app.aria2.gateway import get_aria2_client
from app.domain.errors import (
    BadGatewayError,
    BadRequestError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    PayloadTooLargeError,
)
from app.repositories.downloads import (
    clear_terminal_user_tasks,
    get_global_download_by_id,
    get_user_task_by_id,
    list_user_tasks_page,
)
from app.services.hash import (
    extract_info_hash_from_magnet,
    get_uri_hash,
    is_http_url,
    is_magnet_link,
)
from app.services.http_probe import probe_url_with_get_fallback
from app.services.internal_fetch import (
    CAPABILITY_HEADER,
    create_capability,
    http_resource_identity,
    source_request_options,
)
from app.services.settings_service import get_max_task_size, get_min_free_disk
from app.services.task_projection import (
    InvalidTaskStatusFilter,
    build_rest_task_response,
    filter_rows_for_status,
)
from app.services.task_projection_rows import (
    attach_snapshots_to_rows,
    list_user_task_projections,
)
from app.domain.torrent_metadata import (
    MAX_TORRENT_FILE_COUNT,
    TorrentMetadata,
    TorrentMetadataError,
    build_select_file_option,
    build_selection_resource_key,
    parse_torrent_base64_async,
    selected_total_size,
    validate_selected_indexes,
)
from app.modules.backend.aria2_adapter import Aria2BackendAdapter
from app.modules.task_core.register import (
    RegisterError,
    ResourceSpec,
    register,
)
from app.modules.task_core.submit import submit_tid
from app.modules.task_core.states import ERROR_QUOTA_EXCEEDED
from app.modules.task_core.unref import (
    ERROR_ALREADY_TERMINAL,
    ERROR_FORBIDDEN,
    ERROR_NOT_FOUND,
    UnrefError,
    unref,
)
from app.services.usage_service import get_usage

logger = logging.getLogger(__name__)

# 提交失败对外统一文案：RPC 层错误 message + 用户任务 error_message。
SUBMISSION_FAILED_MESSAGE = "添加下载任务失败"

MAGNET_MIN_SPACE = 1 * 1024 * 1024
MAX_TORRENT_BASE64_LENGTH = 14 * 1024 * 1024


async def check_url_safety(url: str) -> None:
    error = await check_url_ssrf(url)
    if error:
        raise BadRequestError(error)


def check_disk_space() -> tuple[bool, int]:
    download_path = Path(settings.download_dir)
    download_path.mkdir(parents=True, exist_ok=True)
    disk = shutil.disk_usage(download_path)
    min_free = get_min_free_disk()
    return disk.free > min_free, disk.free


def _validate_options(options: dict | None) -> None:
    if not options:
        return
    if "bt-tracker" in options:
        raise BadRequestError("bt-tracker option is not allowed")
    if "out" in options:
        out = str(options["out"])
        if not out or out in {".", ".."} or "/" in out or "\\" in out:
            raise BadRequestError(
                "invalid out option: must be a filename without path separators"
            )


def torrent_preview_response(metadata: TorrentMetadata) -> dict:
    return {
        "info_hash": metadata.info_hash,
        "name": metadata.name,
        "file_count": metadata.file_count,
        "total_size": metadata.total_size,
        "files": [
            {
                "index": file.index,
                "path": list(file.path),
                "size": file.size,
            }
            for file in metadata.files
        ],
        "tree": metadata.tree,
        "limits": {"max_files": MAX_TORRENT_FILE_COUNT},
        "default_selection": "all",
    }


async def parse_torrent_or_error(torrent: str) -> TorrentMetadata:
    try:
        return await parse_torrent_base64_async(torrent)
    except TorrentMetadataError as exc:
        raise BadRequestError(f"无效的种子文件: {exc}") from exc


async def check_torrent_network_safety(metadata: TorrentMetadata) -> None:
    error = await check_torrent_network_endpoints(
        metadata.tracker_urls,
        metadata.webseed_urls,
    )
    if error:
        raise BadRequestError(error)


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


def _get_client() -> Any:
    return get_aria2_client()


# aria2deck 唯一部署的 backend 是 aria2；该 override 只用于注入测试 fake，
# 让 RPC handler 的 register_and_submit 与集成测试的 aria2 client 一致。
_backend_override: BackendPort | None = None
_backend_override_lock = threading.Lock()


def set_task_backend_override(backend: BackendPort | None) -> None:
    global _backend_override
    with _backend_override_lock:
        _backend_override = backend


def _get_backend() -> BackendPort:
    with _backend_override_lock:
        override = _backend_override
    if override is not None:
        return override
    return Aria2BackendAdapter(_get_client())


class _TolerantBackend:
    """BackendPort wrapper that swallows ``remove`` failures.

    Used by cleanup paths (e.g. pending-delete user cleanup) where the pid
    and tid have already been terminalized in DB and a failing backend RPC
    must not roll the flow into a retry loop.
    """

    def __init__(self, inner: BackendPort) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def remove(self, tid: int) -> None:
        try:
            await self._inner.remove(tid)
        except Exception:
            logger.warning(
                "backend remove 失败，残余清理交由后续兜底 tid=%s",
                tid,
                exc_info=True,
            )


def raise_register_error(exc: RegisterError) -> None:
    """Map a Task Core register failure to the public REST error surface."""
    if exc.code == "duplicate_task":
        raise ConflictError(str(exc)) from exc
    if exc.code == ERROR_QUOTA_EXCEEDED:
        raise ForbiddenError(str(exc)) from exc
    if exc.code == "stale":
        raise ConflictError("任务状态已变化，请重试") from exc
    raise ConflictError(str(exc)) from exc


async def register_and_submit(
    *,
    user_id: int,
    quota_bytes: int,
    resource: ResourceSpec,
    options: dict | None = None,
) -> dict:
    """New Task Core entry: register admission, then submit the tid.

    Only DB admission is guaranteed; submit failures cancel the pid via
    ``unref`` and surface a BadGatewayError. Returns the standard REST
    task payload so callers stay response-compatible.
    """
    backend = _get_backend()
    try:
        result = await register(
            user_id=user_id,
            quota_bytes=quota_bytes,
            resource=resource,
        )
    except RegisterError as exc:
        raise_register_error(exc)

    submit_options = dict(options or {})
    if result.outcome == "created":
        try:
            gid = await submit_tid(
                backend=backend,
                tid=result.tid,
                options=submit_options,
            )
        except Exception as exc:
            logger.warning(
                "提交下载任务失败 user_id=%s tid=%s error_type=%s",
                user_id,
                result.tid,
                type(exc).__name__,
            )
            try:
                await unref(
                    user_id=user_id,
                    pid=result.pid,
                    backend=backend,
                    error_message=SUBMISSION_FAILED_MESSAGE,
                )
            except Exception:
                logger.warning(
                    "提交失败后回滚任务失败 user_id=%s pid=%s",
                    user_id,
                    result.pid,
                )
            raise BadGatewayError(SUBMISSION_FAILED_MESSAGE) from exc
        if gid is None:
            logger.warning(
                "提交下载任务失败 user_id=%s tid=%s reason=no_gid",
                user_id,
                result.tid,
            )
            try:
                await unref(
                    user_id=user_id,
                    pid=result.pid,
                    backend=backend,
                    error_message=SUBMISSION_FAILED_MESSAGE,
                )
            except Exception:
                logger.warning(
                    "提交失败后回滚任务失败 user_id=%s pid=%s",
                    user_id,
                    result.pid,
                )
            raise BadGatewayError(SUBMISSION_FAILED_MESSAGE)

    global_download = await get_global_download_by_id(result.tid)
    if result.outcome == "created" and global_download is not None:
        # register 只写 DB；新 attempt 由本流程提交后，需要把 capability /
        # mirror 等提交上下文补进 capability（含 mirrors 的重新下发）。
        gid_value = global_download.get("aria2_gid")
        if gid_value:
            gid = str(gid_value)
            uris = _resolve_join_submission_uris(
                global_download=global_download,
                resource=resource,
                options=submit_options,
            )
            if uris:
                try:
                    await backend.join_submission(
                        tid=result.tid, gid=gid, uris=uris
                    )
                except Exception:
                    logger.warning(
                        "join submission 失败 user_id=%s tid=%s gid=%s",
                        user_id,
                        result.tid,
                        gid,
                        exc_info=True,
                    )
    task_status = (
        str(global_download.get("status"))
        if global_download is not None
        else result.status
    )
    task_row = {
        "id": result.pid,
        "global_download_id": result.tid,
        "status": task_status,
        "display_name": resource.display_name,
        "reserved_bytes": resource.size_bytes if resource.size_known else 0,
    }
    payload = create_task_response(
        task_row=task_row,
        global_download=global_download,
        fallback_uri=resource.display_uri or resource.source_uri,
        fallback_name=resource.display_name,
        fallback_total_length=resource.size_bytes,
    )
    if resource.display_uri:
        payload["uri"] = resource.display_uri
    return payload


def _resolve_join_submission_uris(
    *,
    global_download: dict,
    resource: ResourceSpec,
    options: dict,
) -> list[str]:
    """返回新 attempt 的 join 下发 URI 列表（空表示无需补发）。

    只有 HTTP 且调用方带 mirror 或 header/auth 的 submission 与 capability
    的默认内容不一致时才补发：直接以 capability 重算 gateway uris。
    """
    if str(global_download.get("resource_kind") or "") != "http":
        return []
    source_uri = str(global_download.get("source_uri") or resource.source_uri)
    if not source_uri:
        return []
    mirrors = list(options.get("mirrors") or [])
    has_auth_or_headers = any(
        key in options for key in ("header", "http-user", "http-passwd")
    )
    if not mirrors and not has_auth_or_headers:
        return []
    # mirror 已由 adapter.submit 写入初次提交的 capability；
    # join_submission 只负责把 gateway uri 重新下发到 gid，capability 保持一致。
    source_opts = source_request_options(options, mirrors=[])
    base = f"{get_internal_base_url()}/_internal/fetch/{global_download['id']}"
    uris = [
        f"{base}/{index}" for index in range(1 + len(source_opts.mirrors))
    ]
    capability = create_capability(
        int(global_download["id"]), source_uri, source_opts
    )
    options["header"] = [f"{CAPABILITY_HEADER}: {capability}"]
    return uris


async def create_task(
    *,
    user_id: int,
    quota_bytes: int,
    uri: str,
    options: dict | None,
) -> dict:
    if not (is_magnet_link(uri) or is_http_url(uri)):
        raise BadRequestError("仅支持磁力链接和 HTTP(S) 下载链接")
    if is_magnet_link(uri):
        info_hash = extract_info_hash_from_magnet(uri)
        if not info_hash:
            raise BadRequestError("无效的磁力链接")
        uri = f"magnet:?xt=urn:btih:{info_hash}"
    await check_url_safety(uri)

    disk_ok, disk_free = check_disk_space()
    if not disk_ok:
        logger.warning("创建任务失败 user_id=%s reason=disk_insufficient free=%s", user_id, disk_free)
        raise ForbiddenError(f"磁盘空间不足，剩余 {disk_free / 1024 / 1024 / 1024:.2f} GB")

    usage_info = await get_usage(user_id, quota_bytes)
    available_space = min(usage_info["available_bytes"], disk_free)

    submission_uri = uri
    uri_hash: str | None = None
    name: str | None = None
    total_length: int = 0
    size_known = False
    max_task_size = get_max_task_size()

    if is_magnet_link(uri):
        uri_hash = extract_info_hash_from_magnet(uri)
        if not uri_hash:
            logger.warning("创建任务失败 user_id=%s reason=invalid_magnet", user_id)
            raise BadRequestError("无效的磁力链接")
        submission_uri = f"magnet:?xt=urn:btih:{uri_hash}"
        if available_space < MAGNET_MIN_SPACE:
            logger.warning(
                "创建任务失败 user_id=%s reason=space_low_for_magnet available=%s",
                user_id,
                available_space,
            )
            raise ForbiddenError("可用空间不足，无法添加磁力链接")

    elif is_http_url(uri):
        probe_result = await probe_url_with_get_fallback(uri)
        if not probe_result.success:
            logger.warning(
                "创建任务失败 user_id=%s reason=probe_failed error=%s",
                user_id,
                probe_result.error,
            )
            raise BadRequestError(f"无法访问下载链接: {probe_result.error}")

        final_url = probe_result.final_url or uri
        await check_url_safety(final_url)
        submission_uri = final_url
        uri_hash = get_uri_hash(final_url)
        name = probe_result.filename
        size_known = probe_result.content_length is not None
        total_length = probe_result.content_length or 0

        if size_known:
            if total_length > max_task_size:
                logger.warning(
                    "创建任务失败 user_id=%s reason=task_too_large size=%s limit=%s",
                    user_id,
                    total_length,
                    max_task_size,
                )
                raise ForbiddenError(
                    f"文件大小 {total_length / 1024**3:.2f} GB 超过系统限制 {max_task_size / 1024**3:.2f} GB"
                )

            if total_length > available_space:
                logger.warning(
                    "创建任务失败 user_id=%s reason=user_space_insufficient size=%s available=%s",
                    user_id,
                    total_length,
                    available_space,
                )
                raise ForbiddenError(
                    f"文件大小 {total_length / 1024**3:.2f} GB 超过可用空间 {available_space / 1024**3:.2f} GB"
                )

    else:
        uri_hash = get_uri_hash(uri)

    if not uri_hash:
        logger.warning("创建任务失败 user_id=%s reason=unsupported_uri_type", user_id)
        raise BadRequestError("无法识别的下载链接类型")

    masked_uri = mask_url_credentials(submission_uri)
    _validate_options(options)

    resource_kind = (
        "magnet"
        if is_magnet_link(submission_uri)
        else "http"
        if is_http_url(submission_uri)
        else "other"
    )
    if resource_kind == "http":
        source_opts = source_request_options(options)
        resource_key = http_resource_identity(uri_hash, source_opts)
    else:
        resource_key = uri_hash

    resource = ResourceSpec(
        resource_key=resource_key,
        source_uri=masked_uri,
        resource_kind=resource_kind,
        display_name=name,
        size_bytes=total_length,
        size_known=size_known,
    )
    return await register_and_submit(
        user_id=user_id,
        quota_bytes=quota_bytes,
        resource=resource,
        options=options,
    )


async def preview_torrent_task(*, user_id: int, torrent: str) -> dict:
    if len(torrent) > MAX_TORRENT_BASE64_LENGTH:
        logger.warning("预览种子任务失败 user_id=%s reason=torrent_too_large", user_id)
        raise PayloadTooLargeError("种子文件过大，最大支持 10MB")

    metadata = await parse_torrent_or_error(torrent)
    await check_torrent_network_safety(metadata)
    return torrent_preview_response(metadata)


async def create_torrent_task(
    *,
    user_id: int,
    quota_bytes: int,
    torrent: str,
    selected_file_indexes: list[object] | None,
    options: dict | None,
) -> dict:
    if len(torrent) > MAX_TORRENT_BASE64_LENGTH:
        logger.warning("创建种子任务失败 user_id=%s reason=torrent_too_large", user_id)
        raise PayloadTooLargeError("种子文件过大，最大支持 10MB")

    metadata = await parse_torrent_or_error(torrent)
    await check_torrent_network_safety(metadata)

    disk_ok, disk_free = check_disk_space()
    if not disk_ok:
        logger.warning("创建种子任务失败 user_id=%s reason=disk_insufficient free=%s", user_id, disk_free)
        raise ForbiddenError(f"磁盘空间不足，剩余 {disk_free / 1024 / 1024 / 1024:.2f} GB")

    uri_hash = metadata.info_hash

    usage_info = await get_usage(user_id, quota_bytes)
    available_space = min(usage_info["available_bytes"], disk_free)

    try:
        selected_indexes = validate_selected_indexes(metadata, selected_file_indexes)
    except TorrentMetadataError as exc:
        raise BadRequestError(str(exc)) from exc

    selected_size = selected_total_size(metadata, selected_indexes)

    if selected_size <= 0:
        if available_space < MAGNET_MIN_SPACE:
            logger.warning(
                "创建种子任务失败 user_id=%s reason=space_low_for_torrent available=%s",
                user_id,
                available_space,
            )
            raise ForbiddenError("可用空间不足")
    elif selected_size > available_space:
        logger.warning(
            "创建种子任务失败 user_id=%s reason=user_space_insufficient size=%s available=%s",
            user_id,
            selected_size,
            available_space,
        )
        raise ForbiddenError(
            f"文件大小 {selected_size / 1024**3:.2f} GB 超过可用空间 {available_space / 1024**3:.2f} GB"
        )

    max_task_size = get_max_task_size()
    if selected_size > 0 and selected_size > max_task_size:
        logger.warning(
            "创建种子任务失败 user_id=%s reason=task_too_large size=%s limit=%s",
            user_id,
            selected_size,
            max_task_size,
        )
        raise ForbiddenError(
            f"文件大小 {selected_size / 1024**3:.2f} GB 超过系统限制 {max_task_size / 1024**3:.2f} GB"
        )

    resource_key = build_selection_resource_key(
        uri_hash,
        selected_indexes,
        total_file_count=metadata.file_count,
    )
    select_file = build_select_file_option(
        selected_indexes,
        total_file_count=metadata.file_count,
    )
    magnet_uri = f"magnet:?xt=urn:btih:{uri_hash}"

    _validate_options(options)
    submit_options = dict(options or {})
    if select_file:
        submit_options["select-file"] = select_file

    resource = ResourceSpec(
        resource_key=resource_key,
        source_uri=f"base64:{torrent}",
        resource_kind="torrent",
        display_name=metadata.name,
        size_bytes=selected_size,
        size_known=True,
        display_uri=magnet_uri,
    )
    return await register_and_submit(
        user_id=user_id,
        quota_bytes=quota_bytes,
        resource=resource,
        options=submit_options,
    )


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
    backend: BackendPort | None = _get_backend()
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


async def clear_history(user_id: int) -> dict:
    count = await clear_terminal_user_tasks(user_id)
    logger.info("清空任务记录成功 user_id=%s count=%s", user_id, count)
    return {"ok": True, "count": count}
