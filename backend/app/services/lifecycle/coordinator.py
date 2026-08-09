"""Lifecycle coordinator extracted from ``aria2_lifecycle_service.py`` (M4 T09).

Hosts ``reconcile_attempt_signal``, the unified entry that resolves a single
aria2 observation (listener event or sync poll) against the attempt lock and
delegates terminal transitions to cleanup, handoff, and completion paths.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from app.modules.backend.port import BackendPort
from app.core.security import sanitize_string
from app.core.time_utils import now_ms
from app.domain.lifecycle import ReconcileResult
from app.domain.locks import get_download_lifecycle_lock
from app.domain.quota import candidate_size_from_status
from app.domain.status import (
    ACTIVE_USER_TASK_STATUSES,
    TERMINAL_DOWNLOAD_STATUSES,
)
from app.repositories.task.downloads import (
    get_global_download_by_gid,
    get_global_download_status_snapshot,
    guarded_update_download_and_active_user_tasks,
)
from app.services import download_ops
from app.services.aria2_error_messages import prefer_aria2_error_message
from app.services.lifecycle._shared import (
    _broadcast_download_update,
    _map_v0_status,
    is_missing_gid_error,
    is_transient_rpc_error,
)
from app.services.lifecycle.cleanup import (
    _reclaim_terminal_with_claim,
    _terminalize_missing_gid_locked,
    fail_download_and_reclaim,
)
from app.services.lifecycle.handoff import (
    _handoff_locked,
    coordinate_reported_size,
    resolve_download_for_gid,
)
from app.services.lifecycle.completion import handle_v0_download_complete
from app.services.task_projection import (
    has_live_bt_evidence,
    is_bt_resource_kind,
    is_metadata_phase_status,
)

logger = logging.getLogger(__name__)

V0_SYNC_TRACKED_STATUSES = ACTIVE_USER_TASK_STATUSES


def _has_bittorrent_evidence(
    status: dict[str, Any],
    download: dict[str, Any],
) -> bool:
    return is_bt_resource_kind(download) or has_live_bt_evidence(status)


async def reconcile_attempt_signal(
    *,
    backend: BackendPort,
    observed_gid: str,
    event: str | None,
    observed_status: Mapping[str, Any] | None,
    observed_error: Exception | None = None,
    log_prefix: str,
) -> ReconcileResult:
    """Unified coordinator entry for a single lifecycle observation (spec §6).

    T10: normal status projection (active/waiting/paused), error/removed
    terminalization, and missing-GID handling are resolved inside the
    attempt lock.  Complete still delegates to the existing complete path.
    Size coordination and handoff remain for T11/T12.
    """
    # 1. Pure resolve (no DB writes).
    status_for_resolve: dict[str, Any] | None = (
        dict(observed_status) if observed_status else None
    )
    resolved = await resolve_download_for_gid(observed_gid, status_for_resolve)
    if resolved is None:
        logger.debug(
            "%s No attempt for observed_gid=%s event=%s",
            log_prefix,
            observed_gid,
            event,
        )
        return ReconcileResult.IGNORED

    attempt_id = int(resolved.download["id"])
    is_handoff = resolved.is_handoff_candidate

    # 2. Attempt lock + reread.
    lifecycle_lock = await get_download_lifecycle_lock(attempt_id)

    # Set by the complete/handoff branch for post-lock delegation.
    complete_dispatch: tuple[str, dict[str, Any]] | None = None

    async with lifecycle_lock:
        snapshot = await get_global_download_status_snapshot(attempt_id)
        if snapshot is None:
            logger.debug(
                "%s Attempt vanished during reconcile attempt_id=%s",
                log_prefix,
                attempt_id,
            )
            return ReconcileResult.IGNORED

        current_gid = str(snapshot.get("aria2_gid") or "")
        status_before = str(snapshot.get("status") or "")
        completed_file_id = snapshot.get("completed_file_id")
        total_bytes = download_ops.safe_int(snapshot.get("total_bytes"))

        logger.info(
            "%s reconcile attempt_id=%s observed_gid=%s current_gid=%s "
            "status_before=%s event=%s fence=acquired",
            log_prefix,
            attempt_id,
            observed_gid,
            current_gid,
            status_before,
            event,
        )

        # ---- Missing GID (spec §14.1) ----
        if observed_error is not None and is_missing_gid_error(observed_error):
            if current_gid != observed_gid:
                logger.info(
                    "%s Missing GID stale: observed=%s current=%s attempt_id=%s",
                    log_prefix,
                    observed_gid,
                    current_gid,
                    attempt_id,
                )
                return ReconcileResult.STALE
            if status_before == "completed":
                if completed_file_id is not None:
                    return ReconcileResult.ALREADY_COMPLETE
                logger.info(
                    "%s Missing GID + completed without file, recovery_pending attempt_id=%s",
                    log_prefix,
                    attempt_id,
                )
                return ReconcileResult.RECOVERY_PENDING
            if status_before in ("failed", "cancelled"):
                return ReconcileResult.ALREADY_TERMINAL
            # Live attempt: terminalize based on trusted-size availability.
            return await _terminalize_missing_gid_locked(
                backend=backend,
                attempt_id=attempt_id,
                current_gid=current_gid,
                total_bytes=total_bytes,
                log_prefix=log_prefix,
            )

        # ---- Transient RPC error → waiting (spec §6.2.4) ----
        if observed_error is not None and is_transient_rpc_error(observed_error):
            logger.debug(
                "%s Transient RPC error, waiting attempt_id=%s",
                log_prefix,
                attempt_id,
            )
            return ReconcileResult.WAITING

        # ---- Generic terminal check ----
        if status_before in TERMINAL_DOWNLOAD_STATUSES:
            if status_before == "completed":
                return ReconcileResult.ALREADY_COMPLETE
            logger.debug(
                "%s Already terminal attempt_id=%s status=%s",
                log_prefix,
                attempt_id,
                status_before,
            )
            return ReconcileResult.ALREADY_TERMINAL

        # ---- Fencing ----
        fence_gid = resolved.source_gid if is_handoff else observed_gid
        if current_gid != fence_gid:
            logger.info(
                "%s Stale: fence_gid=%s current_gid=%s attempt_id=%s handoff=%s",
                log_prefix,
                fence_gid,
                current_gid,
                attempt_id,
                is_handoff,
            )
            return ReconcileResult.STALE

        # ---- Handoff candidate: call _handoff_locked (spec §9) ----
        if is_handoff:
            result, complete_info = await _handoff_locked(
                backend=backend,
                attempt_id=attempt_id,
                source_gid=str(resolved.source_gid),
                payload_gid=observed_gid,
                snapshot=snapshot,
                download=resolved.download,
                log_prefix=log_prefix,
                _payload_status=(
                    dict(observed_status) if observed_status else None
                ),
            )
            if complete_info is not None:
                complete_dispatch = complete_info
            if result == ReconcileResult.CHANGED:
                if complete_dispatch is None:
                    return ReconcileResult.CHANGED
                # Handoff committed + payload complete: fall through to
                # end of lock, then post-lock complete delegation.
                # Must NOT enter the projection section below — it would
                # use the stale pre-handoff current_gid for tell_status.
            else:
                return result

        else:
            # ---- Determine aria2 status ----
            working_status: dict[str, Any] | None = (
                dict(observed_status) if observed_status else None
            )
            if working_status is None:
                if not current_gid:
                    return ReconcileResult.WAITING
                try:
                    working_status = await backend.tell_status(current_gid)
                except Exception as exc:
                    logger.debug(
                        "%s tell_status failed attempt_id=%s error=%s",
                        log_prefix,
                        attempt_id,
                        exc,
                    )
                    if is_transient_rpc_error(exc):
                        return ReconcileResult.WAITING
                    if is_missing_gid_error(exc):
                        return await _terminalize_missing_gid_locked(
                            backend=backend,
                            attempt_id=attempt_id,
                            current_gid=current_gid,
                            total_bytes=total_bytes,
                            log_prefix=log_prefix,
                        )
                    raise

            raw_status = str(working_status.get("status") or "")

            # ---- error/removed → terminalize inside lock ----
            if raw_status in ("error", "removed"):
                if raw_status == "removed":
                    message = "外部取消（管理员/外部客户端）"
                    error_code = "removed"
                else:
                    raw_error = working_status.get("errorMessage")
                    message = prefer_aria2_error_message(
                        working_status.get("errorCode"),
                        str(raw_error) if raw_error is not None else None,
                        "后端错误",
                    )
                    error_code = str(working_status.get("errorCode") or "error")
                logger.warning(
                    "%s Terminalizing attempt_id=%s raw_status=%s error_code=%s "
                    "aria2_error=%s",
                    log_prefix,
                    attempt_id,
                    raw_status,
                    error_code,
                    working_status.get("errorMessage"),
                )
                changed = await fail_download_and_reclaim(
                    backend=backend,
                    download_id=attempt_id,
                    message=sanitize_string(message),
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

            # ---- complete → handoff or delegate completion ----
            if raw_status == "complete":
                followed_gid = download_ops.first_followed_gid(working_status)
                if followed_gid:
                    # Pre-fetch followed status before _handoff_locked
                    # to avoid redundant RPC inside the lock.
                    pre_fetched: dict[str, Any] | None = None
                    try:
                        pre_fetched = await backend.tell_status(followed_gid)
                    except Exception as exc:
                        if is_transient_rpc_error(exc) or is_missing_gid_error(
                            exc
                        ):
                            logger.debug(
                                "%s Complete+followedBy pre-fetch failed, "
                                "attempt_id=%s payload=%s error=%s",
                                log_prefix,
                                attempt_id,
                                followed_gid,
                                exc,
                            )
                        else:
                            raise
                    result, complete_info = await _handoff_locked(
                        backend=backend,
                        attempt_id=attempt_id,
                        source_gid=current_gid,
                        payload_gid=followed_gid,
                        snapshot=snapshot,
                        download=resolved.download,
                        log_prefix=log_prefix,
                        _payload_status=pre_fetched,
                    )
                    if complete_info is not None:
                        complete_dispatch = complete_info
                    if result == ReconcileResult.CHANGED:
                        if complete_dispatch is not None:
                            pass  # Fall through to post-lock complete delegation.
                        else:
                            return ReconcileResult.CHANGED
                    else:
                        return result
                else:
                    complete_dispatch = (current_gid, working_status)

            # ---- active/waiting/paused → guarded projection (spec §8.1) ----
            elif raw_status in ("active", "waiting", "paused"):
                bt_evidence = _has_bittorrent_evidence(
                    working_status, resolved.download
                )
                is_metadata = bt_evidence and is_metadata_phase_status(
                    working_status
                )
                admission: dict[str, Any] | None = None

                # Size admission for non-metadata live states (spec §8.2).
                # coordinate_reported_size is a lock-internal action here;
                # acquire_lifecycle_lock=False because we already hold the lock.
                if not is_metadata:
                    require_trusted = not bool(
                        resolved.download.get("size_known")
                    )
                    size_candidate = candidate_size_from_status(
                        working_status, require_trusted_total=require_trusted
                    )
                    if require_trusted or size_candidate is not None:
                        admission = await coordinate_reported_size(
                            backend=backend,
                            download=resolved.download,
                            expected_gid=current_gid,
                            control_gid=current_gid,
                            status=working_status,
                            require_trusted_total=require_trusted,
                            acquire_lifecycle_lock=False,
                        )
                        size_outcome = str(admission.get("outcome") or "")
                        if size_outcome == "stale":
                            return ReconcileResult.STALE
                        if size_outcome == "terminalized":
                            await _broadcast_download_update(attempt_id)
                            return ReconcileResult.TERMINALIZED
                        if size_outcome == "rpc_unavailable":
                            pass  # Skip size admission, continue with projection
                        elif size_outcome == "complete":
                            complete_dispatch = (
                                current_gid, working_status,
                            )
                        elif size_outcome == "unknown_size":
                            changed = await fail_download_and_reclaim(
                                backend=backend,
                                download_id=attempt_id,
                                message="任务运行时无法确认可信文件大小",
                                error_code="unknown_size",
                                expected_gid=current_gid,
                                writer_gid=current_gid,
                                acquire_lifecycle_lock=False,
                                log_prefix=log_prefix,
                            )
                            if changed:
                                await _broadcast_download_update(attempt_id)
                                return ReconcileResult.TERMINALIZED
                            return ReconcileResult.STALE
                        elif size_outcome != "admitted":
                            # disk_budget / max_task_size / no_subscribers:
                            # row already terminal in DB, do physical reclaim
                            # via repair claim (no skip_status_check).
                            await _reclaim_terminal_with_claim(
                                backend=backend,
                                download_id=attempt_id,
                                gid=current_gid,
                                log_prefix=log_prefix,
                            )
                            await _broadcast_download_update(attempt_id)
                            return ReconcileResult.TERMINALIZED

                prev_status = str(snapshot.get("status") or "")
                prev_error_code = str(snapshot.get("error_code") or "")
                size_paused_by_us = bool(
                    admission is not None and admission.get("paused_by_us")
                )

                mapped = _map_v0_status(
                    working_status,
                    attempt_id,
                    prefer_bittorrent_name=bt_evidence,
                )

                # Whether the admission path just learned a trusted size in
                # this reconcile (unknown before, admitted now).
                size_just_admitted = (
                    admission is not None
                    and str(admission.get("outcome") or "") == "admitted"
                    and not bool(resolved.download.get("size_known"))
                )

                global_values: dict[str, Any] = {
                    "status": mapped["status"],
                    "completed_bytes": mapped["completed_bytes"],
                    "updated_at_ms": now_ms(),
                }
                if size_just_admitted:
                    # Mark the pause as system admission ownership so policy
                    # resumes it instead of treating it as external.
                    global_values["error_code"] = "admission_paused"
                    global_values["error_message"] = None
                if not is_metadata:
                    global_values["total_bytes"] = mapped["total_bytes"]
                    if mapped["display_name"]:
                        global_values["display_name"] = mapped["display_name"]

                # Pause projection (arch M2):
                # - never auto-unpause merely because size_known (removed a554c30 path)
                # - never mark metadata-phase pause as external
                # - never mark pause that size-admission just owned (paused_by_us)
                # - never overwrite growth/handoff/admission or queue error codes
                # - only when transitioning into paused from a non-paused live status
                protected_error_codes = {
                    "growth_pause_failed",
                    "growth_unpause_failed",
                    "unpause_failed",
                    "handoff_unknown_size",
                    "unknown_size",
                    "disk_budget",
                    "disk_budget_exceeded",
                    "max_task_size",
                    "admission_rejected",
                    "quota_queued",
                    "disk_queued",
                }
                # Unknown-size initial submit admission pause is
                # system-owned: do not brand it as external (policy will
                # resume it via ``admission_paused``).
                admission_initial_submit_pause = (
                    event != "pause" and size_just_admitted
                )
                if (
                    mapped["status"] == "paused"
                    and not is_metadata
                    and not size_paused_by_us
                    and prev_status in {"active", "queued", "waiting"}
                    and prev_error_code not in protected_error_codes
                    and not admission_initial_submit_pause
                ):
                    global_values["error_message"] = "任务已被外部暂停"
                    global_values["error_code"] = "external_paused"
                    global_values["disk_reserved_bytes"] = max(
                        0,
                        download_ops.safe_int(snapshot.get("completed_bytes")),
                    )
                elif mapped["status"] == "active" and prev_error_code in {
                    "external_paused",
                    "admin_paused",
                }:
                    # Clear sticky external-pause hint when download resumes.
                    global_values["error_code"] = None
                    global_values["error_message"] = None

                updated = await guarded_update_download_and_active_user_tasks(
                    attempt_id,
                    global_values,
                    expected_gid=current_gid,
                    user_status=mapped["status"],
                    display_name=(
                        mapped["display_name"] if not is_metadata else None
                    ),
                )
                if updated is not None:
                    await _broadcast_download_update(attempt_id)
                    logger.debug(
                        "%s Projected attempt_id=%s status_after=%s",
                        log_prefix,
                        attempt_id,
                        mapped["status"],
                    )
                    return ReconcileResult.CHANGED
                return ReconcileResult.STALE

            else:
                logger.debug(
                    "%s Unknown raw_status=%s, waiting attempt_id=%s",
                    log_prefix,
                    raw_status,
                    attempt_id,
                )
                return ReconcileResult.WAITING

    # ---- Post-lock: delegate completion (spec §9.2 step 11: no fake event) ----
    if complete_dispatch is not None:
        completion_gid, completion_status = complete_dispatch
        try:
            updated = await get_global_download_by_gid(completion_gid)
            if updated is not None:
                changed = await handle_v0_download_complete(
                    backend=backend,
                    download=updated,
                    aria2_status=completion_status,
                    completion_gid=completion_gid,
                    log_prefix=log_prefix,
                )
            else:
                changed = False
        except Exception as exc:
            if is_transient_rpc_error(exc):
                logger.debug(
                    "%s Complete delegation hit transient RPC error attempt_id=%s",
                    log_prefix,
                    attempt_id,
                )
                return ReconcileResult.WAITING
            raise
        if changed:
            await _broadcast_download_update(attempt_id)
            return ReconcileResult.COMPLETED
        return ReconcileResult.WAITING

    return ReconcileResult.WAITING
