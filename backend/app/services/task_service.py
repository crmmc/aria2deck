from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from app.core.config import settings
from app.core.security import check_url_ssrf, mask_url_credentials
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
    get_global_by_resource_key,
    list_user_tasks,
)
from app.services.download_service import (
    cancel_user_task,
    create_user_download,
    create_user_torrent_download,
)
from app.services.hash import (
    extract_info_hash_from_magnet,
    get_uri_hash,
    is_http_url,
    is_magnet_link,
)
from app.services.http_probe import probe_url_with_get_fallback
from app.services.settings_service import get_max_task_size, get_min_free_disk
from app.services.task_projection import (
    InvalidTaskStatusFilter,
    build_rest_task_response,
    filter_rows_for_status,
)
from app.services.task_runtime import fetch_active_live_statuses_by_gid
from app.domain.torrent_metadata import (
    MAX_TORRENT_FILE_COUNT,
    TorrentMetadata,
    TorrentMetadataError,
    build_select_file_option,
    build_selection_resource_key,
    parse_torrent_base64,
    selected_total_size,
    validate_selected_indexes,
)
from app.services.usage_service import get_usage

logger = logging.getLogger(__name__)

MAGNET_MIN_SPACE = 1 * 1024 * 1024
MAX_TORRENT_BASE64_LENGTH = 14 * 1024 * 1024


async def check_url_safety(url: str) -> None:
    error = await check_url_ssrf(url)
    if error:
        raise BadRequestError(error)


def has_url_credentials(url: str) -> bool:
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https", "ftp"}:
        return False
    return parsed.username is not None or parsed.password is not None


def check_disk_space() -> tuple[bool, int]:
    download_path = Path(settings.download_dir)
    download_path.mkdir(parents=True, exist_ok=True)
    disk = shutil.disk_usage(download_path)
    min_free = get_min_free_disk()
    return disk.free > min_free, disk.free


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


def parse_torrent_or_error(torrent: str) -> TorrentMetadata:
    try:
        return parse_torrent_base64(torrent)
    except TorrentMetadataError as exc:
        raise BadRequestError(f"无效的种子文件: {exc}") from exc


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


async def create_task(
    *,
    user_id: int,
    quota_bytes: int,
    uri: str,
    options: dict | None,
) -> dict:
    await check_url_safety(uri)
    if has_url_credentials(uri):
        raise BadRequestError("下载链接不支持用户名或密码")

    disk_ok, disk_free = check_disk_space()
    if not disk_ok:
        logger.warning("创建任务失败 user_id=%s reason=disk_insufficient free=%s", user_id, disk_free)
        raise ForbiddenError(f"磁盘空间不足，剩余 {disk_free / 1024 / 1024 / 1024:.2f} GB")

    usage_info = await get_usage(user_id, quota_bytes)
    available_space = min(usage_info["available_bytes"], disk_free)

    masked_uri = mask_url_credentials(uri)
    uri_hash: str | None = None
    name: str | None = None
    total_length: int = 0

    if is_magnet_link(uri):
        uri_hash = extract_info_hash_from_magnet(uri)
        if not uri_hash:
            logger.warning("创建任务失败 user_id=%s reason=invalid_magnet", user_id)
            raise BadRequestError("无效的磁力链接")
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
        uri_hash = get_uri_hash(final_url)
        name = probe_result.filename
        total_length = probe_result.content_length or 0

        if total_length > 0:
            max_task_size = get_max_task_size()
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

    try:
        task_row = await create_user_download(
            user_id=user_id,
            quota_bytes=quota_bytes,
            uri=masked_uri,
            resource_key=uri_hash,
            resource_kind=(
                "magnet"
                if is_magnet_link(uri)
                else "http"
                if is_http_url(uri)
                else "other"
            ),
            display_name=name,
            total_bytes=total_length,
            aria2_client=_get_client(),
            options=options,
        )
    except ValueError as exc:
        if str(exc) == "quota exceeded":
            raise ForbiddenError("空间不足") from exc
        raise BadRequestError(str(exc)) from exc
    except LookupError as exc:
        raise ConflictError("任务状态已变化，请重试") from exc
    except Exception as exc:
        logger.warning("添加下载任务失败 user_id=%s error=%s", user_id, exc)
        raise BadGatewayError("添加下载任务失败") from exc

    global_download = await get_global_by_resource_key(uri_hash)
    return create_task_response(
        task_row=task_row,
        global_download=global_download,
        fallback_uri=masked_uri,
        fallback_name=name,
        fallback_total_length=total_length,
    )


