"""T24: size coordination idempotency from a concurrency/race perspective.

Verifies (spec §22.3, §8.2–§8.4, §20):
1. Size not growing → no pause/unpause, even under concurrent signals.
2. Size growing → pause succeeds → budget admitted → unpause returns active.
3. pause/unpause RPC throws but re-query shows target state → idempotent success.
4. Re-query still paused + fencing valid → real growth_unpause_failed.
5. GID already changed or task already terminal → no growth failure written.
6. magnet metadata phase totalLength=0 → no running-state unknown-size cleanup.

Each scenario emphasizes the race window: the coordinator must re-check the
database GID/status *after* every external RPC before deciding to write a
growth failure or take destructive action.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import select

from app.db.engine import transaction
from app.db.schema import global_downloads, user_tasks
from app.domain.lifecycle import ReconcileResult
from app.services.aria2_lifecycle_service import (
    coordinate_reported_size,
    reconcile_attempt_signal,
)
from app.services import aria2_lifecycle_service
from tests.fakes import make_aria2_client
from tests.helpers_v0 import (
    create_global_download_v0,
    create_user_task_v0,
    create_user_v0,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _set_usage_reserved(user_id: int, reserved: int) -> None:
    from app.db.schema import user_storage_usage

    async with transaction() as conn:
        await conn.execute(
            user_storage_usage.update()
            .where(user_storage_usage.c.user_id == user_id)
            .values(reserved_bytes=reserved)
        )


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


def _gid_error(gid: str = "gid_test") -> Exception:
    return Exception(f"gid {gid} not found (artificial)")


def _patch_cleanup():
    """Patch directory cleanup so terminalization tests don't touch the FS."""
    return (
        patch("app.services.failed_task_cleanup.cleanup_task_download_dir"),
        patch("app.services.failed_task_cleanup.get_downloading_dir"),
    )


# ---------------------------------------------------------------------------
# Scenario 1: size not growing → no pause/unpause (race: concurrent signal)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_pause_when_size_not_growing_concurrent(temp_db: str) -> None:
    """Two concurrent coordinate_reported_size calls with unchanged size.

    Neither call should touch pause/unpause; both return admitted.
    """
    user = await create_user_v0(username="t24_nogrow", quota_bytes=10_000_000)
    download = await create_global_download_v0(
        resource_key="http:t24-nogrow",
        source_uri="https://example.com/file.zip",
        resource_kind="http",
        status="active",
        aria2_gid="gid_nogrow_race",
        total_bytes=2048,
        size_known=True,
        completed_bytes=512,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=2048,
    )
    await _set_usage_reserved(user["id"], 2048)

    status = {
        "status": "active",
        "totalLength": "2048",
        "completedLength": "512",
    }

    client1 = make_aria2_client()
    client2 = make_aria2_client()

    r1, r2 = await asyncio.gather(
        coordinate_reported_size(
            client=client1,
            download=download,
            expected_gid="gid_nogrow_race",
            control_gid="gid_nogrow_race",
            status=status,
            acquire_lifecycle_lock=True,
        ),
        coordinate_reported_size(
            client=client2,
            download=download,
            expected_gid="gid_nogrow_race",
            control_gid="gid_nogrow_race",
            status=status,
            acquire_lifecycle_lock=True,
        ),
    )

    assert r1["outcome"] == "admitted"
    assert r2["outcome"] == "admitted"
    client1.pause.assert_not_called()
    client1.unpause.assert_not_called()
    client2.pause.assert_not_called()
    client2.unpause.assert_not_called()


