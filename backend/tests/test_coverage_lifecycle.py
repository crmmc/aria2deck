"""Coverage supplements for app/services/lifecycle/ modules."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from app.domain.lifecycle import ReconcileResult
from app.services.lifecycle import _shared as shared
from app.services.lifecycle import cleanup as cleanup_mod
from app.services.lifecycle import completion as completion_mod
from app.services.lifecycle import coordinator as coord_mod
from app.services.lifecycle import handoff as handoff_mod
from app.services.lifecycle._shared import _requery_after_control_failure
from app.services.lifecycle.coordinator import reconcile_attempt_signal
from app.services.storage import get_downloading_dir, get_store_dir
from tests.fakes import make_aria2_client
from tests.helpers_v0 import (
    create_global_download_v0,
    create_user_task_v0,
    create_user_v0,
)

from sqlalchemy import select

from app.db.engine import transaction
from app.db.schema import global_downloads


async def _fetch(download_id: int) -> dict | None:
    async with transaction() as conn:
        row = (
            (
                await conn.execute(
                    select(global_downloads).where(global_downloads.c.id == download_id)
                )
            )
            .mappings()
            .first()
        )
    return dict(row) if row else None


def _missing_gid_error() -> Exception:
    return RuntimeError("gid xxx not found")


def _transient_error() -> Exception:
    return ConnectionError("cannot connect to host localhost:6800")


# ---------------------------------------------------------------------------
# _shared helpers
# ---------------------------------------------------------------------------


def test_sanitize_path_branches():
    assert shared._sanitize_path("", 1) is None
    assert shared._sanitize_path(None, 1) is None
    assert shared._sanitize_path("ok.bin", 1) == "ok.bin"
    # ValueError on NUL byte falls back to the raw path
    assert shared._sanitize_path("\0bad", 1) == "\0bad"


@pytest.mark.asyncio
async def test_get_representative_owner_id(temp_db):
    user = await create_user_v0(username="rep_owner")
    dl = await create_global_download_v0(
        resource_key="http:rep", status="active", aria2_gid="g-rep"
    )
    await create_user_task_v0(
        user_id=user["id"], global_download_id=dl["id"], status="active"
    )
    assert await shared.get_representative_owner_id(dl["id"]) == user["id"]
    assert await shared.get_representative_owner_id(999999) is None


def _rq_kwargs(backend, download_id, expected_gid, **overrides):
    kwargs = dict(
        backend=backend,
        download_id=download_id,
        control_gid=expected_gid,
        expected_gid=expected_gid,
        success_statuses={"paused"},
        failure_error_code="growth_pause_failed",
        failure_message="失败",
        acquire_lifecycle_lock=False,
        soft_control_failure=True,
    )
    kwargs.update(overrides)
    return kwargs


@pytest.mark.asyncio
async def test_requery_success_and_complete(temp_db):
    dl = await create_global_download_v0(
        resource_key="http:rq-ok", status="active", aria2_gid="g-rq"
    )
    assert (
        await _requery_after_control_failure(
            **_rq_kwargs(make_aria2_client(tell_status={"status": "paused"}), dl["id"], "g-rq")
        )
        == "success"
    )
    assert (
        await _requery_after_control_failure(
            **_rq_kwargs(make_aria2_client(tell_status={"status": "complete"}), dl["id"], "g-rq")
        )
        == "complete"
    )


@pytest.mark.asyncio
async def test_requery_missing_gid_terminalizes(temp_db):
    dl = await create_global_download_v0(
        resource_key="http:rq-miss",
        status="active",
        aria2_gid="g-rq2",
        total_bytes=100,
        size_known=True,
    )
    backend = make_aria2_client(tell_status=_missing_gid_error())
    result = await _requery_after_control_failure(**_rq_kwargs(backend, dl["id"], "g-rq2"))
    assert result == "missing"
    assert (await _fetch(dl["id"]))["status"] == "failed"
    assert (await _fetch(dl["id"]))["error_code"] == "gid_missing"

    dl0 = await create_global_download_v0(
        resource_key="http:rq-miss0", status="active", aria2_gid="g-rq0"
    )
    result = await _requery_after_control_failure(
        **_rq_kwargs(make_aria2_client(tell_status=_missing_gid_error()), dl0["id"], "g-rq0")
    )
    assert result == "missing"
    assert (await _fetch(dl0["id"]))["error_code"] == "unknown_size"


@pytest.mark.asyncio
async def test_requery_missing_gid_stale(temp_db):
    # Row deleted between re-query failure and lookup → stale.
    backend = make_aria2_client(tell_status=_missing_gid_error())
    assert (
        await _requery_after_control_failure(**_rq_kwargs(backend, 424242, "g-none"))
        == "stale"
    )


@pytest.mark.asyncio
async def test_requery_transient_rpc(temp_db):
    dl = await create_global_download_v0(
        resource_key="http:rq-trans", status="active", aria2_gid="g-tr"
    )
    backend = make_aria2_client(tell_status=_transient_error())
    assert (
        await _requery_after_control_failure(**_rq_kwargs(backend, dl["id"], "g-tr"))
        == "rpc_unavailable"
    )


@pytest.mark.asyncio
async def test_requery_generic_error_stale_and_soft(temp_db):
    dl = await create_global_download_v0(
        resource_key="http:rq-gen", status="active", aria2_gid="g-gen"
    )
    backend = make_aria2_client(tell_status=RuntimeError("weird"))
    # non-soft with vanished row → stale
    assert (
        await _requery_after_control_failure(
            **_rq_kwargs(backend, 424243, "g-none", soft_control_failure=False)
        )
        == "stale"
    )
    # soft with vanished row → stale
    assert (
        await _requery_after_control_failure(**_rq_kwargs(backend, 424244, "g-none"))
        == "stale"
    )
    # soft with live row stamps failure code and keeps status
    result = await _requery_after_control_failure(**_rq_kwargs(backend, dl["id"], "g-gen"))
    assert result == "soft_failed"
    row = await _fetch(dl["id"])
    assert row["error_code"] == "growth_pause_failed"


@pytest.mark.asyncio
async def test_requery_soft_mark_user_task_sync_fails(temp_db, monkeypatch):
    from app.repositories.task import user_tasks as ut_repo

    dl = await create_global_download_v0(
        resource_key="http:rq-softfail", status="active", aria2_gid="g-sf"
    )
    monkeypatch.setattr(
        ut_repo, "update_active_user_tasks", AsyncMock(side_effect=RuntimeError("db"))
    )
    backend = make_aria2_client(tell_status=RuntimeError("weird"))
    result = await _requery_after_control_failure(**_rq_kwargs(backend, dl["id"], "g-sf"))
    assert result == "soft_failed"


@pytest.mark.asyncio
async def test_requery_non_soft_failures(temp_db):
    # re-query error, live row, hard failure path
    dl = await create_global_download_v0(
        resource_key="http:rq-hard1", status="active", aria2_gid="g-h1"
    )
    backend = make_aria2_client(tell_status=RuntimeError("weird"))
    assert (
        await _requery_after_control_failure(
            **_rq_kwargs(backend, dl["id"], "g-h1", soft_control_failure=False)
        )
        == "failed"
    )
    assert (await _fetch(dl["id"]))["status"] == "failed"

    # re-query ok but wrong status → hard failure
    dl2 = await create_global_download_v0(
        resource_key="http:rq-hard2", status="active", aria2_gid="g-h2"
    )
    backend2 = make_aria2_client(tell_status={"status": "active"})
    assert (
        await _requery_after_control_failure(
            **_rq_kwargs(backend2, dl2["id"], "g-h2", soft_control_failure=False)
        )
        == "failed"
    )
    # re-query ok but wrong status and row vanished → stale
    assert (
        await _requery_after_control_failure(
            **_rq_kwargs(backend2, 424245, "g-h3", soft_control_failure=False)
        )
        == "stale"
    )


# ---------------------------------------------------------------------------
# cleanup helpers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reclaim_terminal_with_claim_stale(temp_db, monkeypatch):
    dl = await create_global_download_v0(
        resource_key="http:reclaim-stale", status="failed", aria2_gid="g-rc"
    )
    monkeypatch.setattr(
        cleanup_mod, "claim_terminal_reclaim", AsyncMock(return_value=None)
    )
    # no exception; claim stale path simply returns
    await cleanup_mod._reclaim_terminal_with_claim(
        backend=make_aria2_client(),
        download_id=dl["id"],
        gid="g-rc",
        log_prefix="[T]",
    )


@pytest.mark.asyncio
async def test_fail_download_and_reclaim_cancelled(monkeypatch):
    started = asyncio.Event()

    async def slow_operation(**kwargs):
        started.set()
        await asyncio.sleep(30)

    monkeypatch.setattr(
        cleanup_mod, "_fail_download_and_reclaim_operation", slow_operation
    )
    task = asyncio.create_task(
        cleanup_mod.fail_download_and_reclaim(
            backend=make_aria2_client(),
            download_id=1,
            message="x",
        )
    )
    await asyncio.wait_for(started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_best_effort_helpers_swallow_errors():
    backend = make_aria2_client(
        remove_download_result=RuntimeError("gone"),
        force_remove=RuntimeError("nope"),
    )
    await cleanup_mod._remove_download_result_best_effort(backend, "g", "[T]")
    await cleanup_mod._stop_untracked_gid_best_effort(backend, "g", "[T]")


@pytest.mark.asyncio
async def test_terminalize_missing_gid_stale(monkeypatch):
    async def not_changed(**kwargs):
        return False

    monkeypatch.setattr(cleanup_mod, "fail_download_and_reclaim", not_changed)
    result = await cleanup_mod._terminalize_missing_gid_locked(
        backend=make_aria2_client(),
        attempt_id=1,
        current_gid="g",
        total_bytes=10,
        log_prefix="[T]",
    )
    assert result == ReconcileResult.STALE


# ---------------------------------------------------------------------------
# coordinator branches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconcile_ignored_for_unknown_gid(temp_db):
    result = await reconcile_attempt_signal(
        backend=make_aria2_client(),
        observed_gid="ghost",
        event="poll",
        observed_status=None,
        log_prefix="[T]",
    )
    assert result == ReconcileResult.IGNORED


@pytest.mark.asyncio
async def test_reconcile_snapshot_vanished(temp_db, monkeypatch):
    await create_global_download_v0(
        resource_key="http:vanish", status="active", aria2_gid="g-vanish"
    )
    monkeypatch.setattr(
        coord_mod,
        "get_global_download_status_snapshot",
        AsyncMock(return_value=None),
    )
    result = await reconcile_attempt_signal(
        backend=make_aria2_client(),
        observed_gid="g-vanish",
        event="poll",
        observed_status={"status": "active"},
        log_prefix="[T]",
    )
    assert result == ReconcileResult.IGNORED


@pytest.mark.asyncio
async def test_reconcile_missing_gid_terminal_states(temp_db):
    from sqlalchemy import insert

    from app.core.time_utils import now_ms
    from app.db.schema import stored_files

    async with transaction() as conn:
        file_id = (
            await conn.execute(
                insert(stored_files)
                .values(
                    content_hash="mg-done-file",
                    real_path="/tmp/mg-done-file",
                    size_bytes=1,
                    is_directory=0,
                    original_name="f",
                    created_at_ms=now_ms(),
                )
                .returning(stored_files.c.id)
            )
        ).scalar()
    done = await create_global_download_v0(
        resource_key="http:mg-done",
        status="completed",
        aria2_gid="g-mg-done",
        completed_file_id=file_id,
    )
    result = await reconcile_attempt_signal(
        backend=make_aria2_client(),
        observed_gid="g-mg-done",
        event="poll",
        observed_status=None,
        observed_error=_missing_gid_error(),
        log_prefix="[T]",
    )
    assert result == ReconcileResult.ALREADY_COMPLETE

    failed = await create_global_download_v0(
        resource_key="http:mg-failed", status="failed", aria2_gid="g-mg-failed"
    )
    result = await reconcile_attempt_signal(
        backend=make_aria2_client(),
        observed_gid="g-mg-failed",
        event="poll",
        observed_status=None,
        observed_error=_missing_gid_error(),
        log_prefix="[T]",
    )
    assert result == ReconcileResult.ALREADY_TERMINAL

    # missing GID observed but DB gid changed under the lock → STALE
    other = await create_global_download_v0(
        resource_key="http:mg-stale", status="active", aria2_gid="g-current"
    )
    real_snapshot = coord_mod.get_global_download_status_snapshot

    async def swapped_snapshot(attempt_id):
        row = await real_snapshot(attempt_id)
        if row is not None:
            row = dict(row)
            row["aria2_gid"] = "g-changed"
        return row

    with patch.object(coord_mod, "get_global_download_status_snapshot", swapped_snapshot):
        result = await reconcile_attempt_signal(
            backend=make_aria2_client(),
            observed_gid="g-current",
            event="poll",
            observed_status=None,
            observed_error=_missing_gid_error(),
            log_prefix="[T]",
        )
    assert result == ReconcileResult.STALE


@pytest.mark.asyncio
async def test_reconcile_missing_gid_live_terminalizes(temp_db):
    dl = await create_global_download_v0(
        resource_key="http:mg-live",
        status="active",
        aria2_gid="g-mg-live",
        total_bytes=50,
        size_known=True,
    )
    result = await reconcile_attempt_signal(
        backend=make_aria2_client(),
        observed_gid="g-mg-live",
        event="poll",
        observed_status=None,
        observed_error=_missing_gid_error(),
        log_prefix="[T]",
    )
    assert result == ReconcileResult.TERMINALIZED
    assert (await _fetch(dl["id"]))["error_code"] == "gid_missing"


@pytest.mark.asyncio
async def test_reconcile_transient_error_waits(temp_db):
    await create_global_download_v0(
        resource_key="http:tr", status="active", aria2_gid="g-tr2"
    )
    result = await reconcile_attempt_signal(
        backend=make_aria2_client(),
        observed_gid="g-tr2",
        event="poll",
        observed_status=None,
        observed_error=_transient_error(),
        log_prefix="[T]",
    )
    assert result == ReconcileResult.WAITING


@pytest.mark.asyncio
async def test_reconcile_already_terminal_and_stale_fence(temp_db):
    failed = await create_global_download_v0(
        resource_key="http:al-term", status="failed", aria2_gid="g-at"
    )
    result = await reconcile_attempt_signal(
        backend=make_aria2_client(),
        observed_gid="g-at",
        event="poll",
        observed_status={"status": "active"},
        log_prefix="[T]",
    )
    assert result == ReconcileResult.ALREADY_TERMINAL

    live = await create_global_download_v0(
        resource_key="http:fence",
        status="active",
        aria2_gid="g-new",
        total_bytes=100,
        size_known=True,
    )
    with patch.object(
        coord_mod,
        "coordinate_reported_size",
        AsyncMock(return_value={"outcome": "admitted", "paused_by_us": False}),
    ):
        result = await reconcile_attempt_signal(
        backend=make_aria2_client(),
        observed_gid="g-new",
        event="poll",
            observed_status={"status": "active", "totalLength": "100"},
            log_prefix="[T]",
        )
    assert result == ReconcileResult.CHANGED


@pytest.mark.asyncio
async def test_reconcile_removed_terminalizes(temp_db):
    dl = await create_global_download_v0(
        resource_key="http:rm", status="active", aria2_gid="g-rm"
    )
    result = await reconcile_attempt_signal(
        backend=make_aria2_client(),
        observed_gid="g-rm",
        event="poll",
        observed_status={"status": "removed"},
        log_prefix="[T]",
    )
    assert result == ReconcileResult.TERMINALIZED
    assert (await _fetch(dl["id"]))["error_code"] == "removed"


@pytest.mark.asyncio
async def test_reconcile_error_status_terminalizes(temp_db):
    dl = await create_global_download_v0(
        resource_key="http:err", status="active", aria2_gid="g-err"
    )
    result = await reconcile_attempt_signal(
        backend=make_aria2_client(),
        observed_gid="g-err",
        event="poll",
        observed_status={"status": "error", "errorCode": "13", "errorMessage": "x"},
        log_prefix="[T]",
    )
    assert result == ReconcileResult.TERMINALIZED
    assert (await _fetch(dl["id"]))["status"] == "failed"


@pytest.mark.asyncio
async def test_reconcile_terminalize_claim_stale(temp_db, monkeypatch):
    await create_global_download_v0(
        resource_key="http:claim-stale", status="active", aria2_gid="g-cs"
    )
    monkeypatch.setattr(
        coord_mod, "fail_download_and_reclaim", AsyncMock(return_value=False)
    )
    result = await reconcile_attempt_signal(
        backend=make_aria2_client(),
        observed_gid="g-cs",
        event="poll",
        observed_status={"status": "removed"},
        log_prefix="[T]",
    )
    assert result == ReconcileResult.STALE


@pytest.mark.asyncio
async def test_reconcile_tell_status_errors(temp_db):
    dl = await create_global_download_v0(
        resource_key="http:ts-err", status="active", aria2_gid="g-ts1"
    )
    # non-transient, non-missing RPC errors propagate
    with pytest.raises(RuntimeError):
        await reconcile_attempt_signal(
            backend=make_aria2_client(tell_status=RuntimeError("weird rpc")),
            observed_gid="g-ts1",
            event="poll",
            observed_status=None,
            log_prefix="[T]",
        )


@pytest.mark.asyncio
async def test_reconcile_tell_status_transient_and_missing(temp_db):
    dl = await create_global_download_v0(
        resource_key="http:ts-tr",
        status="active",
        aria2_gid="g-ts2",
        total_bytes=10,
        size_known=True,
    )
    result = await reconcile_attempt_signal(
        backend=make_aria2_client(tell_status=_transient_error()),
        observed_gid="g-ts2",
        event="poll",
        observed_status=None,
        log_prefix="[T]",
    )
    assert result == ReconcileResult.WAITING

    result = await reconcile_attempt_signal(
        backend=make_aria2_client(tell_status=_missing_gid_error()),
        observed_gid="g-ts2",
        event="poll",
        observed_status=None,
        log_prefix="[T]",
    )
    assert result == ReconcileResult.TERMINALIZED


@pytest.mark.asyncio
async def test_reconcile_unknown_raw_status_waits(temp_db):
    await create_global_download_v0(
        resource_key="http:unknown", status="active", aria2_gid="g-unk"
    )
    result = await reconcile_attempt_signal(
        backend=make_aria2_client(),
        observed_gid="g-unk",
        event="poll",
        observed_status={"status": "bizarre"},
        log_prefix="[T]",
    )
    assert result == ReconcileResult.WAITING


@pytest.mark.asyncio
async def test_reconcile_projection_cas_stale(temp_db, monkeypatch):
    await create_global_download_v0(
        resource_key="http:cas-stale",
        status="active",
        aria2_gid="g-cas",
        total_bytes=100,
        size_known=True,
    )
    monkeypatch.setattr(
        coord_mod,
        "guarded_update_download_and_active_user_tasks",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        coord_mod,
        "coordinate_reported_size",
        AsyncMock(return_value={"outcome": "admitted", "paused_by_us": False}),
    )
    result = await reconcile_attempt_signal(
        backend=make_aria2_client(),
        observed_gid="g-cas",
        event="poll",
        observed_status={"status": "active", "totalLength": "100"},
        log_prefix="[T]",
    )
    assert result == ReconcileResult.STALE


@pytest.mark.asyncio
async def test_reconcile_size_admission_outcomes(temp_db, monkeypatch):
    dl = await create_global_download_v0(
        resource_key="http:size-out",
        status="active",
        aria2_gid="g-size",
        total_bytes=10,
        size_known=False,
    )
    for outcome, expected in [
        ("stale", ReconcileResult.STALE),
        ("terminalized", ReconcileResult.TERMINALIZED),
        ("pause_soft_failed", ReconcileResult.CHANGED),
        ("disk_budget", ReconcileResult.TERMINALIZED),
    ]:
        monkeypatch.setattr(
            coord_mod,
            "coordinate_reported_size",
            AsyncMock(return_value={"outcome": outcome, "paused_by_us": False}),
        )
        result = await reconcile_attempt_signal(
            backend=make_aria2_client(),
            observed_gid="g-size",
            event="poll",
            observed_status={"status": "active"},
            log_prefix="[T]",
        )
        assert result == expected, outcome


@pytest.mark.asyncio
async def test_reconcile_size_admission_unknown_size_waits(temp_db, monkeypatch):
    await create_global_download_v0(
        resource_key="http:size-unknown",
        status="active",
        aria2_gid="g-su",
        size_known=False,
    )
    monkeypatch.setattr(
        coord_mod,
        "coordinate_reported_size",
        AsyncMock(return_value={"outcome": "unknown_size", "paused_by_us": False}),
    )
    result = await reconcile_attempt_signal(
        backend=make_aria2_client(),
        observed_gid="g-su",
        event="poll",
        observed_status={"status": "active"},
        log_prefix="[T]",
    )
    assert result == ReconcileResult.WAITING


@pytest.mark.asyncio
async def test_reconcile_complete_delegation_transient(temp_db, monkeypatch):
    await create_global_download_v0(
        resource_key="http:del-tr",
        status="active",
        aria2_gid="g-del",
        total_bytes=1,
        size_known=True,
    )
    monkeypatch.setattr(
        coord_mod,
        "handle_v0_download_complete",
        AsyncMock(side_effect=_transient_error()),
    )
    result = await reconcile_attempt_signal(
        backend=make_aria2_client(),
        observed_gid="g-del",
        event="poll",
        observed_status={
            "status": "complete",
            "totalLength": "1",
            "files": [{"path": "/tmp/f", "length": "1", "selected": "true"}],
        },
        log_prefix="[T]",
    )
    assert result == ReconcileResult.WAITING


# ---------------------------------------------------------------------------
# handoff: coordinate_reported_size outcome branches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_coordinate_size_pause_complete_and_rpc_unavailable(temp_db):
    from app.services.lifecycle.handoff import coordinate_reported_size

    dl = await create_global_download_v0(
        resource_key="http:cr-comp",
        status="active",
        aria2_gid="g-cr1",
        total_bytes=10,
        size_known=True,
    )
    client = make_aria2_client(
        pause=RuntimeError("pause failed"),
        tell_status={"status": "complete", "totalLength": "100"},
    )
    result = await coordinate_reported_size(
        backend=client,
        download=dl,
        expected_gid="g-cr1",
        control_gid="g-cr1",
        status={"status": "active", "totalLength": "100"},
    )
    assert result["outcome"] == "complete"

    client2 = make_aria2_client(
        pause=RuntimeError("pause failed"),
        tell_status=_transient_error(),
    )
    result = await coordinate_reported_size(
        backend=client2,
        download=dl,
        expected_gid="g-cr1",
        control_gid="g-cr1",
        status={"status": "active", "totalLength": "100"},
    )
    assert result["outcome"] == "rpc_unavailable"


@pytest.mark.asyncio
async def test_coordinate_size_stale_generation(temp_db, monkeypatch):
    from app.repositories.task.downloads import SizeReconcileResult
    from app.services.lifecycle.handoff import coordinate_reported_size

    dl = await create_global_download_v0(
        resource_key="http:cr-stale", status="active", aria2_gid="g-cr2"
    )
    monkeypatch.setattr(
        handoff_mod,
        "reconcile_download_size",
        AsyncMock(return_value=SizeReconcileResult(outcome="stale")),
    )
    result = await coordinate_reported_size(
        backend=make_aria2_client(),
        download=dl,
        expected_gid="g-other",
        control_gid="g-cr2",
        status={"status": "active", "totalLength": "5"},
    )
    assert result["outcome"] == "stale"


@pytest.mark.asyncio
async def test_coordinate_size_unpause_outcomes(temp_db, monkeypatch):
    from app.repositories.task.downloads import SizeReconcileResult
    from app.services.lifecycle.handoff import coordinate_reported_size

    async def admitted(**kwargs):
        return SizeReconcileResult(outcome="admitted")

    monkeypatch.setattr(handoff_mod, "reconcile_download_size", admitted)
    for i, (unpause_result, expected_outcome) in enumerate([
        ("complete", "complete"),
        ("rpc_unavailable", "rpc_unavailable"),
        ("terminalized", "terminalized"),
    ]):
        dl = await create_global_download_v0(
            resource_key=f"http:cr-unp-{i}",
            status="active",
            aria2_gid=f"g-cr3-{i}",
            total_bytes=10,
            size_known=True,
        )
        monkeypatch.setattr(
            handoff_mod,
            "system_unpause_gid",
            AsyncMock(return_value=unpause_result),
        )
        result = await coordinate_reported_size(
            backend=make_aria2_client(),  # pause succeeds
            download=dl,
            expected_gid=dl["aria2_gid"],
            control_gid=dl["aria2_gid"],
            status={"status": "active", "totalLength": "100"},
        )
        assert result["outcome"] == expected_outcome, unpause_result
        assert result["paused_by_us"] is True


# ---------------------------------------------------------------------------
# handoff: _handoff_locked branches
# ---------------------------------------------------------------------------


def _snapshot(gid: str, status: str) -> dict:
    return {"aria2_gid": gid, "status": status}


@pytest.mark.asyncio
async def test_handoff_locked_fencing(temp_db):
    from app.services.lifecycle.handoff import _handoff_locked

    dl = await create_global_download_v0(
        resource_key="http:ho-fence", status="active", aria2_gid="g-ho"
    )
    backend = make_aria2_client()
    # already switched to payload → idempotent stale
    result, dispatch = await _handoff_locked(
        backend=backend,
        attempt_id=dl["id"],
        source_gid="g-src",
        payload_gid="g-pay",
        snapshot=_snapshot("g-pay", "active"),
        download=dl,
        log_prefix="[T]",
    )
    assert result == ReconcileResult.STALE and dispatch is None
    # unrelated current gid → stale
    result, _ = await _handoff_locked(
        backend=backend,
        attempt_id=dl["id"],
        source_gid="g-src",
        payload_gid="g-pay",
        snapshot=_snapshot("g-other", "active"),
        download=dl,
        log_prefix="[T]",
    )
    assert result == ReconcileResult.STALE
    # terminal snapshot → already terminal
    result, _ = await _handoff_locked(
        backend=backend,
        attempt_id=dl["id"],
        source_gid="g-ho",
        payload_gid="g-pay",
        snapshot=_snapshot("g-ho", "failed"),
        download=dl,
        log_prefix="[T]",
    )
    assert result == ReconcileResult.ALREADY_TERMINAL


@pytest.mark.asyncio
async def test_handoff_locked_payload_fetch_raises(temp_db):
    from app.services.lifecycle.handoff import _handoff_locked

    dl = await create_global_download_v0(
        resource_key="http:ho-raise", status="active", aria2_gid="g-ho2"
    )
    with pytest.raises(RuntimeError):
        await _handoff_locked(
            backend=make_aria2_client(tell_status=RuntimeError("fatal")),
            attempt_id=dl["id"],
            source_gid="g-ho2",
            payload_gid="g-pay",
            snapshot=_snapshot("g-ho2", "active"),
            download=dl,
            log_prefix="[T]",
        )


@pytest.mark.asyncio
async def test_handoff_locked_unknown_size_waiting(temp_db):
    from app.services.lifecycle.handoff import _handoff_locked

    known = await create_global_download_v0(
        resource_key="http:ho-known",
        status="active",
        aria2_gid="g-ho3",
        total_bytes=100,
        size_known=True,
    )
    # size_known=True but payload reports no size yet → waiting
    result, _ = await _handoff_locked(
        backend=make_aria2_client(),
        attempt_id=known["id"],
        source_gid="g-ho3",
        payload_gid="g-pay",
        snapshot=_snapshot("g-ho3", "active"),
        download=known,
        log_prefix="[T]",
        _payload_status={"status": "active", "totalLength": ""},
    )
    assert result == ReconcileResult.WAITING

    unknown = await create_global_download_v0(
        resource_key="http:ho-unknown", status="active", aria2_gid="g-ho4"
    )
    for raw in ("waiting", "paused"):
        result, _ = await _handoff_locked(
            backend=make_aria2_client(),
            attempt_id=unknown["id"],
            source_gid="g-ho4",
            payload_gid="g-pay",
            snapshot=_snapshot("g-ho4", "active"),
            download=unknown,
            log_prefix="[T]",
            _payload_status={"status": raw, "totalLength": "0"},
        )
        assert result == ReconcileResult.WAITING, raw


@pytest.mark.asyncio
async def test_handoff_locked_active_unknown_size_soft_pause(temp_db):
    from app.services.lifecycle.handoff import _handoff_locked

    dl = await create_global_download_v0(
        resource_key="http:ho-soft", status="active", aria2_gid="g-ho5"
    )
    # soft pause succeeds, re-query paused → waiting
    client = make_aria2_client(tell_status={"status": "paused", "totalLength": "0"})
    result, _ = await _handoff_locked(
        backend=client,
        attempt_id=dl["id"],
        source_gid="g-ho5",
        payload_gid="g-pay",
        snapshot=_snapshot("g-ho5", "active"),
        download=dl,
        log_prefix="[T]",
        _payload_status={"status": "active", "totalLength": "0"},
    )
    assert result == ReconcileResult.WAITING

    # re-query transient → waiting
    client = make_aria2_client(tell_status=_transient_error())
    result, _ = await _handoff_locked(
        backend=client,
        attempt_id=dl["id"],
        source_gid="g-ho5",
        payload_gid="g-pay",
        snapshot=_snapshot("g-ho5", "active"),
        download=dl,
        log_prefix="[T]",
        _payload_status={"status": "active", "totalLength": "0"},
    )
    assert result == ReconcileResult.WAITING

    # re-query non-transient → raise
    client = make_aria2_client(tell_status=RuntimeError("fatal"))
    with pytest.raises(RuntimeError):
        await _handoff_locked(
            backend=client,
            attempt_id=dl["id"],
            source_gid="g-ho5",
            payload_gid="g-pay",
            snapshot=_snapshot("g-ho5", "active"),
            download=dl,
            log_prefix="[T]",
            _payload_status={"status": "active", "totalLength": "0"},
        )

    # re-query still active → pause not confirmed, waiting
    client = make_aria2_client(tell_status={"status": "active", "totalLength": "0"})
    result, _ = await _handoff_locked(
        backend=client,
        attempt_id=dl["id"],
        source_gid="g-ho5",
        payload_gid="g-pay",
        snapshot=_snapshot("g-ho5", "active"),
        download=dl,
        log_prefix="[T]",
        _payload_status={"status": "active", "totalLength": "0"},
    )
    assert result == ReconcileResult.WAITING

    # error/removed payload with unknown size → waiting
    result, _ = await _handoff_locked(
        backend=make_aria2_client(),
        attempt_id=dl["id"],
        source_gid="g-ho5",
        payload_gid="g-pay",
        snapshot=_snapshot("g-ho5", "active"),
        download=dl,
        log_prefix="[T]",
        _payload_status={"status": "error", "totalLength": "0"},
    )
    assert result == ReconcileResult.WAITING


@pytest.mark.asyncio
async def test_handoff_locked_admission_outcomes(temp_db, monkeypatch):
    from app.services.lifecycle.handoff import _handoff_locked

    dl = await create_global_download_v0(
        resource_key="http:ho-adm",
        status="active",
        aria2_gid="g-ho6",
        total_bytes=100,
        size_known=True,
    )
    payload = {"status": "active", "totalLength": "200"}
    for outcome, expected in [
        ("stale", ReconcileResult.STALE),
        ("rpc_unavailable", ReconcileResult.WAITING),
        ("terminalized", ReconcileResult.TERMINALIZED),
    ]:
        monkeypatch.setattr(
            handoff_mod,
            "coordinate_reported_size",
            AsyncMock(return_value={"outcome": outcome, "paused_by_us": False}),
        )
        result, _ = await _handoff_locked(
            backend=make_aria2_client(),
            attempt_id=dl["id"],
            source_gid="g-ho6",
            payload_gid="g-pay",
            snapshot=_snapshot("g-ho6", "active"),
            download=dl,
            log_prefix="[T]",
            _payload_status=payload,
        )
        assert result == expected, outcome

    # rejected admission → fail + terminalize
    monkeypatch.setattr(
        handoff_mod,
        "coordinate_reported_size",
        AsyncMock(return_value={"outcome": "unknown_size", "paused_by_us": False}),
    )
    result, _ = await _handoff_locked(
        backend=make_aria2_client(),
        attempt_id=dl["id"],
        source_gid="g-ho6",
        payload_gid="g-pay",
        snapshot=_snapshot("g-ho6", "active"),
        download=dl,
        log_prefix="[T]",
        _payload_status=payload,
    )
    assert result == ReconcileResult.TERMINALIZED


@pytest.mark.asyncio
async def test_handoff_locked_cas_stale(temp_db, monkeypatch):
    from app.services.lifecycle.handoff import _handoff_locked

    dl = await create_global_download_v0(
        resource_key="http:ho-cas",
        status="active",
        aria2_gid="g-ho7",
        total_bytes=100,
        size_known=True,
    )
    monkeypatch.setattr(
        handoff_mod,
        "coordinate_reported_size",
        AsyncMock(return_value={"outcome": "admitted", "paused_by_us": False}),
    )
    monkeypatch.setattr(
        handoff_mod,
        "guarded_update_download_and_active_user_tasks",
        AsyncMock(return_value=None),
    )
    result, _ = await _handoff_locked(
        backend=make_aria2_client(),
        attempt_id=dl["id"],
        source_gid="g-ho7",
        payload_gid="g-pay",
        snapshot=_snapshot("g-ho7", "active"),
        download=dl,
        log_prefix="[T]",
        _payload_status={"status": "active", "totalLength": "100"},
    )
    assert result == ReconcileResult.STALE


@pytest.mark.asyncio
async def test_handoff_locked_committed_and_unpause_outcomes(temp_db, monkeypatch):
    from app.services.lifecycle.handoff import _handoff_locked

    dl = await create_global_download_v0(
        resource_key="http:ho-commit",
        status="active",
        aria2_gid="g-ho8",
        total_bytes=100,
        size_known=True,
    )
    monkeypatch.setattr(
        handoff_mod,
        "coordinate_reported_size",
        AsyncMock(return_value={"outcome": "admitted", "paused_by_us": False}),
    )
    for unpause_result, expected in [
        ("success", ReconcileResult.CHANGED),
        ("missing", ReconcileResult.TERMINALIZED),
        ("stale", ReconcileResult.STALE),
        ("rpc_unavailable", ReconcileResult.CHANGED),
        ("soft_failed", ReconcileResult.CHANGED),
    ]:
        # reset row before each round
        async with transaction() as conn:
            await conn.execute(
                global_downloads.update()
                .where(global_downloads.c.id == dl["id"])
                .values(status="active", aria2_gid="g-ho8")
            )
        monkeypatch.setattr(
            handoff_mod,
            "system_unpause_gid",
            AsyncMock(return_value=unpause_result),
        )
        result, dispatch = await _handoff_locked(
            backend=make_aria2_client(),
            attempt_id=dl["id"],
            source_gid="g-ho8",
            payload_gid="g-pay",
            snapshot=_snapshot("g-ho8", "active"),
            download=dl,
            log_prefix="[T]",
            _payload_status={"status": "paused", "totalLength": "100"},
        )
        assert result == expected, unpause_result


@pytest.mark.asyncio
async def test_handoff_locked_complete_dispatch(temp_db, monkeypatch):
    from app.services.lifecycle.handoff import _handoff_locked

    dl = await create_global_download_v0(
        resource_key="http:ho-comp",
        status="active",
        aria2_gid="g-ho9",
        total_bytes=100,
        size_known=True,
    )
    monkeypatch.setattr(
        handoff_mod,
        "coordinate_reported_size",
        AsyncMock(return_value={"outcome": "complete", "paused_by_us": False}),
    )
    result, dispatch = await _handoff_locked(
        backend=make_aria2_client(),
        attempt_id=dl["id"],
        source_gid="g-ho9",
        payload_gid="g-pay",
        snapshot=_snapshot("g-ho9", "active"),
        download=dl,
        log_prefix="[T]",
        _payload_status={"status": "complete", "totalLength": "150"},
    )
    assert result == ReconcileResult.CHANGED
    assert dispatch is not None
    assert dispatch[0] == "g-pay"


# ---------------------------------------------------------------------------
# handoff: switch_to_followed_download / resolve / scan helpers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_switch_to_followed_download_branches(temp_db, monkeypatch):
    from app.services.lifecycle.handoff import switch_to_followed_download

    dl = await create_global_download_v0(
        resource_key="http:sw", status="active", aria2_gid="g-sw"
    )

    # snapshot vanished → stop untracked payload, False
    monkeypatch.setattr(
        handoff_mod,
        "get_global_download_status_snapshot",
        AsyncMock(return_value=None),
    )
    assert not await switch_to_followed_download(
        backend=make_aria2_client(),
        download=dl,
        metadata_gid="g-sw",
        followed_gid="g-pay",
        display_name_fallback=None,
        log_prefix="[T]",
    )

    # non-transient pre-fetch failure propagates
    with pytest.raises(RuntimeError):
        await switch_to_followed_download(
            backend=make_aria2_client(tell_status=RuntimeError("fatal")),
            download=dl,
            metadata_gid="g-sw",
            followed_gid="g-pay",
            display_name_fallback=None,
            log_prefix="[T]",
        )

    # transient pre-fetch failure retries inside the lock and waits
    result = await switch_to_followed_download(
        backend=make_aria2_client(tell_status=_transient_error()),
        download=dl,
        metadata_gid="g-sw",
        followed_gid="g-pay",
        display_name_fallback=None,
        log_prefix="[T]",
    )
    assert result is False


@pytest.mark.asyncio
async def test_switch_to_followed_download_complete_dispatch(temp_db, monkeypatch):
    from app.services.lifecycle.handoff import switch_to_followed_download

    dl = await create_global_download_v0(
        resource_key="http:sw2", status="active", aria2_gid="g-sw2"
    )

    async def fake_handoff(**kwargs):
        return ReconcileResult.CHANGED, ("g-pay", {"status": "complete"})

    monkeypatch.setattr(handoff_mod, "_handoff_locked", fake_handoff)
    monkeypatch.setattr(
        handoff_mod,
        "get_global_download_status_snapshot",
        AsyncMock(return_value=_snapshot("g-sw2", "active")),
    )
    monkeypatch.setattr(
        handoff_mod, "get_global_download_by_gid", AsyncMock(return_value=dl)
    )
    completed = AsyncMock(return_value=True)
    monkeypatch.setattr(completion_mod, "handle_v0_download_complete", completed)
    result = await switch_to_followed_download(
        backend=make_aria2_client(),
        download=dl,
        metadata_gid="g-sw2",
        followed_gid="g-pay",
        display_name_fallback=None,
        log_prefix="[T]",
        complete_if_followed_complete=True,
        _real_status={"status": "complete"},
    )
    assert result is True
    completed.assert_awaited_once()


@pytest.mark.asyncio
async def test_resolve_download_for_gid_no_source(temp_db):
    from app.services.lifecycle.handoff import resolve_download_for_gid

    await create_global_download_v0(
        resource_key="http:res", status="active", aria2_gid="g-res"
    )
    assert (
        await resolve_download_for_gid(
            "g-pay2", {"status": "active", "following": "g-unknown"}
        )
        is None
    )
    assert await resolve_download_for_gid("g-pay2", None) is None


@pytest.mark.asyncio
async def test_find_followed_gid_scan_helpers():
    from app.services.lifecycle.handoff import (
        _find_followed_gid_by_following,
        _followed_gid_from_rows,
        _refresh_followed_gid,
    )

    assert _followed_gid_from_rows("not-a-list", "g") is None
    assert _followed_gid_from_rows([{"following": "g-x", "gid": "g-y"}], "g") is None

    backend = make_aria2_client(
        tell_active=RuntimeError("rpc down"),
        tell_waiting=[{"following": "g-src", "gid": "g-pay"}],
    )
    assert await _find_followed_gid_by_following(backend, "g-src", "[T]") == "g-pay"

    # refresh: tell_status fails, scan fallback finds the payload
    backend = make_aria2_client(
        tell_status=RuntimeError("rpc down"),
        tell_active=[{"following": "g-src", "gid": "g-pay"}],
    )
    with patch.object(handoff_mod, "COMPLETE_SOURCE_RETRY_INTERVAL", 0):
        assert await _refresh_followed_gid(backend, "g-src", "[T]") == "g-pay"
        assert await _refresh_followed_gid(backend, None, "[T]") is None


@pytest.mark.asyncio
async def test_coordinator_handoff_branches(temp_db, monkeypatch):
    # complete+followedBy pre-fetch fails transiently, then waits inside lock
    dl = await create_global_download_v0(
        resource_key="magnet:ho-pre",
        source_uri="magnet:?xt=urn:btih:hopre",
        resource_kind="magnet",
        status="active",
        aria2_gid="g-pre",
    )
    client = make_aria2_client(
        tell_status=[
            _transient_error(),
            {"status": "waiting", "following": "g-pre", "totalLength": "0"},
        ]
    )
    result = await reconcile_attempt_signal(
        backend=client,
        observed_gid="g-pre",
        event="poll",
        observed_status={
            "status": "complete",
            "followedBy": ["g-pay"],
            "totalLength": "0",
        },
        log_prefix="[T]",
    )
    assert result == ReconcileResult.WAITING

    # handoff candidate resolves and waits on unknown payload size
    dl2 = await create_global_download_v0(
        resource_key="magnet:ho-wait",
        source_uri="magnet:?xt=urn:btih:howait",
        resource_kind="magnet",
        status="active",
        aria2_gid="g-src2",
    )
    result = await reconcile_attempt_signal(
        backend=make_aria2_client(),
        observed_gid="g-pay2",
        event="poll",
        observed_status={
            "status": "waiting",
            "following": "g-src2",
            "totalLength": "0",
        },
        log_prefix="[T]",
    )
    assert result == ReconcileResult.WAITING

    # handoff candidate completes and dispatches completion after the lock
    dl3 = await create_global_download_v0(
        resource_key="magnet:ho-dispatch",
        source_uri="magnet:?xt=urn:btih:hodispatch",
        resource_kind="magnet",
        status="active",
        aria2_gid="g-src3",
        total_bytes=10,
        size_known=True,
    )
    monkeypatch.setattr(
        coord_mod, "handle_v0_download_complete", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(
        handoff_mod,
        "coordinate_reported_size",
        AsyncMock(return_value={"outcome": "admitted", "paused_by_us": False}),
    )
    result = await reconcile_attempt_signal(
        backend=make_aria2_client(),
        observed_gid="g-pay3",
        event="poll",
        observed_status={
            "status": "complete",
            "following": "g-src3",
            "totalLength": "10",
            "files": [{"path": "/tmp/payload.bin", "length": "10", "selected": "true"}],
        },
        log_prefix="[T]",
    )
    assert result == ReconcileResult.WAITING  # completion returned changed=False
    row = await _fetch(dl3["id"])
    assert row["aria2_gid"] == "g-pay3"


# ---------------------------------------------------------------------------
# _shared / cleanup leftover branches
# ---------------------------------------------------------------------------


def test_sanitize_path_value_error(monkeypatch):
    def boom(arg):
        raise ValueError("bad path")

    monkeypatch.setattr(shared, "Path", boom)
    assert shared._sanitize_path("whatever", 1) == "whatever"


@pytest.mark.asyncio
async def test_requery_soft_mark_with_re_raw(temp_db):
    # re-query succeeds but shows the wrong state; soft path stamps status
    dl = await create_global_download_v0(
        resource_key="http:rq-raw", status="active", aria2_gid="g-raw"
    )
    backend = make_aria2_client(tell_status={"status": "active"})
    result = await _requery_after_control_failure(**_rq_kwargs(backend, dl["id"], "g-raw"))
    assert result == "soft_failed"
    assert (await _fetch(dl["id"]))["status"] == "active"

    # row vanishes between the outer check and the soft-mark refetch → stale
    real = shared.get_global_download_for_generation

    async def vanishing(download_id, expected_gid):
        if vanishing.calls == 0:
            vanishing.calls += 1
            return await real(download_id, expected_gid)
        return None

    vanishing.calls = 0
    with patch.object(shared, "get_global_download_for_generation", vanishing):
        result = await _requery_after_control_failure(
            **_rq_kwargs(backend, dl["id"], "g-raw")
        )
    assert result == "stale"


@pytest.mark.asyncio
async def test_fail_download_writer_gid_none_and_claim_stale(temp_db):
    dl = await create_global_download_v0(
        resource_key="http:wr-none", status="active", aria2_gid="g-wn"
    )
    # explicit writer_gid=None → empty writer gid lists
    changed = await cleanup_mod.fail_download_and_reclaim(
        backend=make_aria2_client(),
        download_id=dl["id"],
        message="失败",
        expected_gid="g-wn",
        writer_gid=None,
    )
    assert changed is True

    # already terminal → claim rejected → False
    changed = await cleanup_mod.fail_download_and_reclaim(
        backend=make_aria2_client(),
        download_id=dl["id"],
        message="再次失败",
        expected_gid="g-wn",
    )
    assert changed is False


@pytest.mark.asyncio
async def test_terminalize_missing_gid_unknown_size(monkeypatch, temp_db):
    calls = {}

    async def fake_fail(**kwargs):
        calls["code"] = kwargs["error_code"]
        return True

    monkeypatch.setattr(cleanup_mod, "fail_download_and_reclaim", fake_fail)
    result = await cleanup_mod._terminalize_missing_gid_locked(
        backend=make_aria2_client(),
        attempt_id=1,
        current_gid="g",
        total_bytes=0,
        log_prefix="[T]",
    )
    assert result == ReconcileResult.TERMINALIZED
    assert calls["code"] == "unknown_size"


@pytest.mark.asyncio
async def test_reconcile_snapshot_record_failure(temp_db, monkeypatch):
    from app.modules.task_core import sync as sync_mod

    await create_global_download_v0(
        resource_key="http:snap-fail",
        status="active",
        aria2_gid="g-snap",
        total_bytes=10,
        size_known=True,
    )
    monkeypatch.setattr(
        sync_mod, "record_observed_snapshot", AsyncMock(side_effect=RuntimeError("x"))
    )
    with patch.object(
        coord_mod,
        "coordinate_reported_size",
        AsyncMock(return_value={"outcome": "admitted", "paused_by_us": False}),
    ):
        result = await reconcile_attempt_signal(
            backend=make_aria2_client(),
            observed_gid="g-snap",
            event="poll",
            observed_status={"status": "active", "totalLength": "10"},
            log_prefix="[T]",
        )
    assert result == ReconcileResult.CHANGED


@pytest.mark.asyncio
async def test_reconcile_missing_gid_recovery_pending(temp_db):
    await create_global_download_v0(
        resource_key="http:mg-recov",
        status="completed",
        aria2_gid="g-mr",
        completed_file_id=None,
    )
    result = await reconcile_attempt_signal(
        backend=make_aria2_client(),
        observed_gid="g-mr",
        event="poll",
        observed_status=None,
        observed_error=_missing_gid_error(),
        log_prefix="[T]",
    )
    assert result == ReconcileResult.RECOVERY_PENDING


@pytest.mark.asyncio
async def test_reconcile_complete_followed_prefetch_fatal(temp_db):
    dl = await create_global_download_v0(
        resource_key="magnet:pf",
        source_uri="magnet:?xt=urn:btih:pf",
        resource_kind="magnet",
        status="active",
        aria2_gid="g-pf",
    )
    with pytest.raises(RuntimeError):
        await reconcile_attempt_signal(
            backend=make_aria2_client(tell_status=RuntimeError("fatal")),
            observed_gid="g-pf",
            event="poll",
            observed_status={"status": "complete", "followedBy": ["g-pay"]},
            log_prefix="[T]",
        )


@pytest.mark.asyncio
async def test_reconcile_size_outcome_complete_dispatch(temp_db, monkeypatch):
    await create_global_download_v0(
        resource_key="http:sz-comp",
        status="active",
        aria2_gid="g-sz",
        size_known=False,
    )
    monkeypatch.setattr(
        coord_mod,
        "coordinate_reported_size",
        AsyncMock(return_value={"outcome": "complete", "paused_by_us": False}),
    )
    result = await reconcile_attempt_signal(
        backend=make_aria2_client(),
        observed_gid="g-sz",
        event="poll",
        observed_status={"status": "active"},
        log_prefix="[T]",
    )
    # dispatch is recorded but projection still runs first and reports CHANGED
    assert result == ReconcileResult.CHANGED


@pytest.mark.asyncio
async def test_reconcile_size_outcome_rpc_unavailable_projects(temp_db, monkeypatch):
    await create_global_download_v0(
        resource_key="http:sz-rpc",
        status="active",
        aria2_gid="g-sz2",
        size_known=False,
    )
    monkeypatch.setattr(
        coord_mod,
        "coordinate_reported_size",
        AsyncMock(return_value={"outcome": "rpc_unavailable", "paused_by_us": False}),
    )
    result = await reconcile_attempt_signal(
        backend=make_aria2_client(),
        observed_gid="g-sz2",
        event="poll",
        observed_status={"status": "active"},
        log_prefix="[T]",
    )
    assert result == ReconcileResult.CHANGED


@pytest.mark.asyncio
async def test_reconcile_handoff_changed_without_dispatch(temp_db, monkeypatch):
    dl = await create_global_download_v0(
        resource_key="magnet:ho-plain",
        source_uri="magnet:?xt=urn:btih:hoplain",
        resource_kind="magnet",
        status="active",
        aria2_gid="g-hp",
        total_bytes=10,
        size_known=True,
    )

    async def fake_handoff(**kwargs):
        return ReconcileResult.CHANGED, None

    monkeypatch.setattr(coord_mod, "_handoff_locked", fake_handoff)
    result = await reconcile_attempt_signal(
        backend=make_aria2_client(),
        observed_gid="g-pay9",
        event="poll",
        observed_status={
            "status": "active",
            "following": "g-hp",
            "totalLength": "10",
        },
        log_prefix="[T]",
    )
    assert result == ReconcileResult.CHANGED


# ---------------------------------------------------------------------------
# completion: pure helpers
# ---------------------------------------------------------------------------


def test_list_task_dir_entries_oserror(tmp_path, monkeypatch):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    (task_dir / "f").write_bytes(b"x")

    def boom(self):
        raise OSError("io")

    monkeypatch.setattr(Path, "iterdir", boom)
    assert completion_mod._list_task_dir_entries(task_dir) == []
    assert completion_mod._list_task_dir_entries(tmp_path / "missing") == []


def test_resolve_complete_source_path_branches(tmp_path):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    (task_dir / "a.bin").write_bytes(b"a")
    (task_dir / "b.bin").write_bytes(b"b")
    ext = tmp_path / "ext.bin"
    ext.write_bytes(b"e")

    # duplicate task candidates collapse to one existing entry
    assert (
        completion_mod.resolve_complete_source_path(
            task_dir, [{"path": str(task_dir / "a.bin")}] * 2, None
        )
        == task_dir / "a.bin"
    )
    # two existing task candidates collapse to the whole dir
    (task_dir / "b.bin").write_bytes(b"b")
    assert (
        completion_mod.resolve_complete_source_path(
            task_dir,
            [
                {"path": str(task_dir / "a.bin")},
                {"path": str(task_dir / "b.bin")},
            ],
            None,
        )
        == task_dir
    )
    (task_dir / "b.bin").unlink()
    # candidate equals task_dir itself (empty rel path)
    assert (
        completion_mod.resolve_complete_source_path(task_dir, [{"path": str(task_dir)}], None)
        == task_dir
    )
    # missing task candidate falls through to dir entries
    (task_dir / "b.bin").write_bytes(b"b")
    assert (
        completion_mod.resolve_complete_source_path(
            task_dir, [{"path": str(task_dir / "gone.bin")}], None
        )
        == task_dir  # two entries → whole dir
    )
    # single remaining dir entry wins
    (task_dir / "b.bin").unlink()
    assert (
        completion_mod.resolve_complete_source_path(task_dir, [], None)
        == task_dir / "a.bin"
    )
    # empty dir + existing external candidate
    (task_dir / "a.bin").unlink()
    assert (
        completion_mod.resolve_complete_source_path(task_dir, [{"path": str(ext)}], None)
        == ext
    )
    # named candidate fallback
    (task_dir / "named.bin").write_bytes(b"n")
    assert (
        completion_mod.resolve_complete_source_path(task_dir, [], "named.bin")
        == task_dir / "named.bin"
    )
    # named fallback where the only entry is filtered out (.aria2)
    hidden = task_dir / "x.aria2"
    hidden.write_bytes(b"c")
    (task_dir / "named.bin").unlink()
    assert (
        completion_mod.resolve_complete_source_path(task_dir, [], "x.aria2") == hidden
    )
    hidden.unlink()
    # nothing at all
    assert completion_mod.resolve_complete_source_path(task_dir, [], None) is None
    # file item without a path string is skipped
    assert completion_mod.resolve_complete_source_path(task_dir, [{"path": ""}], None) is None


def test_expected_completed_size_branches(tmp_path):
    f = tmp_path / "f"
    f.write_bytes(b"12345")
    # non-dict file items are skipped
    assert (
        completion_mod.expected_completed_size({"files": [42, {"path": "x", "length": "5", "selected": "true"}]}, f)
        == 5
    )
    # selected=false only → zero expected
    assert (
        completion_mod.expected_completed_size(
            {"files": [{"path": "x", "selected": "false", "length": "5"}]}, f
        )
        == 0
    )
    # no usable length and negative total → None
    assert (
        completion_mod.expected_completed_size({"files": [{"path": "x", "selected": "true"}]}, f)
        is None
    )
    # zero total and plain file → None
    assert completion_mod.expected_completed_size({"totalLength": "0"}, f) is None
    # zero total but directory source → total
    d = tmp_path / "d"
    d.mkdir()
    assert completion_mod.expected_completed_size({"totalLength": "0"}, d) == 0


@pytest.mark.asyncio
async def test_resolve_complete_source_with_retry(monkeypatch, tmp_path):
    monkeypatch.setattr(completion_mod, "COMPLETE_SOURCE_RETRY_INTERVAL", 0)
    # no gid + nothing found → None after retries
    assert (
        await completion_mod.resolve_complete_source_with_retry(
            completion_gid=None,
            task_dir=tmp_path,
            files=[],
            task_name=None,
            backend=None,
        )
        is None
    )
    # refresh failure is swallowed
    assert (
        await completion_mod.resolve_complete_source_with_retry(
            completion_gid="g",
            task_dir=tmp_path,
            files=[],
            task_name=None,
            backend=make_aria2_client(tell_status=RuntimeError("rpc down")),
        )
        is None
    )


def test_move_to_content_store_branches(tmp_path, monkeypatch):
    from app.services.storage import get_store_path_for_hash

    src = tmp_path / "src"
    src.write_bytes(b"data")
    # OSError with existing store target → reuse
    def fail_rename(self, target):
        raise OSError("rename refused")

    monkeypatch.setattr(Path, "rename", fail_rename)
    target = tmp_path / "target"
    target.write_bytes(b"existing")
    monkeypatch.setattr(
        completion_mod, "get_store_path_for_hash", lambda h: target
    )
    assert completion_mod._move_to_content_store(src, "h") == (target, False)

    # OSError without existing target → raise
    missing = tmp_path / "missing"
    monkeypatch.setattr(completion_mod, "get_store_path_for_hash", lambda h: missing)
    with pytest.raises(OSError):
        completion_mod._move_to_content_store(src, "h")


def test_delete_download_source_missing(tmp_path):
    completion_mod._delete_download_source(tmp_path / "gone", recursive=False)


@pytest.mark.asyncio
async def test_compensate_completion_safely_cancel(tmp_path):
    started = asyncio.Event()

    async def hanging(**kwargs):
        started.set()
        await asyncio.sleep(30)

    release = asyncio.Event()

    async def hanging(**kwargs):
        started.set()
        await release.wait()

    with patch.object(completion_mod, "_compensate_incomplete_completion", hanging):
        task = asyncio.create_task(completion_mod._compensate_completion_safely(x=1))
        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel()
        release.set()
        assert await task is None  # cancellation is shielded, compensation finishes


@pytest.mark.asyncio
async def test_compensate_skips_finalized_download(temp_db):
    from sqlalchemy import insert, update

    from app.core.time_utils import now_ms
    from app.db.schema import stored_files

    source = tmp_store_source(b"finalized")
    dl = await create_global_download_v0(
        resource_key="http:comp-done",
        status="completed",
        aria2_gid=None,
    )
    async with transaction() as conn:
        file_id = (
            await conn.execute(
                insert(stored_files)
                .values(
                    content_hash="comp-done",
                    real_path=str(source),
                    size_bytes=1,
                    is_directory=0,
                    original_name="f",
                    created_at_ms=now_ms(),
                )
                .returning(stored_files.c.id)
            )
        ).scalar()
        await conn.execute(
            update(global_downloads)
            .where(global_downloads.c.id == dl["id"])
            .values(completed_file_id=file_id)
        )
    await completion_mod._compensate_incomplete_completion(
        global_download_id=dl["id"],
        content_hash="whatever",
        store_path=source,
        source_path=source,
        moved_source=True,
        created_stored_file_id=None,
        registration_started=True,
    )
    assert source.exists()  # finalized download: nothing touched


@pytest.mark.asyncio
async def test_compensate_incomplete_completion_branches(temp_db):
    from sqlalchemy import select

    from app.db.schema import stored_files

    dl = await create_global_download_v0(
        resource_key="http:comp", status="active", aria2_gid="g-comp"
    )
    source = tmp_store_source(b"compensate")
    store_path = await _register_stored_row(source)
    from app.services.storage_index import scan_storage_path_async

    identity = (await scan_storage_path_async(source)).content_identity

    await completion_mod._compensate_incomplete_completion(
        global_download_id=dl["id"],
        content_hash=identity.content_hash,
        store_path=store_path,
        source_path=source,
        moved_source=True,
        created_stored_file_id=None,
        registration_started=True,
    )
    async with transaction() as conn:
        left = (
            (
                await conn.execute(
                    select(stored_files).where(
                        stored_files.c.content_hash == identity.content_hash
                    )
                )
            ).first()
        )
    assert left is None  # unclaimed registration deleted
    assert source.exists()  # moved source restored


def tmp_store_source(payload: bytes) -> Path:
    import tempfile

    from app.core.config import settings

    src_dir = Path(settings.download_dir) / "downloading" / "comp-src"
    src_dir.mkdir(parents=True, exist_ok=True)
    src = src_dir / "payload.bin"
    src.write_bytes(payload)
    return src


# ---------------------------------------------------------------------------
# completion: complete_global_download_locked branches
# ---------------------------------------------------------------------------


async def _register_stored_row(src: Path, *, real_path: Path | None = None) -> Path:
    """Insert a stored_files row matching the v2 identity of *src*'s content."""
    from sqlalchemy import insert

    from app.core.time_utils import now_ms
    from app.db.schema import stored_files
    from app.services.storage import get_store_path_for_hash
    from app.services.storage_index import scan_storage_path_async

    scan = await scan_storage_path_async(src)
    identity = scan.content_identity
    store_path = real_path or get_store_path_for_hash(identity.content_hash)
    async with transaction() as conn:
        await conn.execute(
            insert(stored_files).values(
                content_hash=identity.content_hash,
                content_hash_version=identity.version,
                content_object_kind=identity.object_kind,
                content_digest=identity.digest,
                real_path=str(store_path),
                size_bytes=scan.size_bytes,
                is_directory=1 if scan.is_directory else 0,
                original_name=src.name,
                created_at_ms=now_ms(),
            )
        )
    return store_path




