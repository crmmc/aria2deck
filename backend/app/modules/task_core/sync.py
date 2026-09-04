"""Minimal batch sync from backend into Task Core.

职责边界（M3 投影缓存）：本模块只负责 **读投影**——将后端快照写入进程内存观测仓
``observation_store``，并保留既有的
``global_downloads.status`` / ``completed_bytes`` 进度记账与暂停/排队
policy pass。状态机流转、完成/失败 handoff、终态落库与物理清理一律
归 ``reconcile_attempt_signal``（``app/services/lifecycle/coordinator.py``）
处理，本模块不得调用 ``claim_attempt_terminal`` / ``cleanup_with_claim`` /
``fail_download_and_reclaim`` 等终态路径。

v1 keeps the sync model simple: list live tids that already have a
backend gid, ask the backend for snapshots, and write the observed
``completed_bytes``/``status`` back to the global download row. After
progress bookkeeping, each still-live tid is run through the pause / queue
policy (``decide_on_snapshot`` + ``apply_decision``) so that
``quota_queued`` / ``disk_queued`` tasks resume when resources recover and
external pauses are recorded without auto-unpausing.

``apply_queue_policy`` is a lighter variant for production wiring: it skips
progress bookkeeping (the reconcile loop already handles that) and focuses
on the policy pass for queue-eligible tids only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.config import settings
from app.core.time_utils import now_ms
from app.domain.quota import get_disk_available_bytes
from app.modules.backend.port import BackendPort, Snapshot
from app.modules.task_core import observation_store
from app.modules.task_core.policy import (
    SYSTEM_QUEUE_CODES,
    QuotaContext,
    apply_decision,
    decide_on_snapshot,
)
from app.repositories.auth import get_user_by_id
from app.repositories.task.downloads import (
    get_global_download_by_id,
    list_tracked_global_downloads,
    update_global_download,
)
from app.repositories.task.user_tasks import get_representative_active_owner_id
from app.services.settings_service import get_min_free_disk
from app.services.usage_service import get_usage

_SYNCED_STATUSES = {"active", "waiting", "paused"}

# NOTE: sanitize import lives inside _upsert_snapshot_row to avoid a
# circular import (task_projection -> app.modules -> task_core.sync).


@dataclass(frozen=True)
class SyncReport:
    """Outcome of one sync_once() round."""

    fetched: int = 0
    updated: int = 0
    skipped: int = 0
    snapshots: list[Snapshot] = field(default_factory=list)


async def _build_quota_context(tid: int) -> QuotaContext:
    """Assemble resource facts for ``tid`` from its representative owner."""
    owner_id = await get_representative_active_owner_id(tid)
    if owner_id is None:
        return QuotaContext()
    user = await get_user_by_id(owner_id)
    if user is None:
        return QuotaContext()
    quota_bytes = int(user["quota_bytes"])
    usage = await get_usage(owner_id, quota_bytes)
    quota_used = int(usage["used_bytes"]) + int(usage["reserved_bytes"])
    return QuotaContext(
        quota_bytes=quota_bytes,
        quota_used_bytes=quota_used,
        disk_available=get_disk_available_bytes(
            settings.download_dir, min_free_disk=get_min_free_disk()
        ) > 0,
    )


async def _run_policy_for_snapshot(
    backend: BackendPort, snap: Snapshot, tid_row: dict
) -> bool:
    """Decide + apply policy for one live tid. Returns True if a decision was applied."""
    ctx = await _build_quota_context(snap.tid)
    decision = decide_on_snapshot(tid_row, str(snap.status), quota=ctx)
    if decision.action in ("noop", "keep"):
        return False
    await apply_decision(backend, snap.tid, decision)
    return True


async def _upsert_snapshot_row(snap: Snapshot) -> None:
    """Record one sanitized snapshot for ``snap`` into the observation store."""
    from app.services.aria2_snapshot_sanitize import sanitize_status

    sanitized = sanitize_status(snap.raw)
    observation_store.record_observed_detail(snap.tid, sanitized, now_ms())


async def record_observed_snapshot(
    *,
    tid: int,
    observed_status: dict[str, Any],
) -> None:
    """Persist one live-observed backend status into the observation store.

    The production sync loop is a trigger-only observer for the lifecycle
    coordinator; this keeps the snapshot read-model fresh so REST lists and
    WS broadcasts (speed/progress) reflect live downloads.
    """
    status = str(observed_status.get("status") or "")
    if not status:
        return
    await _upsert_snapshot_row(Snapshot(tid=tid, status=status, raw=observed_status))


async def sync_once(backend: BackendPort) -> SyncReport:
    """Run one batch sync round against the backend.

    TEST / TASK-CORE API ONLY — do not wire into the production lifespan.
    Production sync is the trigger-only observer in app/aria2/sync.py plus
    the coordinator observation gate; wiring this in would double-write
    completed_bytes/status.
    

    Lists live tids with a gid, calls ``backend.tell_many``, writes
    ``completed_bytes`` plus ``status`` back to ``global_downloads``, then
    runs the pause / queue policy for each still-live tid.
    Terminal statuses observed from the backend are only recorded when the
    DB row is still active; completion file linking is Task 5.
    """
    live = await list_tracked_global_downloads(_SYNCED_STATUSES)
    tids = [int(row["id"]) for row in live]
    if not tids:
        return SyncReport()

    snapshots = await backend.tell_many(tids)
    updated = 0
    skipped = 0
    for snap in snapshots:
        # Re-read to ensure the row still exists and is still active.
        current = await get_global_download_by_id(snap.tid)
        if current is None:
            skipped += 1
            continue
        if str(current.get("status")) not in _SYNCED_STATUSES:
            skipped += 1
            continue
        await _upsert_snapshot_row(snap)
        completed = int(snap.raw.get("completedLength", 0) or 0)
        status = str(snap.status)
        if status not in _SYNCED_STATUSES and status not in {"complete", "error", "removed"}:
            status = str(current.get("status"))
        # Map aria2 terminal statuses to DB strings; Task 5 owns the full
        # completion/cancel handoff. For v1 we only record them.
        db_status = {
            "complete": "completed",
            "error": "failed",
            "removed": "cancelled",
        }.get(status, status)
        values: dict = {"completed_bytes": completed}
        if db_status != str(current.get("status")):
            values["status"] = db_status
        await update_global_download(snap.tid, values)
        updated += 1

        # Policy pass: only for rows that are still live after progress write.
        if db_status not in _SYNCED_STATUSES:
            continue
        await _run_policy_for_snapshot(backend, snap, current)
    return SyncReport(
        fetched=len(snapshots),
        updated=updated,
        skipped=skipped,
        snapshots=snapshots,
    )


async def apply_queue_policy(backend: BackendPort) -> SyncReport:
    """Run only the policy pass for queue-eligible tids.

    Intended for production wiring alongside the existing reconcile loop:
    lists live downloads whose ``error_code`` is a system queue code or whose
    status is ``paused``, fetches fresh snapshots, and applies
    ``decide_on_snapshot`` + ``apply_decision``. Skips progress bookkeeping
    that the reconcile loop already handles.
    """
    live = await list_tracked_global_downloads(_SYNCED_STATUSES)
    eligible = [
        row
        for row in live
        if row.get("error_code") in SYSTEM_QUEUE_CODES
        or str(row.get("status")) == "paused"
    ]
    tids = [int(row["id"]) for row in eligible]
    if not tids:
        return SyncReport()

    snapshots = await backend.tell_many(tids)
    updated = 0
    skipped = 0
    for snap in snapshots:
        current = await get_global_download_by_id(snap.tid)
        if current is None:
            skipped += 1
            continue
        if str(current.get("status")) not in _SYNCED_STATUSES:
            skipped += 1
            continue
        await _upsert_snapshot_row(snap)
        if await _run_policy_for_snapshot(backend, snap, current):
            updated += 1
    return SyncReport(
        fetched=len(snapshots),
        updated=updated,
        skipped=skipped,
        snapshots=snapshots,
    )
