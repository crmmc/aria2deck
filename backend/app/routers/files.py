"""用户文件管理接口模块（共享下载架构）

提供用户文件的查看、下载、删除、重命名等功能。
基于 UserFile 引用模型，支持 BT 文件夹浏览。
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import update
from sqlmodel import func, or_, select
from starlette.responses import StreamingResponse
from urllib.parse import quote

from app.auth import require_user
from app.core.config import settings
from app.core.rate_limit import api_limiter
from app.database import get_session
from app.models import User, PackTask, UserFile, StoredFile, ShareLink
from app.services.pack import cleanup_pack_output
from app.services.storage import (
    delete_user_file_reference,
    get_user_space_info,
)

logger = logging.getLogger(__name__)

UF: Any = UserFile.__dict__["__table__"].c
SF: Any = StoredFile.__dict__["__table__"].c
PT: Any = PackTask.__dict__["__table__"].c
SL: Any = ShareLink.__dict__["__table__"].c

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


def _require_user_id(user: User) -> int:
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

def _user_file_to_dict(user_file: UserFile, stored_file: StoredFile) -> dict:
    """Convert UserFile + StoredFile to API response dict"""
    return {
        "id": user_file.id,
        "content_hash": stored_file.content_hash,
        "name": user_file.display_name,
        "size": stored_file.size,
        "is_directory": stored_file.is_directory,
        "created_at": user_file.created_at,
    }


async def _get_user_file_by_hash(
    user_id: int, content_hash: str
) -> tuple[UserFile, StoredFile] | None:
    """通过 content_hash 和 owner_id 查找用户文件"""
    async with get_session() as db:
        result = await db.exec(
            select(UserFile, StoredFile)
            .join(StoredFile, UF.stored_file_id == SF.id)
            .where(
                SF.content_hash == content_hash,
                UF.owner_id == user_id,
            )
            .order_by(UF.id.asc())
        )
        return result.first()


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
            status_code=status.HTTP_403_FORBIDDEN,
            detail="无权访问此路径"
        )

    return target


def _range_file_response(request: Request, file_path: Path, filename: str):
    """支持 Range 请求的文件下载响应（多线程下载/断点续传）"""
    file_size = file_path.stat().st_size
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
        with open(file_path, "rb") as f:
            f.seek(start)
            remaining = content_length
            while remaining > 0:
                chunk = f.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

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


async def _resolve_file_ids(user_id: int, file_ids: list[int]) -> list[tuple[str, int, str]]:
    """将 UserFile IDs 解析为 (绝对路径, 大小, 显示名) 列表，验证归属"""
    if not file_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="文件列表不能为空"
        )

    async with get_session() as db:
        result = await db.exec(
            select(UserFile, StoredFile)
            .join(StoredFile, UF.stored_file_id == SF.id)
            .where(UF.id.in_(file_ids), UF.owner_id == user_id)
        )
        pairs = result.all()

    if len(pairs) != len(file_ids):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="部分文件不存在或无权访问"
        )

    return [(sf.real_path, sf.size, uf.display_name or "未命名") for uf, sf in pairs]


# ========== API Endpoints ==========

@router.get("", response_model=FileListResponse)
async def list_files(user: User = Depends(require_user)) -> FileListResponse:
    """列出用户的所有文件引用

    返回用户根目录下的所有文件/文件夹条目。
    """
    user_id = _require_user_id(user)
    async with get_session() as db:
        result = await db.exec(
            select(UserFile, StoredFile)
            .join(StoredFile, UF.stored_file_id == SF.id)
            .where(UF.owner_id == user_id)
            .order_by(UF.created_at.desc())
        )
        rows = result.all()

    files = [FileInfo(**_user_file_to_dict(uf, sf)) for uf, sf in rows]
    logger.debug("查询文件列表 user_id=%s count=%s", user_id, len(files))

    space_info = await get_user_space_info(user_id, user.quota)

    return FileListResponse(
        files=files,
        space={
            "used": space_info["used"],
            "frozen": space_info["frozen"],
            "available": space_info["available"],
        }
    )


@router.get("/{file_hash}/browse")
async def browse_file(
    file_hash: str,
    path: str = "",
    user: User = Depends(require_user),
) -> list[dict]:
    """浏览 BT 文件夹内容

    Args:
        file_hash: 文件的 content_hash
        path: 文件夹内的相对路径
    """
    # Get user file and stored file
    user_id = _require_user_id(user)
    row = await _get_user_file_by_hash(user_id, file_hash)

    if not row:
        logger.warning("浏览文件失败 user_id=%s file_hash=%s reason=not_found", user_id, file_hash)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="文件不存在"
        )

    user_file, stored_file = row

    if not stored_file.is_directory:
        logger.warning("浏览文件失败 user_id=%s file_hash=%s reason=not_directory", user_id, file_hash)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="此文件不是文件夹"
        )

    # Validate and resolve path
    base_path = Path(stored_file.real_path)
    if not base_path.exists():
        logger.warning("浏览文件失败 user_id=%s file_hash=%s reason=base_missing", user_id, file_hash)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="文件夹不存在"
        )

    target_path = _validate_subpath(base_path, path)

    if not target_path.exists():
        logger.warning("浏览文件失败 user_id=%s file_hash=%s reason=path_missing", user_id, file_hash)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="路径不存在"
        )

    if not target_path.is_dir():
        logger.warning("浏览文件失败 user_id=%s file_hash=%s reason=path_not_directory", user_id, file_hash)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="路径不是文件夹"
        )

    # List directory contents
    files = []
    try:
        for entry in sorted(target_path.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            try:
                stat = entry.stat()
                files.append({
                    "name": entry.name,
                    "size": stat.st_size if entry.is_file() else 0,
                    "is_directory": entry.is_dir(),
                })
            except OSError as e:
                logger.warning("Failed to stat file %s: %s", entry, e)
                continue
    except FileNotFoundError:
        logger.warning("浏览文件失败 user_id=%s file_hash=%s reason=file_deleted", user_id, file_hash)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="文件或目录已被删除"
        )
    except PermissionError:
        logger.warning("浏览文件失败 user_id=%s file_hash=%s reason=permission_denied", user_id, file_hash)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="无权访问此目录"
        )

    logger.debug("浏览文件成功 user_id=%s file_hash=%s count=%s", user_id, file_hash, len(files))

    return files


@router.get("/{file_hash}/download")
async def download_file(
    file_hash: str,
    request: Request,
    path: str = "",
    user: User = Depends(require_user),
):
    """下载文件
    支持下载整个文件或 BT 文件夹内的单个文件。

    Args:
        file_hash: 文件的 content_hash
        path: BT 文件夹内的相对路径（可选）
    """
    user_id = _require_user_id(user)
    if not await api_limiter.is_allowed(user_id, "download_file", limit=settings.rate_limit_download_file, window_seconds=60):
        logger.warning("下载文件被限流 user_id=%s file_hash=%s", user_id, file_hash)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="下载请求过于频繁，请稍后再试"
        )
    # Get user file and stored file
    row = await _get_user_file_by_hash(user_id, file_hash)
    if not row:
        logger.warning("下载文件失败 user_id=%s file_hash=%s reason=not_found", user_id, file_hash)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="文件不存在"
        )
    user_file, stored_file = row
    base_path = Path(stored_file.real_path)
    if not base_path.exists():
        logger.warning("下载文件失败 user_id=%s file_hash=%s reason=base_missing", user_id, file_hash)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="文件不存在"
        )
    # Determine target file
    if path:
        if not stored_file.is_directory:
            logger.warning("下载文件失败 user_id=%s file_hash=%s reason=path_on_non_dir", user_id, file_hash)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="此文件不是文件夹，不支持路径参数"
            )
        target_path = _validate_subpath(base_path, path)
    else:
        target_path = base_path
    if not target_path.exists():
        logger.warning("下载文件失败 user_id=%s file_hash=%s reason=target_missing", user_id, file_hash)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="文件不存在"
        )
    if target_path.is_dir():
        logger.warning("下载文件失败 user_id=%s file_hash=%s reason=target_is_directory", user_id, file_hash)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="不能直接下载文件夹，请选择具体文件"
        )
    # 确定下载文件名：整个文件用 display_name，子文件用实际文件名
    download_name = target_path.name if path else user_file.display_name
    logger.info("下载文件成功 user_id=%s file_hash=%s file=%s", user_id, file_hash, download_name)
    return _range_file_response(request, target_path, download_name)


@router.delete("/pack")
async def clear_finished_pack_tasks(
    user: User = Depends(require_user),
) -> dict:
    """一键清空已完成/失败/取消的打包任务记录"""
    user_id = _require_user_id(user)
    terminal_statuses = ["done", "failed", "cancelled"]
    async with get_session() as db:
        result = await db.exec(
            select(PackTask).where(
                PT.owner_id == user_id,
                PT.status.in_(terminal_statuses),
            )
        )
        tasks = result.all()

        count = 0
        for task in tasks:
            # failed/cancelled 任务可能有残留的半成品文件
            if task.status in ("failed", "cancelled") and task.output_path:
                cleanup_pack_output(Path(task.output_path))
            await db.delete(task)
            count += 1

    return {"ok": True, "count": count}


@router.delete("/pack/{task_id}")
async def cancel_or_delete_pack_task(
    task_id: int,
    user: User = Depends(require_user)
) -> dict:
    """取消或删除打包任务"""
    user_id = _require_user_id(user)
    from app.services.pack import PackTaskManager

    async with get_session() as db:
        result = await db.exec(
            select(PackTask).where(PT.id == task_id, PT.owner_id == user_id)
        )
        task = result.first()

    if not task:
        logger.warning("取消/删除打包任务失败 user_id=%s task_id=%s reason=not_found", user_id, task_id)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")

    task_status = task.status

    if task_status in ("pending", "packing"):
        await PackTaskManager.cancel_pack(task_id)
        async with get_session() as db:
            await db.execute(
                update(PackTask)
                .where(
                    PT.id == task_id,
                    PT.status.in_(["pending", "packing"]),
                )
                .values(
                    status="cancelled",
                    progress=0,
                    reserved_space=0,
                    updated_at=utc_now()
                )
            )
            refreshed = await db.exec(select(PackTask).where(PT.id == task_id, PT.owner_id == user_id))
            refreshed_task = refreshed.first()
            cancelled = bool(refreshed_task and refreshed_task.status == "cancelled")

        if cancelled:
            logger.info("取消打包任务成功 user_id=%s task_id=%s", user_id, task_id)
            return {"ok": True, "message": "任务已取消"}

        # 状态已变化，重新读取并按实际状态处理
        async with get_session() as db:
            result = await db.exec(
                select(PackTask).where(PT.id == task_id, PT.owner_id == user_id)
            )
            task = result.first()
        if not task:
            logger.warning("取消/删除打包任务失败 user_id=%s task_id=%s reason=not_found_after_reload", user_id, task_id)
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")
        task_status = task.status

    if task_status in ("done", "failed", "cancelled"):
        # Clean up partial zip for failed/cancelled tasks
        if task_status in ("failed", "cancelled") and task.output_path:
            cleaned = cleanup_pack_output(Path(task.output_path))
            if cleaned:
                logger.info("Cleaned up partial pack file: %s", task.output_path)

        async with get_session() as db:
            result = await db.exec(select(PackTask).where(PackTask.id == task_id))
            db_task = result.first()
            if db_task:
                await db.delete(db_task)
        logger.info("删除打包任务记录成功 user_id=%s task_id=%s status=%s", user_id, task_id, task_status)
        return {"ok": True, "message": "任务已删除"}

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="无法处理该任务状态"
    )


@router.delete("/{file_hash}")
async def delete_file(
    file_hash: str,
    user: User = Depends(require_user),
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
        logger.warning("删除文件失败 user_id=%s file_hash=%s reason=not_found", user_id, file_hash)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="文件不存在"
        )

    user_file, _ = row
    # 检查是否有活跃分享
    async with get_session() as db:

        now_str = datetime.now(timezone.utc).isoformat()
        active_share_count = await db.scalar(
            select(func.count()).select_from(ShareLink).where(
                SL.user_file_id == user_file.id,
                SL.status == "active",
                or_(
                    SL.expires_at.is_(None),
                    SL.expires_at > now_str,
                ),
            )
        )
        if active_share_count and active_share_count > 0:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="该文件有活跃的分享链接，请先失效所有分享后再删除"
            )
    # Delete reference (handles ref_count and physical file cleanup)
    if user_file.id is None:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="文件记录异常")
    success = await delete_user_file_reference(user_file.id)
    if not success:
        logger.warning("删除文件失败 user_id=%s file_hash=%s reason=delete_reference_failed", user_id, file_hash)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="文件不存在"
        )

    logger.info("删除文件成功 user_id=%s file_hash=%s", user_id, file_hash)

    return {"ok": True}


@router.put("/{file_hash}/rename")
async def rename_file(
    file_hash: str,
    payload: RenameRequest,
    user: User = Depends(require_user),
) -> dict:
    """重命名文件
    只修改显示名称，不影响实际存储。

    Args:
        file_hash: 文件的 content_hash
    """
    user_id = _require_user_id(user)
    name = payload.name.strip()
    if not name:
        logger.warning("重命名文件失败 user_id=%s file_hash=%s reason=empty_name", user_id, file_hash)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="名称不能为空"
        )

    if "/" in name or "\\" in name:
        logger.warning("重命名文件失败 user_id=%s file_hash=%s reason=invalid_name", user_id, file_hash)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="名称不能包含路径分隔符"
        )

    if name in {".", ".."}:
        logger.warning("重命名文件失败 user_id=%s file_hash=%s reason=dot_name", user_id, file_hash)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="名称不合法"
        )

    if any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        logger.warning("重命名文件失败 user_id=%s file_hash=%s reason=control_char", user_id, file_hash)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="名称包含非法字符"
        )

    async with get_session() as db:
        result = await db.exec(
            select(UserFile, StoredFile)
            .join(StoredFile, UF.stored_file_id == SF.id)
            .where(
                SF.content_hash == file_hash,
                UF.owner_id == user_id,
            )
            .order_by(UF.id.asc())
        )
        row = result.first()
        if not row:
            logger.warning("重命名文件失败 user_id=%s file_hash=%s reason=not_found", user_id, file_hash)
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="文件不存在"
            )
        user_file, _ = row
        user_file.display_name = name
        db.add(user_file)

    logger.info("重命名文件成功 user_id=%s file_hash=%s", user_id, file_hash)

    return {"ok": True}


@router.get("/space")
async def get_space(user: User = Depends(require_user)) -> dict:
    """获取用户空间信息"""
    user_id = _require_user_id(user)
    space_info = await get_user_space_info(user_id, user.quota)
    logger.debug("查询空间信息 user_id=%s", user_id)
    return space_info



def _pack_task_to_dict(task: PackTask) -> dict:
    """Convert PackTask model to dict"""
    return {
        "id": task.id,
        "owner_id": task.owner_id,
        "folder_path": task.folder_path,
        "folder_size": task.folder_size,
        "reserved_space": task.reserved_space,
        "output_path": task.output_path,
        "output_name": task.output_name,
        "output_size": task.output_size,
        "stored_file_id": task.stored_file_id,
        "delete_source": task.delete_source,
        "status": task.status,
        "progress": task.progress,
        "error_message": task.error_message,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
    }


# Pack endpoints remain largely unchanged as they work with physical files
# These will be updated in a future iteration to work with StoredFile

@router.post("/pack/calculate-size")
async def calculate_paths_size(
    payload: CalculateSizeRequest,
    user: User = Depends(require_user)
) -> dict:
    """计算多个文件的总大小"""
    user_id = _require_user_id(user)
    resolved = await _resolve_file_ids(user_id, payload.file_ids)
    total_size = sum(size for _, size, _ in resolved)
    return {"total_size": total_size}


@router.get("/pack/available-space")
async def get_pack_available_space(
    user: User = Depends(require_user)
) -> dict:
    """获取用户可用于打包的空间"""
    from app.services.storage import get_user_space_info

    user_id = _require_user_id(user)
    info = await get_user_space_info(user_id, user.quota)
    return {
        "available": info["available"],
        "quota": info["quota"],
        "used": info["used"],
    }


@router.post("/pack", status_code=status.HTTP_201_CREATED)
async def create_pack_task(
    payload: PackRequest,
    user: User = Depends(require_user)
) -> dict:
    """创建打包任务 - 基于 UserFile ID"""
    user_id = _require_user_id(user)
    # 频率限制
    if not await api_limiter.is_allowed(user_id, "create_pack", limit=settings.rate_limit_create_pack, window_seconds=60):
        logger.warning("创建打包任务被限流 user_id=%s", user_id)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="操作过于频繁，请稍后再试"
        )

    from app.services.pack import PackTaskManager
    from app.services.storage import get_user_space_info

    # 解析文件 ID → 绝对路径
    resolved = await _resolve_file_ids(user_id, payload.file_ids)
    abs_paths = [path for path, _, _ in resolved]
    source_names = [name for _, _, name in resolved]
    total_size = sum(size for _, size, _ in resolved)

    if total_size == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="选中的文件为空"
        )

    folder_path_value = json.dumps(sorted(payload.file_ids))
    reserved_space = total_size

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
                detail="输出文件名不能超过 200 个字符"
            )
        _INVALID_CHARS = set('/\\:*?"<>|\0')
        if _INVALID_CHARS & set(output_name):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="输出文件名包含非法字符"
            )

    # Check available space + create task record atomically
    async with _get_pack_create_lock():
        # 检查是否已有相同源文件的已完成打包产物
        async with get_session() as db:
            result = await db.exec(
                select(PackTask).where(
                    PT.owner_id == user_id,
                    PT.folder_path == folder_path_value,
                    PT.status == "done",
                    PT.stored_file_id.is_not(None),
                )
            )
            done_tasks = result.all()
            for done_task in done_tasks:
                uf_result = await db.exec(
                    select(UserFile).where(
                        UF.owner_id == user_id,
                        UF.stored_file_id == done_task.stored_file_id,
                    )
                )
                user_file = uf_result.first()
                if user_file:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=f"已存在打包完成的文件「{user_file.display_name or '未知文件'}」"
                    )
            # 所有历史产物的 UserFile 均已删除，允许重新打包

        info = await get_user_space_info(user_id, user.quota)
        available = info["available"]
        if reserved_space > available:
            logger.warning(
                "创建打包任务失败 user_id=%s reason=insufficient_space required=%s available=%s",
                user_id,
                reserved_space,
                available,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"空间不足。需要: {reserved_space / 1024**3:.2f} GB, 可用: {available / 1024**3:.2f} GB"
            )

        async with get_session() as db:
            now = utc_now()
            existing_result = await db.exec(
                select(PackTask).where(
                    PT.owner_id == user_id,
                    PT.folder_path == folder_path_value,
                    PT.status.in_(["pending", "packing"]),
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
                    detail="相同文件已有进行中的打包任务"
                )

            pack_task_obj = PackTask(
                owner_id=user_id,
                folder_path=folder_path_value,
                folder_size=total_size,
                reserved_space=reserved_space,
                output_name=output_name,
                delete_source=payload.delete_source,
                status="pending",
                created_at=now,
                updated_at=now,
            )
            db.add(pack_task_obj)
            await db.flush()
            if pack_task_obj.id is None:
                raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="创建打包任务失败")
            task_id = int(pack_task_obj.id)

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

    async with get_session() as db:
        result = await db.exec(select(PackTask).where(PackTask.id == task_id))
        task = result.first()
        if not task:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="创建打包任务失败")
        return _pack_task_to_dict(task)


@router.get("/pack")
async def list_pack_tasks(user: User = Depends(require_user)) -> list[dict]:
    """列出用户的打包任务"""
    user_id = _require_user_id(user)
    async with get_session() as db:
        result = await db.exec(
            select(PackTask)
            .where(PT.owner_id == user_id)
            .order_by(PT.created_at.desc())
        )
        tasks = result.all()
        logger.debug("查询打包任务列表 user_id=%s count=%s", user_id, len(tasks))
        return [_pack_task_to_dict(t) for t in tasks]


@router.get("/pack/{task_id}")
async def get_pack_task(task_id: int, user: User = Depends(require_user)) -> dict:
    """获取打包任务详情"""
    user_id = _require_user_id(user)
    async with get_session() as db:
        result = await db.exec(
            select(PackTask).where(PT.id == task_id, PT.owner_id == user_id)
        )
        task = result.first()

    if not task:
        logger.warning("查询打包任务失败 user_id=%s task_id=%s reason=not_found", user_id, task_id)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")
    logger.debug("查询打包任务详情 user_id=%s task_id=%s", user_id, task_id)
    return _pack_task_to_dict(task)


# Legacy quota endpoint for backward compatibility
@router.get("/quota")
async def get_quota(user: User = Depends(require_user)) -> dict:
    """获取用户空间配额信息（兼容旧接口）"""
    user_id = _require_user_id(user)
    space_info = await get_user_space_info(user_id, user.quota)

    # Calculate percentage
    total = space_info["used"] + space_info["available"]
    percentage = (space_info["used"] / total * 100) if total > 0 else 0

    logger.debug("查询配额信息 user_id=%s", user_id)

    return {
        "used": space_info["used"],
        "total": total,
        "percentage": round(percentage, 2),
    }
