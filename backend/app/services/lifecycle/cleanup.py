"""Terminal cleanup paths extracted from ``aria2_lifecycle_service.py`` (M4 T06).

Hosts the claim-based fail/reclaim operation plus the best-effort physical
cleanup helpers used by the coordinator.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable

from app.modules.backend.port import BackendPort
from app.domain.lifecycle import ReconcileResult
from app.domain.locks import get_download_lifecycle_lock
from app.domain.status import FAILABLE_GLOBAL_DOWNLOAD_STATUSES
from app.repositories.task.downloads import (
    claim_attempt_terminal,
    claim_terminal_reclaim,
)
from app.services.failed_task_cleanup import cleanup_with_claim
from app.services.lifecycle._shared import _broadcast_download_update

logger = logging.getLogger(__name__)


async def _reclaim_terminal_with_claim(
    *,
    backend: BackendPort,
    download_id: int,
    gid: str,
    log_prefix: str,
) -> None:
    """Reclaim an already-terminal attempt using a repair claim (spec §10.4).

    Used by the coordinator after ``reconcile_download_size`` has already
    terminalized the row (disk_budget / max_task_size / no_subscribers).
    Obtains a ``RepairClaim`` via ``claim_terminal_reclaim``, then delegates
    physical cleanup to ``cleanup_with_claim``.  No ``skip_status_check``.
    """
    claim = await claim_terminal_reclaim(
        attempt_id=download_id,
        expected_gid=gid,
    )
    if claim is None:
        logger.debug(
            "%s Reclaim claim stale: attempt_id=%s gid=%s",
            log_prefix, download_id, gid,
        )
        return
    await cleanup_with_claim(backend, claim, log_prefix=log_prefix)


_WRITER_GID_UNSET: object = object()


async def _fail_download_and_reclaim_operation(
    *,
    backend: BackendPort,
    download_id: int,
    message: str,
    error_code: str | None,
    expected_gid: str | None,
    writer_gid: str | None,
    expected_statuses: Iterable[str],
    clear_gid: bool,
    acquire_lifecycle_lock: bool,
    log_prefix: str,
) -> bool:
    """Claim terminal state, then reclaim physical resources.

    Implements the two-step model (spec §10.1):
    1. ``claim_attempt_terminal`` CAS — sole authorization to terminalize.
    2. ``cleanup_with_claim`` — physical reclamation under the claim.

    Claim failure means no authorization: no force_remove, no directory
    deletion, no error overwrite.  Cleanup failure does not roll back the
    terminal state (spec §10.6).
    """

    async def _claim_and_reclaim() -> bool:
        if (
            expected_gid is not None
            and writer_gid is not None
            and expected_gid != writer_gid
        ):
            writer_gids: list[str] = [expected_gid, writer_gid]
            result_gids: list[str] = [expected_gid, writer_gid]
        elif writer_gid is not None:
            writer_gids = [writer_gid]
            result_gids = [writer_gid]
        else:
            writer_gids = []
            result_gids = []

        claim = await claim_attempt_terminal(
            attempt_id=download_id,
            expected_gid=expected_gid,
            terminal_status="failed",
            error_code=error_code,
            error_message=message,
            expected_statuses=tuple(expected_statuses),
            writer_gids=writer_gids,
            result_gids=result_gids,
        )
        if claim is None:
            return False

        await cleanup_with_claim(backend, claim, log_prefix=log_prefix)
        return True

    if not acquire_lifecycle_lock:
        return await _claim_and_reclaim()

    lifecycle_lock = await get_download_lifecycle_lock(download_id)
    async with lifecycle_lock:
        return await _claim_and_reclaim()


async def fail_download_and_reclaim(
    *,
    backend: BackendPort,
    download_id: int,
    message: str,
    error_code: str | None = None,
    expected_gid: str | None = None,
    writer_gid: str | None | object = _WRITER_GID_UNSET,
    expected_statuses: Iterable[str] = FAILABLE_GLOBAL_DOWNLOAD_STATUSES,
    clear_gid: bool = False,
    acquire_completion_lock: bool = False,
    acquire_lifecycle_lock: bool = True,
    log_prefix: str = "[Fail]",
) -> bool:
    """Mark failed and best-effort reclaim aria2 writer + downloading/<id>.

    Does not broadcast. Dual-gid handoff uses writer_gid != expected_gid.
    ``acquire_completion_lock`` is accepted for backward compatibility but
    is a no-op; lifecycle protection is solely via ``acquire_lifecycle_lock``.
    """
    if writer_gid is _WRITER_GID_UNSET:
        effective_writer: str | None = expected_gid
    else:
        effective_writer = writer_gid  # type: ignore[assignment]

    operation = asyncio.create_task(
        _fail_download_and_reclaim_operation(
            backend=backend,
            download_id=download_id,
            message=message,
            error_code=error_code,
            expected_gid=expected_gid,
            writer_gid=effective_writer,
            expected_statuses=expected_statuses,
            clear_gid=clear_gid,
            acquire_lifecycle_lock=acquire_lifecycle_lock,
            log_prefix=log_prefix,
        )
    )
    try:
        return await asyncio.shield(operation)
    except asyncio.CancelledError:
        await asyncio.shield(operation)
        raise


async def _remove_download_result_best_effort(
    backend: BackendPort,
    gid: str,
    log_prefix: str,
) -> None:
    try:
        await backend.remove_download_result_gid(gid)
    except Exception as exc:
        logger.debug(
            "%s Failed to remove aria2 result gid=%s error=%s", log_prefix, gid, exc
        )


async def _stop_untracked_gid_best_effort(
    backend: BackendPort,
    gid: str,
    log_prefix: str,
) -> None:
    try:
        await backend.force_remove_gid(gid)
    except Exception as exc:
        logger.debug(
            "%s Failed to stop untracked aria2 gid=%s error=%s",
            log_prefix, gid, exc,
        )
    await _remove_download_result_best_effort(backend, gid, log_prefix)


async def _terminalize_missing_gid_locked(
    *,
    backend: BackendPort,
    attempt_id: int,
    current_gid: str,
    total_bytes: int,
    log_prefix: str,
) -> ReconcileResult:
    """Terminalize a live attempt whose GID is confirmed missing (spec §14.1)."""
    if total_bytes > 0:
        error_code = "gid_missing"
        message = "aria2 任务 GID 已丢失"
    else:
        error_code = "unknown_size"
        message = "任务 GID 丢失且文件大小未知，无法安全恢复"
    logger.warning(
        "%s Missing GID for live attempt_id=%s current_gid=%s total_bytes=%s error_code=%s",
        log_prefix,
        attempt_id,
        current_gid,
        total_bytes,
        error_code,
    )
    changed = await fail_download_and_reclaim(
        backend=backend,
        download_id=attempt_id,
        message=message,
        error_code=error_code,
        expected_gid=current_gid,
        writer_gid=current_gid,
        acquire_lifecycle_lock=False,
        log_prefix=log_prefix,
    )
    if changed:
        await _broadcast_download_update(attempt_id)
        return ReconcileResult.TERMINALIZED
    return ReconcileResult.STALE
