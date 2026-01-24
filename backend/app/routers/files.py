"""用户文件管理接口模块

提供用户目录下的文件查看、下载、删除、重命名等功能。
包含路径安全验证和空间配额管理。
"""
from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from time import time

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.auth import require_user
from app.core.config import settings


router = APIRouter(prefix="/api/files", tags=["files"])


# ========== Cache ==========

_dir_size_cache: dict[str, tuple[int, float]] = {}
_DIR_SIZE_CACHE_TTL = 30.0  # 缓存有效期（秒）


# ========== Schemas ==========

class FileInfo(BaseModel):
    """文件信息"""
    name: str
    path: str  # 相对于用户目录的路径
    is_dir: bool
    size: int  # 字节
    modified_at: float  # Unix 时间戳


class FileListResponse(BaseModel):
    """文件列表响应"""
    current_path: str
    parent_path: str | None
    files: list[FileInfo]


class RenameRequest(BaseModel):
    """重命名请求"""
    old_path: str
    new_name: str


class QuotaResponse(BaseModel):
    """配额信息响应"""
    used: int  # 已使用空间（字节）
    total: int  # 总配额（字节）
    percentage: float  # 使用百分比


class PackRequest(BaseModel):
    """创建打包任务请求"""
    folder_path: str | None = None  # 单文件夹路径（向后兼容）
    paths: list[str] | None = None  # 多文件/文件夹路径
    output_name: str | None = None  # 自定义输出文件名（不含扩展名）


class PackTaskResponse(BaseModel):
    """打包任务响应"""
    id: int
    owner_id: int
    folder_path: str
    folder_size: int
    reserved_space: int
    output_path: str | None
    output_size: int | None
    status: str
    progress: int
    error_message: str | None
    created_at: str
    updated_at: str


# ========== Helpers ==========

def _get_user_dir(user_id: int) -> Path:
    """获取用户目录的 Path 对象"""
    base = Path(settings.download_dir).resolve()
    user_dir = base / str(user_id)
    user_dir.mkdir(parents=True, exist_ok=True)
    return user_dir


def _validate_path(user_dir: Path, relative_path: str) -> Path:
    """验证路径安全性，防止路径遍历攻击

    Args:
        user_dir: 用户根目录
        relative_path: 用户提供的相对路径

    Returns:
        验证后的绝对路径

    Raises:
        HTTPException: 路径不合法或超出边界
    """
    if not relative_path:
        return user_dir

    # 规范化路径
    target = (user_dir / relative_path).resolve()

    # 确保目标路径在用户目录内
    try:
        target.relative_to(user_dir)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="无权访问此路径"
        )

    # 检查符号链接是否指向用户目录外
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


def _calculate_dir_size(path: Path) -> int:
    """递归计算目录大小（字节），带缓存"""
    key = str(path)
    now = time()
    if key in _dir_size_cache:
        size, ts = _dir_size_cache[key]
        if now - ts < _DIR_SIZE_CACHE_TTL:
            return size

    total = 0
    try:
        for entry in path.rglob("*"):
            if entry.is_file():
                total += entry.stat().st_size
    except Exception:
        pass

    _dir_size_cache[key] = (total, now)
    return total


def _invalidate_dir_size_cache(user_dir: Path) -> None:
    """清除用户目录的大小缓存"""
    key = str(user_dir)
    if key in _dir_size_cache:
        del _dir_size_cache[key]


def _get_user_quota(user_id: int) -> int:
    """从数据库获取用户配额（字节）"""
    from app.db import fetch_one
    user = fetch_one("SELECT quota FROM users WHERE id = ?", [user_id])
    if user and user.get("quota"):
        return user["quota"]
    # 默认 100GB
    return 100 * 1024 * 1024 * 1024


# ========== API Endpoints ==========

@router.get("", response_model=FileListResponse)
def list_files(path: str = "", user: dict = Depends(require_user)) -> FileListResponse:
    """列出用户目录下的文件和文件夹

    Args:
        path: 相对路径（可选），默认为根目录
    """
    from app.routers.config import get_hidden_file_extensions

    # 禁止访问 .incomplete 目录
    if path == ".incomplete" or path.startswith(".incomplete/"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="无权访问此目录"
        )

    user_dir = _get_user_dir(user["id"])
    target_dir = _validate_path(user_dir, path)
    
    if not target_dir.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="目录不存在"
        )
    
    if not target_dir.is_dir():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="路径不是目录"
        )
    
    # 获取隐藏的文件后缀名列表
    hidden_extensions = get_hidden_file_extensions()
    
    # 获取文件列表
    files: list[FileInfo] = []
    try:
        for entry in sorted(target_dir.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            try:
                # 隐藏 .incomplete 目录（下载中的文件存放位置）
                if entry.name == ".incomplete":
                    continue

                # 检查是否应该隐藏该文件
                if entry.is_file() and hidden_extensions:
                    file_ext = entry.suffix.lower()
                    if file_ext in hidden_extensions:
                        continue  # 跳过黑名单中的文件
                
                stat = entry.stat()
                relative = entry.relative_to(user_dir)
                files.append(FileInfo(
                    name=entry.name,
                    path=str(relative),
                    is_dir=entry.is_dir(),
                    size=stat.st_size if entry.is_file() else 0,
                    modified_at=stat.st_mtime
                ))
            except Exception:
                continue
    except PermissionError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="无权访问此目录"
        )
    
    # 计算父目录路径
    parent_path = None
    if target_dir != user_dir:
        parent = target_dir.parent.relative_to(user_dir)
        parent_path = str(parent) if str(parent) != "." else ""
    
    return FileListResponse(
        current_path=path,
        parent_path=parent_path,
        files=files
    )


