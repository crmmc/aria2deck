"""Cleanup helpers for failed download tasks."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import overload

from app.aria2.protocol import Aria2Gateway
from app.domain.lifecycle import RepairClaim, TerminalizationClaim
from app.repositories.downloads import (
    clear_terminal_download_gid,
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


# --------------------------------------------------------------------------- #
# Claim-only public API (spec §10.3–10.5)                                     #
# --------------------------------------------------------------------------- #


@overload
def cleanup_with_claim(
    client: Aria2Gateway,
    claim: TerminalizationClaim,
    *,
    log_prefix: str,
) -> CleanupResult: ...


@overload
def cleanup_with_claim(
    client: Aria2Gateway,
    claim: RepairClaim,
    *,
    log_prefix: str,
) -> CleanupResult: ...


async def cleanup_with_claim(
    client: Aria2Gateway,
    claim: TerminalizationClaim | RepairClaim,
    *,
    log_prefix: str,
) -> CleanupResult:
    """Physical reclamation authorized by a terminal or repair claim.

    This is the sole authorized entry point for destructive cleanup.
    It requires a claim produced by ``claim_attempt_terminal`` or
    ``claim_terminal_reclaim``, never an ad-hoc ``task_id + gid`` pair.

    Cleanup order (spec §10.3):
      1. ``force_remove`` every ``writer_gid``; if any is not confirmed
         stopped, keep ``downloading/<attempt_id>`` and end this round.
      2. After all writers stop, delete ``downloading/<attempt_id>``.
      3. After directory deletion, best-effort ``remove_download_result``
         for each ``result_gid``.
      4. If the DB ``aria2_gid`` still equals ``expected_current_gid`` and
         the attempt is still terminal, CAS-clear the GID.
    """
    attempt_id = claim.attempt_id
    writer_gids = claim.writer_gids
    result_gids = claim.result_gids
    expected_gid = claim.expected_current_gid
    path = str(get_downloading_dir() / str(attempt_id))

    # Step 1: stop all writers.
    all_writers_stopped = True
    for wgid in writer_gids:
        try:
            await client.force_remove(wgid)
        except Exception as exc:
            if _writer_already_stopped(exc):
                logger.debug(
                    "[CLEANUP] writer_already_stopped %s attempt_id=%s gid=%s",
                    log_prefix,
                    attempt_id,
                    wgid,
                )
                continue
            # Network / permission / unknown RPC error — not stopped.
            all_writers_stopped = False
            logger.warning(
                "[CLEANUP] rpc_failed %s attempt_id=%s gid=%s "
                "path=%s error_type=%s op=force_remove writer_stopped=False error=%s",
                log_prefix,
                attempt_id,
                wgid,
                path,
                CleanupErrorType.RPC_FAILURE.value,
                exc,
            )
            break

    if not all_writers_stopped:
        return CleanupResult(False, False, False)

    # Step 2: delete the downloading directory.
    directory_cleaned = False
    try:
        await cleanup_task_download_dir(attempt_id)
        directory_cleaned = True
    except Exception as exc:
        logger.warning(
            "[CLEANUP] fs_failed %s attempt_id=%s "
            "path=%s error_type=%s error=%s",
            log_prefix,
            attempt_id,
            path,
            CleanupErrorType.FS_FAILURE.value,
            exc,
        )

    # Step 3: best-effort remove_download_result for each result GID.
    result_removed = True
    for rgid in result_gids:
        try:
            await client.remove_download_result(rgid)
        except Exception as exc:
            result_removed = False
            logger.debug(
                "[CLEANUP] result_remove_failed %s attempt_id=%s gid=%s error=%s",
                log_prefix,
                attempt_id,
                rgid,
                exc,
            )

    # Step 4: CAS-clear the residual GID if still current.
    gid_cleared = False
    if expected_gid is not None:
        cleared = await clear_terminal_download_gid(
            attempt_id, expected_gid=expected_gid
        )
        gid_cleared = cleared

    logger.info(
        "[CLEANUP] completed %s attempt_id=%s "
        "path=%s result=success dir=%s result_removed=%s gid_cleared=%s",
        log_prefix,
        attempt_id,
        path,
        directory_cleaned,
        result_removed,
        gid_cleared,
    )
    return CleanupResult(all_writers_stopped, directory_cleaned, result_removed)
