"""用户文件管理接口模块

提供用户目录下的文件查看、下载、删除、重命名等功能。
包含路径安全验证和空间配额管理。
"""
from __future__ import annotations

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
        if target.is_dir():
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
