"""系统状态接口模块"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends
from sqlmodel import select, func

from app.auth import require_admin, require_user
from app.core.config import settings
from app.database import get_session
from app.models import DownloadTask, User, UserTaskSubscription
from app.services.storage import get_user_used_space_async, get_user_frozen_space


router = APIRouter(prefix="/api/stats", tags=["stats"])
logger = logging.getLogger(__name__)


def _get_directory_size_bytes(path: Path) -> int:
    """Recursively calculate directory size in bytes."""
    if not path.exists():
        return 0

    total = 0
    for entry in path.rglob("*"):
        if entry.is_file():
            try:
                total += entry.stat().st_size
            except Exception as exc:
                logger.warning("统计目录大小失败 path=%s error=%s", entry, exc)
    return total


@router.get("")
async def get_stats(user: User = Depends(require_user)) -> dict:
    """获取系统状态

    所有用户返回:
    - disk_total_space: 用户配额（字节）
    - disk_used_space: 用户已使用空间（字节）
    - disk_frozen_space: 用户冻结空间（字节，下载中锁定）
    - disk_space_limited: 是否受机器空间限制
    - download_speed: 用户任务下载速度总和（字节/秒）
    - upload_speed: 用户任务上传速度总和（字节/秒）
    - active_task_count: 用户活跃任务数
    """
    used_space = await get_user_used_space_async(user.id)
    frozen_space = await get_user_frozen_space(user.id)

    # 用户配额
    user_quota = user.quota if user.quota else 100 * 1024 * 1024 * 1024  # 默认 100GB

    # 获取机器实际剩余空间
    download_path = Path(settings.download_dir)
    download_path.mkdir(parents=True, exist_ok=True)
    disk = shutil.disk_usage(download_path)
    machine_free = disk.free

    # 用户理论可用空间（基于配额）
    user_free_by_quota = max(0, user_quota - used_space - frozen_space)

    # 判断是否受机器空间限制
    is_limited = machine_free < user_free_by_quota

    # 动态调整显示的总空间：
    # - 如果受限：总空间 = 已使用 + 冻结 + 机器剩余空间
    # - 如果不受限：总空间 = 用户配额
    display_total = used_space + frozen_space + machine_free if is_limited else user_quota

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

    result = {
        "disk_total_space": display_total,
        "disk_used_space": used_space,
        "disk_frozen_space": frozen_space,
        "disk_space_limited": is_limited,
        "download_speed": int(total_download),
        "upload_speed": int(total_upload),
        "active_task_count": active_count,
    }
    logger.debug("获取用户统计 user_id=%s active=%s", user.id, active_count)
    return result


@router.get("/machine")
async def get_machine_stats(user: User = Depends(require_admin)) -> dict:
    """获取机器磁盘空间信息（仅管理员）

    返回:
    - disk_total: 磁盘总空间（字节）
    - disk_used: 磁盘已使用空间（字节）
    - disk_free: 磁盘剩余空间（字节）
    - download_used: 下载目录占用（字节）
    - system_used: 系统占用（字节，disk_used - download_used）
    """
    download_path = Path(settings.download_dir)
    download_path.mkdir(parents=True, exist_ok=True)
    disk = shutil.disk_usage(download_path)
    download_used = _get_directory_size_bytes(download_path)
    system_used = max(disk.used - download_used, 0)

    result = {
        "disk_total": disk.total,
        "disk_used": disk.used,
        "disk_free": disk.free,
        "download_used": download_used,
        "system_used": system_used,
    }
    logger.debug("获取机器统计 admin_id=%s", user.id)
    return result
