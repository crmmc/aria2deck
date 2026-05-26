"""任务管理接口模块（共享下载架构）

提供任务的添加、查询、取消等功能。
实现共享下载：多用户可订阅同一下载任务。
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from app.aria2.client import Aria2Client
from app.auth import AuthUser, require_user
from app.core.config import settings
from app.core.request_rate_guard import RateLimitScope, ensure_authenticated_allowed
from app.core.security import check_url_ssrf, mask_url_credentials
from app.core.state import AppState, get_aria2_client
from app.repositories.downloads import (
    clear_terminal_user_tasks,
    get_global_by_resource_key,
    list_user_tasks_for_download,
    list_user_tasks,
)
from app.routers.config import get_max_task_size, get_min_free_disk
from app.services.hash import (
    extract_info_hash_from_magnet,
    get_uri_hash,
    is_http_url,
    is_magnet_link,
)
from app.services.http_probe import probe_url_with_get_fallback
from app.services.task_projection import (
    InvalidTaskStatusFilter,
    build_rest_task_response,
    filter_rows_for_status,
)
from app.services.task_runtime import (
    fetch_active_live_statuses_by_gid,
    fetch_cached_live_status_for_row,
)
from app.services.download_service import (
    cancel_user_task,
    create_user_download,
    create_user_torrent_download,
)
from app.services.torrent_metadata import (
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

router = APIRouter(prefix="/api/tasks", tags=["tasks"])

# Minimum space required for magnet links (1MB)
MAGNET_MIN_SPACE = 1 * 1024 * 1024


async def _check_url_safety(url: str) -> None:
    """检查 URL 是否安全（SSRF 防护），不安全时抛出 HTTPException"""
    error = await check_url_ssrf(url)
    if error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=error)


def _has_url_credentials(url: str) -> bool:
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https", "ftp"}:
        return False
    return parsed.username is not None or parsed.password is not None


def _v0_create_task_response(
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


def _v0_list_task_response(row: dict, live: dict | None = None) -> dict:
    return build_rest_task_response(row, live)


# ========== Schemas ==========


class TaskCreate(BaseModel):
    """创建任务请求体"""

    uri: str
    options: dict | None = None


class TorrentCreate(BaseModel):
    """上传种子请求体"""

    torrent: str  # Base64 encoded
    selected_file_indexes: list[object] | None = None
    options: dict | None = None


class TorrentPreviewCreate(BaseModel):
    """预览种子请求体"""

    torrent: str


# ========== Helpers ==========


def _get_client(request: Request) -> Aria2Client:
    return get_aria2_client(request)


def _check_disk_space() -> tuple[bool, int]:
    """检查磁盘空间是否足够"""
    download_path = Path(settings.download_dir)
    download_path.mkdir(parents=True, exist_ok=True)
    disk = shutil.disk_usage(download_path)
    min_free = get_min_free_disk()
    return disk.free > min_free, disk.free


def _torrent_preview_response(metadata: TorrentMetadata) -> dict:
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


def _parse_torrent_or_400(torrent: str) -> TorrentMetadata:
    try:
        return parse_torrent_base64(torrent)
    except TorrentMetadataError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"无效的种子文件: {exc}",
        ) from exc


# ========== API Endpoints ==========


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_task(
    payload: TaskCreate,
    request: Request,
    user: AuthUser = Depends(require_user),
) -> dict:
    """创建新下载任务

    支持：
    - HTTP(S) URL：预检查获取大小后创建
    - 磁力链接：可用空间 > 1MB 时允许添加

    返回用户的订阅信息。
    """
    user_id = user.id
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="用户未登录"
        )

    # Rate limit
    try:
        await ensure_authenticated_allowed(
            user_id,
            RateLimitScope.CREATE_TASK,
            detail="操作过于频繁，请稍后再试",
        )
    except HTTPException:
        logger.warning("创建任务被限流 user_id=%s", user.id)
        raise

    # SSRF protection
    await _check_url_safety(payload.uri)
    if _has_url_credentials(payload.uri):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="下载链接不支持用户名或密码",
        )

    # Check disk space
    disk_ok, disk_free = _check_disk_space()
    if not disk_ok:
        logger.warning(
            "创建任务失败 user_id=%s reason=disk_insufficient free=%s",
            user.id,
            disk_free,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"磁盘空间不足，剩余 {disk_free / 1024 / 1024 / 1024:.2f} GB",
        )

    # Get user space info
    usage_info = await get_usage(user_id, user.quota)
    available_space = min(usage_info["available_bytes"], disk_free)

    # Determine URI type and get uri_hash
    uri = payload.uri
    masked_uri = mask_url_credentials(uri)
    uri_hash: str | None = None
    name: str | None = None
    total_length: int = 0

    if is_magnet_link(uri):
        # Magnet link: extract info_hash
        uri_hash = extract_info_hash_from_magnet(uri)
        if not uri_hash:
            logger.warning("创建任务失败 user_id=%s reason=invalid_magnet", user.id)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="无效的磁力链接"
            )

        # Check minimum space for magnet
        if available_space < MAGNET_MIN_SPACE:
            logger.warning(
                "创建任务失败 user_id=%s reason=space_low_for_magnet available=%s",
                user.id,
                available_space,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="可用空间不足，无法添加磁力链接",
            )

    elif is_http_url(uri):
        # HTTP(S): probe to get size and final URL
        probe_result = await probe_url_with_get_fallback(uri)

        if not probe_result.success:
            logger.warning(
                "创建任务失败 user_id=%s reason=probe_failed error=%s",
                user.id,
                probe_result.error,
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"无法访问下载链接: {probe_result.error}",
            )

        # Use final URL for hash (after redirects)
        final_url = probe_result.final_url or uri
        uri_hash = get_uri_hash(final_url)
        name = probe_result.filename
        total_length = probe_result.content_length or 0

        # Check size limits
        if total_length > 0:
            max_task_size = get_max_task_size()
            if total_length > max_task_size:
                logger.warning(
                    "创建任务失败 user_id=%s reason=task_too_large size=%s limit=%s",
                    user.id,
                    total_length,
                    max_task_size,
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"文件大小 {total_length / 1024**3:.2f} GB 超过系统限制 {max_task_size / 1024**3:.2f} GB",
                )

            if total_length > available_space:
                logger.warning(
                    "创建任务失败 user_id=%s reason=user_space_insufficient size=%s available=%s",
                    user.id,
                    total_length,
                    available_space,
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"文件大小 {total_length / 1024**3:.2f} GB 超过可用空间 {available_space / 1024**3:.2f} GB",
                )

    else:
        # Other protocols (ftp, etc.)
        uri_hash = get_uri_hash(uri)

    if not uri_hash:
        logger.warning("创建任务失败 user_id=%s reason=unsupported_uri_type", user.id)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="无法识别的下载链接类型"
        )

    try:
        task_row = await create_user_download(
            user_id=user_id,
            quota_bytes=user.quota,
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
            aria2_client=_get_client(request),
            options=payload.options,
        )
    except ValueError as exc:
        if str(exc) == "quota exceeded":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="空间不足",
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="任务状态已变化，请重试",
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("添加下载任务失败 user_id=%s error=%s", user.id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="添加下载任务失败",
        ) from exc

    global_download = await get_global_by_resource_key(uri_hash)
    return _v0_create_task_response(
        task_row=task_row,
        global_download=global_download,
        fallback_uri=masked_uri,
        fallback_name=name,
        fallback_total_length=total_length,
    )


@router.post("/torrent/preview")
async def preview_torrent_task(
    payload: TorrentPreviewCreate,
    user: AuthUser = Depends(require_user),
) -> dict:
    """解析种子文件并返回可选择的文件列表，不创建任务。"""
    user_id = user.id
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="用户未登录"
        )

    try:
        await ensure_authenticated_allowed(
            user_id,
            RateLimitScope.CREATE_TORRENT,
            detail="操作过于频繁，请稍后再试",
        )
    except HTTPException:
        logger.warning("预览种子任务被限流 user_id=%s", user.id)
        raise

    max_base64_length = 14 * 1024 * 1024
    if len(payload.torrent) > max_base64_length:
        logger.warning("预览种子任务失败 user_id=%s reason=torrent_too_large", user.id)
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="种子文件过大，最大支持 10MB",
        )

    metadata = _parse_torrent_or_400(payload.torrent)
    return _torrent_preview_response(metadata)


@router.post("/torrent", status_code=status.HTTP_201_CREATED)
async def create_torrent_task(
    payload: TorrentCreate,
    request: Request,
    user: AuthUser = Depends(require_user),
) -> dict:
    """通过种子文件创建下载任务"""
    user_id = user.id
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="用户未登录"
        )

    # Rate limit
    try:
        await ensure_authenticated_allowed(
            user_id,
            RateLimitScope.CREATE_TORRENT,
            detail="操作过于频繁，请稍后再试",
        )
    except HTTPException:
        logger.warning("创建种子任务被限流 user_id=%s", user.id)
        raise

    # Size limit
    max_base64_length = 14 * 1024 * 1024
    if len(payload.torrent) > max_base64_length:
        logger.warning("创建种子任务失败 user_id=%s reason=torrent_too_large", user.id)
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="种子文件过大，最大支持 10MB",
        )

    # Check disk space
    disk_ok, disk_free = _check_disk_space()
    if not disk_ok:
        logger.warning(
            "创建种子任务失败 user_id=%s reason=disk_insufficient free=%s",
            user.id,
            disk_free,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"磁盘空间不足，剩余 {disk_free / 1024 / 1024 / 1024:.2f} GB",
        )

    metadata = _parse_torrent_or_400(payload.torrent)
    uri_hash = metadata.info_hash

    # Get user space info
    usage_info = await get_usage(user_id, user.quota_bytes)
    available_space = min(usage_info["available_bytes"], disk_free)

    try:
        selected_indexes = validate_selected_indexes(
            metadata, payload.selected_file_indexes
        )
    except TorrentMetadataError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    selected_size = selected_total_size(metadata, selected_indexes)

    if selected_size <= 0:
        if available_space < MAGNET_MIN_SPACE:
            logger.warning(
                "创建种子任务失败 user_id=%s reason=space_low_for_torrent available=%s",
                user.id,
                available_space,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="可用空间不足"
            )
    elif selected_size > available_space:
        logger.warning(
            "创建种子任务失败 user_id=%s reason=user_space_insufficient size=%s available=%s",
            user.id,
            selected_size,
            available_space,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"文件大小 {selected_size / 1024**3:.2f} GB 超过可用空间 {available_space / 1024**3:.2f} GB",
        )

    max_task_size = get_max_task_size()
    if selected_size > 0 and selected_size > max_task_size:
        logger.warning(
            "创建种子任务失败 user_id=%s reason=task_too_large size=%s limit=%s",
            user.id,
            selected_size,
            max_task_size,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"文件大小 {selected_size / 1024**3:.2f} GB 超过系统限制 {max_task_size / 1024**3:.2f} GB",
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
            quota_bytes=int(user.quota_bytes),
            torrent_data=payload.torrent,
            resource_key=resource_key,
            source_uri=magnet_uri,
            display_name=metadata.name,
            total_bytes=selected_size,
            aria2_client=_get_client(request),
            options=payload.options,
            server_options=server_options,
        )
    except ValueError as exc:
        if str(exc) == "quota exceeded":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="空间不足",
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="任务状态已变化，请重试",
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("添加种子任务失败 user_id=%s error=%s", user.id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="添加下载任务失败",
        ) from exc

    global_download = await get_global_by_resource_key(resource_key)
    return _v0_create_task_response(
        task_row=task_row,
        global_download=global_download,
        fallback_uri=magnet_uri,
        fallback_name=metadata.name,
        fallback_total_length=selected_size,
    )


@router.get("")
async def list_tasks(
    request: Request,
    status_filter: str | None = None,
    user: AuthUser = Depends(require_user),
) -> list[dict]:
    """获取当前用户的任务订阅列表"""
    rows = await list_user_tasks(user.id)
    try:
        rows = filter_rows_for_status(rows, status_filter)
    except InvalidTaskStatusFilter as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported status_filter: {exc.args[0]}",
        ) from exc
    live_by_gid = await fetch_active_live_statuses_by_gid(
        rows,
        _get_client(request),
        logger,
    )

    logger.debug(
        "查询任务列表 user_id=%s status_filter=%s count=%s",
        user.id,
        status_filter,
        len(rows),
    )

    return [
        _v0_list_task_response(row, live_by_gid.get(str(row.get("aria2_gid") or "")))
        for row in rows
    ]


@router.delete("/{subscription_id}")
async def cancel_task(
    subscription_id: int,
    request: Request,
    user: AuthUser = Depends(require_user),
) -> dict:
    """取消当前用户的 v0 下载任务。"""
    try:
        await cancel_user_task(
            user_id=user.id,
            user_task_id=subscription_id,
            quota_bytes=int(user.quota_bytes),
            aria2_client=_get_client(request),
        )
    except LookupError:
        logger.warning(
            "取消任务失败 user_id=%s task_id=%s reason=not_found",
            user.id,
            subscription_id,
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="任务不存在",
        )
    except Exception as exc:
        logger.warning(
            "取消任务失败 user_id=%s task_id=%s error=%s", user.id, subscription_id, exc
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="取消下载任务失败",
        ) from exc

    logger.info("取消任务成功 user_id=%s task_id=%s", user.id, subscription_id)
    return {"ok": True}


@router.delete("")
async def clear_history(user: AuthUser = Depends(require_user)) -> dict:
    """清空当前用户的已完成/失败任务订阅"""
    count = await clear_terminal_user_tasks(user.id)

    logger.info("清空任务记录成功 user_id=%s count=%s", user.id, count)

    return {"ok": True, "count": count}


# ========== Broadcast Helpers ==========


async def _broadcast_task_update(state: AppState, task_id: int) -> None:
    """Broadcast a v0 global download update to all subscribers.

    Handles connection failures gracefully.
    """
    from app.aria2.sync import unregister_ws

    rows = await list_user_tasks_for_download(task_id)
    client = get_aria2_client(state=state)
    live_by_gid: dict[str, dict] = {}

    # Broadcast to each subscriber
    for row in rows:
        owner_id = int(row["user_id"])
        live = await fetch_cached_live_status_for_row(
            row,
            client,
            state,
            logger,
            live_by_gid,
        )
        payload = _v0_list_task_response(row, live)

        async with state.lock:
            sockets = list(state.ws_connections.get(owner_id, set()))

        failed_sockets = []
        for ws in sockets:
            try:
                await ws.send_json({"type": "task_update", "task": payload})
            except Exception as e:
                logger.debug("WebSocket send failed for user %s: %s", owner_id, e)
                failed_sockets.append(ws)

        # Clean up failed connections outside the iteration
        for ws in failed_sockets:
            try:
                await unregister_ws(state, owner_id, ws)
            except Exception as e:
                logger.warning(
                    "Failed to unregister websocket for user %s: %s", owner_id, e
                )


async def broadcast_task_update_to_subscribers(state: AppState, task_id: int) -> None:
    """Public function to broadcast task updates (used by listener/sync)"""
    await _broadcast_task_update(state, task_id)