@router.get("/download")
def download_file(path: str, user: dict = Depends(require_user)) -> FileResponse:
    """下载文件

    Args:
        path: 文件的相对路径
    """
    if not path:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="缺少文件路径"
        )

    # 禁止访问 .incomplete 目录
    if path == ".incomplete" or path.startswith(".incomplete/"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="无权访问此文件"
        )

    user_dir = _get_user_dir(user["id"])
    target_file = _validate_path(user_dir, path)
    
    if not target_file.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="文件不存在"
        )
    
    if not target_file.is_file():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="路径不是文件"
        )
    
    return FileResponse(
        path=str(target_file),
        filename=target_file.name,
        media_type="application/octet-stream"
    )


@router.delete("")
def delete_file(path: str, user: dict = Depends(require_user)) -> dict:
    """删除文件或文件夹

    Args:
        path: 文件/文件夹的相对路径
    """
    if not path:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="缺少路径"
        )

    # 禁止访问 .incomplete 目录
    if path == ".incomplete" or path.startswith(".incomplete/"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="无权删除此文件"
        )

    # 检查文件是否正在被打包
    from app.db import fetch_all
    import json
    active_pack_tasks = fetch_all(
        "SELECT folder_path FROM pack_tasks WHERE owner_id = ? AND status IN ('pending', 'packing')",
        [user["id"]]
    )
    for task in active_pack_tasks:
        folder_path = task["folder_path"]
        # 检查单文件或多文件打包
        if folder_path.startswith("["):
            try:
                paths = json.loads(folder_path)
                for p in paths:
                    if path == p or path.startswith(p + "/") or p.startswith(path + "/"):
                        raise HTTPException(
                            status_code=status.HTTP_409_CONFLICT,
                            detail="文件正在被打包，无法删除"
                        )
            except json.JSONDecodeError:
                pass
        else:
            if path == folder_path or path.startswith(folder_path + "/") or folder_path.startswith(path + "/"):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="文件正在被打包，无法删除"
                )

    user_dir = _get_user_dir(user["id"])
    target = _validate_path(user_dir, path)
    
    if not target.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="文件或目录不存在"
        )
    
    # 不允许删除用户根目录
    if target == user_dir:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="不能删除根目录"
        )
    
    try:
        # 再次检查符号链接（防止 TOCTOU 攻击）
        if target.is_symlink():
            # 只删除符号链接本身，不跟随
            target.unlink()
        elif target.is_dir():
            # 删除目录前检查是否包含指向外部的符号链接
            for item in target.rglob("*"):
                if item.is_symlink():
                    real_path = item.resolve()
                    try:
                        real_path.relative_to(user_dir)
                    except ValueError:
                        raise HTTPException(
                            status_code=status.HTTP_403_FORBIDDEN,
                            detail="目录包含指向外部的符号链接，无法删除"
                        )
            shutil.rmtree(target)
        else:
            target.unlink()

            # 检查并删除对应的 .aria2 控制文件（静默）
            aria2_file = target.parent / f"{target.name}.aria2"
            if aria2_file.exists() and aria2_file.is_file():
                try:
                    aria2_file.unlink()
                except Exception:
                    pass  # 静默失败，不影响主文件删除

        # 清除目录大小缓存，确保 quota 立即更新
        _invalidate_dir_size_cache(user_dir)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"删除失败: {exc}"
        )
    
    return {"ok": True, "message": "删除成功"}


