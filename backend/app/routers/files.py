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

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import text, update
from sqlmodel import select
from starlette.responses import StreamingResponse
from urllib.parse import quote

from app.auth import require_user
from app.core.config import settings
from app.core.rate_limit import api_limiter
from app.database import get_session
from app.models import User, PackTask, UserFile, StoredFile
from app.services.storage import (
    delete_user_file_reference,
    get_user_space_info,
)

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


# ========== Schemas ==========

class FileInfo(BaseModel):
    """文件信息"""
    id: int
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
    name: str


class PackRequest(BaseModel):
    """打包请求 - 基于 UserFile ID"""
    file_ids: list[int]
    output_name: str | None = None
    delete_source: bool = False


class CalculateSizeRequest(BaseModel):
    """计算大小请求 - 基于 UserFile ID"""
    file_ids: list[int]


# ========== Helpers ==========

def _user_file_to_dict(user_file: UserFile, stored_file: StoredFile) -> dict:
    """Convert UserFile + StoredFile to API response dict"""
    return {
        "id": user_file.id,
        "name": user_file.display_name,
        "size": stored_file.size,
        "is_directory": stored_file.is_directory,
        "created_at": user_file.created_at,
    }


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
        return base_path

    # Normalize and resolve
    target = (base_path / subpath).resolve()

    # Ensure it's within base path
    try:
        target.relative_to(base_path)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="无权访问此路径"
        )

    return target


def _range_file_response(request: Request, file_path: Path, filename: str):
    """支持 Range 请求的文件下载响应（多线程下载/断点续传）"""
    file_size = file_path.stat().st_size
    safe_name = filename.replace('"', '\\"')
    encoded_name = quote(filename)
    disposition = f"attachment; filename=\"{safe_name}\"; filename*=UTF-8''{encoded_name}"

    range_header = request.headers.get("range")
    if not range_header:
        return FileResponse(
            path=str(file_path),
            filename=filename,
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
            # Suffix range: bytes=-500 means last 500 bytes
            suffix_length = int(parts[1])
            start = max(0, file_size - suffix_length)
            end = file_size - 1
        else:
            start = int(parts[0])
            end = int(parts[1]) if parts[1] else file_size - 1
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
            .join(StoredFile, UserFile.stored_file_id == StoredFile.id)
            .where(UserFile.id.in_(file_ids), UserFile.owner_id == user_id)
        )
        pairs = result.all()

    if len(pairs) != len(file_ids):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="部分文件不存在或无权访问"
        )

    return [(sf.real_path, sf.size, uf.display_name) for uf, sf in pairs]


# ========== API Endpoints ==========

@router.get("", response_model=FileListResponse)
async def list_files(user: User = Depends(require_user)) -> FileListResponse:
    """列出用户的所有文件引用

    返回用户根目录下的所有文件/文件夹条目。
    """
    async with get_session() as db:
        result = await db.exec(
            select(UserFile, StoredFile)
            .join(StoredFile, UserFile.stored_file_id == StoredFile.id)
            .where(UserFile.owner_id == user.id)
            .order_by(UserFile.created_at.desc())
        )
        rows = result.all()

    files = [_user_file_to_dict(uf, sf) for uf, sf in rows]
    logger.debug("查询文件列表 user_id=%s count=%s", user.id, len(files))

    # Get space info
    space_info = await get_user_space_info(user.id, user.quota)

    return FileListResponse(
        files=files,
        space={
            "used": space_info["used"],
            "frozen": space_info["frozen"],
            "available": space_info["available"],
        }
    )


@router.get("/{file_id}/browse")
async def browse_file(
    file_id: int,
    path: str = "",
    user: User = Depends(require_user),
) -> list[dict]:
    """浏览 BT 文件夹内容

    Args:
        file_id: UserFile ID
        path: 文件夹内的相对路径
    """
    # Get user file and stored file
    async with get_session() as db:
        result = await db.exec(
            select(UserFile, StoredFile)
            .join(StoredFile, UserFile.stored_file_id == StoredFile.id)
            .where(
                UserFile.id == file_id,
                UserFile.owner_id == user.id,
            )
        )
        row = result.first()

    if not row:
        logger.warning("浏览文件失败 user_id=%s file_id=%s reason=not_found", user.id, file_id)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="文件不存在"
        )

    user_file, stored_file = row

    if not stored_file.is_directory:
        logger.warning("浏览文件失败 user_id=%s file_id=%s reason=not_directory", user.id, file_id)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="此文件不是文件夹"
        )

    # Validate and resolve path
    base_path = Path(stored_file.real_path)
    if not base_path.exists():
        logger.warning("浏览文件失败 user_id=%s file_id=%s reason=base_missing", user.id, file_id)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="文件夹不存在"
        )

    target_path = _validate_subpath(base_path, path)

    if not target_path.exists():
        logger.warning("浏览文件失败 user_id=%s file_id=%s reason=path_missing", user.id, file_id)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="路径不存在"
        )

    if not target_path.is_dir():
        logger.warning("浏览文件失败 user_id=%s file_id=%s reason=path_not_directory", user.id, file_id)
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
            except Exception:
                continue
    except PermissionError:
        logger.warning("浏览文件失败 user_id=%s file_id=%s reason=permission_denied", user.id, file_id)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="无权访问此目录"
        )

    logger.debug("浏览文件成功 user_id=%s file_id=%s count=%s", user.id, file_id, len(files))

    return files


