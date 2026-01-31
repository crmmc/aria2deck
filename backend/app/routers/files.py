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

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import text, update
from sqlmodel import select

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
    """打包请求"""
    folder_path: str | None = None
    paths: list[str] | None = None
    output_name: str | None = None


class CalculateSizeRequest(BaseModel):
    """计算大小请求"""
    paths: list[str]


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
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="文件不存在"
        )

    user_file, stored_file = row

    if not stored_file.is_directory:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="此文件不是文件夹"
        )

    # Validate and resolve path
    base_path = Path(stored_file.real_path)
    if not base_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="文件夹不存在"
        )

    target_path = _validate_subpath(base_path, path)

    if not target_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="路径不存在"
        )

    if not target_path.is_dir():
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
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="无权访问此目录"
        )

    return files


@router.get("/{file_id}/download")
async def download_file(
    file_id: int,
    path: str = "",
    user: User = Depends(require_user),
) -> FileResponse:
    """下载文件

    支持下载整个文件或 BT 文件夹内的单个文件。

    Args:
        file_id: UserFile ID
        path: BT 文件夹内的相对路径（可选）
    """
    if not await api_limiter.is_allowed(user.id, "download_file", limit=60, window_seconds=60):
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
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="文件不存在"
        )

    user_file, stored_file = row
    base_path = Path(stored_file.real_path)

    if not base_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="文件不存在"
        )

    # Determine target file
    if path:
        if not stored_file.is_directory:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="此文件不是文件夹，不支持路径参数"
            )
        target_path = _validate_subpath(base_path, path)
    else:
        target_path = base_path

    if not target_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="文件不存在"
        )

    if target_path.is_dir():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="不能直接下载文件夹，请选择具体文件"
        )

    return FileResponse(
        path=str(target_path),
        filename=target_path.name,
        media_type="application/octet-stream"
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
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="文件不存在"
        )

    # Delete reference (handles ref_count and physical file cleanup)
    success = await delete_user_file_reference(file_id)

    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="文件不存在"
        )

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
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="名称不能为空"
        )

    # Validate name
    if "/" in payload.name or "\\" in payload.name:
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
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="文件不存在"
            )

        user_file.display_name = payload.name
        db.add(user_file)

    return {"ok": True}


@router.get("/space")
async def get_space(user: User = Depends(require_user)) -> dict:
    """获取用户空间信息"""
    space_info = await get_user_space_info(user.id, user.quota)
    return space_info


# ========== Legacy Pack Endpoints (kept for compatibility) ==========
# These endpoints work with the old filesystem-based approach
# and will be deprecated in favor of the new architecture

def _get_user_dir(user_id: int) -> Path:
    """获取用户目录的 Path 对象（兼容旧代码）"""
    base = Path(settings.download_dir).resolve()
    user_dir = base / str(user_id)
    user_dir.mkdir(parents=True, exist_ok=True)
    return user_dir


def _validate_path(user_dir: Path, relative_path: str) -> Path:
    """验证路径安全性（兼容旧代码）"""
    if not relative_path:
        return user_dir

    target = (user_dir / relative_path).resolve()

    try:
        target.relative_to(user_dir)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="无权访问此路径"
        )

    if target.exists() and target.is_symlink():
        real_target = target.resolve()
        try:
            real_target.relative_to(user_dir)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="无权访问此路径"
            )

    return target


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
    """计算多个文件/文件夹的总大小"""
    from app.services.pack import calculate_folder_size, get_user_available_space_for_pack

    if not payload.paths:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="路径列表不能为空"
        )

    user_dir = _get_user_dir(user.id)
    total_size = 0

    for path in payload.paths:
        # 禁止访问 .incomplete 目录
        if path == ".incomplete" or path.startswith(".incomplete/"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="无权访问此文件"
            )

        target = _validate_path(user_dir, path)
        if not target.exists():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"路径不存在: {path}"
            )
        if target.is_dir():
            total_size += calculate_folder_size(target)
        else:
            total_size += target.stat().st_size

    available = await get_user_available_space_for_pack(user.id)

    return {
        "total_size": total_size,
        "user_available": available,
    }


@router.get("/pack/available-space")
async def get_pack_available_space(
    folder_path: str | None = None,
    user: User = Depends(require_user)
) -> dict:
    """获取用户可用于打包的空间"""
    from app.services.pack import (
        calculate_folder_size,
        get_server_available_space,
        get_user_available_space_for_pack,
    )

    user_available = await get_user_available_space_for_pack(user.id)
    server_available = await get_server_available_space()

    result = {
        "user_available": user_available,
        "server_available": server_available,
    }

    if folder_path:
        user_dir = _get_user_dir(user.id)
        target = _validate_path(user_dir, folder_path)
        if target.exists() and target.is_dir():
            result["folder_size"] = calculate_folder_size(target)
        else:
            result["folder_size"] = 0

    return result