async def preview_torrent_task(*, user_id: int, torrent: str) -> dict:
    if len(torrent) > MAX_TORRENT_BASE64_LENGTH:
        logger.warning("预览种子任务失败 user_id=%s reason=torrent_too_large", user_id)
        raise PayloadTooLargeError("种子文件过大，最大支持 10MB")

    metadata = parse_torrent_or_error(torrent)
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

    disk_ok, disk_free = check_disk_space()
    if not disk_ok:
        logger.warning("创建种子任务失败 user_id=%s reason=disk_insufficient free=%s", user_id, disk_free)
        raise ForbiddenError(f"磁盘空间不足，剩余 {disk_free / 1024 / 1024 / 1024:.2f} GB")

    metadata = parse_torrent_or_error(torrent)
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
    server_options = {"select-file": select_file} if select_file else None
    magnet_uri = f"magnet:?xt=urn:btih:{uri_hash}"
    try:
        task_row = await create_user_torrent_download(
            user_id=user_id,
            quota_bytes=int(quota_bytes),
            torrent_data=torrent,
            resource_key=resource_key,
            source_uri=magnet_uri,
            display_name=metadata.name,
            total_bytes=selected_size,
            aria2_client=_get_client(),
            options=options,
            server_options=server_options,
        )
    except ValueError as exc:
        if str(exc) == "quota exceeded":
            raise ForbiddenError("空间不足") from exc
        raise BadRequestError(str(exc)) from exc
    except LookupError as exc:
        raise ConflictError("任务状态已变化，请重试") from exc
    except Exception as exc:
        logger.warning("添加种子任务失败 user_id=%s error=%s", user_id, exc)
        raise BadGatewayError("添加下载任务失败") from exc

    global_download = await get_global_by_resource_key(resource_key)
    return create_task_response(
        task_row=task_row,
        global_download=global_download,
        fallback_uri=magnet_uri,
        fallback_name=metadata.name,
        fallback_total_length=selected_size,
    )


async def list_tasks(
    *,
    user_id: int,
    status_filter: str | None,
) -> list[dict]:
    rows = await list_user_tasks(user_id)
    try:
        rows = filter_rows_for_status(rows, status_filter)
    except InvalidTaskStatusFilter as exc:
        raise BadRequestError(f"Unsupported status_filter: {exc.args[0]}") from exc
    live_by_gid = await fetch_active_live_statuses_by_gid(rows, _get_client(), logger)

    logger.debug(
        "查询任务列表 user_id=%s status_filter=%s count=%s",
        user_id,
        status_filter,
        len(rows),
    )

    return [
        list_task_response(row, live_by_gid.get(str(row.get("aria2_gid") or "")))
        for row in rows
    ]


async def cancel_task(
    *,
    user_id: int,
    user_task_id: int,
    quota_bytes: int,
) -> dict:
    try:
        await cancel_user_task(
            user_id=user_id,
            user_task_id=user_task_id,
            quota_bytes=quota_bytes,
            aria2_client=_get_client(),
        )
    except LookupError as exc:
        logger.warning(
            "取消任务失败 user_id=%s task_id=%s reason=not_found",
            user_id,
            user_task_id,
        )
        raise NotFoundError("任务不存在") from exc
    except Exception as exc:
        logger.warning("取消任务失败 user_id=%s task_id=%s error=%s", user_id, user_task_id, exc)
        raise BadGatewayError("取消下载任务失败") from exc

    logger.info("取消任务成功 user_id=%s task_id=%s", user_id, user_task_id)
    return {"ok": True}


async def clear_history(user_id: int) -> dict:
    count = await clear_terminal_user_tasks(user_id)
    logger.info("清空任务记录成功 user_id=%s count=%s", user_id, count)
    return {"ok": True, "count": count}
