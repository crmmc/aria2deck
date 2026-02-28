"""Cleanup helpers for failed download tasks."""
from __future__ import annotations

import logging

from sqlmodel import select

from app.aria2.client import Aria2Client
from app.database import get_session
from app.models import DownloadTask
from app.services.storage import cleanup_task_download_dir

logger = logging.getLogger(__name__)

# Valid failed states that trigger cleanup
FAILED_STATES = frozenset({"error", "removed"})


async def cleanup_failed_task_artifacts(
    client: Aria2Client,
    task_id: int,
    gid: str | None,
    *,
    log_prefix: str,
    skip_status_check: bool = False,
) -> bool:
    """Unified cleanup for failed tasks.

    This is the single entry point for all failed task cleanup operations.
    Handles aria2 RPC cleanup and local file deletion.

    Args:
        client: Aria2 RPC client
        task_id: Database task ID
        gid: Aria2 GID (may be None for orphan tasks)
        log_prefix: Prefix for log messages
        skip_status_check: Skip DB status validation (for callers who already verified)

    Returns:
        True if cleanup was performed or task was already clean/non-failed
        False only on unexpected errors (logged as WARNING)

    Idempotency: Safe to call multiple times; missing files are skipped.
    Status check: Returns True immediately if task is not in failed state.
    """
    # Status validation (unless caller already verified)
    if not skip_status_check:
        async with get_session() as db:
            result = await db.exec(
                select(DownloadTask).where(DownloadTask.id == task_id)
            )
            task = result.first()

        if task is None:
            logger.debug(
                "%s cleanup skipped task_id=%s reason=task_not_found",
                log_prefix,
                task_id,
            )
            return True  # Already cleaned or never existed

        if task.status not in FAILED_STATES:
            logger.debug(
                "%s cleanup skipped task_id=%s reason=not_failed_state status=%s",
                log_prefix,
                task_id,
                task.status,
            )
            return True  # Not a failed task, no cleanup needed

    # RPC cleanup (best effort, continue on failure)
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

    # File cleanup (with error handling)
    try:
        await cleanup_task_download_dir(task_id)
        logger.info(
            "%s cleanup completed task_id=%s gid=%s",
            log_prefix,
            task_id,
            gid,
        )
        return True
    except RuntimeError as exc:
        # Boundary violation from safe_delete_path
        logger.warning(
            "%s cleanup failed task_id=%s gid=%s error=%s",
            log_prefix,
            task_id,
            gid,
            exc,
        )
        return False
    except Exception as exc:
        # Unexpected error
        logger.warning(
            "%s cleanup failed task_id=%s gid=%s error=%s",
            log_prefix,
            task_id,
            gid,
            exc,
        )
        return False