@router.post("/pack", status_code=status.HTTP_201_CREATED)
async def create_pack_task(
    payload: PackRequest,
    user: User = Depends(require_user)
) -> dict:
    """创建打包任务

    支持两种模式：
    1. 单文件夹打包：提供 folder_path
    2. 多文件打包：提供 paths 列表
    """
    # 频率限制
    if not await api_limiter.is_allowed(user.id, "create_pack", limit=5, window_seconds=60):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="操作过于频繁，请稍后再试"
        )

    from app.services.pack import (
        PackTaskManager, calculate_folder_size,
        get_user_available_space_for_pack
    )

    user_dir = _get_user_dir(user.id)

    # 确定打包路径列表
    if payload.paths and len(payload.paths) > 0:
        paths = payload.paths
        is_multi = True
    elif payload.folder_path:
        paths = [payload.folder_path]
        is_multi = False
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="请提供 folder_path 或 paths"
        )

    # 验证所有路径并计算总大小
    total_size = 0
    for path in paths:
        if path == ".incomplete" or path.startswith(".incomplete/"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="无权访问此文件"
            )

        target = _validate_path(user_dir, path)
        if not target.exists():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"路径不存在: {path}"
            )
        if target.is_dir():
            total_size += calculate_folder_size(target)
        else:
            total_size += target.stat().st_size

    if total_size == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="选中的文件/文件夹为空"
        )

    folder_path_value = json.dumps(paths) if is_multi else paths[0]

    reserved_space = total_size

    # 验证输出文件名
    output_name = payload.output_name
    if output_name and ("/" in output_name or "\\" in output_name):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="输出文件名不能包含路径分隔符"
        )

    # Check available space + create task record atomically (avoid concurrent oversell)
    async with _get_pack_create_lock():
        available = await get_user_available_space_for_pack(user.id)
        if reserved_space > available:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"空间不足。需要: {reserved_space / 1024**3:.2f} GB, 可用: {available / 1024**3:.2f} GB"
            )

        # Create task record atomically (avoid concurrent duplicates)
        async with get_session() as db:
            now = utc_now()
            result = await db.execute(
                text(
                    """
                    INSERT INTO pack_tasks (
                        owner_id, folder_path, folder_size, reserved_space,
                        output_name, status, created_at, updated_at
                    )
                    SELECT
                        :owner_id, :folder_path, :folder_size, :reserved_space,
                        :output_name, 'pending', :created_at, :updated_at
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
                    "created_at": now,
                    "updated_at": now,
                },
            )

            if result.rowcount == 0:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="相同路径已有进行中的打包任务"
                )

            # Fetch newly created task
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
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="创建打包任务失败"
                )
            task_id = pack_task.id

    # Start async packing
    asyncio.create_task(PackTaskManager.start_pack(task_id, user.id, folder_path_value, output_name))

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
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")
    return _pack_task_to_dict(task)


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
            return {"ok": True, "message": "任务已取消"}

        # 状态已变化，重新读取并按实际状态处理
        async with get_session() as db:
            result = await db.exec(
                select(PackTask).where(PackTask.id == task_id, PackTask.owner_id == user.id)
            )
            task = result.first()
        if not task:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")
        task_status = task.status

    if task_status in ("done", "failed", "cancelled"):
        async with get_session() as db:
            result = await db.exec(select(PackTask).where(PackTask.id == task_id))
            db_task = result.first()
            if db_task:
                await db.delete(db_task)
        return {"ok": True, "message": "任务已删除"}

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="无法处理该任务状态"
    )


@router.get("/pack/{task_id}/download")
async def download_pack_result(task_id: int, user: User = Depends(require_user)) -> FileResponse:
    """下载已完成的打包文件"""
    async with get_session() as db:
        result = await db.exec(
            select(PackTask).where(PackTask.id == task_id, PackTask.owner_id == user.id)
        )
        task = result.first()

    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")

    if task.status != "done":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="打包任务未完成"
        )

    output_path = task.output_path
    if not output_path or not Path(output_path).exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="打包文件不存在")

    # Path traversal protection: ensure output_path is within user directory
    user_dir = _get_user_dir(user.id)
    output_path_resolved = Path(output_path).resolve()
    try:
        output_path_resolved.relative_to(user_dir)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="无权访问此文件"
        )

    return FileResponse(
        path=output_path,
        filename=Path(output_path).name,
        media_type="application/octet-stream"
    )


# Legacy quota endpoint for backward compatibility
@router.get("/quota")
async def get_quota(user: User = Depends(require_user)) -> dict:
    """获取用户空间配额信息（兼容旧接口）"""
    space_info = await get_user_space_info(user.id, user.quota)

    # Calculate percentage
    total = space_info["used"] + space_info["available"]
    percentage = (space_info["used"] / total * 100) if total > 0 else 0

    return {
        "used": space_info["used"],
        "total": total,
        "percentage": round(percentage, 2),
    }