@pytest.mark.asyncio
async def test_complete_locked_source_missing(temp_db):
    dl = await create_global_download_v0(
        resource_key="http:cl-miss", status="active", aria2_gid="g-cl1"
    )
    with pytest.raises(FileNotFoundError):
        await completion_mod.complete_global_download_locked(
            global_download_id=dl["id"],
            expected_gid="g-cl1",
            source_path=Path("/nonexistent/source"),
            original_name="x",
        )


@pytest.mark.asyncio
async def test_complete_locked_generation_stale(temp_db, tmp_path):
    dl = await create_global_download_v0(
        resource_key="http:cl-stale", status="active", aria2_gid="g-cl2"
    )
    src = tmp_path / "s"
    src.write_bytes(b"x")
    assert (
        await completion_mod.complete_global_download_locked(
            global_download_id=dl["id"],
            expected_gid="g-other",
            source_path=src,
            original_name="x",
        )
        is None
    )


@pytest.mark.asyncio
async def test_complete_locked_invalid_source(temp_db, tmp_path, monkeypatch):
    from app.services.storage_index import StorageScanError

    dl = await create_global_download_v0(
        resource_key="http:cl-invalid", status="active", aria2_gid="g-cl3"
    )
    src = tmp_path / "s"
    src.write_bytes(b"x")

    async def boom(path):
        raise StorageScanError("bad layout")

    monkeypatch.setattr(completion_mod, "_scan_completed_source", boom)
    result = await completion_mod.complete_global_download_locked(
        global_download_id=dl["id"],
        expected_gid="g-cl3",
        source_path=src,
        original_name="x",
    )
    assert result["status"] == "invalid_source"


