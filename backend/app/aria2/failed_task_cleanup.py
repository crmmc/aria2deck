"""Cleanup helpers for failed download tasks."""
from __future__ import annotations

import logging

from app.aria2.client import Aria2Client
from app.services.storage import cleanup_task_download_dir

logger = logging.getLogger(__name__)


async def cleanup_failed_task_artifacts(
    client: Aria2Client,
    task_id: int,
    gid: str | None,
    *,
    log_prefix: str,
) -> None:
    """Remove aria2 records and local task directory for a failed task."""
    if gid:
        try:
            await client.force_remove(gid)
        except Exception as exc:
            logger.warning(
                "%s force_remove failed task_id=%s gid=%s error=%s",
                log_prefix,
                task_id,
                gid,
                exc,
            )
        try:
            await client.remove_download_result(gid)
        except Exception as exc:
            logger.warning(
                "%s remove_download_result failed task_id=%s gid=%s error=%s",
                log_prefix,
                task_id,
                gid,
                exc,
            )

    await cleanup_task_download_dir(task_id)
