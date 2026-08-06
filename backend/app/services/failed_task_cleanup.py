"""Cleanup helpers for failed download tasks."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

from app.aria2.protocol import Aria2Gateway
from app.domain.status import FAILED_DOWNLOAD_STATUSES
from app.repositories.downloads import (
    clear_terminal_download_gid,
    get_global_download_status_snapshot,
    get_representative_active_owner_id,
)
from app.services.storage import cleanup_task_download_dir, get_downloading_dir

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CleanupResult:
    writer_stopped: bool
    directory_cleaned: bool
    result_removed: bool
    skipped: bool = False

    @property
    def safe_to_reuse(self) -> bool:
        return self.writer_stopped and self.directory_cleaned


class CleanupErrorType(str, Enum):
    """Error classification for cleanup operations."""

    RPC_FAILURE = "RPC_FAILURE"
    FS_FAILURE = "FS_FAILURE"
    STATUS_CONFLICT = "STATUS_CONFLICT"
    NONE = "NONE"


_MISSING_GID_PATTERNS = (
    "gid not found",
    "gid is not found",
    "no such download",
    "unknown gid",
    "invalid gid",
)


def _writer_already_stopped(exc: Exception) -> bool:
    message = str(exc).lower()
    if "gid" in message and "not found" in message:
        return True
    return any(pattern in message for pattern in _MISSING_GID_PATTERNS)


async def get_representative_owner_id(task_id: int) -> int | None:
    """Get an active owner_id for a global download for logging."""
    return await get_representative_active_owner_id(task_id)


async def cleanup_failed_task_artifacts(
    client: Aria2Gateway,
    task_id: int,
    gid: str | None,
    owner_id: int | None,
    *,
    log_prefix: str,
    skip_status_check: bool = False,
) -> CleanupResult:
    """Stop the writer, clean its task directory, then remove stopped history.

    ``safe_to_reuse`` depends only on confirmed writer shutdown and directory cleanup.
    Removing stopped-history metadata is best effort after the writer has stopped.
    """
    if not skip_status_check:
        snapshot = await get_global_download_status_snapshot(task_id)
        if snapshot is None:
            path = str(get_downloading_dir() / str(task_id))
            logger.debug(
                "[CLEANUP] skipped %s task_id=%s owner_id=%s gid=%s "
                "path=%s reason=task_not_found",
                log_prefix,
                task_id,
                owner_id,
                gid,
                path,
            )
            return CleanupResult(False, False, False, skipped=True)
        status = str(snapshot["status"])
        if status not in FAILED_DOWNLOAD_STATUSES:
            path = str(get_downloading_dir() / str(task_id))
            logger.debug(
                "[CLEANUP] skipped %s task_id=%s owner_id=%s gid=%s "
                "path=%s error_type=%s status=%s",
                log_prefix,
                task_id,
                owner_id,
                gid,
                path,
                CleanupErrorType.STATUS_CONFLICT.value,
                status,
            )
            return CleanupResult(False, False, False, skipped=True)

    return await cleanup_failed_task_artifacts_unchecked(
        client=client,
        task_id=task_id,
        gid=gid,
        owner_id=owner_id,
        log_prefix=log_prefix,
    )


async def cleanup_terminal_download_generation(
    client: Aria2Gateway,
    task_id: int,
    gid: str,
    owner_id: int | None,
    *,
    log_prefix: str,
    skip_status_check: bool = False,
) -> CleanupResult:
    result = await cleanup_failed_task_artifacts(
        client=client,
        task_id=task_id,
        gid=gid,
        owner_id=owner_id,
        log_prefix=log_prefix,
        skip_status_check=skip_status_check,
    )
    # Only drop the residual gid after writer stop + directory cleanup.
    # Budget no longer counts terminal residual gids, but retry safety still
    # requires the on-disk generation to be gone before reuse.
    if result.safe_to_reuse:
        await clear_terminal_download_gid(task_id, expected_gid=gid)
    return result


async def cleanup_failed_task_artifacts_unchecked(
    client: Aria2Gateway,
    task_id: int,
    gid: str | None,
    owner_id: int | None,
    *,
    log_prefix: str,
) -> CleanupResult:
    path = str(get_downloading_dir() / str(task_id))
    writer_stopped = gid is None
    if gid:
        try:
            await client.force_remove(gid)
            writer_stopped = True
        except Exception as exc:
            writer_stopped = _writer_already_stopped(exc)
            level = logger.debug if writer_stopped else logger.warning
            level(
                "[CLEANUP] rpc_failed %s task_id=%s owner_id=%s gid=%s "
                "path=%s error_type=%s op=force_remove writer_stopped=%s error=%s",
                log_prefix,
                task_id,
                owner_id,
                gid,
                path,
                CleanupErrorType.RPC_FAILURE.value,
                writer_stopped,
                exc,
            )
    if not writer_stopped:
        return CleanupResult(False, False, False)

    directory_cleaned = False
    try:
        await cleanup_task_download_dir(task_id)
        directory_cleaned = True
    except Exception as exc:
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

    result_removed = gid is None
    if gid:
        try:
            await client.remove_download_result(gid)
            result_removed = True
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

    if writer_stopped and directory_cleaned:
        logger.info(
            "[CLEANUP] completed %s task_id=%s owner_id=%s gid=%s "
            "path=%s result=success result_removed=%s",
            log_prefix,
            task_id,
            owner_id,
            gid,
            path,
            result_removed,
        )
    return CleanupResult(writer_stopped, directory_cleaned, result_removed)
