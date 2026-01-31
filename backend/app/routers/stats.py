"""系统状态接口模块"""
from __future__ import annotations

import shutil
from pathlib import Path

from fastapi import APIRouter, Depends
from sqlmodel import select, func

from app.auth import require_admin, require_user
from app.core.config import settings
from app.database import get_session
from app.models import DownloadTask, User, UserTaskSubscription


router = APIRouter(prefix="/api/stats", tags=["stats"])


@router.get("")
async def get_stats(user: User = Depends(require_user)) -> dict:
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
    user_dir = Path(settings.download_dir) / str(user.id)
    used_space = 0
    if user_dir.exists():
        for file_path in user_dir.rglob("*"):
            if file_path.is_file():
                try:
                    used_space += file_path.stat().st_size
                except Exception:
                    pass

    # 用户配额
    user_quota = user.quota if user.quota else 100 * 1024 * 1024 * 1024  # 默认 100GB

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

    # 当前用户活跃任务统计和速度总和
    async with get_session() as db:
        # 活跃任务数：用户订阅中 pending 状态且任务为 active 的数量
        count_result = await db.exec(
            select(func.count(UserTaskSubscription.id))
            .join(DownloadTask, UserTaskSubscription.task_id == DownloadTask.id)
            .where(
                UserTaskSubscription.owner_id == user.id,
                UserTaskSubscription.status == "pending",
                DownloadTask.status == "active"
            )
        )
        active_count = count_result.first() or 0

        # 速度总和：用户订阅的活跃任务的速度总和
        speed_result = await db.exec(
            select(
                func.coalesce(func.sum(DownloadTask.download_speed), 0),
                func.coalesce(func.sum(DownloadTask.upload_speed), 0)
            )
            .join(UserTaskSubscription, UserTaskSubscription.task_id == DownloadTask.id)
            .where(
                UserTaskSubscription.owner_id == user.id,
                UserTaskSubscription.status == "pending",
                DownloadTask.status == "active"
            )
        )
        speed_row = speed_result.first()
        total_download = speed_row[0] if speed_row else 0
        total_upload = speed_row[1] if speed_row else 0

    return {
        "disk_total_space": display_total,
        "disk_used_space": used_space,
        "disk_space_limited": is_limited,
        "download_speed": int(total_download),
        "upload_speed": int(total_upload),
        "active_task_count": active_count,
    }


@router.get("/machine")
async def get_machine_stats(user: User = Depends(require_admin)) -> dict:
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