# ---------------------------------------------------------------------------
# Scenario 2: size growing → pause succeeds → budget admitted → unpause active
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_grow_pause_admit_unpause(temp_db: str) -> None:
    user = await create_user_v0(username="t24_grow", quota_bytes=10_000_000)
    download = await create_global_download_v0(
        resource_key="http:t24-grow",
        source_uri="https://example.com/file.zip",
        resource_kind="http",
        status="active",
        aria2_gid="gid_grow_race",
        total_bytes=1024,
        size_known=True,
        completed_bytes=512,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=1024,
    )
    await _set_usage_reserved(user["id"], 1024)

    client = make_aria2_client()
    result = await coordinate_reported_size(
        client=client,
        download=download,
        expected_gid="gid_grow_race",
        control_gid="gid_grow_race",
        status={
            "status": "active",
            "totalLength": "4096",
            "completedLength": "512",
        },
        acquire_lifecycle_lock=False,
    )
    assert result["outcome"] == "admitted"
    assert result["paused_by_us"] is True
    client.pause.assert_called_once_with("gid_grow_race")
    client.unpause.assert_called_once_with("gid_grow_race")


# ---------------------------------------------------------------------------
# Scenario 3: pause/unpause RPC throws but re-query shows target state →
#             idempotent success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pause_exception_requery_paused_idempotent(temp_db: str) -> None:
    """pause RPC throws, but re-query shows already paused → treat as success.

    This is the race where aria2 had already paused the download (e.g. due to
    an external pause arriving concurrently) at the moment our RPC fired.
    """
    user = await create_user_v0(username="t24_pexcp", quota_bytes=10_000_000)
    download = await create_global_download_v0(
        resource_key="http:t24-pexcp",
        source_uri="https://example.com/file.zip",
        resource_kind="http",
        status="active",
        aria2_gid="gid_pexcp_race",
        total_bytes=1024,
        size_known=True,
        completed_bytes=512,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=1024,
    )
    await _set_usage_reserved(user["id"], 1024)

    client = make_aria2_client(
        pause=Exception("artificial pause race"),
        tell_status={
            "status": "paused",
            "totalLength": "4096",
            "completedLength": "512",
        },
    )
    result = await coordinate_reported_size(
        client=client,
        download=download,
        expected_gid="gid_pexcp_race",
        control_gid="gid_pexcp_race",
        status={
            "status": "active",
            "totalLength": "4096",
            "completedLength": "512",
        },
        acquire_lifecycle_lock=False,
    )
    assert result["outcome"] == "admitted"
    client.pause.assert_called_once_with("gid_pexcp_race")
    client.tell_status.assert_called_once_with("gid_pexcp_race")
    client.unpause.assert_called_once_with("gid_pexcp_race")

    stored = await _fetch_global(download["id"])
    assert stored["status"] == "active"


@pytest.mark.asyncio
async def test_unpause_exception_requery_active_idempotent(temp_db: str) -> None:
    """unpause RPC throws, but re-query shows already active → idempotent success.

    The download was resumed by a concurrent signal or external client between
    our pause and our unpause call. No growth_unpause_failed should be written.
    """
    user = await create_user_v0(username="t24_uexcp", quota_bytes=10_000_000)
    download = await create_global_download_v0(
        resource_key="http:t24-uexcp",
        source_uri="https://example.com/file.zip",
        resource_kind="http",
        status="active",
        aria2_gid="gid_uexcp_race",
        total_bytes=1024,
        size_known=True,
        completed_bytes=512,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=1024,
    )
    await _set_usage_reserved(user["id"], 1024)

    client = make_aria2_client(
        unpause=Exception("artificial unpause race"),
        tell_status={
            "status": "active",
            "totalLength": "4096",
            "completedLength": "512",
        },
    )
    result = await coordinate_reported_size(
        client=client,
        download=download,
        expected_gid="gid_uexcp_race",
        control_gid="gid_uexcp_race",
        status={
            "status": "active",
            "totalLength": "4096",
            "completedLength": "512",
        },
        acquire_lifecycle_lock=False,
    )
    assert result["outcome"] == "admitted"

    stored = await _fetch_global(download["id"])
    assert stored["status"] == "active"
    assert stored["error_code"] != "growth_unpause_failed"