@router.put("/rename")
def rename_file(payload: RenameRequest, user: dict = Depends(require_user)) -> dict:
    """重命名文件或文件夹

    Args:
        payload: 包含旧路径和新名称
    """
    if not payload.old_path or not payload.new_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="缺少必要参数"
        )

    # 禁止访问 .incomplete 目录
    if payload.old_path == ".incomplete" or payload.old_path.startswith(".incomplete/"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="无权操作此文件"
        )

    # 验证新名称不包含路径分隔符
    if "/" in payload.new_name or "\\" in payload.new_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="新名称不能包含路径分隔符"
        )
    
    user_dir = _get_user_dir(user["id"])
    old_path = _validate_path(user_dir, payload.old_path)
    
    if not old_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="文件或目录不存在"
        )
    
    # 不允许重命名用户根目录
    if old_path == user_dir:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="不能重命名根目录"
        )
    
    # 计算新路径
    new_path = old_path.parent / payload.new_name
    
    # 验证新路径也在用户目录内
    _validate_path(user_dir, str(new_path.relative_to(user_dir)))
    
    if new_path.exists():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="目标名称已存在"
        )
    
    try:
        old_path.rename(new_path)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"重命名失败: {exc}"
        )
    
    return {"ok": True, "message": "重命名成功", "new_path": str(new_path.relative_to(user_dir))}


@router.get("/quota", response_model=QuotaResponse)
def get_quota(user: dict = Depends(require_user)) -> QuotaResponse:
    """获取用户空间配额信息（考虑机器空间限制）"""
    user_dir = _get_user_dir(user["id"])
    
    # 计算已使用空间
    used = _calculate_dir_size(user_dir)
    
    # 从数据库获取用户配额
    user_quota = _get_user_quota(user["id"])
    
    # 获取机器实际剩余空间
    download_path = Path(settings.download_dir)
    download_path.mkdir(parents=True, exist_ok=True)
    disk = shutil.disk_usage(download_path)
    machine_free = disk.free
    
    # 用户理论可用空间（基于配额）
    user_free_by_quota = max(0, user_quota - used)
    
    # 判断是否受机器空间限制
    is_limited = machine_free < user_free_by_quota
    
    # 动态调整显示的总空间：
    # - 如果受限：总空间 = 已使用 + 机器剩余空间
    # - 如果不受限：总空间 = 用户配额
    display_total = used + machine_free if is_limited else user_quota
    
    # 计算百分比
    percentage = (used / display_total * 100) if display_total > 0 else 0
    
    return QuotaResponse(
        used=used,
        total=display_total,
        percentage=round(percentage, 2)
    )


# ========== Pack Endpoints ==========

class CalculateSizeRequest(BaseModel):
    """计算多文件大小请求"""
    paths: list[str]


@router.post("/pack/calculate-size")
def calculate_paths_size(
    payload: CalculateSizeRequest,
    user: dict = Depends(require_user)
) -> dict:
    """计算多个文件/文件夹的总大小"""
    from app.services.pack import calculate_folder_size, get_user_available_space_for_pack

    if not payload.paths:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="路径列表不能为空"
        )

    user_dir = _get_user_dir(user["id"])
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

    available = get_user_available_space_for_pack(user["id"])

    return {
        "total_size": total_size,
        "user_available": available,
    }


@router.get("/pack/available-space")
def get_pack_available_space(
    folder_path: str | None = None,
    user: dict = Depends(require_user)
) -> dict:
    """获取用户可用于打包的空间

    如果提供 folder_path，同时返回文件夹大小
    """
    from app.services.pack import get_user_available_space_for_pack, get_server_available_space, calculate_folder_size

    available = get_user_available_space_for_pack(user["id"])
    server_available = get_server_available_space()

    result = {
        "user_available": available,
        "server_available": server_available,
    }

    # Calculate folder size if path provided
    if folder_path:
        user_dir = _get_user_dir(user["id"])
        target = _validate_path(user_dir, folder_path)
        if target.exists() and target.is_dir():
            result["folder_size"] = calculate_folder_size(target)
        else:
            result["folder_size"] = 0

    return result


@router.get("/pack")
def list_pack_tasks(user: dict = Depends(require_user)) -> list[dict]:
    """列出用户的打包任务（按创建时间倒序）"""
    from app.db import fetch_all
    return fetch_all(
        """SELECT * FROM pack_tasks
           WHERE owner_id = ?
           ORDER BY created_at DESC""",
        [user["id"]]
    )