@router.get("/{file_id}/download")
async def download_file(
    file_id: int,
    request: Request,
    path: str = "",
    user: User = Depends(require_user),
):
    """下载文件

    支持下载整个文件或 BT 文件夹内的单个文件。

    Args:
        file_id: UserFile ID
        path: BT 文件夹内的相对路径（可选）
    """
    if not await api_limiter.is_allowed(user.id, "download_file", limit=60, window_seconds=60):
        logger.warning("下载文件被限流 user_id=%s file_id=%s", user.id, file_id)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="下载请求过于频繁，请稍后再试"
        )
    # Get user file and stored file
    async with get_session() as db:
        result = await db.exec(
            select(UserFile, StoredFile)
            .join(StoredFile, UserFile.stored_file_id == StoredFile.id)
            .where(
                UserFile.id == file_id,
                UserFile.owner_id == user.id,
            )
        )
        row = result.first()

    if not row:
        logger.warning("下载文件失败 user_id=%s file_id=%s reason=not_found", user.id, file_id)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="文件不存在"
        )

    user_file, stored_file = row
    base_path = Path(stored_file.real_path)

    if not base_path.exists():
        logger.warning("下载文件失败 user_id=%s file_id=%s reason=base_missing", user.id, file_id)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="文件不存在"
        )

    # Determine target file
    if path:
        if not stored_file.is_directory:
            logger.warning("下载文件失败 user_id=%s file_id=%s reason=path_on_non_dir", user.id, file_id)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="此文件不是文件夹，不支持路径参数"
            )
        target_path = _validate_subpath(base_path, path)
    else:
        target_path = base_path

    if not target_path.exists():
        logger.warning("下载文件失败 user_id=%s file_id=%s reason=target_missing", user.id, file_id)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="文件不存在"
        )

    if target_path.is_dir():
        logger.warning("下载文件失败 user_id=%s file_id=%s reason=target_is_directory", user.id, file_id)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="不能直接下载文件夹，请选择具体文件"
        )

    logger.info("下载文件成功 user_id=%s file_id=%s file=%s", user.id, file_id, target_path.name)

    return _range_file_response(request, target_path, target_path.name)


@router.delete("/pack")
async def clear_finished_pack_tasks(
    user: User = Depends(require_user),
) -> dict:
    """一键清空已完成/失败/取消的打包任务记录"""
    terminal_statuses = ["done", "failed", "cancelled"]
    async with get_session() as db:
        result = await db.exec(
            select(PackTask).where(
                PackTask.owner_id == user.id,
                PackTask.status.in_(terminal_statuses),
            )
        )
        tasks = result.all()

        count = 0
        for task in tasks:
            # failed/cancelled 任务可能有残留的半成品文件
            if task.status in ("failed", "cancelled") and task.output_path:
                output = Path(task.output_path)
                if output.exists():
                    output.unlink()
            await db.delete(task)
            count += 1

    return {"ok": True, "count": count}