@pytest.mark.asyncio
async def test_complete_locked_admission_stale(temp_db, tmp_path, monkeypatch):
    from app.repositories.task.downloads import SizeReconcileResult

    dl = await create_global_download_v0(
        resource_key="http:cl-adm", status="active", aria2_gid="g-cl4"
    )
    src = tmp_path / "s"
    src.write_bytes(b"hello")

    async def stale(**kwargs):
        return SizeReconcileResult(outcome="stale")

    monkeypatch.setattr(completion_mod, "reconcile_download_size", stale)
    assert (
        await completion_mod.complete_global_download_locked(
            global_download_id=dl["id"],
            expected_gid="g-cl4",
            source_path=src,
            original_name="x",
        )
        is None
    )


@pytest.mark.asyncio
async def _make_completion_download(resource_key: str, gid: str):
    return await create_global_download_v0(
        resource_key=resource_key, status="active", aria2_gid=gid
    )


def _admitted(monkeypatch):
    from app.repositories.task.downloads import SizeReconcileResult

    async def ok(**kwargs):
        return SizeReconcileResult(outcome="admitted")

    monkeypatch.setattr(completion_mod, "reconcile_download_size", ok)


@pytest.mark.asyncio
async def test_complete_locked_restores_missing_store_file(temp_db, tmp_path, monkeypatch):
    from sqlalchemy import insert

    from app.core.time_utils import now_ms
    from app.db.schema import stored_files
    from app.services.storage import get_store_dir

    dl = await _make_completion_download("http:cl-restore", "g-cl5")
    _admitted(monkeypatch)
    payload = b"restore-me"
    src = tmp_path / "s"
    src.write_bytes(payload)
    store_path = await _register_stored_row(src)  # row registered, file missing
    monkeypatch.setattr(
        completion_mod,
        "complete_active_user_tasks_for_stored_file",
        AsyncMock(return_value=0),
    )
    result = await completion_mod.complete_global_download_locked(
        global_download_id=dl["id"],
        expected_gid="g-cl5",
        source_path=src,
        original_name="x",
    )
    assert result["status"] == "completed"
    assert store_path.exists()


