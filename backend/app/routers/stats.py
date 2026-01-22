"""系统状态接口模块"""
from __future__ import annotations

import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, Request

from app.aria2.client import Aria2Client
from app.auth import require_admin, require_user
from app.core.config import settings
from app.core.state import get_aria2_client
from app.db import fetch_one


router = APIRouter(prefix="/api/stats", tags=["stats"])


def _get_client(request: Request) -> Aria2Client:
    return get_aria2_client(request)


@router.get("")
async def get_stats(request: Request, user: dict = Depends(require_user)) -> dict:
    """获取系统状态
    
    所有用户返回:
    - disk_total_space: 用户配额（字节）
    - disk_used_space: 用户已使用空间（字节）
    - disk_space_limited: 是否受机器空间限制
    - download_speed: 用户任务下载速度总和（字节/秒）
    - upload_speed: 用户任务上传速度总和（字节/秒）
    - active_task_count: 用户活跃任务数
    """
    # 计算用户已使用的空间
    user_dir = Path(settings.download_dir) / str(user["id"])
    used_space = 0
    if user_dir.exists():
        for file_path in user_dir.rglob("*"):
            if file_path.is_file():
                try:
                    used_space += file_path.stat().st_size
                except Exception:
                    pass
    
    # 用户配额
    user_quota = user.get("quota", 100 * 1024 * 1024 * 1024)  # 默认 100GB
    
    # 获取机器实际剩余空间
    download_path = Path(settings.download_dir)
    download_path.mkdir(parents=True, exist_ok=True)
    disk = shutil.disk_usage(download_path)
    machine_free = disk.free
    
    # 用户理论可用空间（基于配额）
    user_free_by_quota = max(0, user_quota - used_space)
    
    # 判断是否受机器空间限制
    is_limited = machine_free < user_free_by_quota
    
    # 动态调整显示的总空间：
    # - 如果受限：总空间 = 已使用 + 机器剩余空间
    # - 如果不受限：总空间 = 用户配额
    display_total = used_space + machine_free if is_limited else user_quota
    
    # 当前用户活跃任务统计
    user_active = fetch_one(
        "SELECT COUNT(*) as cnt FROM tasks WHERE owner_id = ? AND status = 'active'",
        [user["id"]]
    )
    
    # 计算用户任务的速度总和
    user_tasks = fetch_one(
        """
        SELECT 
            COALESCE(SUM(download_speed), 0) as total_download,
            COALESCE(SUM(upload_speed), 0) as total_upload
        FROM tasks 
        WHERE owner_id = ? AND status = 'active'
        """,
        [user["id"]]
    )
    
    return {
        "disk_total_space": display_total,
        "disk_used_space": used_space,
        "disk_space_limited": is_limited,
        "download_speed": int(user_tasks["total_download"]) if user_tasks else 0,
        "upload_speed": int(user_tasks["total_upload"]) if user_tasks else 0,
        "active_task_count": user_active["cnt"] if user_active else 0,
    }


@router.get("/machine")
async def get_machine_stats(user: dict = Depends(require_admin)) -> dict:
    """获取机器磁盘空间信息（仅管理员）
    
    返回:
    - disk_total: 磁盘总空间（字节）
    - disk_used: 磁盘已使用空间（字节）
    - disk_free: 磁盘剩余空间（字节）
    """
    download_path = Path(settings.download_dir)
    download_path.mkdir(parents=True, exist_ok=True)
    disk = shutil.disk_usage(download_path)
    
    return {
        "disk_total": disk.total,
        "disk_used": disk.used,
        "disk_free": disk.free,
    }
