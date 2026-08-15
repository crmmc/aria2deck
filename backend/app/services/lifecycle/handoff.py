"""Handoff paths extracted from ``aria2_lifecycle_service.py`` (M4 T07).

Hosts the explicit followedBy/following GID handoff (spec §9) and the
observation resolution used by the coordinator.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.modules.backend.port import BackendPort
from app.domain.lifecycle import ReconcileResult
from app.domain.locks import get_download_lifecycle_lock
from app.domain.quota import candidate_size_from_status, get_disk_available_bytes
from app.domain.status import TERMINAL_DOWNLOAD_STATUSES
from app.modules.task_core.states import (
    ERROR_ADMISSION_PAUSED,
    ERROR_GROWTH_PAUSE_FAILED,
    ERROR_GROWTH_UNPAUSE_FAILED,
    ERROR_METADATA_ADMISSION_PAUSED,
    ERROR_UNPAUSE_FAILED,
)
from app.repositories.task.downloads import (
    get_global_download_by_gid,
    get_global_download_for_generation,
    get_global_download_status_snapshot,
    guarded_update_download_and_active_user_tasks,
    reconcile_download_size,
    update_global_download,
)
from app.services import download_ops
from app.services.lifecycle._shared import (
    _broadcast_download_update,
    is_missing_gid_error,
    is_transient_rpc_error,
    system_pause_gid,
    system_unpause_gid,
)
from app.services.lifecycle.cleanup import (
    _remove_download_result_best_effort,
    _stop_untracked_gid_best_effort,
    fail_download_and_reclaim,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResolveResult:
    """Pure resolution of a GID observation to an attempt (spec §6.3).

    ``source_gid`` is ``None`` for direct matches (``observed_gid`` ==
    ``current_gid``), or set to the source GID for a handoff candidate
    where ``observed_status`` contains ``following=source_gid``.
    """

    download: dict[str, Any]
    observed_gid: str
    source_gid: str | None

    @property
    def is_handoff_candidate(self) -> bool:
        return self.source_gid is not None


COMPLETE_SOURCE_RETRY_COUNT = 4
COMPLETE_SOURCE_RETRY_INTERVAL = 0.5


async def _guarded_update_global_download(
    download_id: int,
    values: Mapping[str, Any],
    *,
    expected_gid: str,
) -> dict[str, Any] | None:
    from app.repositories.task.downloads import guarded_update_global_download

    return await guarded_update_global_download(
        download_id,
        dict(values),
        expected_gid=expected_gid,
        return_row=True,
    )


async def update_download_and_active_user_tasks(
    *,
    download_id: int,
    expected_gid: str,
    global_values: dict[str, Any],
    user_status: str | None = None,
    force_display_name: bool = False,
) -> bool:
    updated = await guarded_update_download_and_active_user_tasks(
        download_id,
        global_values,
        expected_gid=expected_gid,
        user_status=user_status,
        display_name=global_values.get("display_name"),
        force_display_name=force_display_name,
    )
    return updated is not None


async def coordinate_reported_size(
    *,
    backend: BackendPort,
    download: dict[str, Any],
    expected_gid: str,
    control_gid: str,
    status: Mapping[str, Any],
    require_trusted_total: bool = False,
    resume_after_admission: bool = True,
    acquire_lifecycle_lock: bool = True,
) -> dict[str, Any]:
    """Lock-internal size admission with idempotent pause/unpause (spec §8.2).

    When ``acquire_lifecycle_lock=False`` the caller must already hold the
    attempt lock.  Outcomes:

    - ``admitted`` – size accepted, proceed with projection.
    - ``stale`` – DB GID changed, no action.
    - ``unknown_size`` – no trusted size; live reconcile waits (not hard reclaim).
    - ``complete`` – re-query shows complete, route to completion.
    - ``rpc_unavailable`` – transient RPC, skip size admission.
    - ``terminalized`` – already terminalized (growth failure or missing GID).
    - ``disk_budget`` / ``max_task_size`` / ``no_subscribers`` – row already
      terminal in DB, caller does physical reclaim.
    """
    from app.core.config import settings
    from app.services.settings_service import get_max_task_size, get_min_free_disk

    download_id = int(download["id"])

    current = await get_global_download_for_generation(
        download_id, expected_gid
    )
    if current is None:
        return {"outcome": "stale", "paused_by_us": False}

    require_trusted_total = require_trusted_total or not bool(
        current.get("size_known")
    )
    candidate = candidate_size_from_status(
        status, require_trusted_total=require_trusted_total
    )
    if candidate is None:
        return {"outcome": "unknown_size", "paused_by_us": False}
    old_size = int(current.get("total_bytes") or 0)
    size_known = bool(current.get("size_known"))
    raw_status = str(status.get("status") or "")

    # M6: once size is trusted, a smaller live candidate is noise (e.g. BT
    # select-file partial file list). Never shrink reserved/total floor.
    if size_known and candidate[0] < old_size:
        return {
            "outcome": "admitted",
            "paused_by_us": False,
            "size_bytes": old_size,
            "skipped_shrink": True,
        }

    paused_by_us = False

    # Pause for safe size accounting when size grew (spec §8.2).
    # Already paused or complete → skip pause (rule 1).
    if candidate[0] > old_size and raw_status not in {"paused", "complete"}:
        # Stamp system ownership on successful pause so a crash between
        # pause and unpause cannot be branded external_paused (M7).
        result_str = await system_pause_gid(
            backend=backend,
            download_id=download_id,
            control_gid=control_gid,
            expected_gid=expected_gid,
            failure_error_code=ERROR_GROWTH_PAUSE_FAILED,
            failure_message="任务大小增长时无法安全暂停",
            ownership_error_code=ERROR_ADMISSION_PAUSED,
            acquire_lifecycle_lock=acquire_lifecycle_lock,
        )
        if result_str == "success":
            paused_by_us = True
        elif result_str == "complete":
            return {"outcome": "complete", "paused_by_us": False}
        elif result_str == "rpc_unavailable":
            return {"outcome": "rpc_unavailable", "paused_by_us": False}
        elif result_str == "soft_failed":
            return {
                "outcome": "pause_soft_failed",
                "paused_by_us": False,
                "pause_soft_failed": True,
            }
        else:
            return {"outcome": "terminalized", "paused_by_us": False}

    # Budget / reservation atomic operation (spec §8.2).
    result = await reconcile_download_size(
        download_id=download_id,
        expected_gid=expected_gid,
        candidate_bytes=candidate[0],
        completed_bytes=candidate[1],
        size_limit_bytes=int(
            current.get("size_limit_bytes") or get_max_task_size()
        ),
        disk_available_bytes=lambda: get_disk_available_bytes(
            settings.download_dir, min_free_disk=get_min_free_disk()
        ),
    )
    result["paused_by_us"] = paused_by_us

    if not result.admitted:
        outcome = str(result.get("outcome") or "")
        if outcome == "stale":
            return result
        # disk_budget / max_task_size / no_subscribers: row already terminal
        # in DB.  Return outcome so caller does physical reclaim.
        return result

    # Idempotent unpause only when we confirmed the pause (spec §8.4 / M9 §3.3).
    # system_unpause_gid success means re-query is active|waiting — only then
    # clear ownership. soft_failed keeps the growth code for multi-round resume.
    # complete: leave pending to the completion path (no false clear here).
    if paused_by_us and resume_after_admission:
        result_str = await system_unpause_gid(
            backend=backend,
            download_id=download_id,
            control_gid=control_gid,
            expected_gid=expected_gid,
            failure_error_code=ERROR_GROWTH_UNPAUSE_FAILED,
            failure_message="任务大小调整后恢复下载失败",
            acquire_lifecycle_lock=acquire_lifecycle_lock,
        )
        if result_str == "success":
            # Clear only after re-query confirmed running (not RPC-only success).
            await update_global_download(
                download_id,
                {"error_code": None, "error_message": None},
            )
        elif result_str == "complete":
            result["outcome"] = "complete"
        elif result_str == "rpc_unavailable":
            result["outcome"] = "rpc_unavailable"
        elif result_str == "soft_failed":
            # Keep growth_unpause_failed (or prior ownership); do not clear.
            result["outcome"] = "admitted"
            result["unpause_soft_failed"] = True
        else:
            result["outcome"] = "terminalized"

    return result


async def _handoff_locked(
    *,
    backend: BackendPort,
    attempt_id: int,
    source_gid: str,
    payload_gid: str,
    snapshot: dict[str, Any],
    download: dict[str, Any],
    log_prefix: str,
    _payload_status: dict[str, Any] | None = None,
) -> tuple[ReconcileResult, tuple[str, dict[str, Any]] | None]:
    """Lock-internal handoff from source GID to payload GID (spec §9.2).

    Caller must already hold the attempt lock.

    Returns ``(result, complete_dispatch)`` where *complete_dispatch* is
    ``(completion_gid, completion_status)`` when the caller should run the
    completion path after releasing the lock, or ``None``.

    When ``_payload_status`` is provided, the payload's tell_status has
    already been fetched by the caller outside the lock.  When ``None``,
    the function falls back to fetching it inside the lock (legacy callers).

    Only accepts explicit Aria2 signals:
    - source status contains ``followedBy=[payload_gid]``
    - payload status contains ``following=source_gid``
    """
    current_gid = str(snapshot.get("aria2_gid") or "")
    status_before = str(snapshot.get("status") or "")

    # 1. Fencing: current_gid == source_gid and live.
    if current_gid != source_gid:
        if current_gid == payload_gid:
            logger.debug(
                "%s Handoff idempotent: already switched to payload %s",
                log_prefix,
                payload_gid,
            )
            return ReconcileResult.STALE, None
        logger.info(
            "%s Handoff stale: current=%s source=%s attempt_id=%s",
            log_prefix,
            current_gid,
            source_gid,
            attempt_id,
        )
        return ReconcileResult.STALE, None

    if status_before in TERMINAL_DOWNLOAD_STATUSES:
        logger.debug(
            "%s Handoff: already terminal attempt_id=%s status=%s",
            log_prefix,
            attempt_id,
            status_before,
        )
        return ReconcileResult.ALREADY_TERMINAL, None

    # 2. payload_gid confirmed by caller (explicit followedBy/following).

    # 3. Query payload latest status (or use pre-fetched status).
    payload_status = _payload_status
    if payload_status is None:
        try:
            payload_status = await backend.tell_status(payload_gid)
        except Exception as exc:
            if is_transient_rpc_error(exc) or is_missing_gid_error(exc):
                logger.debug(
                    "%s Handoff payload tell_status unavailable, waiting "
                    "attempt_id=%s payload=%s error=%s",
                    log_prefix,
                    attempt_id,
                    payload_gid,
                    exc,
                )
                return ReconcileResult.WAITING, None
            raise

    raw_status = str(payload_status.get("status") or "")

    # 4. Unknown-size rules (spec §9.2: payload non-complete, no trusted size).
    #
    # When size_known=True the size was already admitted at creation time.
    # A transient totalLength=0 from the payload means aria2 has not yet
    # reported the payload size.  Wait for the next reconcile rather than
    # terminalizing or committing with stale data.
    require_trusted = not bool(download.get("size_known"))
    size_candidate = candidate_size_from_status(
        payload_status, require_trusted_total=require_trusted
    )

    if size_candidate is None and not require_trusted and raw_status != "complete":
        logger.debug(
            "%s Handoff payload not yet reporting size (size_known), "
            "waiting attempt_id=%s",
            log_prefix,
            attempt_id,
        )
        return ReconcileResult.WAITING, None

    if size_candidate is None and require_trusted and raw_status != "complete":
        if raw_status in ("waiting", "paused"):
            logger.debug(
                "%s Handoff payload unknown size, %s → waiting attempt_id=%s",
                log_prefix,
                raw_status,
                attempt_id,
            )
            return ReconcileResult.WAITING, None

        if raw_status == "active":
            # Soft system pause to wait for trusted size (M7 entry).
            # Success path must also stamp ownership so WAITING is never bare.
            await system_pause_gid(
                backend=backend,
                download_id=attempt_id,
                control_gid=payload_gid,
                expected_gid=source_gid,
                ownership_error_code=ERROR_METADATA_ADMISSION_PAUSED,
                failure_error_code=ERROR_METADATA_ADMISSION_PAUSED,
                failure_message="磁力任务 payload 等待可信大小",
                acquire_lifecycle_lock=False,
            )
            try:
                re_status = await backend.tell_status(payload_gid)
            except Exception as re_exc:
                if is_transient_rpc_error(re_exc) or is_missing_gid_error(
                    re_exc
                ):
                    return ReconcileResult.WAITING, None
                raise
            re_raw = str(re_status.get("status") or "")
            if re_raw == "paused":
                return ReconcileResult.WAITING, None
            if re_raw == "complete":
                payload_status = re_status
                raw_status = "complete"
            else:
                # Pause not confirmed yet: wait for later reconcile rather than
                # one-shot terminalize (M6 residual R2 / M7).
                logger.info(
                    "%s Handoff unknown size: pause not confirmed, "
                    "waiting attempt_id=%s payload=%s re_status=%s",
                    log_prefix,
                    attempt_id,
                    payload_gid,
                    re_raw,
                )
                return ReconcileResult.WAITING, None

        # Other statuses (error/removed) → waiting for next reconcile.
        return ReconcileResult.WAITING, None

    # 5. Trusted-size admission (spec §9.2 step 5).
    admission = await coordinate_reported_size(
        backend=backend,
        download=download,
        expected_gid=source_gid,
        control_gid=payload_gid,
        status=payload_status,
        require_trusted_total=require_trusted,
        resume_after_admission=False,
        acquire_lifecycle_lock=False,
    )
    outcome = str(admission.get("outcome") or "")

    if outcome == "stale":
        return ReconcileResult.STALE, None
    if outcome == "rpc_unavailable":
        return ReconcileResult.WAITING, None
    if outcome == "terminalized":
        await _broadcast_download_update(attempt_id)
        return ReconcileResult.TERMINALIZED, None

    if outcome not in ("admitted", "complete"):
        # Admission rejected (unknown_size / disk_budget / max_task_size / …).
        changed = await fail_download_and_reclaim(
            backend=backend,
            download_id=attempt_id,
            message="磁力任务准入被拒绝",
            error_code=outcome if outcome else "admission_rejected",
            expected_gid=source_gid,
            writer_gid=payload_gid,
            acquire_lifecycle_lock=False,
            log_prefix=log_prefix,
        )
        if changed:
            await _broadcast_download_update(attempt_id)
            return ReconcileResult.TERMINALIZED, None
        return ReconcileResult.STALE, None

    # 6. Condition update: source → payload (spec §9.2 step 7).
    mapped_status = download_ops.map_aria2_status(payload_status)
    if raw_status == "complete":
        mapped_status = "active"

    display_name_fallback = str(
        download.get("display_name") or download.get("source_uri") or ""
    )
    progress = download_ops.map_progress_values(
        payload_status, display_name_fallback
    )

    # pause-metadata / admission pause is system-owned. Tag it before the
    # CAS so a subsequent reconcile cannot brand it as external_paused if
    # we crash between commit and unpause.
    system_owned_pause = (
        raw_status == "paused" or bool(admission.get("paused_by_us"))
    ) and raw_status != "complete"

    # M10: size truth is admission-owned. reconcile_download_size already
    # wrote total_bytes when admitted; map_progress_values no longer carries
    # total. Prefer admission size_bytes, else keep current download total.
    # "complete" admission carries no size_bytes: the payload is already
    # finished, so its live total IS the final size (completion semantics),
    # never a speculative value.
    admission_size = admission.get("size_bytes")
    if admission_size is not None:
        admitted_total = int(admission_size)
    elif str(admission.get("outcome") or "") == "complete":
        admitted_total = int(
            payload_status.get("totalLength")
            or download.get("total_bytes")
            or 0
        )
    else:
        admitted_total = int(download.get("total_bytes") or 0)

    global_values: dict[str, Any] = {
        "aria2_gid": payload_gid,
        "status": mapped_status,
        "completed_bytes": progress.get("completed_bytes", 0),
        "total_bytes": admitted_total,
    }
    if progress.get("display_name"):
        global_values["display_name"] = progress["display_name"]
    if str(download.get("resource_kind") or "") != "torrent":
        global_values["resource_kind"] = "torrent"
    bt_hash = download_ops.bt_info_hash_from_status(payload_status)
    if bt_hash:
        global_values["bt_info_hash"] = bt_hash
    if system_owned_pause:
        global_values["error_code"] = ERROR_METADATA_ADMISSION_PAUSED
        global_values["error_message"] = None
    elif raw_status in {"active", "waiting"}:
        # Payload already running (non-metadata): clear create-time pending
        # credential (Spec §3.1.1 / M9 — handoff confirms payload release).
        global_values["error_code"] = None
        global_values["error_message"] = None

    updated = await guarded_update_download_and_active_user_tasks(
        attempt_id,
        global_values,
        expected_gid=source_gid,
        user_status=mapped_status,
        display_name=progress.get("display_name"),
        force_display_name=True,
    )

    if updated is None:
        # CAS failed → stale, do not cleanup payload (spec §9.2 step 8).
        logger.info(
            "%s Handoff CAS failed (stale) attempt_id=%s",
            log_prefix,
            attempt_id,
        )
        return ReconcileResult.STALE, None

    logger.info(
        "%s Handoff committed: %s → %s attempt_id=%s status=%s",
        log_prefix,
        source_gid,
        payload_gid,
        attempt_id,
        mapped_status,
    )

    # 7. Best-effort remove source stopped result (spec §9.2 step 9).
    if source_gid != payload_gid:
        await _remove_download_result_best_effort(
            backend, source_gid, log_prefix
        )

    # 8. Resume payload if needed (spec §9.2 step 10 / M9 §3.3).
    # Ownership is metadata_admission_paused (or paused_by_us growth pause).
    # Clear codes only on system_unpause_gid success (re-query active|waiting).
    # soft_failed keeps the system code for multi-round resume (symmetric growth).
    if system_owned_pause:
        result_str = await system_unpause_gid(
            backend=backend,
            download_id=attempt_id,
            control_gid=payload_gid,
            expected_gid=payload_gid,
            failure_error_code=ERROR_UNPAUSE_FAILED,
            failure_message="磁力任务准入后恢复下载失败",
            acquire_lifecycle_lock=False,
        )
        if result_str == "success":
            # Only re-project when the payload itself was paused. An internal
            # size-admission pause of a waiting payload must not overwrite the
            # committed waiting status with active.
            if raw_status == "paused":
                await guarded_update_download_and_active_user_tasks(
                    attempt_id,
                    {
                        "status": "active",
                        "error_code": None,
                        "error_message": None,
                    },
                    expected_gid=payload_gid,
                    user_status="active",
                )
            else:
                await guarded_update_download_and_active_user_tasks(
                    attempt_id,
                    {"error_code": None, "error_message": None},
                    expected_gid=payload_gid,
                )
        elif result_str == "soft_failed":
            # Soft path stamped failure code; keep live, do not clear ownership.
            await _broadcast_download_update(attempt_id)
        elif result_str == "missing":
            await _broadcast_download_update(attempt_id)
            return ReconcileResult.TERMINALIZED, None
        elif result_str == "stale":
            return ReconcileResult.STALE, None
        elif result_str == "rpc_unavailable":
            await _broadcast_download_update(attempt_id)
        # complete: do not clear here; fall through to completion dispatch

    # 9. Payload already complete → dispatch completion (spec §9.2 step 11).
    if raw_status == "complete" or outcome == "complete":
        await _broadcast_download_update(attempt_id)
        return ReconcileResult.CHANGED, (payload_gid, payload_status)

    await _broadcast_download_update(attempt_id)
    return ReconcileResult.CHANGED, None


async def switch_to_followed_download(
    *,
    backend: BackendPort,
    download: dict[str, Any],
    metadata_gid: str,
    followed_gid: str,
    display_name_fallback: str | None,
    log_prefix: str,
    complete_if_followed_complete: bool = False,
    _lock_held: bool = False,
    _real_status: dict[str, Any] | None = None,
) -> bool:
    """Thin wrapper: delegates to ``_handoff_locked`` (spec §9.2).

    Retained for backward-compatible callers (listener, sync, completion).
    Does NOT fabricate a ``complete`` event (spec §9.5).

    Fetches ``tell_status(followed_gid)`` OUTSIDE the lifecycle lock to
    avoid deadlock when another caller (e.g. cancel) holds or waits for
    the same lock.
    """
    download_id = int(download["id"])

    # Pre-fetch payload status outside the lock (spec §9.2 anti-deadlock).
    payload_status: dict[str, Any] | None = None
    if _real_status is not None:
        payload_status = _real_status
    else:
        try:
            payload_status = await backend.tell_status(followed_gid)
        except Exception as exc:
            if is_transient_rpc_error(exc) or is_missing_gid_error(exc):
                logger.debug(
                    "%s Handoff pre-fetch tell_status failed, "
                    "will retry inside lock attempt_id=%s payload=%s error=%s",
                    log_prefix,
                    download_id,
                    followed_gid,
                    exc,
                )
            else:
                raise

    lifecycle_lock = await get_download_lifecycle_lock(download_id)
    async with lifecycle_lock:
        snapshot = await get_global_download_status_snapshot(download_id)
        if snapshot is None:
            await _stop_untracked_gid_best_effort(
                backend, followed_gid, log_prefix
            )
            return False

        result, complete_dispatch = await _handoff_locked(
            backend=backend,
            attempt_id=download_id,
            source_gid=metadata_gid,
            payload_gid=followed_gid,
            snapshot=snapshot,
            download=download,
            log_prefix=log_prefix,
            _payload_status=payload_status,
        )

        if result == ReconcileResult.STALE:
            # Stale handoff: stop the untracked followed GID best-effort
            # but do NOT delete the shared download directory.
            await _stop_untracked_gid_best_effort(
                backend, followed_gid, log_prefix
            )

    # Complete dispatch outside the lock (spec §9.2 step 11: no fake event).
    if complete_dispatch is not None and complete_if_followed_complete:
        completion_gid, completion_status = complete_dispatch
        updated = await get_global_download_by_gid(completion_gid)
        if updated is not None:
            from app.services.lifecycle.completion import handle_v0_download_complete

            await handle_v0_download_complete(
                backend=backend,
                download=updated,
                aria2_status=completion_status,
                completion_gid=completion_gid,
                log_prefix=log_prefix,
                allow_metadata_handoff_defer=False,
            )

    return result in (
        ReconcileResult.CHANGED,
        ReconcileResult.COMPLETED,
        ReconcileResult.TERMINALIZED,
    )


async def resolve_download_for_gid(
    gid: str,
    aria2_status: dict[str, Any] | None,
) -> ResolveResult | None:
    """Purely resolve a GID observation to an attempt (spec §6.3).

    Returns ``ResolveResult`` with the matching attempt dict.  Does not
    update ``aria2_gid``, ``resource_kind``, ``status`` or any user task.

    - Direct match: ``aria2_gid == observed_gid`` returns the attempt.
    - Handoff candidate: ``following=source_gid`` in *aria2_status* finds
      the attempt whose ``aria2_gid == source_gid`` and returns it without
      writing the new GID.
    """
    download = await get_global_download_by_gid(gid)
    if download is not None:
        return ResolveResult(
            download=download,
            observed_gid=gid,
            source_gid=None,
        )

    following_gid = download_ops.following_gid(aria2_status) if aria2_status else None
    if not following_gid:
        return None

    source_download = await get_global_download_by_gid(str(following_gid))
    if source_download is None:
        return None

    logger.debug(
        "[Resolve] Handoff candidate: source_gid=%s observed_gid=%s attempt_id=%s",
        following_gid,
        gid,
        source_download["id"],
    )
    return ResolveResult(
        download=source_download,
        observed_gid=gid,
        source_gid=str(following_gid),
    )


def _followed_gid_from_rows(
    rows: object,
    metadata_gid: str,
) -> str | None:
    if not isinstance(rows, list):
        return None
    for row in rows:
        if download_ops.following_gid(row) != metadata_gid:
            continue

        gid = row.get("gid")
        if isinstance(gid, (str, int)) and str(gid):
            return str(gid)
    return None


async def _find_followed_gid_by_following(
    backend: BackendPort,
    metadata_gid: str,
    log_prefix: str,
) -> str | None:
    list_calls = (
        ("active", backend.tell_active, ()),
        ("waiting", backend.tell_waiting, (0, 1000)),
        ("stopped", backend.tell_stopped, (0, 1000)),
    )
    for label, call, args in list_calls:
        try:
            rows = await call(*args)
        except Exception as exc:
            logger.debug(
                "%s Failed to scan aria2 %s tasks for following=%s error=%s",
                log_prefix,
                label,
                metadata_gid,
                exc,
            )
            continue

        followed_gid = _followed_gid_from_rows(rows, metadata_gid)
        if followed_gid is not None:
            return followed_gid
    return None


async def _refresh_followed_gid(
    backend: BackendPort,
    gid: str | None,
    log_prefix: str,
) -> str | None:
    if not gid:
        return None

    for attempt in range(COMPLETE_SOURCE_RETRY_COUNT):
        try:
            status = await backend.tell_status(gid)
        except Exception as exc:
            logger.debug(
                "%s Failed to refresh metadata status gid=%s error=%s",
                log_prefix,
                gid,
                exc,
            )
        else:
            followed_gid = download_ops.first_followed_gid(status)
            if followed_gid is not None:
                return followed_gid

        followed_gid = await _find_followed_gid_by_following(backend, gid, log_prefix)
        if followed_gid is not None:
            return followed_gid

        if attempt < COMPLETE_SOURCE_RETRY_COUNT - 1:
            await asyncio.sleep(COMPLETE_SOURCE_RETRY_INTERVAL)

    return None


async def switch_to_late_followed_download_if_supported(
    *,
    backend: BackendPort,
    download: dict[str, Any],
    metadata_gid: str,
    display_name_fallback: str | None,
    log_prefix: str,
    complete_if_followed_complete: bool = False,
) -> bool:
    followed_gid = await _refresh_followed_gid(backend, metadata_gid, log_prefix)
    if followed_gid is None:
        return False

    return await switch_to_followed_download(
        backend=backend,
        download=download,
        metadata_gid=metadata_gid,
        followed_gid=followed_gid,
        display_name_fallback=display_name_fallback,
        log_prefix=log_prefix,
        complete_if_followed_complete=complete_if_followed_complete,
    )


async def defer_metadata_completion_if_handoff_pending(
    *,
    backend: BackendPort,
    download: dict[str, Any],
    aria2_status: dict[str, Any],
    metadata_gid: str,
    display_name_fallback: str | None,
    log_prefix: str,
) -> tuple[bool, bool]:
    if not download_ops.is_metadata_handoff_pending(download, aria2_status):
        return False, False

    switched = await switch_to_late_followed_download_if_supported(
        backend=backend,
        download=download,
        metadata_gid=metadata_gid,
        display_name_fallback=display_name_fallback,
        log_prefix=log_prefix,
    )
    if switched:
        return True, True

    logger.info(
        "%s Metadata download complete without followedBy, waiting for handoff id=%s gid=%s",
        log_prefix,
        download["id"],
        metadata_gid,
    )
    changed = await update_download_and_active_user_tasks(
        download_id=int(download["id"]),
        expected_gid=metadata_gid,
        global_values={"status": "active"},
        user_status="active",
    )
    return True, changed