@pytest.mark.asyncio
async def test_complete_locked_create_raises(temp_db, tmp_path, monkeypatch):
    dl = await _make_completion_download("http:cl-raise", "g-cl6")
    _admitted(monkeypatch)
    src = tmp_path / "s"
    src.write_bytes(b"payload")

    async def boom(values, entries):
        raise RuntimeError("db down")

    monkeypatch.setattr(completion_mod, "create_stored_file_with_entries", boom)
    with pytest.raises(RuntimeError):
        await completion_mod.complete_global_download_locked(
            global_download_id=dl["id"],
            expected_gid="g-cl6",
            source_path=src,
            original_name="x",
        )
    # source was moved then compensated back
    assert src.exists()


@pytest.mark.asyncio
async def test_complete_locked_create_cancelled(temp_db, tmp_path, monkeypatch):
    dl = await _make_completion_download("http:cl-cancel", "g-cl12")
    _admitted(monkeypatch)
    src = tmp_path / "s"
    src.write_bytes(b"cancel-me")

    async def conflict(values, entries):
        raise asyncio.CancelledError()

    monkeypatch.setattr(completion_mod, "create_stored_file_with_entries", conflict)
    with pytest.raises(asyncio.CancelledError):
        await completion_mod.complete_global_download_locked(
            global_download_id=dl["id"],
            expected_gid="g-cl12",
            source_path=src,
            original_name="x",
        )
    assert src.exists()  # compensated back


