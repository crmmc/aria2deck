"""Cleanup helpers for failed download tasks."""

from __future__ import annotations

import logging
from enum import Enum

from app.aria2.client import Aria2Client
from app.services.storage import cleanup_task_download_dir, get_downloading_dir

logger = logging.getLogger(__name__)

class CleanupErrorType(str, Enum):
    """Error classification for cleanup operations."""

    RPC_FAILURE = "RPC_FAILURE"
    FS_FAILURE = "FS_FAILURE"
    STATUS_CONFLICT = "STATUS_CONFLICT"
    NONE = "NONE"


async def get_representative_owner_id(task_id: int) -> int | None:
    """Get an active owner_id for a global download for logging."""
    from app.services.aria2_lifecycle_service import get_representative_owner_id

    return await get_representative_owner_id(task_id)


async def cleanup_failed_task_artifacts(
    client: Aria2Client,
    task_id: int,
    gid: str | None,
    owner_id: int | None,
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
        owner_id: Owner user ID for logging (may be None for orphan tasks)
        log_prefix: Prefix for log messages (caller context)
        skip_status_check: Skip DB status validation (for callers who already verified)

    Returns:
        True if cleanup was performed or task was already clean/non-failed
        False only on unexpected errors (logged as WARNING)

    Idempotency: Safe to call multiple times; missing files are skipped.
    Status check: Returns True immediately if task is not in failed state.
    """
    if not skip_status_check:
        from app.services.aria2_lifecycle_service import (
            cleanup_failed_download_artifacts,
        )

        return await cleanup_failed_download_artifacts(
            client=client,
            task_id=task_id,
            gid=gid,
            owner_id=owner_id,
            log_prefix=log_prefix,
            validate_status=True,
        )

    return await cleanup_failed_task_artifacts_unchecked(
        client=client,
        task_id=task_id,
        gid=gid,
        owner_id=owner_id,
        log_prefix=log_prefix,
    )


async def cleanup_failed_task_artifacts_unchecked(
    client: Aria2Client,
    task_id: int,
    gid: str | None,
    owner_id: int | None,
    *,
    log_prefix: str,
) -> bool:
    """Cleanup aria2 and filesystem artifacts after service validation."""
    path = str(get_downloading_dir() / str(task_id))

    # RPC cleanup (best effort, continue on failure)
    if gid:
        try:
            await client.force_remove(gid)
        except Exception as exc:
            logger.warning(
                "[CLEANUP] rpc_failed %s task_id=%s owner_id=%s gid=%s "
                "path=%s error_type=%s op=force_remove error=%s",
                log_prefix,
                task_id,
                owner_id,
                gid,
                path,
                CleanupErrorType.RPC_FAILURE.value,
                exc,
            )
        try:
            await client.remove_download_result(gid)
        except Exception as exc:
            logger.warning(
                "[CLEANUP] rpc_failed %s task_id=%s owner_id=%s gid=%s "
                "path=%s error_type=%s op=remove_download_result error=%s",
                log_prefix,
                task_id,
                owner_id,
                gid,
                path,
                CleanupErrorType.RPC_FAILURE.value,
                exc,
            )

    # File cleanup (with error handling)
    try:
        await cleanup_task_download_dir(task_id)
        logger.info(
            "[CLEANUP] completed %s task_id=%s owner_id=%s gid=%s path=%s result=success",
            log_prefix,
            task_id,
            owner_id,
            gid,
            path,
        )
        return True
    except RuntimeError as exc:
        # Boundary violation from safe_delete_path
        logger.warning(
            "[CLEANUP] fs_failed %s task_id=%s owner_id=%s gid=%s "
            "path=%s error_type=%s error=%s",
            log_prefix,
            task_id,
            owner_id,
            gid,
            path,
            CleanupErrorType.FS_FAILURE.value,
            exc,
        )
        return False
    except Exception as exc:
        # Unexpected error
        logger.warning(
            "[CLEANUP] fs_failed %s task_id=%s owner_id=%s gid=%s "
            "path=%s error_type=%s error=%s",
            log_prefix,
            task_id,
            owner_id,
            gid,
            path,
            CleanupErrorType.FS_FAILURE.value,
            exc,
        )
        return False
