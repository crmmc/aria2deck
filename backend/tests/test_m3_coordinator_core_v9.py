"""T08: pure resolve and unified reconcile_attempt_signal entry tests.

Verifies (spec §6.2-6.3, task T08):
1. Exact current GID resolves to the attempt; reconcile returns non-ignored.
2. Payload ``following`` finds source attempt candidate without writing GID.
3. Unrelated observed_gid -> ignored/stale, no DB writes.
4. Pure resolve path performs zero repository update calls.
5. Transient RPC error -> waiting, task not failed.
6. Current GID already changed -> stale for the old observed_gid.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from app.domain.lifecycle import ReconcileResult
from app.services.lifecycle import cleanup as cleanup_mod
from app.services.lifecycle import coordinator as coordinator_mod
from app.services.lifecycle import handoff as handoff_mod
from app.services.lifecycle.coordinator import reconcile_attempt_signal
from app.services.lifecycle.handoff import (
    ResolveResult,
    resolve_download_for_gid,
)
from tests.helpers_v0 import (
    create_global_download_v0,
    create_user_task_v0,
    create_user_v0,
)


def _make_client(
    *,
    tell_status_side_effect: Any = None,
) -> AsyncMock:
    client = AsyncMock()
    if tell_status_side_effect is not None:
        client.tell_status.side_effect = tell_status_side_effect
    else:
        client.tell_status.return_value = {"status": "active", "totalLength": "0"}
    return client


def _transient_error() -> Exception:
    return ConnectionError("cannot connect to host localhost:6800")


# ---------------------------------------------------------------------------
# 1. Exact current GID resolves; reconcile returns non-ignored
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_exact_current_gid_resolves_and_reconciles(temp_db: str):
    user = await create_user_v0(username="t08_exact")
    download = await create_global_download_v0(
        resource_key="magnet:t08_exact",
        source_uri="magnet:?xt=urn:btih:t08_exact",
        resource_kind="magnet",
        status="active",
        aria2_gid="gid_current_001",
        total_bytes=1024,
        size_known=True,
    )

    resolved = await resolve_download_for_gid(
        "gid_current_001", {"status": "active"}
    )
    assert resolved is not None
    assert resolved.download["id"] == download["id"]
    assert resolved.source_gid is None
    assert not resolved.is_handoff_candidate

    client = _make_client()
    result = await reconcile_attempt_signal(
        backend=client,
        observed_gid="gid_current_001",
        event="start",
        observed_status={"status": "active", "totalLength": "1024"},
        log_prefix="[T08]",
    )
    assert result != ReconcileResult.IGNORED


# ---------------------------------------------------------------------------
# 2. Payload following finds source candidate but does not write GID
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_following_finds_source_without_writing_gid(temp_db: str):
    user = await create_user_v0(username="t08_following")
    download = await create_global_download_v0(
        resource_key="magnet:t08_following",
        source_uri="magnet:?xt=urn:btih:t08_following",
        resource_kind="magnet",
        status="active",
        aria2_gid="gid_source_002",
        total_bytes=1024,
        size_known=True,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
    )

    payload_gid = "gid_payload_002"
    observed_status = {
        "status": "waiting",
        "following": "gid_source_002",
        "totalLength": "2048",
        "completedLength": "0",
        "files": [{"path": "/tmp/payload.bin", "length": "2048"}],
    }

    resolved = await resolve_download_for_gid(payload_gid, observed_status)
    assert resolved is not None
    assert resolved.download["id"] == download["id"]
    assert resolved.source_gid == "gid_source_002"
    assert resolved.is_handoff_candidate

    from app.repositories.task.downloads import get_global_download_by_gid

    # Pure resolve must not write payload GID onto the source attempt.
    db_row = await get_global_download_by_gid("gid_source_002")
    assert db_row is not None
    assert db_row["aria2_gid"] == "gid_source_002"
    assert await get_global_download_by_gid(payload_gid) is None

    client = _make_client()
    client.tell_status.return_value = {
        "gid": payload_gid,
        "status": "waiting",
        "following": "gid_source_002",
        "totalLength": "2048",
        "completedLength": "0",
        "files": [{"path": "/tmp/payload.bin", "length": "2048"}],
    }
    result = await reconcile_attempt_signal(
        backend=client,
        observed_gid=payload_gid,
        event="start",
        observed_status=observed_status,
        log_prefix="[T08]",
    )
    assert result in {ReconcileResult.WAITING, ReconcileResult.CHANGED}
    if result == ReconcileResult.CHANGED:
        after = await get_global_download_by_gid(payload_gid)
        assert after is not None
        assert after["aria2_gid"] == payload_gid
    else:
        source_after = await get_global_download_by_gid("gid_source_002")
        assert source_after is not None
        assert source_after["aria2_gid"] == "gid_source_002"


# ---------------------------------------------------------------------------
# 3. Unrelated observed_gid -> ignored, no DB writes
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unrelated_gid_returns_ignored(temp_db: str):
    user = await create_user_v0(username="t08_unrelated")
    await create_global_download_v0(
        resource_key="magnet:t08_unrelated",
        source_uri="magnet:?xt=urn:btih:t08_unrelated",
        resource_kind="magnet",
        status="active",
        aria2_gid="gid_unrelated_existing",
        total_bytes=512,
        size_known=True,
    )

    resolved = await resolve_download_for_gid(
        "gid_completely_unknown", {"status": "active"}
    )
    assert resolved is None

    client = _make_client()
    result = await reconcile_attempt_signal(
        backend=client,
        observed_gid="gid_completely_unknown",
        event="start",
        observed_status={"status": "active"},
        log_prefix="[T08]",
    )
    assert result == ReconcileResult.IGNORED


# ---------------------------------------------------------------------------
# 4. Pure resolve path: zero repository update calls
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pure_resolve_no_repository_writes(temp_db: str):
    update_spies = {
        "guarded_update_global_download": (
            "app.repositories.task.downloads.guarded_update_global_download"
        ),
        "guarded_update_download_and_active_user_tasks": (
            "app.repositories.task.downloads."
            "guarded_update_download_and_active_user_tasks"
        ),
    }

    user = await create_user_v0(username="t08_pure")
    download = await create_global_download_v0(
        resource_key="magnet:t08_pure",
        source_uri="magnet:?xt=urn:btih:t08_pure",
        resource_kind="magnet",
        status="active",
        aria2_gid="gid_pure_direct",
        total_bytes=256,
        size_known=True,
    )

    payload_gid = "gid_pure_payload"
    observed_status = {
        "status": "active",
        "following": "gid_pure_direct",
    }

    patches = [
        patch(target, new=AsyncMock())
        for target in update_spies.values()
    ]
    for p in patches:
        p.start()
    try:
        resolved_direct = await resolve_download_for_gid(
            "gid_pure_direct", {"status": "active"}
        )
        assert resolved_direct is not None

        resolved_handoff = await resolve_download_for_gid(
            payload_gid, observed_status
        )
        assert resolved_handoff is not None
        assert resolved_handoff.source_gid == "gid_pure_direct"

        for target, p in zip(update_spies, patches):
            mock_obj = p.new
            assert mock_obj.call_count == 0, (
                f"{target} was called during pure resolve"
            )
    finally:
        for p in patches:
            p.stop()


# ---------------------------------------------------------------------------
# 5. Transient RPC error -> waiting, not failed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_transient_rpc_error_returns_waiting(temp_db: str):
    user = await create_user_v0(username="t08_transient")
    download = await create_global_download_v0(
        resource_key="magnet:t08_transient",
        source_uri="magnet:?xt=urn:btih:t08_transient",
        resource_kind="magnet",
        status="active",
        aria2_gid="gid_transient_003",
        total_bytes=512,
        size_known=True,
    )

    fail_mock = AsyncMock(return_value=False)
    with patch.object(
        cleanup_mod, "fail_download_and_reclaim", fail_mock
    ):
        err = _transient_error()
        client = _make_client(tell_status_side_effect=err)

        result = await reconcile_attempt_signal(
            backend=client,
            observed_gid="gid_transient_003",
            event="start",
            observed_status={"status": "active"},
            observed_error=err,
            log_prefix="[T08]",
        )

    assert result == ReconcileResult.WAITING
    assert fail_mock.call_count == 0, (
        "fail_download_and_reclaim must not be called on transient RPC error"
    )

    from app.repositories.task.downloads import get_global_download_status_snapshot

    snapshot = await get_global_download_status_snapshot(download["id"])
    assert snapshot is not None
    assert snapshot["status"] == "active"


# ---------------------------------------------------------------------------
# 6. Current GID changed -> stale for handoff candidate
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stale_when_current_gid_changed(temp_db: str):
    """When DB current_gid changed after resolve but before fencing,
    reconcile returns stale.

    The scenario: resolve finds the attempt by direct GID match, but
    the lock reread shows current_gid has already changed (e.g. handoff
    completed between resolve and lock acquisition).  This is a genuine
    stale signal.
    """
    user = await create_user_v0(username="t08_stale")
    download = await create_global_download_v0(
        resource_key="magnet:t08_stale",
        source_uri="magnet:?xt=urn:btih:t08_stale",
        resource_kind="magnet",
        status="active",
        aria2_gid="gid_original",
        total_bytes=512,
        size_known=True,
    )

    # Resolve finds the attempt by direct GID match.
    resolved = await resolve_download_for_gid(
        "gid_original", {"status": "active"}
    )
    assert resolved is not None
    assert resolved.source_gid is None

    # Mock the in-lock snapshot to return a changed GID, simulating a
    # handoff that completed between the resolve and the lock reread.
    fake_snapshot = {
        "id": download["id"],
        "aria2_gid": "gid_new_after_handoff",
        "status": "active",
        "completed_file_id": None,
        "completed_bytes": 0,
        "total_bytes": 512,
    }

    client = _make_client()
    with patch.object(
        coordinator_mod,
        "get_global_download_status_snapshot",
        new=AsyncMock(return_value=fake_snapshot),
    ):
        result = await reconcile_attempt_signal(
            backend=client,
            observed_gid="gid_original",
            event="start",
            observed_status={"status": "active"},
            log_prefix="[T08]",
        )
    assert result == ReconcileResult.STALE


# ---------------------------------------------------------------------------
# 09-05 fix-pause-ownership-loss: projection must never clear system pause
# ownership from a (possibly stale) active observation, and an unconfirmed
# unpause (rpc_unavailable) must not project the pre-control status.
# ---------------------------------------------------------------------------


async def _fetch_download_row(download_id) -> dict:
    from sqlalchemy import select  # noqa: PLC0415  # local import keeps module head unchanged

    from app.db.engine import transaction
    from app.db.schema import global_downloads

    async with transaction() as conn:
        row = (
            await conn.execute(
                select(global_downloads).where(global_downloads.c.id == download_id)
            )
        ).mappings().one()
    return dict(row)


@pytest.mark.asyncio
async def test_projection_active_keeps_system_pause_codes(temp_db: str):
    """A (possibly stale) active observation must not clear owned pause codes.

    Pre-fix: the generic ACTIVE_CLEAR branch wiped system pause codes on
    projected active, so the next paused observation branded the task
    external_paused (permanent dead end by design). Sweep finding: the
    exposure covered every SYSTEM_OWNED code (queue codes included), so the
    regression locks quota_queued as well.
    """
    from app.modules.task_core.states import (
        ERROR_ADMISSION_PAUSED,
        ERROR_EXTERNAL_PAUSED,
        ERROR_GROWTH_UNPAUSE_FAILED,
        ERROR_QUOTA_QUEUED,
    )

    user = await create_user_v0(username="t09_stale_active", quota_bytes=10_000_000)

    growth_case = await create_global_download_v0(
        resource_key="http:t09-growth-unpause-code",
        source_uri="https://example.com/t09-growth.bin",
        resource_kind="http",
        status="paused",
        aria2_gid="gid_t09_growth",
        total_bytes=2048,
        completed_bytes=0,
        disk_reserved_bytes=2048,
        size_known=True,
        error_code=ERROR_GROWTH_UNPAUSE_FAILED,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=growth_case["id"],
        status="paused",
        reserved_bytes=2048,
    )

    admission_case = await create_global_download_v0(
        resource_key="http:t09-admission-code",
        source_uri="https://example.com/t09-admission.bin",
        resource_kind="http",
        status="paused",
        aria2_gid="gid_t09_admission",
        total_bytes=2048,
        completed_bytes=0,
        disk_reserved_bytes=2048,
        size_known=True,
        error_code=ERROR_ADMISSION_PAUSED,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=admission_case["id"],
        status="paused",
        reserved_bytes=2048,
    )

    external_case = await create_global_download_v0(
        resource_key="http:t09-external-code",
        source_uri="https://example.com/t09-external.bin",
        resource_kind="http",
        status="paused",
        aria2_gid="gid_t09_external",
        total_bytes=2048,
        completed_bytes=0,
        disk_reserved_bytes=2048,
        size_known=True,
        error_code=ERROR_EXTERNAL_PAUSED,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=external_case["id"],
        status="paused",
        reserved_bytes=2048,
    )

    quota_case = await create_global_download_v0(
        resource_key="http:t09-quota-code",
        source_uri="https://example.com/t09-quota.bin",
        resource_kind="http",
        status="paused",
        aria2_gid="gid_t09_quota",
        total_bytes=2048,
        completed_bytes=0,
        disk_reserved_bytes=2048,
        size_known=True,
        error_code=ERROR_QUOTA_QUEUED,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=quota_case["id"],
        status="paused",
        reserved_bytes=2048,
    )

    client = _make_client()
    observed = {"status": "active", "totalLength": "2048", "completedLength": "0"}
    for gid in ("gid_t09_growth", "gid_t09_admission", "gid_t09_external", "gid_t09_quota"):
        result = await reconcile_attempt_signal(
            backend=client,
            observed_gid=gid,
            event=None,
            observed_status=dict(observed),
            log_prefix="[T09]",
        )
        assert result == ReconcileResult.CHANGED

    growth_row = await _fetch_download_row(growth_case["id"])
    assert growth_row["error_code"] == ERROR_GROWTH_UNPAUSE_FAILED
    admission_row = await _fetch_download_row(admission_case["id"])
    assert admission_row["error_code"] == ERROR_ADMISSION_PAUSED
    quota_row = await _fetch_download_row(quota_case["id"])
    assert quota_row["error_code"] == ERROR_QUOTA_QUEUED
    # external_paused MUST still clear on observed active (operator release
    # path via an external client — protected semantics, do not regress).
    external_row = await _fetch_download_row(external_case["id"])
    assert external_row["error_code"] is None


@pytest.mark.asyncio
async def test_admission_unpause_rpc_unavailable_keeps_pause_state(temp_db: str):
    """Growth admission unpause transient failure → no stale active projection.

    Pre-fix: the coordinator projected the pre-pause observed "active" over
    the row while the payload was actually paused, opening the
    external_paused branding window on the next sync round.
    """
    from app.modules.task_core.states import ERROR_ADMISSION_PAUSED

    user = await create_user_v0(username="t09_rpc_unavailable", quota_bytes=10_000_000)
    download = await create_global_download_v0(
        resource_key="http:t09-rpc-unavailable",
        source_uri="https://example.com/t09-rpc.bin",
        resource_kind="http",
        status="paused",
        aria2_gid="gid_t09_rpc",
        total_bytes=1024,
        completed_bytes=0,
        disk_reserved_bytes=1024,
        size_known=True,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="paused",
        reserved_bytes=1024,
    )

    transient = ConnectionError("cannot connect to host localhost:6800")
    client = AsyncMock()
    client.pause_gid.return_value = "OK"
    client.unpause_gid.side_effect = transient
    client.tell_status.side_effect = transient

    result = await reconcile_attempt_signal(
        backend=client,
        observed_gid="gid_t09_rpc",
        event=None,
        observed_status={
            "status": "active",
            "totalLength": "2048",
            "completedLength": "0",
        },
        log_prefix="[T09]",
    )
    assert result == ReconcileResult.CHANGED

    row = await _fetch_download_row(download["id"])
    # Row must stay paused with the ownership credential intact; the next
    # sync round retries the unpause via policy instead of branding.
    assert row["status"] == "paused"
    assert row["error_code"] == ERROR_ADMISSION_PAUSED