@pytest.mark.asyncio
async def test_complete_locked_conflict_uses_existing(temp_db, tmp_path, monkeypatch):
    from app.repositories.errors import RepositoryConflictError
    from app.services.storage_index import content_identity_from_content_hash

    dl = await _make_completion_download("http:cl-conf", "g-cl7")
    _admitted(monkeypatch)
    payload = b"conflict-payload"
    src_dir = get_downloading_dir() / str(dl["id"])
    src_dir.mkdir(parents=True, exist_ok=True)
    src = src_dir / "payload.bin"
    src.write_bytes(payload)

    async def conflict(values, entries):
        raise RepositoryConflictError("conflict")

    monkeypatch.setattr(completion_mod, "create_stored_file_with_entries", conflict)

    # existing row appears only after the first lookup misses
    from app.services.storage_index import scan_storage_path_async

    scan = await scan_storage_path_async(src)
    identity = scan.content_identity
    store_path = await _register_stored_row(src)  # row only, file still missing

    async def lookup_after_miss(ident):
        if lookup_after_miss.calls == 0:
            lookup_after_miss.calls += 1
            return None
        return {
            "id": 1,
            "content_hash": identity.content_hash,
            "real_path": str(store_path),
            "size_bytes": len(payload),
        }

    lookup_after_miss.calls = 0
    monkeypatch.setattr(completion_mod, "get_stored_file_by_identity", lookup_after_miss)
    monkeypatch.setattr(
        completion_mod,
        "complete_active_user_tasks_for_stored_file",
        AsyncMock(return_value=0),
    )
    result = await completion_mod.complete_global_download_locked(
        global_download_id=dl["id"],
        expected_gid="g-cl7",
        source_path=src,
        original_name="x",
    )
    assert result["status"] == "completed"


