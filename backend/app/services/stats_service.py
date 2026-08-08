from __future__ import annotations

import asyncio
import logging
import shutil
from time import monotonic
from pathlib import Path

from app.core.config import settings
from app.services.task_projection import speed_totals, stat_counts
from app.services.task_projection_rows import list_user_task_projections
from app.services.usage_service import get_visible_space

logger = logging.getLogger(__name__)
MACHINE_SIZE_CACHE_TTL_SECONDS = 5.0
_machine_size_cache: tuple[str, float, int] | None = None
_machine_size_lock = asyncio.Lock()


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


async def _get_cached_directory_size(path: Path) -> int:
    global _machine_size_cache

    key = str(path)
    now = monotonic()
    if _machine_size_cache:
        cached_key, expires_at, cached_size = _machine_size_cache
        if cached_key == key and now < expires_at:
            return cached_size

    async with _machine_size_lock:
        now = monotonic()
        if _machine_size_cache:
            cached_key, expires_at, cached_size = _machine_size_cache
            if cached_key == key and now < expires_at:
                return cached_size
        size = await asyncio.to_thread(get_directory_size_bytes, path)
        _machine_size_cache = (key, now + MACHINE_SIZE_CACHE_TTL_SECONDS, size)
        return size


async def get_user_stats(
    *,
    user_id: int,
    quota_bytes: int,
) -> dict:
    visible_space = await get_visible_space(user_id, quota_bytes)
    used_space = int(visible_space["used"])
    frozen_space = int(visible_space["frozen"])

    rows = await list_user_task_projections(user_id)
    counts = stat_counts(rows)
    speeds = speed_totals(rows)
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
    download_used = await _get_cached_directory_size(download_path)
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
