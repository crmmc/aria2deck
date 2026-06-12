from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

from app.aria2.gateway import get_aria2_client
from app.core.config import settings
from app.repositories.downloads import list_user_tasks
from app.services.task_projection import speed_totals, stat_counts
from app.services.task_runtime import fetch_active_live_statuses_by_gid
from app.services.usage_service import get_usage, visible_space_from_usage

logger = logging.getLogger(__name__)


def get_directory_size_bytes(path: Path) -> int:
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


async def get_user_stats(
    *,
    user_id: int,
    quota_bytes: int,
) -> dict:
    usage = await get_usage(user_id, quota_bytes)
    used_space = int(usage["used_bytes"])
    frozen_space = int(usage["reserved_bytes"])

    download_path = Path(settings.download_dir)
    download_path.mkdir(parents=True, exist_ok=True)
    disk = shutil.disk_usage(download_path)
    visible_space = visible_space_from_usage(usage, machine_free=int(disk.free))

    rows = await list_user_tasks(user_id)
    counts = stat_counts(rows)
    client: Any = get_aria2_client()
    live_by_gid = await fetch_active_live_statuses_by_gid(rows, client, logger)
    speeds = speed_totals(rows, live_by_gid)
    active_count = counts["current"]

    result = {
        "disk_total_space": visible_space["total"],
        "disk_used_space": used_space,
        "disk_frozen_space": frozen_space,
        "disk_space_limited": visible_space["limited"],
        "download_speed": speeds["download_speed"],
        "upload_speed": speeds["upload_speed"],
        "active_task_count": active_count,
    }
    logger.debug("获取用户统计 user_id=%s active=%s", user_id, active_count)
    return result


async def get_machine_stats(admin_id: int | None) -> dict:
    download_path = Path(settings.download_dir)
    download_path.mkdir(parents=True, exist_ok=True)
    disk = shutil.disk_usage(download_path)
    download_used = get_directory_size_bytes(download_path)
    system_used = max(disk.used - download_used, 0)

    result = {
        "disk_total": disk.total,
        "disk_used": disk.used,
        "disk_free": disk.free,
        "download_used": download_used,
        "system_used": system_used,
    }
    logger.debug("获取机器统计 admin_id=%s", admin_id)
    return result