@pytest.mark.asyncio
async def test_complete_locked_conflict_without_existing(temp_db, tmp_path, monkeypatch):
    from app.repositories.errors import RepositoryConflictError

    dl = await _make_completion_download("http:cl-conf2", "g-cl8")
    _admitted(monkeypatch)
    src = tmp_path / "s"
    src.write_bytes(b"other")

    async def conflict(values, entries):
        raise RepositoryConflictError("conflict")

    async def none(identity):
        return None

    monkeypatch.setattr(completion_mod, "create_stored_file_with_entries", conflict)
    monkeypatch.setattr(completion_mod, "get_stored_file_by_identity", none)
    with pytest.raises(RepositoryConflictError):
        await completion_mod.complete_global_download_locked(
            global_download_id=dl["id"],
            expected_gid="g-cl8",
            source_path=src,
            original_name="x",
        )
    assert src.exists()  # compensated


@pytest.mark.asyncio
async def test_complete_locked_conflict_lookup_cancelled(temp_db, tmp_path, monkeypatch):
    from app.repositories.errors import RepositoryConflictError

    dl = await _make_completion_download("http:cl-conf3", "g-cl9")
    _admitted(monkeypatch)
    src = tmp_path / "s"
    src.write_bytes(b"other2")

    async def conflict(values, entries):
        raise RepositoryConflictError("conflict")

    async def lookup(ident):
        lookup.calls += 1
        if lookup.calls == 2:
            raise asyncio.CancelledError()
        return None

    lookup.calls = 0

    monkeypatch.setattr(completion_mod, "create_stored_file_with_entries", conflict)
    monkeypatch.setattr(completion_mod, "get_stored_file_by_identity", lookup)
    with pytest.raises(asyncio.CancelledError):
        await completion_mod.complete_global_download_locked(
            global_download_id=dl["id"],
            expected_gid="g-cl9",
            source_path=src,
            original_name="x",
        )