@router.delete("/pack/{task_id}")
async def cancel_or_delete_pack_task(
    task_id: int,
    user: User = Depends(require_user)
) -> dict:
    """取消或删除打包任务"""
    from app.services.pack import PackTaskManager

    async with get_session() as db:
        result = await db.exec(
            select(PackTask).where(PackTask.id == task_id, PackTask.owner_id == user.id)
        )
        task = result.first()

    if not task:
        logger.warning("取消/删除打包任务失败 user_id=%s task_id=%s reason=not_found", user.id, task_id)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")

    task_status = task.status

    if task_status in ("pending", "packing"):
        await PackTaskManager.cancel_pack(task_id)
        async with get_session() as db:
            result = await db.execute(
                update(PackTask)
                .where(
                    PackTask.id == task_id,
                    PackTask.status.in_(["pending", "packing"]),
                )
                .values(
                    status="cancelled",
                    reserved_space=0,
                    updated_at=utc_now()
                )
            )
            cancelled = result.rowcount > 0

        if cancelled:
            logger.info("取消打包任务成功 user_id=%s task_id=%s", user.id, task_id)
            return {"ok": True, "message": "任务已取消"}

        # 状态已变化，重新读取并按实际状态处理
        async with get_session() as db:
            result = await db.exec(
                select(PackTask).where(PackTask.id == task_id, PackTask.owner_id == user.id)
            )
            task = result.first()
        if not task:
            logger.warning("取消/删除打包任务失败 user_id=%s task_id=%s reason=not_found_after_reload", user.id, task_id)
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")
        task_status = task.status

    if task_status in ("done", "failed", "cancelled"):
        # Clean up partial zip for failed/cancelled tasks
        if task_status in ("failed", "cancelled") and task.output_path:
            output_file = Path(task.output_path)
            if output_file.exists():
                try:
                    output_file.unlink()
                    logger.info("Cleaned up partial pack file: %s", task.output_path)
                except Exception as e:
                    logger.warning("Failed to clean up pack file %s: %s", task.output_path, e)

        async with get_session() as db:
            result = await db.exec(select(PackTask).where(PackTask.id == task_id))
            db_task = result.first()
            if db_task:
                await db.delete(db_task)
        logger.info("删除打包任务记录成功 user_id=%s task_id=%s status=%s", user.id, task_id, task_status)
        return {"ok": True, "message": "任务已删除"}

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="无法处理该任务状态"
    )


@router.delete("/{file_id}")
async def delete_file(
    file_id: int,
    user: User = Depends(require_user),
) -> dict:
    """删除文件引用

    只能删除根目录的整个文件/文件夹引用。
    如果是最后一个引用，物理文件也会被删除。
    """
    # Verify ownership
    async with get_session() as db:
        result = await db.exec(
            select(UserFile).where(
                UserFile.id == file_id,
                UserFile.owner_id == user.id,
            )
        )
        user_file = result.first()

    if not user_file:
        logger.warning("删除文件失败 user_id=%s file_id=%s reason=not_found", user.id, file_id)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="文件不存在"
        )

    # Delete reference (handles ref_count and physical file cleanup)
    success = await delete_user_file_reference(file_id)

    if not success:
        logger.warning("删除文件失败 user_id=%s file_id=%s reason=delete_reference_failed", user.id, file_id)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="文件不存在"
        )

    logger.info("删除文件成功 user_id=%s file_id=%s", user.id, file_id)

    return {"ok": True}


@router.put("/{file_id}/rename")
async def rename_file(
    file_id: int,
    payload: RenameRequest,
    user: User = Depends(require_user),
) -> dict:
    """重命名文件

    只修改显示名称，不影响实际存储。
    """
    if not payload.name:
        logger.warning("重命名文件失败 user_id=%s file_id=%s reason=empty_name", user.id, file_id)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="名称不能为空"
        )

    # Validate name
    if "/" in payload.name or "\\" in payload.name:
        logger.warning("重命名文件失败 user_id=%s file_id=%s reason=invalid_name", user.id, file_id)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="名称不能包含路径分隔符"
        )

    async with get_session() as db:
        result = await db.exec(
            select(UserFile).where(
                UserFile.id == file_id,
                UserFile.owner_id == user.id,
            )
        )
        user_file = result.first()

        if not user_file:
            logger.warning("重命名文件失败 user_id=%s file_id=%s reason=not_found", user.id, file_id)
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="文件不存在"
            )

        user_file.display_name = payload.name
        db.add(user_file)

    logger.info("重命名文件成功 user_id=%s file_id=%s", user.id, file_id)

    return {"ok": True}


@router.get("/space")
async def get_space(user: User = Depends(require_user)) -> dict:
    """获取用户空间信息"""
    space_info = await get_user_space_info(user.id, user.quota)
    logger.debug("查询空间信息 user_id=%s", user.id)
    return space_info


# ========== Legacy Pack Endpoints (kept for compatibility) ==========
# Legacy helper - still used by download_pack_result

def _get_user_dir(user_id: int) -> Path:
    """获取用户目录的 Path 对象（兼容旧代码）"""
    base = Path(settings.download_dir).resolve()
    user_dir = base / str(user_id)
    user_dir.mkdir(parents=True, exist_ok=True)
    return user_dir


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
    resolved = await _resolve_file_ids(user.id, payload.file_ids)
    total_size = sum(size for _, size, _ in resolved)
    return {"total_size": total_size}


