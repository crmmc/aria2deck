"""用户文件管理接口模块（共享下载架构）

提供用户文件的查看、下载、删除、重命名等功能。
基于 UserFile 引用模型，支持 BT 文件夹浏览。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import case, delete, func, insert, select, update
from starlette.responses import StreamingResponse

from app.auth import AuthUser, require_user
from app.core.download_limiter import DownloadLease, download_limiter
from app.core.request_rate_guard import RateLimitScope, ensure_authenticated_allowed
from app.db.engine import transaction
from app.db.schema import (
    global_downloads,
    pack_tasks,
    share_links,
    stored_file_entries,
    stored_files,
    user_tasks,
    user_files,
    user_storage_usage,
)
from app.services.usage_service import get_usage, release_reserved, reserve_bytes

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/files", tags=["files"])
# 打包任务创建锁，防止并发校验导致空间超卖（按事件循环隔离）
_pack_create_lock: asyncio.Lock | None = None
_pack_create_lock_loop: asyncio.AbstractEventLoop | None = None


def _get_pack_create_lock() -> asyncio.Lock:
    global _pack_create_lock, _pack_create_lock_loop
    loop = asyncio.get_running_loop()
    if _pack_create_lock is None or _pack_create_lock_loop is not loop:
        _pack_create_lock = asyncio.Lock()
        _pack_create_lock_loop = loop
    return _pack_create_lock


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def now_ms() -> int:
    return int(time.time() * 1000)


def ms_to_iso(timestamp_ms: int | None) -> str | None:
    if timestamp_ms is None:
        return None
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).isoformat()


PACK_ACTIVE_STATUSES = ("pending", "packing")
PACK_TERMINAL_STATUSES = ("completed", "failed", "cancelled")


def _require_user_id(user: AuthUser) -> int:
    if user.id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未登录")
    return int(user.id)


# ========== Schemas ==========


class FileInfo(BaseModel):
    """文件信息"""

    id: int
    content_hash: str  # 用于 URL 路径，隐藏真实 ID
    name: str
    size: int
    is_directory: bool
    created_at: str


class FileListResponse(BaseModel):
    """文件列表响应"""

    files: list[FileInfo]
    total: int
    space: dict  # {used, frozen, available}


class RenameRequest(BaseModel):
    """重命名请求"""

    name: str = Field(..., min_length=1, max_length=200)


class PackRequest(BaseModel):
    """打包请求 - 基于 UserFile ID"""

    file_ids: list[int] = Field(..., min_length=1, max_length=100)
    output_name: str | None = None
    delete_source: bool = False


class CalculateSizeRequest(BaseModel):
    """计算大小请求 - 基于 UserFile ID"""

    file_ids: list[int] = Field(..., min_length=1, max_length=1000)


# ========== Helpers ==========


def _file_row_to_dict(row: dict[str, Any]) -> dict:
    """Convert v0 UserFile + StoredFile row to API response dict."""
    return {
        "id": row["user_file_id"],
        "content_hash": row["content_hash"],
        "name": row["display_name"],
        "size": row["size_bytes"],
        "is_directory": bool(row["is_directory"]),
        "created_at": ms_to_iso(row["user_file_created_at_ms"]) or "",
    }


def _file_select():
    return select(
        user_files.c.id.label("user_file_id"),
        user_files.c.user_id,
        user_files.c.stored_file_id,
        user_files.c.display_name,
        user_files.c.created_at_ms.label("user_file_created_at_ms"),
        user_files.c.updated_at_ms.label("user_file_updated_at_ms"),
        stored_files.c.content_hash,
        stored_files.c.real_path,
        stored_files.c.size_bytes,
        stored_files.c.is_directory,
        stored_files.c.original_name,
        stored_files.c.created_at_ms.label("stored_file_created_at_ms"),
    ).select_from(
        user_files.join(stored_files, user_files.c.stored_file_id == stored_files.c.id)
    )


async def _get_user_file_by_hash(
    user_id: int, content_hash: str
) -> dict[str, Any] | None:
    """通过 content_hash 和 user_id 查找用户文件"""
    stmt = (
        _file_select()
        .where(
            stored_files.c.content_hash == content_hash,
            user_files.c.user_id == user_id,
        )
        .order_by(user_files.c.id.asc())
    )
    async with transaction() as conn:
        row = (await conn.execute(stmt)).mappings().first()
    return dict(row) if row else None


def _normalize_entry_parent(path: str) -> str:
    candidate = (path or "").strip().replace("\\", "/").strip("/")
    if not candidate:
        return ""
    parts = [part for part in candidate.split("/") if part]
    if any(part in {".", ".."} for part in parts):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="无权访问此路径",
        )
    return "/".join(parts)


async def _directory_entries(
    stored_file_id: int, parent_path: str
) -> list[dict[str, Any]]:
    async with transaction() as conn:
        if parent_path:
            parent = (
                await conn.execute(
                    select(stored_file_entries.c.is_dir)
                    .where(
                        stored_file_entries.c.stored_file_id == stored_file_id,
                        stored_file_entries.c.relative_path == parent_path,
                    )
                    .limit(1)
                )
            ).first()
            if parent is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="路径不存在",
                )
            if not bool(parent[0]):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="路径不是文件夹",
                )

        rows = (
            (
                await conn.execute(
                    select(stored_file_entries)
                    .where(
                        stored_file_entries.c.stored_file_id == stored_file_id,
                        stored_file_entries.c.parent_path == parent_path,
                        stored_file_entries.c.relative_path != ".",
                    )
                    .order_by(
                        stored_file_entries.c.sort_key, stored_file_entries.c.name
                    )
                )
            )
            .mappings()
            .all()
        )

    return [
        {
            "name": row["name"],
            "path": row["relative_path"],
            "size": row["size_bytes"],
            "is_dir": bool(row["is_dir"]),
            "is_directory": bool(row["is_dir"]),
            "modified_at": row["mtime_ms"],
        }
        for row in rows
    ]


async def _get_user_space_info_v0(user_id: int, quota_bytes: int) -> dict[str, int]:
    async with transaction() as conn:
        used = (
            await conn.execute(
                select(func.coalesce(func.sum(stored_files.c.size_bytes), 0))
                .select_from(
                    user_files.join(
                        stored_files, user_files.c.stored_file_id == stored_files.c.id
                    )
                )
                .where(user_files.c.user_id == user_id)
            )
        ).scalar_one()
    usage = await get_usage(user_id, quota_bytes)
    reserved = int(usage["reserved_bytes"])
    available = max(0, quota_bytes - int(used or 0) - reserved)
    return {
        "quota": quota_bytes,
        "used": int(used or 0),
        "frozen": reserved,
        "available": available,
    }


async def _delete_user_file_reference_v0(user_id: int, user_file_id: int) -> bool:
    async with transaction() as conn:
        row = (
            (
                await conn.execute(
                    select(
                        user_files.c.stored_file_id,
                        stored_files.c.size_bytes,
                        stored_files.c.real_path,
                    )
                    .select_from(
                        user_files.join(
                            stored_files,
                            user_files.c.stored_file_id == stored_files.c.id,
                        )
                    )
                    .where(
                        user_files.c.id == user_file_id, user_files.c.user_id == user_id
                    )
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            return False

        deleted = await conn.execute(
            delete(user_files).where(
                user_files.c.id == user_file_id,
                user_files.c.user_id == user_id,
            )
        )
        if not deleted.rowcount:
            return False
        used_expr = user_storage_usage.c.used_bytes - int(row["size_bytes"] or 0)
        await conn.execute(
            update(user_storage_usage)
            .where(user_storage_usage.c.user_id == user_id)
            .values(
                used_bytes=case((used_expr < 0, 0), else_=used_expr),
                updated_at_ms=now_ms(),
            )
        )
        refs = (
            await conn.execute(
                select(func.count())
                .select_from(user_files)
                .where(user_files.c.stored_file_id == row["stored_file_id"])
            )
        ).scalar_one()
        if int(refs or 0) > 0:
            return True

        timestamp = now_ms()
        affected_downloads = (
            await conn.execute(
                update(global_downloads)
                .where(global_downloads.c.completed_file_id == row["stored_file_id"])
                .values(
                    status="cancelled",
                    aria2_gid=None,
                    completed_file_id=None,
                    completed_bytes=0,
                    completed_at_ms=None,
                    error_code="stored_file_deleted",
                    error_message="Stored file was deleted",
                    updated_at_ms=timestamp,
                )
                .returning(global_downloads.c.id)
            )
        ).all()
        affected_download_ids = [int(item[0]) for item in affected_downloads]
        if affected_download_ids:
            await conn.execute(
                update(user_tasks)
                .where(
                    user_tasks.c.global_download_id.in_(affected_download_ids),
                    user_tasks.c.status == "completed",
                )
                .values(
                    status="cancelled",
                    error_message="Stored file was deleted",
                    updated_at_ms=timestamp,
                    finished_at_ms=timestamp,
                )
            )
        await conn.execute(
            update(pack_tasks)
            .where(pack_tasks.c.output_stored_file_id == row["stored_file_id"])
            .values(output_stored_file_id=None, updated_at_ms=timestamp)
        )
        await conn.execute(
            delete(stored_file_entries).where(
                stored_file_entries.c.stored_file_id == row["stored_file_id"]
            )
        )
        await conn.execute(
            delete(stored_files).where(stored_files.c.id == row["stored_file_id"])
        )
        real_path = str(row["real_path"])

    from app.services.storage import get_store_dir, safe_delete_path

    path = Path(real_path)
    try:
        safe_delete_path(
            base_dir=get_store_dir(),
            target=path,
            recursive=path.is_dir(),
            allow_missing=True,
        )
    except Exception:
        logger.warning(
            "Failed to delete unreferenced stored path=%s", path, exc_info=True
        )
    return True


def _validate_subpath(base_path: Path, subpath: str) -> Path:
    """Validate and resolve a subpath within a base directory.

    Args:
        base_path: The base directory path
        subpath: The relative subpath to validate

    Returns:
        Resolved absolute path

    Raises:
        HTTPException: If path is invalid or escapes base directory
    """
    if not subpath:
        return base_path.resolve()

    # Normalize and resolve both paths
    resolved_base = base_path.resolve()
    target = (resolved_base / subpath).resolve()

    # Ensure it's within base path
    try:
        target.relative_to(resolved_base)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="无权访问此路径"
        )

    return target


def _range_file_response(request: Request, file_path: Path, filename: str):
    """支持 Range 请求的文件下载响应（多线程下载/断点续传）"""
    try:
        file_size = file_path.stat().st_size
    except FileNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文件不存在")
    encoded_name = quote(filename)
    if encoded_name != filename:
        disposition = f"attachment; filename*=utf-8''{encoded_name}"
    else:
        disposition = f'attachment; filename="{filename}"'

    range_header = request.headers.get("range")
    if not range_header:
        # Do NOT pass `filename=` to FileResponse — uvicorn encodes headers
        # with latin-1, which crashes on CJK characters (UnicodeEncodeError).
        # We set Content-Disposition manually with RFC 5987 UTF-8 encoding.
        return FileResponse(
            path=str(file_path),
            media_type="application/octet-stream",
            headers={"Accept-Ranges": "bytes", "Content-Disposition": disposition},
        )

    # Parse "bytes=start-end"
    try:
        unit, ranges = range_header.split("=", 1)
        if unit.strip() != "bytes":
            raise ValueError
        range_spec = ranges.split(",")[0].strip()
        parts = range_spec.split("-")
        if not parts[0]:
            suffix_length = int(parts[1])
            if suffix_length < 0:
                raise ValueError
            start = max(0, file_size - suffix_length)
            end = file_size - 1
        else:
            start = int(parts[0])
            end = int(parts[1]) if parts[1] else file_size - 1
            if start < 0 or end < 0:
                raise ValueError
    except (ValueError, IndexError):
        raise HTTPException(416, "Invalid Range header")

    if start >= file_size or start > end:
        raise HTTPException(
            416,
            headers={"Content-Range": f"bytes */{file_size}"},
        )
    end = min(end, file_size - 1)
    content_length = end - start + 1

    def iter_file():
        try:
            with open(file_path, "rb") as f:
                f.seek(start)
                remaining = content_length
                while remaining > 0:
                    chunk = f.read(min(65536, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk
        except FileNotFoundError:
            # 文件在流式传输过程中被删除，静默结束
            return

    return StreamingResponse(
        iter_file(),
        status_code=206,
        media_type="application/octet-stream",
        headers={
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Content-Length": str(content_length),
            "Accept-Ranges": "bytes",
            "Content-Disposition": disposition,
        },
    )


def _tracked_response(
    response: FileResponse | StreamingResponse,
    lease: DownloadLease | None,
) -> FileResponse | StreamingResponse:
    """包装 response，在流结束/客户端断开时释放并发连接"""
    if lease is None:
        return response

    if isinstance(response, StreamingResponse) and response.body_iterator is not None:
        original_iter = response.body_iterator

        async def _releasing_iter():
            try:
                async for chunk in original_iter:  # type: ignore[union-attr]
                    yield chunk
            finally:
                await lease.release()

        # 同步 generator 需要包装
        import inspect

        if inspect.isasyncgen(original_iter):
            response.body_iterator = _releasing_iter()
        else:
            # 同步 generator → 包装为 async
            sync_iter = original_iter

            async def _sync_releasing_iter():
                try:
                    for chunk in sync_iter:  # type: ignore[union-attr]
                        yield chunk
                finally:
                    await lease.release()

            response.body_iterator = _sync_releasing_iter()
    else:
        # FileResponse — 用 BackgroundTask 释放
        from starlette.background import BackgroundTask

        async def _release():
            await lease.release()

        if response.background:
            # 链式 background task
            original_bg = response.background

            async def _chained():
                await original_bg()  # type: ignore[misc]
                await _release()

            response.background = BackgroundTask(_chained)
        else:
            response.background = BackgroundTask(_release)
    return response


async def _resolve_file_ids(
    user_id: int, file_ids: list[int]
) -> list[tuple[str, int, str]]:
    """将 UserFile IDs 解析为 (绝对路径, 大小, 显示名) 列表，验证归属"""
    if not file_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="文件列表不能为空"
        )

    requested_ids = list(dict.fromkeys(file_ids))
    stmt = _file_select().where(
        user_files.c.id.in_(requested_ids),
        user_files.c.user_id == user_id,
    )
    async with transaction() as conn:
        rows = (await conn.execute(stmt)).mappings().all()

    by_id = {int(row["user_file_id"]): dict(row) for row in rows}
    if len(by_id) != len(requested_ids):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="部分文件不存在或无权访问"
        )

    return [
        (
            str(by_id[file_id]["real_path"]),
            int(by_id[file_id]["size_bytes"]),
            str(by_id[file_id]["display_name"] or "未命名"),
        )
        for file_id in requested_ids
    ]


# ========== API Endpoints ==========


@router.get("", response_model=FileListResponse)
async def list_files(
    page: int = 1,
    page_size: int = 10,
    user: AuthUser = Depends(require_user),
) -> FileListResponse:
    """列出用户的文件引用（分页）

    Args:
        page: 页码，从 1 开始
        page_size: 每页数量，允许 10/20/30/50/100
    """
    user_id = _require_user_id(user)

    # 限流
    await ensure_authenticated_allowed(
        user_id,
        RateLimitScope.AUTHENTICATED_API,
        detail="请求过于频繁，请稍后再试",
    )

    # 参数校验
    if page_size not in (10, 20, 30, 50, 100):
        page_size = 10
    if page < 1:
        page = 1

    offset = (page - 1) * page_size

    async with transaction() as conn:
        total = int(
            (
                await conn.execute(
                    select(func.count())
                    .select_from(user_files)
                    .where(user_files.c.user_id == user_id)
                )
            ).scalar_one()
            or 0
        )
        rows = (
            (
                await conn.execute(
                    _file_select()
                    .where(user_files.c.user_id == user_id)
                    .order_by(user_files.c.created_at_ms.desc())
                    .offset(offset)
                    .limit(page_size)
                )
            )
            .mappings()
            .all()
        )

    files = [FileInfo(**_file_row_to_dict(dict(row))) for row in rows]
    logger.debug(
        "查询文件列表 user_id=%s page=%s page_size=%s total=%s",
        user_id,
        page,
        page_size,
        total,
    )

    space_info = await _get_user_space_info_v0(user_id, user.quota)

    return FileListResponse(
        files=files,
        total=total,
        space={
            "used": space_info["used"],
            "frozen": space_info["frozen"],
            "available": space_info["available"],
        },
    )


@router.get("/{file_hash}/browse")
async def browse_file(
    file_hash: str,
    path: str = "",
    user: AuthUser = Depends(require_user),
) -> list[dict]:
    """浏览 BT 文件夹内容

    Args:
        file_hash: 文件的 content_hash
        path: 文件夹内的相对路径
    """
    # Get user file and stored file
    user_id = _require_user_id(user)
    await ensure_authenticated_allowed(
        user_id,
        RateLimitScope.AUTHENTICATED_API,
        detail="请求过于频繁，请稍后再试",
    )
    row = await _get_user_file_by_hash(user_id, file_hash)

    if not row:
        logger.warning(
            "浏览文件失败 user_id=%s file_hash=%s reason=not_found", user_id, file_hash
        )
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="文件不存在")

    if not row["is_directory"]:
        logger.warning(
            "浏览文件失败 user_id=%s file_hash=%s reason=not_directory",
            user_id,
            file_hash,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="此文件不是文件夹"
        )

    parent_path = _normalize_entry_parent(path)
    files = await _directory_entries(int(row["stored_file_id"]), parent_path)

    logger.debug(
        "浏览文件成功 user_id=%s file_hash=%s count=%s", user_id, file_hash, len(files)
    )

    return files


@router.get("/{file_hash}/download")
async def download_file(
    file_hash: str,
    request: Request,
    path: str = "",
    user: AuthUser = Depends(require_user),
):
    """下载文件
    支持下载整个文件或 BT 文件夹内的单个文件。

    Args:
        file_hash: 文件的 content_hash
        path: BT 文件夹内的相对路径（可选）
    """
    user_id = _require_user_id(user)
    await ensure_authenticated_allowed(
        user_id,
        RateLimitScope.AUTHENTICATED_DOWNLOAD,
        detail="下载请求过于频繁，请稍后再试",
    )
    acquire_result = await download_limiter.acquire_authenticated(user_id, file_hash)
    if not acquire_result.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=acquire_result.detail(),
        )

    lease = acquire_result.lease
    try:
        # Get user file and stored file
        row = await _get_user_file_by_hash(user_id, file_hash)
        if not row:
            logger.warning(
                "下载文件失败 user_id=%s file_hash=%s reason=not_found",
                user_id,
                file_hash,
            )
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="文件不存在"
            )
        base_path = Path(str(row["real_path"]))
        if not base_path.exists():
            logger.warning(
                "下载文件失败 user_id=%s file_hash=%s reason=base_missing",
                user_id,
                file_hash,
            )
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="文件不存在"
            )
        # Determine target file
        if path:
            if not row["is_directory"]:
                logger.warning(
                    "下载文件失败 user_id=%s file_hash=%s reason=path_on_non_dir",
                    user_id,
                    file_hash,
                )
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="此文件不是文件夹，不支持路径参数",
                )
            target_path = _validate_subpath(base_path, path)
        else:
            target_path = base_path
        if not target_path.exists():
            logger.warning(
                "下载文件失败 user_id=%s file_hash=%s reason=target_missing",
                user_id,
                file_hash,
            )
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="文件不存在"
            )
        if target_path.is_dir():
            logger.warning(
                "下载文件失败 user_id=%s file_hash=%s reason=target_is_directory",
                user_id,
                file_hash,
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="不能直接下载文件夹，请选择具体文件",
            )
        # 确定下载文件名：整个文件用 display_name，子文件用实际文件名
        download_name = target_path.name if path else str(row["display_name"])
        logger.info(
            "下载文件成功 user_id=%s file_hash=%s file=%s",
            user_id,
            file_hash,
            download_name,
        )
        response = _range_file_response(request, target_path, download_name)
        return _tracked_response(response, lease)
    except Exception:
        if lease is not None:
            await lease.release()
        raise


@router.delete("/pack")
async def clear_finished_pack_tasks(
    user: AuthUser = Depends(require_user),
) -> dict:
    """一键清空已完成/失败/取消的打包任务记录"""
    user_id = _require_user_id(user)
    async with transaction() as conn:
        result = await conn.execute(
            delete(pack_tasks)
            .where(
                pack_tasks.c.user_id == user_id,
                pack_tasks.c.status.in_(PACK_TERMINAL_STATUSES),
            )
            .returning(pack_tasks)
        )
        tasks = result.mappings().all()

    count = len(tasks)

    return {"ok": True, "count": count}


@router.delete("/pack/{task_id}")
async def cancel_or_delete_pack_task(
    task_id: int, user: AuthUser = Depends(require_user)
) -> dict:
    """取消或删除打包任务"""
    user_id = _require_user_id(user)
    from app.services.pack import PackTaskManager

    async with transaction() as conn:
        task = (
            (
                await conn.execute(
                    select(pack_tasks).where(
                        pack_tasks.c.id == task_id, pack_tasks.c.user_id == user_id
                    )
                )
            )
            .mappings()
            .first()
        )

    if not task:
        logger.warning(
            "取消/删除打包任务失败 user_id=%s task_id=%s reason=not_found",
            user_id,
            task_id,
        )
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")

    task_status = task["status"]

    if task_status in PACK_ACTIVE_STATUSES:
        await PackTaskManager.cancel_pack(task_id)
        reserved_to_release = int(task["reserved_bytes"] or 0)
        async with transaction() as conn:
            result = await conn.execute(
                update(pack_tasks)
                .where(
                    pack_tasks.c.id == task_id,
                    pack_tasks.c.user_id == user_id,
                    pack_tasks.c.status.in_(PACK_ACTIVE_STATUSES),
                )
                .values(
                    status="cancelled",
                    progress=0,
                    reserved_bytes=0,
                    updated_at_ms=now_ms(),
                    finished_at_ms=now_ms(),
                )
                .returning(pack_tasks)
            )
            refreshed_task = result.mappings().first()
            cancelled = bool(refreshed_task and refreshed_task["status"] == "cancelled")
        if cancelled and reserved_to_release > 0:
            await release_reserved(user_id, reserved_to_release, quota_bytes=user.quota)

        if cancelled:
            logger.info("取消打包任务成功 user_id=%s task_id=%s", user_id, task_id)
            return {"ok": True, "message": "任务已取消"}

        # 状态已变化，重新读取并按实际状态处理
        async with transaction() as conn:
            task = (
                (
                    await conn.execute(
                        select(pack_tasks).where(
                            pack_tasks.c.id == task_id, pack_tasks.c.user_id == user_id
                        )
                    )
                )
                .mappings()
                .first()
            )
        if not task:
            logger.warning(
                "取消/删除打包任务失败 user_id=%s task_id=%s reason=not_found_after_reload",
                user_id,
                task_id,
            )
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在"
            )
        task_status = task["status"]

    if task_status in PACK_TERMINAL_STATUSES:
        async with transaction() as conn:
            await conn.execute(
                delete(pack_tasks).where(
                    pack_tasks.c.id == task_id, pack_tasks.c.user_id == user_id
                )
            )
        logger.info(
            "删除打包任务记录成功 user_id=%s task_id=%s status=%s",
            user_id,
            task_id,
            task_status,
        )
        return {"ok": True, "message": "任务已删除"}

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST, detail="无法处理该任务状态"
    )


@router.delete("/{file_hash}")
async def delete_file(
    file_hash: str,
    user: AuthUser = Depends(require_user),
) -> dict:
    """删除文件引用
    如果是最后一个引用，物理文件也会被删除。
    Args:
        file_hash: 文件的 content_hash
    """
    # Verify ownership
    user_id = _require_user_id(user)
    row = await _get_user_file_by_hash(user_id, file_hash)

    if not row:
        logger.warning(
            "删除文件失败 user_id=%s file_hash=%s reason=not_found", user_id, file_hash
        )
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="文件不存在")

    # 检查是否有活跃分享
    async with transaction() as conn:
        timestamp = now_ms()
        active_share_count = (
            await conn.execute(
                select(func.count())
                .select_from(share_links)
                .where(
                    share_links.c.user_file_id == row["user_file_id"],
                    share_links.c.status == "active",
                    (
                        share_links.c.expires_at_ms.is_(None)
                        | (share_links.c.expires_at_ms > timestamp)
                    ),
                    (
                        share_links.c.max_downloads.is_(None)
                        | (share_links.c.download_count < share_links.c.max_downloads)
                    ),
                )
            )
        ).scalar_one()
        if active_share_count and active_share_count > 0:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="该文件有活跃的分享链接，请先失效所有分享后再删除",
            )
    success = await _delete_user_file_reference_v0(user_id, int(row["user_file_id"]))
    if not success:
        logger.warning(
            "删除文件失败 user_id=%s file_hash=%s reason=delete_reference_failed",
            user_id,
            file_hash,
        )
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="文件不存在")

    logger.info("删除文件成功 user_id=%s file_hash=%s", user_id, file_hash)

    return {"ok": True}


@router.put("/{file_hash}/rename")
async def rename_file(
    file_hash: str,
    payload: RenameRequest,
    user: AuthUser = Depends(require_user),
) -> dict:
    """重命名文件
    只修改显示名称，不影响实际存储。

    Args:
        file_hash: 文件的 content_hash
    """
    user_id = _require_user_id(user)
    name = payload.name.strip()
    if not name:
        logger.warning(
            "重命名文件失败 user_id=%s file_hash=%s reason=empty_name",
            user_id,
            file_hash,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="名称不能为空"
        )

    if "/" in name or "\\" in name:
        logger.warning(
            "重命名文件失败 user_id=%s file_hash=%s reason=invalid_name",
            user_id,
            file_hash,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="名称不能包含路径分隔符"
        )

    if name in {".", ".."}:
        logger.warning(
            "重命名文件失败 user_id=%s file_hash=%s reason=dot_name", user_id, file_hash
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="名称不合法"
        )

    if any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        logger.warning(
            "重命名文件失败 user_id=%s file_hash=%s reason=control_char",
            user_id,
            file_hash,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="名称包含非法字符"
        )

    async with transaction() as conn:
        result = await conn.execute(
            update(user_files)
            .where(
                user_files.c.id
                == select(user_files.c.id)
                .select_from(
                    user_files.join(
                        stored_files, user_files.c.stored_file_id == stored_files.c.id
                    )
                )
                .where(
                    stored_files.c.content_hash == file_hash,
                    user_files.c.user_id == user_id,
                )
                .order_by(user_files.c.id.asc())
                .limit(1)
                .scalar_subquery()
            )
            .values(display_name=name, updated_at_ms=now_ms())
            .returning(user_files.c.id)
        )
        row = result.first()
        if not row:
            logger.warning(
                "重命名文件失败 user_id=%s file_hash=%s reason=not_found",
                user_id,
                file_hash,
            )
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="文件不存在"
            )

    logger.info("重命名文件成功 user_id=%s file_hash=%s", user_id, file_hash)

    return {"ok": True}


@router.get("/space")
async def get_space(user: AuthUser = Depends(require_user)) -> dict:
    """获取用户空间信息"""
    user_id = _require_user_id(user)
    await ensure_authenticated_allowed(
        user_id,
        RateLimitScope.AUTHENTICATED_API,
        detail="请求过于频繁，请稍后再试",
    )
    space_info = await _get_user_space_info_v0(user_id, user.quota)
    logger.debug("查询空间信息 user_id=%s", user_id)
    return space_info


def _pack_task_to_dict(task: dict[str, Any]) -> dict:
    """Convert v0 PackTask row to legacy-compatible API dict."""
    response_status = "done" if task["status"] == "completed" else task["status"]
    return {
        "id": task["id"],
        "user_id": task["user_id"],
        "owner_id": task["user_id"],
        "source_user_file_ids": json.loads(task["source_user_file_ids_json"] or "[]"),
        "source_user_file_ids_json": task["source_user_file_ids_json"],
        "source_size_bytes": task["source_size_bytes"],
        "folder_path": task["source_user_file_ids_json"],
        "folder_size": task["source_size_bytes"],
        "reserved_bytes": task["reserved_bytes"],
        "reserved_space": task["reserved_bytes"],
        "output_name": task["output_name"],
        "output_stored_file_id": task["output_stored_file_id"],
        "stored_file_id": task["output_stored_file_id"],
        "delete_source": bool(task["delete_source"]),
        "output_size": task.get("output_size"),
        "output_path": None,
        "status": response_status,
        "progress": task["progress"],
        "error_message": task["error_message"],
        "created_at": ms_to_iso(task["created_at_ms"]),
        "updated_at": ms_to_iso(task["updated_at_ms"]),
        "finished_at": ms_to_iso(task["finished_at_ms"]),
    }


def _pack_task_select():
    return select(
        pack_tasks,
        stored_files.c.size_bytes.label("output_size"),
    ).select_from(
        pack_tasks.outerjoin(
            stored_files,
            pack_tasks.c.output_stored_file_id == stored_files.c.id,
        )
    )


# Pack endpoints remain largely unchanged as they work with physical files
# These will be updated in a future iteration to work with StoredFile


@router.post("/pack/calculate-size")
async def calculate_paths_size(
    payload: CalculateSizeRequest, user: AuthUser = Depends(require_user)
) -> dict:
    """计算多个文件的总大小"""
    user_id = _require_user_id(user)
    await ensure_authenticated_allowed(
        user_id,
        RateLimitScope.AUTHENTICATED_API,
        detail="请求过于频繁，请稍后再试",
    )
    resolved = await _resolve_file_ids(user_id, payload.file_ids)
    total_size = sum(size for _, size, _ in resolved)
    return {"total_size": total_size}


@router.get("/pack/available-space")
async def get_pack_available_space(user: AuthUser = Depends(require_user)) -> dict:
    """获取用户可用于打包的空间"""
    user_id = _require_user_id(user)
    await ensure_authenticated_allowed(
        user_id,
        RateLimitScope.AUTHENTICATED_API,
        detail="请求过于频繁，请稍后再试",
    )
    info = await _get_user_space_info_v0(user_id, user.quota)
    return {
        "available": info["available"],
        "quota": info["quota"],
        "used": info["used"],
    }


@router.post("/pack", status_code=status.HTTP_201_CREATED)
async def create_pack_task(
    payload: PackRequest, user: AuthUser = Depends(require_user)
) -> dict:
    """创建打包任务 - 基于 UserFile ID"""
    user_id = _require_user_id(user)
    # 频率限制
    try:
        await ensure_authenticated_allowed(
            user_id,
            RateLimitScope.CREATE_PACK,
            detail="操作过于频繁，请稍后再试",
        )
    except HTTPException:
        logger.warning("创建打包任务被限流 user_id=%s", user_id)
        raise

    from app.services.pack import PackTaskManager

    # 解析文件 ID → 绝对路径
    resolved = await _resolve_file_ids(user_id, payload.file_ids)
    abs_paths = [path for path, _, _ in resolved]
    source_names = [name for _, _, name in resolved]
    total_size = sum(size for _, size, _ in resolved)

    if total_size == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="选中的文件为空"
        )

    source_user_file_ids_json = json.dumps(sorted(payload.file_ids))
    reserved_bytes = total_size

    # 验证输出文件名；未指定时用首个文件的显示名
    output_name = payload.output_name
    if not output_name and len(resolved) == 1:
        # 去掉扩展名，用 display_name 作为默认压缩包名
        display_name = resolved[0][2]
        output_name = Path(display_name).stem or display_name
    if output_name:
        if len(output_name) > 200:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="输出文件名不能超过 200 个字符",
            )
        _INVALID_CHARS = set('/\\:*?"<>|\0')
        if _INVALID_CHARS & set(output_name):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="输出文件名包含非法字符"
            )

    # Check available space + create task record atomically
    async with _get_pack_create_lock():
        # 检查是否已有相同源文件的已完成打包产物
        async with transaction() as conn:
            done_tasks = (
                (
                    await conn.execute(
                        select(pack_tasks).where(
                            pack_tasks.c.user_id == user_id,
                            pack_tasks.c.source_user_file_ids_json
                            == source_user_file_ids_json,
                            pack_tasks.c.status == "completed",
                            pack_tasks.c.output_stored_file_id.is_not(None),
                        )
                    )
                )
                .mappings()
                .all()
            )
            for done_task in done_tasks:
                user_file = (
                    await conn.execute(
                        select(user_files.c.display_name).where(
                            user_files.c.user_id == user_id,
                            user_files.c.stored_file_id
                            == done_task["output_stored_file_id"],
                        )
                    )
                ).first()
                if user_file:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=f"已存在打包完成的文件「{user_file[0] or '未知文件'}」",
                    )
            # 所有历史产物的 UserFile 均已删除，允许重新打包

        try:
            await reserve_bytes(user_id, reserved_bytes, quota_bytes=user.quota)
        except ValueError:
            info = await _get_user_space_info_v0(user_id, user.quota)
            logger.warning(
                "创建打包任务失败 user_id=%s reason=insufficient_space required=%s available=%s",
                user_id,
                reserved_bytes,
                info["available"],
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"空间不足。需要: {reserved_bytes / 1024**3:.2f} GB, 可用: {info['available'] / 1024**3:.2f} GB",
            )

        try:
            async with transaction() as conn:
                existing_result = await conn.execute(
                    select(pack_tasks.c.id).where(
                        pack_tasks.c.user_id == user_id,
                        pack_tasks.c.source_user_file_ids_json
                        == source_user_file_ids_json,
                        pack_tasks.c.status.in_(PACK_ACTIVE_STATUSES),
                    )
                )
                if existing_result.first() is not None:
                    logger.warning(
                        "创建打包任务冲突 user_id=%s file_ids=%s",
                        user_id,
                        payload.file_ids,
                    )
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="相同文件已有进行中的打包任务",
                    )

                timestamp = now_ms()
                task_row = (
                    (
                        await conn.execute(
                            insert(pack_tasks)
                            .values(
                                user_id=user_id,
                                source_user_file_ids_json=source_user_file_ids_json,
                                source_size_bytes=total_size,
                                reserved_bytes=reserved_bytes,
                                output_name=output_name,
                                delete_source=1 if payload.delete_source else 0,
                                status="pending",
                                progress=0,
                                created_at_ms=timestamp,
                                updated_at_ms=timestamp,
                            )
                            .returning(pack_tasks)
                        )
                    )
                    .mappings()
                    .one()
                )
                task_id = int(task_row["id"])
        except Exception:
            await release_reserved(user_id, reserved_bytes, quota_bytes=user.quota)
            raise

    # Start async packing - pass absolute paths directly
    asyncio.create_task(
        PackTaskManager.start_pack(
            task_id,
            user_id,
            abs_paths,
            payload.file_ids,
            output_name,
            payload.delete_source,
            source_names=source_names,
        )
    )
    logger.info("创建打包任务成功 user_id=%s task_id=%s", user_id, task_id)

    return _pack_task_to_dict(dict(task_row))


@router.get("/pack")
async def list_pack_tasks(user: AuthUser = Depends(require_user)) -> list[dict]:
    """列出用户的打包任务"""
    user_id = _require_user_id(user)
    await ensure_authenticated_allowed(
        user_id,
        RateLimitScope.AUTHENTICATED_API,
        detail="请求过于频繁，请稍后再试",
    )
    async with transaction() as conn:
        tasks = (
            (
                await conn.execute(
                    _pack_task_select()
                    .where(pack_tasks.c.user_id == user_id)
                    .order_by(pack_tasks.c.created_at_ms.desc())
                )
            )
            .mappings()
            .all()
        )
        logger.debug("查询打包任务列表 user_id=%s count=%s", user_id, len(tasks))
        return [_pack_task_to_dict(dict(t)) for t in tasks]


@router.get("/pack/{task_id}")
async def get_pack_task(task_id: int, user: AuthUser = Depends(require_user)) -> dict:
    """获取打包任务详情"""
    user_id = _require_user_id(user)
    await ensure_authenticated_allowed(
        user_id,
        RateLimitScope.AUTHENTICATED_API,
        detail="请求过于频繁，请稍后再试",
    )
    async with transaction() as conn:
        task = (
            (
                await conn.execute(
                    _pack_task_select().where(
                        pack_tasks.c.id == task_id, pack_tasks.c.user_id == user_id
                    )
                )
            )
            .mappings()
            .first()
        )

    if not task:
        logger.warning(
            "查询打包任务失败 user_id=%s task_id=%s reason=not_found", user_id, task_id
        )
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")
    logger.debug("查询打包任务详情 user_id=%s task_id=%s", user_id, task_id)
    return _pack_task_to_dict(dict(task))


# Legacy quota endpoint for backward compatibility
@router.get("/quota")
async def get_quota(user: AuthUser = Depends(require_user)) -> dict:
    """获取用户空间配额信息（兼容旧接口）"""
    user_id = _require_user_id(user)
    await ensure_authenticated_allowed(
        user_id,
        RateLimitScope.AUTHENTICATED_API,
        detail="请求过于频繁，请稍后再试",
    )
    space_info = await _get_user_space_info_v0(user_id, user.quota)

    # Calculate percentage
    total = space_info["used"] + space_info["available"]
    percentage = (space_info["used"] / total * 100) if total > 0 else 0

    logger.debug("查询配额信息 user_id=%s", user_id)

    return {
        "used": space_info["used"],
        "total": total,
        "percentage": round(percentage, 2),
    }