@pytest.mark.asyncio
async def test_complete_locked_user_task_registration_fails(
    temp_db, tmp_path, monkeypatch
):
    dl = await _make_completion_download("http:cl-utfail", "g-cl10")
    _admitted(monkeypatch)
    src = tmp_path / "s"
    src.write_bytes(b"utfail")

    monkeypatch.setattr(
        completion_mod,
        "complete_active_user_tasks_for_stored_file",
        AsyncMock(side_effect=RuntimeError("boom")),
    )
    with pytest.raises(RuntimeError):
        await completion_mod.complete_global_download_locked(
            global_download_id=dl["id"],
            expected_gid="g-cl10",
            source_path=src,
            original_name="x",
        )


@pytest.mark.asyncio
async def test_complete_locked_user_files_none(temp_db, tmp_path, monkeypatch):
    dl = await _make_completion_download("http:cl-none", "g-cl11")
    _admitted(monkeypatch)
    src = tmp_path / "s"
    src.write_bytes(b"none-payload")

    monkeypatch.setattr(
        completion_mod,
        "complete_active_user_tasks_for_stored_file",
        AsyncMock(return_value=None),
    )
    assert (
        await completion_mod.complete_global_download_locked(
            global_download_id=dl["id"],
            expected_gid="g-cl11",
            source_path=src,
            original_name="x",
        )
        is None
    )


# ---------------------------------------------------------------------------
# completion: handle_v0_download_complete branches
# ---------------------------------------------------------------------------


def _status_with_file(path: Path, length: int) -> dict:
    return {
        "status": "complete",
        "totalLength": str(length),
        "completedLength": str(length),
        "files": [{"path": str(path), "length": str(length), "selected": "true"}],
    }


@pytest.mark.asyncio
async def test_handle_complete_stale_generation(temp_db):
    dl = await _make_completion_download("http:h-stale", "g-h1")
    assert not await completion_mod.handle_v0_download_complete(
        backend=make_aria2_client(),
        download=dl,
        aria2_status={},
        completion_gid="g-wrong",
        log_prefix="[T]",
        allow_metadata_handoff_defer=False,
    )


@pytest.mark.asyncio
async def test_handle_complete_fenced_stale(temp_db, monkeypatch):
    dl = await _make_completion_download("http:h-fenced", "g-h2")
    real = completion_mod.get_global_download_for_generation
    calls = {"n": 0}

    async def once_then_none(download_id, gid):
        calls["n"] += 1
        if calls["n"] == 1:
            return await real(download_id, gid)
        return None

    monkeypatch.setattr(
        completion_mod, "get_global_download_for_generation", once_then_none
    )
    assert not await completion_mod.handle_v0_download_complete(
        backend=make_aria2_client(),
        download=dl,
        aria2_status={"files": None},
        completion_gid="g-h2",
        log_prefix="[T]",
        allow_metadata_handoff_defer=False,
    )


@pytest.mark.asyncio
async def test_handle_complete_completion_result_none(temp_db, monkeypatch):
    dl = await _make_completion_download("http:h-resnone", "g-h9")
    task_dir = get_downloading_dir() / str(dl["id"])
    task_dir.mkdir()
    payload = task_dir / "p.bin"
    payload.write_bytes(b"x")
    monkeypatch.setattr(
        completion_mod, "complete_global_download_locked", AsyncMock(return_value=None)
    )
    assert not await completion_mod.handle_v0_download_complete(
        backend=make_aria2_client(),
        download=dl,
        aria2_status=_status_with_file(payload, 1),
        completion_gid="g-h9",
        log_prefix="[T]",
        allow_metadata_handoff_defer=False,
    )


@pytest.mark.asyncio
async def test_handle_complete_original_name_from_source(temp_db, monkeypatch):
    user = await create_user_v0(username="h_name")
    dl = await create_global_download_v0(
        resource_key="magnet:h-name",
        source_uri="magnet:?xt=urn:btih:hname",
        resource_kind="magnet",
        status="active",
        aria2_gid="g-h10",
        display_name=None,
    )
    await create_user_task_v0(
        user_id=user["id"], global_download_id=dl["id"], status="active"
    )
    task_dir = get_downloading_dir() / str(dl["id"])
    task_dir.mkdir()
    payload = task_dir / "src-name.bin"
    payload.write_bytes(b"named-payload")

    monkeypatch.setattr(
        completion_mod.download_ops, "extract_display_name", lambda *a, **k: None
    )
    assert await completion_mod.handle_v0_download_complete(
        backend=make_aria2_client(),
        download=dl,
        aria2_status=_status_with_file(payload, len(b"named-payload")),
        completion_gid="g-h10",
        log_prefix="[T]",
        allow_metadata_handoff_defer=False,
    )
    assert (await _fetch(dl["id"]))["status"] == "completed"


@pytest.mark.asyncio
async def test_handle_complete_late_switch_on_missing_source(temp_db, monkeypatch):
    dl = await _make_completion_download("magnet:h-late", "g-h3")
    dl["resource_kind"] = "magnet"
    monkeypatch.setattr(
        completion_mod,
        "switch_to_late_followed_download_if_supported",
        AsyncMock(return_value=True),
    )
    assert await completion_mod.handle_v0_download_complete(
        backend=make_aria2_client(),
        download=dl,
        aria2_status={"status": "complete", "files": []},
        completion_gid="g-h3",
        log_prefix="[T]",
        allow_metadata_handoff_defer=False,
    )