# ---------------------------------------------------------------------------
# Scenario 4: re-query still paused + fencing valid → growth_unpause_failed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unpause_real_failure_writes_growth_failure(temp_db: str) -> None:
    """unpause throws, re-query still paused, DB GID unchanged → real failure.

    This is the only condition under which growth_unpause_failed is justified:
    the control GID is still current, the attempt is still live, and the
    download is genuinely stuck in paused state.
    """
    user = await create_user_v0(username="t24_ufail", quota_bytes=10_000_000)
    download = await create_global_download_v0(
        resource_key="http:t24-ufail",
        source_uri="https://example.com/file.zip",
        resource_kind="http",
        status="active",
        aria2_gid="gid_ufail_race",
        total_bytes=1024,
        size_known=True,
        completed_bytes=512,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=1024,
    )
    await _set_usage_reserved(user["id"], 1024)

    client = make_aria2_client(
        unpause=Exception("real unpause failure"),
        tell_status={
            "status": "paused",
            "totalLength": "4096",
            "completedLength": "512",
        },
    )
    mock_dir_patch, mock_get_dir_patch = _patch_cleanup()
    with mock_dir_patch as mock_dir, mock_get_dir_patch as mock_get_dir:
        mock_dir.return_value = None
        mock_get_dir.return_value.__truediv__ = lambda self, x: f"/dl/{x}"

        result = await coordinate_reported_size(
            client=client,
            download=download,
            expected_gid="gid_ufail_race",
            control_gid="gid_ufail_race",
            status={
                "status": "active",
                "totalLength": "4096",
                "completedLength": "512",
            },
            acquire_lifecycle_lock=False,
        )

    assert result["outcome"] == "terminalized"
    stored = await _fetch_global(download["id"])
    assert stored["status"] == "failed"
    assert stored["error_code"] == "growth_unpause_failed"
    tasks = await _fetch_user_tasks(download["id"])
    assert len(tasks) == 1
    assert tasks[0]["status"] == "failed"


# ---------------------------------------------------------------------------
# Scenario 5: GID changed or task terminal → no growth failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gid_changed_no_growth_failure(temp_db: str) -> None:
    """DB GID has already changed (handoff committed by concurrent signal).

    coordinate_reported_size must return stale without touching pause/unpause
    or writing any growth failure.
    """
    user = await create_user_v0(username="t24_gidchg", quota_bytes=10_000_000)
    download = await create_global_download_v0(
        resource_key="http:t24-gidchg",
        source_uri="https://example.com/file.zip",
        resource_kind="http",
        status="active",
        aria2_gid="gid_new_payload",
        total_bytes=1024,
        size_known=True,
        completed_bytes=512,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=1024,
    )
    await _set_usage_reserved(user["id"], 1024)

    client = make_aria2_client()
    result = await coordinate_reported_size(
        client=client,
        download=download,
        expected_gid="gid_old_metadata",
        control_gid="gid_old_metadata",
        status={
            "status": "active",
            "totalLength": "4096",
            "completedLength": "512",
        },
        acquire_lifecycle_lock=False,
    )
    assert result["outcome"] == "stale"
    client.pause.assert_not_called()
    client.unpause.assert_not_called()

    stored = await _fetch_global(download["id"])
    assert stored["status"] == "active"
    assert stored["error_code"] not in ("growth_pause_failed", "growth_unpause_failed")