@router.post("/pack", status_code=status.HTTP_201_CREATED)
async def create_pack_task(
    payload: PackRequest,
    user: dict = Depends(require_user)
) -> dict:
    """创建打包任务

    支持两种模式：
    1. 单文件夹打包：提供 folder_path
    2. 多文件打包：提供 paths 列表

    可选提供 output_name 自定义输出文件名。

    验证：
    - 所有路径存在且属于该用户
    - 用户有足够的空间（配额 + 服务器）

    预留空间并在后台启动异步打包。
    """
    import json
    from app.db import execute, fetch_one
    from app.db import utc_now
    from app.services.pack import (
        PackTaskManager, calculate_folder_size,
        get_user_available_space_for_pack
    )

    user_dir = _get_user_dir(user["id"])

    # 确定打包路径列表
    if payload.paths and len(payload.paths) > 0:
        # 多文件打包模式
        paths = payload.paths
        is_multi = True
    elif payload.folder_path:
        # 单文件夹打包模式（向后兼容）
        paths = [payload.folder_path]
        is_multi = False
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="请提供 folder_path 或 paths"
        )

    # 验证所有路径并计算总大小
    total_size = 0
    validated_paths = []
    for path in paths:
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
        validated_paths.append(path)
        if target.is_dir():
            total_size += calculate_folder_size(target)
        else:
            total_size += target.stat().st_size

    if total_size == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="选中的文件/文件夹为空"
        )

    # 存储路径：多文件用 JSON，单文件用原始路径
    folder_path_value = json.dumps(paths) if is_multi else paths[0]

    # Check for existing pack task on same paths
    existing = fetch_one(
        "SELECT id FROM pack_tasks WHERE owner_id = ? AND folder_path = ? AND status IN ('pending', 'packing')",
        [user["id"], folder_path_value]
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="相同路径已有进行中的打包任务"
        )

    # Reserve space = total size
    reserved_space = total_size

    # Check available space
    available = get_user_available_space_for_pack(user["id"])
    if reserved_space > available:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"空间不足。需要: {reserved_space / 1024 / 1024 / 1024:.2f} GB, 可用: {available / 1024 / 1024 / 1024:.2f} GB"
        )

    # 验证输出文件名
    output_name = payload.output_name
    if output_name:
        # 不允许路径分隔符
        if "/" in output_name or "\\" in output_name:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="输出文件名不能包含路径分隔符"
            )

    # Create task record
    task_id = execute(
        """
        INSERT INTO pack_tasks
        (owner_id, folder_path, folder_size, reserved_space, output_name, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [user["id"], folder_path_value, total_size, reserved_space, output_name, "pending", utc_now(), utc_now()]
    )

    # Start async packing
    asyncio.create_task(PackTaskManager.start_pack(task_id, user["id"], folder_path_value, output_name))

    return fetch_one("SELECT * FROM pack_tasks WHERE id = ?", [task_id])


@router.get("/pack/{task_id}")
def get_pack_task(task_id: int, user: dict = Depends(require_user)) -> dict:
    """获取打包任务详情"""
    from app.db import fetch_one
    task = fetch_one(
        "SELECT * FROM pack_tasks WHERE id = ? AND owner_id = ?",
        [task_id, user["id"]]
    )
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")
    return task


@router.delete("/pack/{task_id}")
async def cancel_or_delete_pack_task(
    task_id: int,
    user: dict = Depends(require_user)
) -> dict:
    """取消或删除打包任务

    - pending/packing 状态: 取消运行中的进程
    - done/failed/cancelled 状态: 仅删除任务记录（不删除压缩包文件）
    """
    from app.db import fetch_one, execute
    from app.db import utc_now
    from app.services.pack import PackTaskManager

    task = fetch_one(
        "SELECT * FROM pack_tasks WHERE id = ? AND owner_id = ?",
        [task_id, user["id"]]
    )
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")

    task_status = task["status"]

    # 进行中的任务：取消
    if task_status in ("pending", "packing"):
        await PackTaskManager.cancel_pack(task_id)
        execute(
            "UPDATE pack_tasks SET status = ?, reserved_space = 0, updated_at = ? WHERE id = ?",
            ["cancelled", utc_now(), task_id]
        )
        return {"ok": True, "message": "任务已取消"}

    # 已完成/失败/已取消的任务：仅删除记录（保留文件）
    if task_status in ("done", "failed", "cancelled"):
        execute("DELETE FROM pack_tasks WHERE id = ?", [task_id])
        return {"ok": True, "message": "任务已删除"}

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="无法处理该任务状态"
    )


@router.get("/pack/{task_id}/download")
def download_pack_result(task_id: int, user: dict = Depends(require_user)) -> FileResponse:
    """下载已完成的打包文件"""
    from app.db import fetch_one

    task = fetch_one(
        "SELECT * FROM pack_tasks WHERE id = ? AND owner_id = ?",
        [task_id, user["id"]]
    )
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")

    if task["status"] != "done":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="打包任务未完成"
        )

    output_path = task.get("output_path")
    if not output_path or not Path(output_path).exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="打包文件不存在")

    # Validate output path is within user's directory
    user_dir = _get_user_dir(user["id"])
    output_resolved = Path(output_path).resolve()
    try:
        output_resolved.relative_to(user_dir)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权访问该文件")

    return FileResponse(
        path=output_path,
        filename=Path(output_path).name,
        media_type="application/octet-stream"
    )