@pytest.mark.asyncio
async def test_handle_complete_normal_and_dir_cleanup_failure(temp_db, monkeypatch):
    user = await create_user_v0(username="h_norm")
    dl = await create_global_download_v0(
        resource_key="http:h-norm",
        source_uri="https://example.com/h-norm",
        resource_kind="http",
        status="active",
        aria2_gid="g-h4",
        display_name="norm.bin",
        total_bytes=0,
        size_known=False,
    )
    await create_user_task_v0(
        user_id=user["id"], global_download_id=dl["id"], status="active"
    )
    task_dir = get_downloading_dir() / str(dl["id"])
    task_dir.mkdir()
    payload = task_dir / "norm.bin"
    payload.write_bytes(b"norm-data")

    status = _status_with_file(payload, len(b"norm-data"))
    assert await completion_mod.handle_v0_download_complete(
        backend=make_aria2_client(),
        download=dl,
        aria2_status=status,
        completion_gid="g-h4",
        log_prefix="[T]",
        allow_metadata_handoff_defer=False,
    )
    assert (await _fetch(dl["id"]))["status"] == "completed"

    # second download whose dir cleanup fails → still reported True
    dl2 = await create_global_download_v0(
        resource_key="http:h-cleanfail",
        source_uri="https://example.com/h-cf",
        resource_kind="http",
        status="active",
        aria2_gid="g-h5",
        display_name="cf.bin",
        total_bytes=0,
        size_known=False,
    )
    await create_user_task_v0(
        user_id=user["id"], global_download_id=dl2["id"], status="active"
    )
    task_dir2 = get_downloading_dir() / str(dl2["id"])
    task_dir2.mkdir()
    payload2 = task_dir2 / "cf.bin"
    payload2.write_bytes(b"cf-data")

    async def boom(download_id):
        if download_id == dl2["id"]:
            raise OSError("cleanup fail")

    monkeypatch.setattr(completion_mod, "cleanup_task_download_dir", boom)
    assert await completion_mod.handle_v0_download_complete(
        backend=make_aria2_client(),
        download=dl2,
        aria2_status=_status_with_file(payload2, len(b"cf-data")),
        completion_gid="g-h5",
        log_prefix="[T]",
        allow_metadata_handoff_defer=False,
    )


@pytest.mark.asyncio
async def test_handle_complete_incomplete_late_switch(temp_db, monkeypatch):
    user = await create_user_v0(username="h_inc")
    dl = await create_global_download_v0(
        resource_key="http:h-inc",
        source_uri="https://example.com/h-inc",
        resource_kind="http",
        status="active",
        aria2_gid="g-h6",
        display_name="inc.bin",
        total_bytes=0,
        size_known=False,
    )
    await create_user_task_v0(
        user_id=user["id"], global_download_id=dl["id"], status="active"
    )
    task_dir = get_downloading_dir() / str(dl["id"])
    task_dir.mkdir()
    payload = task_dir / "inc.bin"
    payload.write_bytes(b"short")

    # aria2 claims 100 bytes but the file is smaller → incomplete → late switch
    monkeypatch.setattr(
        completion_mod,
        "switch_to_late_followed_download_if_supported",
        AsyncMock(return_value=True),
    )
    assert await completion_mod.handle_v0_download_complete(
        backend=make_aria2_client(),
        download=dl,
        aria2_status=_status_with_file(payload, 100),
        completion_gid="g-h6",
        log_prefix="[T]",
        allow_metadata_handoff_defer=False,
    )


@pytest.mark.asyncio
async def test_handle_complete_incomplete_fails(temp_db, monkeypatch):
    user = await create_user_v0(username="h_inc2")
    dl = await create_global_download_v0(
        resource_key="http:h-inc2",
        source_uri="https://example.com/h-inc2",
        resource_kind="http",
        status="active",
        aria2_gid="g-h7",
        display_name="inc2.bin",
        total_bytes=0,
        size_known=False,
    )
    await create_user_task_v0(
        user_id=user["id"], global_download_id=dl["id"], status="active"
    )
    task_dir = get_downloading_dir() / str(dl["id"])
    task_dir.mkdir()
    payload = task_dir / "inc2.bin"
    payload.write_bytes(b"short")

    monkeypatch.setattr(
        completion_mod,
        "switch_to_late_followed_download_if_supported",
        AsyncMock(return_value=False),
    )
    assert await completion_mod.handle_v0_download_complete(
        backend=make_aria2_client(),
        download=dl,
        aria2_status=_status_with_file(payload, 100),
        completion_gid="g-h7",
        log_prefix="[T]",
        allow_metadata_handoff_defer=False,
    )
    assert (await _fetch(dl["id"]))["status"] == "failed"


@pytest.mark.asyncio
async def test_handle_complete_source_not_found_fails(temp_db, monkeypatch):
    dl = await _make_completion_download("http:h-nof", "g-h8")
    monkeypatch.setattr(
        completion_mod,
        "switch_to_late_followed_download_if_supported",
        AsyncMock(return_value=False),
    )
    assert await completion_mod.handle_v0_download_complete(
        backend=make_aria2_client(),
        download=dl,
        aria2_status={"status": "complete", "files": []},
        completion_gid="g-h8",
        log_prefix="[T]",
        allow_metadata_handoff_defer=False,
    )
    assert (await _fetch(dl["id"]))["status"] == "failed"


@pytest.mark.asyncio
async def test_handle_complete_metadata_named_source(temp_db, monkeypatch):
    from app.services.task_projection import METADATA_NAME_PREFIX

    user = await create_user_v0(username="h_meta")
    dl = await create_global_download_v0(
        resource_key="magnet:h-meta",
        source_uri="magnet:?xt=urn:btih:hmeta",
        resource_kind="magnet",
        status="active",
        aria2_gid="g-h11",
        display_name=None,
    )
    await create_user_task_v0(
        user_id=user["id"], global_download_id=dl["id"], status="active"
    )
    task_dir = get_downloading_dir() / str(dl["id"])
    task_dir.mkdir()
    payload = task_dir / "meta.bin"
    payload.write_bytes(b"meta-payload")

    monkeypatch.setattr(
        completion_mod.download_ops,
        "extract_display_name",
        lambda *a, **k: METADATA_NAME_PREFIX + "old",
    )
    assert await completion_mod.handle_v0_download_complete(
        backend=make_aria2_client(),
        download=dl,
        aria2_status=_status_with_file(payload, len(b"meta-payload")),
        completion_gid="g-h11",
        log_prefix="[T]",
        allow_metadata_handoff_defer=False,
    )
    assert (await _fetch(dl["id"]))["status"] == "completed"


@pytest.mark.asyncio
async def test_handle_complete_rejected_and_invalid(temp_db, monkeypatch):
    dl = await _make_completion_download("http:h-rej", "g-h12")
    task_dir = get_downloading_dir() / str(dl["id"])
    task_dir.mkdir()
    payload = task_dir / "p.bin"
    payload.write_bytes(b"x")

    monkeypatch.setattr(
        completion_mod,
        "complete_global_download_locked",
        AsyncMock(
            return_value={
                "status": "rejected",
                "reason": "disk_budget",
                "entries_created": 0,
                "user_files_created": 0,
            }
        ),
    )
    assert await completion_mod.handle_v0_download_complete(
        backend=make_aria2_client(),
        download=dl,
        aria2_status=_status_with_file(payload, 1),
        completion_gid="g-h12",
        log_prefix="[T]",
        allow_metadata_handoff_defer=False,
    )

    dl2 = await _make_completion_download("http:h-inv", "g-h13")
    task_dir2 = get_downloading_dir() / str(dl2["id"])
    task_dir2.mkdir()
    payload2 = task_dir2 / "p.bin"
    payload2.write_bytes(b"x")
    monkeypatch.setattr(
        completion_mod,
        "complete_global_download_locked",
        AsyncMock(
            return_value={
                "status": "invalid_source",
                "entries_created": 0,
                "user_files_created": 0,
            }
        ),
    )
    assert await completion_mod.handle_v0_download_complete(
        backend=make_aria2_client(),
        download=dl2,
        aria2_status=_status_with_file(payload2, 1),
        completion_gid="g-h13",
        log_prefix="[T]",
        allow_metadata_handoff_defer=False,
    )
    assert (await _fetch(dl2["id"]))["status"] == "failed"


@pytest.mark.asyncio
async def test_complete_locked_user_tasks_cancelled(temp_db, tmp_path, monkeypatch):
    dl = await _make_completion_download("http:cl-utc", "g-cl13")
    _admitted(monkeypatch)
    src = tmp_path / "s"
    src.write_bytes(b"utc")

    monkeypatch.setattr(
        completion_mod,
        "complete_active_user_tasks_for_stored_file",
        AsyncMock(side_effect=asyncio.CancelledError()),
    )
    with pytest.raises(asyncio.CancelledError):
        await completion_mod.complete_global_download_locked(
            global_download_id=dl["id"],
            expected_gid="g-cl13",
            source_path=src,
            original_name="x",
        )
    assert src.exists()  # compensated


@pytest.mark.asyncio
async def test_complete_locked_existing_store_file(temp_db, monkeypatch):
    dl = await _make_completion_download("http:cl-exist", "g-cl14")
    _admitted(monkeypatch)
    src_dir = get_downloading_dir() / str(dl["id"])
    src_dir.mkdir(parents=True, exist_ok=True)
    src = src_dir / "p.bin"
    src.write_bytes(b"already-stored")

    store_path = await _register_stored_row(src)
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_bytes(b"already-stored")  # physical store copy exists
    monkeypatch.setattr(
        completion_mod,
        "complete_active_user_tasks_for_stored_file",
        AsyncMock(return_value=0),
    )
    result = await completion_mod.complete_global_download_locked(
        global_download_id=dl["id"],
        expected_gid="g-cl14",
        source_path=src,
        original_name="x",
    )
    assert result["status"] == "completed"
    assert not src.exists()  # unclaimed source deleted, store copy kept


# ---------------------------------------------------------------------------
# lifecycle/repair leftover branches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_legacy_http_job_fatal_error():
    from app.services.lifecycle.repair import LEGACY_HTTP_STOP_ERROR, _stop_legacy_http_job

    gateway = AsyncMock()
    gateway.force_remove = AsyncMock(side_effect=RuntimeError("rpc dead"))
    with pytest.raises(RuntimeError, match=LEGACY_HTTP_STOP_ERROR):
        await _stop_legacy_http_job(gateway, download_id=1, gid="g")

    # remove_download_result failure is only logged
    gateway2 = AsyncMock()
    gateway2.force_remove = AsyncMock(side_effect=RuntimeError("gid not found"))
    gateway2.remove_download_result = AsyncMock(side_effect=OSError("boom"))
    await _stop_legacy_http_job(gateway2, download_id=1, gid="g")


@pytest.mark.asyncio
async def test_reconcile_legacy_http_claim_stale(temp_db, monkeypatch):
    from app.services.lifecycle.repair import reconcile_legacy_http_downloads_v0

    user = await create_user_v0(username="legacy_http")
    dl = await create_global_download_v0(
        resource_key="http:legacy-stale",
        source_uri="http://example.com/f.torrent",
        resource_kind="http",
        status="active",
        aria2_gid="g-legacy",
    )
    await create_user_task_v0(
        user_id=user["id"], global_download_id=dl["id"], status="active"
    )

    async def not_changed(**kwargs):
        return False

    monkeypatch.setattr(
        "app.services.lifecycle.repair.fail_download_and_reclaim", not_changed
    )
    gateway = AsyncMock()
    gateway.get_uris = AsyncMock(return_value=[{"uri": "http://evil/"}])
    count = await reconcile_legacy_http_downloads_v0(gateway)
    assert count == 0
