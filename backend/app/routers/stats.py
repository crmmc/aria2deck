"""系统状态接口模块"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends

from app.auth import AuthUser, require_admin, require_user
from app.core.config import settings
from app.repositories.downloads import list_user_tasks
from app.services.task_projection import stat_counts
from app.services.usage_service import get_usage


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
async def get_stats(user: AuthUser = Depends(require_user)) -> dict:
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
    usage = await get_usage(user.id, user.quota_bytes)
    used_space = usage["used_bytes"]
    frozen_space = usage["reserved_bytes"]

    # 用户配额
    user_quota = (
        user.quota_bytes if user.quota_bytes else 100 * 1024 * 1024 * 1024
    )  # 默认 100GB

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
    display_total = (
        used_space + frozen_space + machine_free if is_limited else user_quota
    )

    rows = await list_user_tasks(user.id)
    active_count = stat_counts(rows)["current"]

    result = {
        "disk_total_space": display_total,
        "disk_used_space": used_space,
        "disk_frozen_space": frozen_space,
        "disk_space_limited": is_limited,
        "download_speed": 0,
        "upload_speed": 0,
        "active_task_count": active_count,
    }
    logger.debug("获取用户统计 user_id=%s active=%s", user.id, active_count)
    return result


@router.get("/machine")
async def get_machine_stats(user: AuthUser = Depends(require_admin)) -> dict:
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