@router.get("/pack/available-space")
async def get_pack_available_space(
    user: User = Depends(require_user)
) -> dict:
    """获取用户可用于打包的空间"""
    from app.services.storage import get_user_space_info

    info = await get_user_space_info(user.id, user.quota)
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
    # 频率限制
    if not await api_limiter.is_allowed(user.id, "create_pack", limit=5, window_seconds=60):
        logger.warning("创建打包任务被限流 user_id=%s", user.id)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="操作过于频繁，请稍后再试"
        )

    from app.services.pack import PackTaskManager
    from app.services.storage import get_user_space_info

    # 解析文件 ID → 绝对路径
    resolved = await _resolve_file_ids(user.id, payload.file_ids)
    abs_paths = [path for path, _, _ in resolved]
    total_size = sum(size for _, size, _ in resolved)

    if total_size == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="选中的文件为空"
        )

    folder_path_value = json.dumps(payload.file_ids)
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
        info = await get_user_space_info(user.id, user.quota)
        available = info["available"]
        if reserved_space > available:
            logger.warning(
                "创建打包任务失败 user_id=%s reason=insufficient_space required=%s available=%s",
                user.id,
                reserved_space,
                available,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"空间不足。需要: {reserved_space / 1024**3:.2f} GB, 可用: {available / 1024**3:.2f} GB"
            )

        async with get_session() as db:
            now = utc_now()
            result = await db.execute(
                text(
                    """
                    INSERT INTO pack_tasks (
                        owner_id, folder_path, folder_size, reserved_space,
                        output_name, delete_source, status, created_at, updated_at
                    )
                    SELECT
                        :owner_id, :folder_path, :folder_size, :reserved_space,
                        :output_name, :delete_source, 'pending', :created_at, :updated_at
                    WHERE NOT EXISTS (
                        SELECT 1 FROM pack_tasks
                        WHERE owner_id = :owner_id
                          AND folder_path = :folder_path
                          AND status IN ('pending', 'packing')
                    )
                    """
                ),
                {
                    "owner_id": user.id,
                    "folder_path": folder_path_value,
                    "folder_size": total_size,
                    "reserved_space": reserved_space,
                    "output_name": output_name,
                    "delete_source": payload.delete_source,
                    "created_at": now,
                    "updated_at": now,
                },
            )

            if result.rowcount == 0:
                logger.warning(
                    "创建打包任务冲突 user_id=%s file_ids=%s",
                    user.id,
                    payload.file_ids,
                )
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="相同文件已有进行中的打包任务"
                )

            result = await db.exec(
                select(PackTask)
                .where(
                    PackTask.owner_id == user.id,
                    PackTask.folder_path == folder_path_value,
                    PackTask.status == "pending",
                )
                .order_by(PackTask.id.desc())
            )
            pack_task = result.first()
            if not pack_task:
                logger.error("创建打包任务失败 user_id=%s reason=task_not_found_after_insert", user.id)
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="创建打包任务失败"
                )
            task_id = pack_task.id

    # Start async packing - pass absolute paths directly
    asyncio.create_task(PackTaskManager.start_pack(task_id, user.id, abs_paths, payload.file_ids, output_name, payload.delete_source))
    logger.info("创建打包任务成功 user_id=%s task_id=%s", user.id, task_id)

    async with get_session() as db:
        result = await db.exec(select(PackTask).where(PackTask.id == task_id))
        task = result.first()
        return _pack_task_to_dict(task)


@router.get("/pack")
async def list_pack_tasks(user: User = Depends(require_user)) -> list[dict]:
    """列出用户的打包任务"""
    async with get_session() as db:
        result = await db.exec(
            select(PackTask)
            .where(PackTask.owner_id == user.id)
            .order_by(PackTask.created_at.desc())
        )
        tasks = result.all()
        logger.debug("查询打包任务列表 user_id=%s count=%s", user.id, len(tasks))
        return [_pack_task_to_dict(t) for t in tasks]


@router.get("/pack/{task_id}")
async def get_pack_task(task_id: int, user: User = Depends(require_user)) -> dict:
    """获取打包任务详情"""
    async with get_session() as db:
        result = await db.exec(
            select(PackTask).where(PackTask.id == task_id, PackTask.owner_id == user.id)
        )
        task = result.first()

    if not task:
        logger.warning("查询打包任务失败 user_id=%s task_id=%s reason=not_found", user.id, task_id)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")
    logger.debug("查询打包任务详情 user_id=%s task_id=%s", user.id, task_id)
    return _pack_task_to_dict(task)


# Legacy quota endpoint for backward compatibility
@router.get("/quota")
async def get_quota(user: User = Depends(require_user)) -> dict:
    """获取用户空间配额信息（兼容旧接口）"""
    space_info = await get_user_space_info(user.id, user.quota)

    # Calculate percentage
    total = space_info["used"] + space_info["available"]
    percentage = (space_info["used"] / total * 100) if total > 0 else 0

    logger.debug("查询配额信息 user_id=%s", user.id)

    return {
        "used": space_info["used"],
        "total": total,
        "percentage": round(percentage, 2),
    }
