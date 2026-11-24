"""T10: normal status projection and missing-GID converge tests.

Verifies (spec §8.1, §14, task T10):
1. active status projection updates completed_bytes / status.
2. old GID event is stale / ignored, no DB writes.
3. already-failed task receiving error event does not re-fail or cleanup.
4. missing GID + DB current GID already switched → stale.
5. missing GID + live known total → terminalized with gid_missing.
6. missing GID + completed without file → recovery_pending, no fail / purge.
7. transient RPC error → waiting, task stays live.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.db.engine import transaction
from app.db.schema import global_downloads, user_tasks
from app.domain.lifecycle import ReconcileResult
from app.services.lifecycle import coordinator as coordinator_mod
from app.services.lifecycle.coordinator import reconcile_attempt_signal
from tests.fakes import make_aria2_client
from tests.helpers_v0 import (
    create_global_download_v0,
    create_user_task_v0,
    create_user_v0,
)


async def _set_usage_reserved(user_id: int, reserved: int) -> None:
    from app.db.schema import user_storage_usage

    async with transaction() as conn:
        await conn.execute(
            user_storage_usage.update()
            .where(user_storage_usage.c.user_id == user_id)
            .values(reserved_bytes=reserved)
        )


def _missing_gid_error(gid: str = "gid_test") -> Exception:
    return Exception(f"gid {gid} not found (artificial)")


def _transient_error() -> Exception:
    return ConnectionError("cannot connect to host localhost:6800")


async def _fetch_global(download_id: int) -> dict:
    async with transaction() as conn:
        row = (
            (
                await conn.execute(
                    select(global_downloads).where(
                        global_downloads.c.id == download_id
                    )
                )
            )
            .mappings()
            .one()
        )
    return dict(row)


async def _fetch_user_tasks(download_id: int) -> list[dict]:
    async with transaction() as conn:
        rows = (
            (
                await conn.execute(
                    select(user_tasks).where(
                        user_tasks.c.global_download_id == download_id
                    )
                )
            )
            .mappings()
            .all()
        )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 1. active status projection updates completed_bytes / status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_active_projection_updates_status_and_bytes(temp_db: str) -> None:
    user = await create_user_v0(username="t10_active", quota_bytes=10_000)
    download = await create_global_download_v0(
        resource_key="http:t10-active",
        source_uri="https://example.com/file.zip",
        resource_kind="http",
        status="active",
        aria2_gid="gid_active_001",
        total_bytes=1024,
        size_known=True,
        completed_bytes=0,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
    )

    client = make_aria2_client()
    result = await reconcile_attempt_signal(
        backend=client,
        observed_gid="gid_active_001",
        event="start",
        observed_status={
            "status": "active",
            "totalLength": "1024",
            "completedLength": "512",
        },
        log_prefix="[T10]",
    )
    assert result == ReconcileResult.CHANGED

    stored = await _fetch_global(download["id"])
    assert stored["status"] == "active"
    assert stored["completed_bytes"] == 512

    tasks = await _fetch_user_tasks(download["id"])
    assert len(tasks) == 1
    assert tasks[0]["status"] == "active"


# ---------------------------------------------------------------------------
# 2. old GID event is stale / ignored, no DB writes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_old_gid_event_does_not_modify_db(temp_db: str) -> None:
    user = await create_user_v0(username="t10_oldgid", quota_bytes=10_000)
    download = await create_global_download_v0(
        resource_key="http:t10-oldgid",
        source_uri="https://example.com/file.zip",
        resource_kind="http",
        status="active",
        aria2_gid="gid_new_current",
        total_bytes=1024,
        size_known=True,
        completed_bytes=200,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
    )

    # Observed GID does not match any download → resolve returns None → IGNORED.
    client = make_aria2_client()
    result = await reconcile_attempt_signal(
        backend=client,
        observed_gid="gid_old_before_handoff",
        event="start",
        observed_status={"status": "active", "totalLength": "1024"},
        log_prefix="[T10]",
    )
    assert result == ReconcileResult.IGNORED

    stored = await _fetch_global(download["id"])
    assert stored["status"] == "active"
    assert stored["completed_bytes"] == 200
    assert stored["aria2_gid"] == "gid_new_current"


# ---------------------------------------------------------------------------
# 3. already-failed task receiving error event does not re-fail / cleanup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_task_error_event_no_double_fail(temp_db: str) -> None:
    user = await create_user_v0(username="t10_failed", quota_bytes=10_000)
    download = await create_global_download_v0(
        resource_key="http:t10-failed",
        source_uri="https://example.com/file.zip",
        resource_kind="http",
        status="failed",
        aria2_gid="gid_failed_003",
        total_bytes=1024,
        completed_bytes=0,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="failed",
    )

    client = make_aria2_client()
    result = await reconcile_attempt_signal(
        backend=client,
        observed_gid="gid_failed_003",
        event="error",
        observed_status={
            "status": "error",
            "errorCode": "timeout",
            "errorMessage": "connection timeout",
        },
        log_prefix="[T10]",
    )
    assert result == ReconcileResult.ALREADY_TERMINAL

    # No destructive operations performed.
    client.force_remove.assert_not_called()
    client.remove_download_result.assert_not_called()

    stored = await _fetch_global(download["id"])
    assert stored["status"] == "failed"


# ---------------------------------------------------------------------------
# 4. missing GID + DB current GID already switched → stale
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_gid_db_switched_stale(temp_db: str) -> None:
    user = await create_user_v0(username="t10_missing_stale", quota_bytes=10_000)
    download = await create_global_download_v0(
        resource_key="http:t10-missing-stale",
        source_uri="https://example.com/file.zip",
        resource_kind="http",
        status="active",
        aria2_gid="gid_original_004",
        total_bytes=1024,
        size_known=True,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
    )

    # Simulate: resolve finds the attempt by "gid_original_004", but the
    # in-lock snapshot shows the GID has already switched.
    fake_snapshot = {
        "id": download["id"],
        "aria2_gid": "gid_new_after_switch",
        "status": "active",
        "completed_file_id": None,
        "completed_bytes": 0,
        "total_bytes": 1024,
    }

    err = _missing_gid_error("gid_original_004")
    client = make_aria2_client()
    with patch.object(
        coordinator_mod,
        "get_global_download_status_snapshot",
        new=AsyncMock(return_value=fake_snapshot),
    ):
        result = await reconcile_attempt_signal(
            backend=client,
            observed_gid="gid_original_004",
            event=None,
            observed_status=None,
            observed_error=err,
            log_prefix="[T10]",
        )
    assert result == ReconcileResult.STALE

    client.force_remove.assert_not_called()


# ---------------------------------------------------------------------------
# 5. missing GID + live known total → terminalized with gid_missing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_gid_live_known_size_terminalized(temp_db: str) -> None:
    user = await create_user_v0(username="t10_missing_live", quota_bytes=10_000)
    download = await create_global_download_v0(
        resource_key="http:t10-missing-live",
        source_uri="https://example.com/file.zip",
        resource_kind="http",
        status="active",
        aria2_gid="gid_live_005",
        total_bytes=4096,
        size_known=True,
        disk_reserved_bytes=4096,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=4096,
    )
    await _set_usage_reserved(user["id"], 4096)

    err = _missing_gid_error("gid_live_005")
    client = make_aria2_client()
    with (
        patch("app.services.failed_task_cleanup.cleanup_task_download_dir") as mock_dir,
        patch(
            "app.services.failed_task_cleanup.get_downloading_dir"
        ) as mock_get_dir,
    ):
        mock_dir.return_value = None
        mock_get_dir.return_value.__truediv__ = lambda self, x: f"/dl/{x}"

        result = await reconcile_attempt_signal(
            backend=client,
            observed_gid="gid_live_005",
            event=None,
            observed_status=None,
            observed_error=err,
            log_prefix="[T10]",
        )

    assert result == ReconcileResult.TERMINALIZED

    stored = await _fetch_global(download["id"])
    assert stored["status"] == "failed"
    assert stored["error_code"] == "gid_missing"

    tasks = await _fetch_user_tasks(download["id"])
    assert len(tasks) == 1
    assert tasks[0]["status"] == "failed"


# ---------------------------------------------------------------------------
# 6. missing GID + completed without file → recovery_pending
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_gid_completed_no_file_recovery_pending(
    temp_db: str,
) -> None:
    user = await create_user_v0(username="t10_recovery", quota_bytes=10_000)
    download = await create_global_download_v0(
        resource_key="http:t10-recovery",
        source_uri="https://example.com/file.zip",
        resource_kind="http",
        status="completed",
        aria2_gid="gid_completed_006",
        total_bytes=2048,
        size_known=True,
        completed_bytes=2048,
        completed_file_id=None,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="completed",
    )

    err = _missing_gid_error("gid_completed_006")
    client = make_aria2_client()
    with (
        patch("app.services.failed_task_cleanup.cleanup_task_download_dir") as mock_dir,
        patch(
            "app.services.failed_task_cleanup.get_downloading_dir"
        ) as mock_get_dir,
    ):
        mock_dir.return_value = None
        mock_get_dir.return_value.__truediv__ = lambda self, x: f"/dl/{x}"

        result = await reconcile_attempt_signal(
            backend=client,
            observed_gid="gid_completed_006",
            event=None,
            observed_status=None,
            observed_error=err,
            log_prefix="[T10]",
        )

    assert result == ReconcileResult.RECOVERY_PENDING

    # No fail, no purge.
    client.force_remove.assert_not_called()
    mock_dir.assert_not_called()

    stored = await _fetch_global(download["id"])
    assert stored["status"] == "completed"


# ---------------------------------------------------------------------------
# 7. transient RPC error → waiting, task stays live
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transient_rpc_error_returns_waiting(temp_db: str) -> None:
    user = await create_user_v0(username="t10_transient", quota_bytes=10_000)
    download = await create_global_download_v0(
        resource_key="http:t10-transient",
        source_uri="https://example.com/file.zip",
        resource_kind="http",
        status="active",
        aria2_gid="gid_transient_007",
        total_bytes=512,
        size_known=True,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
    )

    err = _transient_error()
    client = make_aria2_client()
    result = await reconcile_attempt_signal(
        backend=client,
        observed_gid="gid_transient_007",
        event="start",
        observed_status={"status": "active"},
        observed_error=err,
        log_prefix="[T10]",
    )
    assert result == ReconcileResult.WAITING

    stored = await _fetch_global(download["id"])
    assert stored["status"] == "active"
    assert stored["aria2_gid"] == "gid_transient_007"

    tasks = await _fetch_user_tasks(download["id"])
    assert len(tasks) == 1
    assert tasks[0]["status"] == "active"