@pytest.mark.asyncio
async def test_task_already_terminal_no_growth_failure(temp_db: str) -> None:
    """Task already terminalized by a concurrent failure signal.

    coordinate_reported_size sees a stale row (expected GID matches but the
    row is no longer live after the fencing check inside reconcile_download_size).
    No growth failure should be written.
    """
    user = await create_user_v0(username="t24_terminal", quota_bytes=10_000_000)
    download = await create_global_download_v0(
        resource_key="http:t24-terminal",
        source_uri="https://example.com/file.zip",
        resource_kind="http",
        status="failed",
        aria2_gid="gid_terminal_race",
        total_bytes=1024,
        size_known=True,
        completed_bytes=512,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="failed",
        reserved_bytes=0,
    )

    client = make_aria2_client()
    result = await coordinate_reported_size(
        client=client,
        download=download,
        expected_gid="gid_terminal_race",
        control_gid="gid_terminal_race",
        status={
            "status": "active",
            "totalLength": "4096",
            "completedLength": "512",
        },
        acquire_lifecycle_lock=False,
    )
    # The row is already terminal; coordinate_reported_size must not write a
    # growth failure or call any Aria2 control.
    assert result["outcome"] == "stale"
    client.pause.assert_not_called()
    client.unpause.assert_not_called()

    stored = await _fetch_global(download["id"])
    assert stored["status"] == "failed"
    # The original failure error_code must be preserved, not overwritten.
    assert stored["error_code"] not in ("growth_pause_failed", "growth_unpause_failed")


@pytest.mark.asyncio
async def test_requery_gid_missing_db_changed_no_growth_failure(temp_db: str) -> None:
    """pause throws, re-query GID missing, but DB GID already changed.

    The concurrent handoff already switched the DB to a new GID. The missing
    GID from the old control_gid is stale, not a growth failure.
    """
    user = await create_user_v0(username="t24_rqmissing", quota_bytes=10_000_000)
    download = await create_global_download_v0(
        resource_key="http:t24-rqmissing",
        source_uri="https://example.com/file.zip",
        resource_kind="http",
        status="active",
        aria2_gid="gid_new_after_race",
        total_bytes=1024,
        size_known=True,
        completed_bytes=512,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=1024,
    )
    await _set_usage_reserved(user["id"], 1024)

    client = make_aria2_client(
        pause=Exception("artificial pause error"),
        tell_status=_gid_error("gid_old_before_race"),
    )
    result = await coordinate_reported_size(
        client=client,
        download=download,
        expected_gid="gid_old_before_race",
        control_gid="gid_old_before_race",
        status={
            "status": "active",
            "totalLength": "4096",
            "completedLength": "512",
        },
        acquire_lifecycle_lock=False,
    )
    # The initial DB fencing check detects the GID mismatch before pause
    # is even attempted.  No growth failure or Aria2 control side effects.
    assert result["outcome"] == "stale"
    client.pause.assert_not_called()

    stored = await _fetch_global(download["id"])
    assert stored["error_code"] != "growth_pause_failed"
    assert stored["error_code"] != "growth_unpause_failed"


# ---------------------------------------------------------------------------
# Scenario 6: magnet metadata totalLength=0 → no unknown-size cleanup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_magnet_metadata_total_zero_no_cleanup(temp_db: str) -> None:
    """During magnet metadata phase, totalLength=0 must not trigger unknown-size.

    The reconcile_attempt_signal active branch must detect the metadata phase
    (via is_metadata_phase_status) and skip size admission entirely.
    """
    user = await create_user_v0(username="t24_meta", quota_bytes=10_000_000)
    download = await create_global_download_v0(
        resource_key="magnet:t24-meta",
        source_uri="magnet:?xt=urn:btih:t24_meta_race",
        resource_kind="magnet",
        status="active",
        aria2_gid="gid_meta_race",
        total_bytes=0,
        size_known=False,
        completed_bytes=0,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
    )

    metadata_status: dict[str, Any] = {
        "status": "active",
        "totalLength": "0",
        "completedLength": "0",
        "files": [
            {
                "path": "[METADATA]",
                "length": "0",
                "selected": "true",
            }
        ],
    }
    client = make_aria2_client(tell_status=metadata_status)
    result = await reconcile_attempt_signal(
        client=client,
        observed_gid="gid_meta_race",
        event="start",
        observed_status=metadata_status,
        log_prefix="[T24]",
    )
    assert result != ReconcileResult.TERMINALIZED

    stored = await _fetch_global(download["id"])
    assert stored["status"] != "failed"
    assert stored["error_code"] != "unknown_size"
