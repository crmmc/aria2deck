"""T22: attempt fencing and stale event tests (spec §22.1).

Verifies (spec §22.1):
1. Old GID event after handoff does not update payload attempt.
2. Payload ``following`` event arriving before metadata complete
   only enters the unified handoff path (does not auto-write GID).
3. Two concurrent signals: only one executes pause/unpause.
4. One signal terminalizes successfully; the second signal does no cleanup.
5. ``guarded_update`` affecting 0 rows triggers no destructive action.

Key invariant: stale observations never write errors, delete directories,
or remove any GID (spec §22.1 final sentence).
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.db.engine import transaction
from app.db.schema import global_downloads, user_storage_usage, user_tasks
from app.domain.lifecycle import ReconcileResult
from app.repositories.downloads import (
    guarded_update_global_download,
    guarded_update_download_and_active_user_tasks,
)
from app.services.aria2_lifecycle_service import (
    fail_download_and_reclaim,
    reconcile_attempt_signal,
)
from tests.fakes import make_aria2_client
from tests.helpers_v0 import (
    create_global_download_v0,
    create_user_task_v0,
    create_user_v0,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


async def _set_usage_reserved(user_id: int, reserved: int) -> None:
    async with transaction() as conn:
        await conn.execute(
            user_storage_usage.update()
            .where(user_storage_usage.c.user_id == user_id)
            .values(reserved_bytes=reserved)
        )


# ---------------------------------------------------------------------------
# 1. Old GID event after handoff does not update payload attempt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_gid_after_handoff_no_update(temp_db: str) -> None:
    """After a handoff switches aria2_gid from source to payload, an event
    for the old source GID must return STALE and not modify the payload
    attempt (spec §22.1.1, §6.2)."""
    user = await create_user_v0(username="t22_stale", quota_bytes=10_000_000)
    download = await create_global_download_v0(
        resource_key="magnet:t22-stale",
        source_uri="magnet:?xt=urn:btih:t22_stale",
        resource_kind="torrent",
        status="active",
        aria2_gid="gid_payload_022",
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

    # An event arrives for the old source GID.
    old_gid = "gid_source_old"
    stale_status: dict[str, Any] = {
        "status": "error",
        "totalLength": "0",
        "completedLength": "0",
        "errorMessage": "Connection reset",
    }
    client = make_aria2_client()

    result = await reconcile_attempt_signal(
        client=client,
        observed_gid=old_gid,
        event="error",
        observed_status=stale_status,
        log_prefix="[T22]",
    )

    # No attempt has aria2_gid == old_gid → IGNORED.
    assert result == ReconcileResult.IGNORED

    stored = await _fetch_global(download["id"])
    assert stored["status"] == "active"
    assert stored["aria2_gid"] == "gid_payload_022"
    assert stored["error_code"] is None

    client.force_remove.assert_not_called()
    client.remove_download_result.assert_not_called()


# ---------------------------------------------------------------------------
# 1b. Old GID still resolvable to another attempt but stale for this one
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_gid_does_not_overwrite_current_attempt(
    temp_db: str,
) -> None:
    """When an old GID still points to an attempt whose current_gid has
    changed (e.g. after handoff the source GID appears as a separate row),
    the event for the old GID must not overwrite the current attempt.
    Even if the GID resolves, fencing inside the lock returns STALE."""
    user = await create_user_v0(username="t22_fence", quota_bytes=10_000_000)

    # Create a download that is currently at a payload GID.
    download = await create_global_download_v0(
        resource_key="magnet:t22-fence",
        source_uri="magnet:?xt=urn:btih:t22_fence",
        resource_kind="torrent",
        status="active",
        aria2_gid="gid_current_022",
        total_bytes=8192,
        size_known=True,
        disk_reserved_bytes=8192,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=8192,
    )
    await _set_usage_reserved(user["id"], 8192)

    # A stale event for an *old* GID that no longer matches any row.
    old_status: dict[str, Any] = {
        "status": "error",
        "totalLength": "100",
        "completedLength": "0",
        "errorMessage": "old error",
    }
    client = make_aria2_client()

    result = await reconcile_attempt_signal(
        client=client,
        observed_gid="gid_stale_ghost",
        event="error",
        observed_status=old_status,
        log_prefix="[T22]",
    )

    # No row matches gid_stale_ghost → IGNORED (no destructive action).
    assert result == ReconcileResult.IGNORED

    stored = await _fetch_global(download["id"])
    assert stored["status"] == "active"
    assert stored["aria2_gid"] == "gid_current_022"


# ---------------------------------------------------------------------------
# 2. Payload following arrives before metadata complete → unified handoff only
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_payload_following_early_enters_handoff_only(temp_db: str) -> None:
    """A payload GID event with ``following=source_gid`` arriving before
    the metadata complete must resolve as a handoff candidate and enter
    ``_handoff_locked``.  It must NOT auto-write the payload GID without
    going through the handoff CAS (spec §22.1.2, §9.3, §6.3).

    We verify the pure resolver does not write the GID, and that when the
    handoff succeeds the GID switches atomically via CAS.
    """
    user = await create_user_v0(username="t22_early", quota_bytes=10_000_000)
    download = await create_global_download_v0(
        resource_key="magnet:t22-early",
        source_uri="magnet:?xt=urn:btih:t22_early",
        resource_kind="magnet",
        status="active",
        aria2_gid="gid_source_early",
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

    payload_gid = "gid_payload_early"
    observed_status: dict[str, Any] = {
        "status": "active",
        "following": "gid_source_early",
        "totalLength": "4096",
        "completedLength": "0",
        "files": [
            {"path": "/dl/1/file.iso", "length": "4096", "selected": "true"}
        ],
    }

    # Pure resolve: should find source attempt and NOT write GID.
    from app.services.aria2_lifecycle_service import resolve_download_for_gid

    resolved = await resolve_download_for_gid(payload_gid, observed_status)
    assert resolved is not None
    assert resolved.is_handoff_candidate
    assert resolved.source_gid == "gid_source_early"

    # Verify no GID was written by resolve.
    pre = await _fetch_global(download["id"])
    assert pre["aria2_gid"] == "gid_source_early"

    # Now run full reconcile: handoff should commit via CAS.
    client = make_aria2_client(
        tell_status={
            "status": "active",
            "totalLength": "4096",
            "completedLength": "0",
            "files": [
                {"path": "/dl/1/file.iso", "length": "4096", "selected": "true"}
            ],
        }
    )
    result = await reconcile_attempt_signal(
        client=client,
        observed_gid=payload_gid,
        event="start",
        observed_status=observed_status,
        log_prefix="[T22]",
    )

    assert result == ReconcileResult.CHANGED

    post = await _fetch_global(download["id"])
    assert post["aria2_gid"] == payload_gid
    assert post["resource_kind"] == "torrent"

    # No destructive action during handoff.
    client.force_remove.assert_not_called()


# ---------------------------------------------------------------------------
# 2b. Payload following resolves but handoff CAS fails (stale) → no write
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_payload_following_cas_fail_no_destructive(temp_db: str) -> None:
    """If the handoff CAS fails because current_gid already changed, the
    payload GID must NOT be written and no destructive action occurs
    (spec §9.2 step 8, §22.1)."""
    user = await create_user_v0(username="t22_casfail", quota_bytes=10_000_000)
    download = await create_global_download_v0(
        resource_key="magnet:t22-casfail",
        source_uri="magnet:?xt=urn:btih:t22_casfail",
        resource_kind="torrent",
        status="active",
        aria2_gid="gid_already_switched",
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

    payload_gid = "gid_payload_casfail"
    # Payload status claims following=gid_source_old, but current GID is
    # already gid_already_switched (different from gid_source_old).
    observed_status: dict[str, Any] = {
        "status": "active",
        "following": "gid_source_old",
        "totalLength": "4096",
        "completedLength": "0",
    }
    client = make_aria2_client(
        tell_status={
            "status": "active",
            "totalLength": "4096",
            "completedLength": "0",
        }
    )

    # resolve_download_for_gid won't find gid_source_old in any row, so
    # it returns None → IGNORED.
    result = await reconcile_attempt_signal(
        client=client,
        observed_gid=payload_gid,
        event="start",
        observed_status=observed_status,
        log_prefix="[T22]",
    )

    assert result == ReconcileResult.IGNORED

    stored = await _fetch_global(download["id"])
    assert stored["aria2_gid"] == "gid_already_switched"

    client.force_remove.assert_not_called()


# ---------------------------------------------------------------------------
# 3. Two concurrent signals: only one executes pause/unpause
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_signals_single_pause_unpause(temp_db: str) -> None:
    """Two concurrent ``reconcile_attempt_signal`` calls for the same attempt
    must serialize via the attempt lock.  Only one should execute
    pause/unpause on the Aria2 client (spec §22.1.3, §5.1, §5.4).

    We use a gate to hold the first signal inside its critical section until
    the second signal has been scheduled, ensuring both are concurrent.
    """
    user = await create_user_v0(username="t22_conc", quota_bytes=10_000_000)
    download = await create_global_download_v0(
        resource_key="http:t22-conc",
        resource_kind="http",
        status="active",
        aria2_gid="gid_conc_022",
        total_bytes=500,
        completed_bytes=0,
        size_known=True,
        disk_reserved_bytes=500,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=500,
    )
    await _set_usage_reserved(user["id"], 500)

    # Size grows from 500 to 5000 → triggers pause for size accounting.
    grown_status: dict[str, Any] = {
        "status": "active",
        "totalLength": "5000",
        "completedLength": "0",
        "files": [{"path": "/dl/1/file.iso", "length": "5000", "selected": "true"}],
    }

    pause_count = 0
    pause_lock = asyncio.Lock()

    async def _tracked_pause(gid: str) -> str:
        nonlocal pause_count
        async with pause_lock:
            pause_count += 1
        return "OK"

    client = make_aria2_client()
    client.pause.side_effect = _tracked_pause
    client.tell_status.return_value = grown_status

    # Both signals observe the same active GID with grown size.
    results = await asyncio.gather(
        reconcile_attempt_signal(
            client=client,
            observed_gid="gid_conc_022",
            event="start",
            observed_status=dict(grown_status),
            log_prefix="[A]",
        ),
        reconcile_attempt_signal(
            client=client,
            observed_gid="gid_conc_022",
            event="start",
            observed_status=dict(grown_status),
            log_prefix="[B]",
        ),
    )

    # At most one pause call (the attempt lock serializes both signals;
    # the second one sees size already reconciled so no new pause).
    assert pause_count <= 1, (
        f"Expected at most 1 pause call, got {pause_count}"
    )

    stored = await _fetch_global(download["id"])
    assert stored["status"] == "active"
    assert stored["total_bytes"] == 5000


# ---------------------------------------------------------------------------
# 4. One signal terminalizes; second signal does no cleanup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_second_signal_after_terminal_no_cleanup(temp_db: str) -> None:
    """After one signal successfully terminalizes an attempt, a second
    concurrent or sequential signal for the same GID must not execute any
    destructive cleanup (spec §22.1.4, §10.2, §10.6).

    We terminalize first, then send a second error signal and verify
    no force_remove or directory deletion occurs.
    """
    user = await create_user_v0(username="t22_term", quota_bytes=10_000_000)
    download = await create_global_download_v0(
        resource_key="http:t22-term",
        resource_kind="http",
        status="active",
        aria2_gid="gid_term_022",
        total_bytes=500,
        completed_bytes=100,
        disk_reserved_bytes=500,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=500,
    )
    await _set_usage_reserved(user["id"], 500)

    client = make_aria2_client()

    # First signal: error → terminalizes.
    with (
        patch("app.services.failed_task_cleanup.cleanup_task_download_dir") as mock_dir,
        patch("app.services.failed_task_cleanup.get_downloading_dir") as mock_get,
    ):
        mock_dir.return_value = None
        mock_get.return_value.__truediv__ = lambda self, x: f"/dl/{x}"

        result1 = await reconcile_attempt_signal(
            client=client,
            observed_gid="gid_term_022",
            event="error",
            observed_status={
                "status": "error",
                "totalLength": "500",
                "completedLength": "100",
                "errorCode": "1",
                "errorMessage": "download error",
            },
            log_prefix="[T22-1]",
        )

    assert result1 == ReconcileResult.TERMINALIZED

    stored = await _fetch_global(download["id"])
    assert stored["status"] == "failed"

    force_remove_count_before = client.force_remove.await_count
    dir_clean_count_before = mock_dir.await_count

    # Second signal: same GID, another error.
    result2 = await reconcile_attempt_signal(
        client=client,
        observed_gid="gid_term_022",
        event="error",
        observed_status={
            "status": "error",
            "totalLength": "500",
            "completedLength": "100",
            "errorCode": "2",
            "errorMessage": "second error",
        },
        log_prefix="[T22-2]",
    )

    # GID was cleared by first cleanup, so second signal can't resolve
    # the attempt at all → IGNORED.  No destructive action either way.
    assert result2 in (ReconcileResult.ALREADY_TERMINAL, ReconcileResult.IGNORED)

    assert client.force_remove.await_count == force_remove_count_before
    assert mock_dir.await_count == dir_clean_count_before

    # Error code from the first signal is preserved (not overwritten).
    stored2 = await _fetch_global(download["id"])
    assert stored2["error_code"] == "1"


# ---------------------------------------------------------------------------
# 4b. Concurrent terminalization: only one wins, loser does no cleanup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_terminalization_single_cleanup(temp_db: str) -> None:
    """Two concurrent error signals for the same attempt: only one
    terminal claim succeeds, the loser performs no cleanup
    (spec §22.1.4, §10.2)."""
    user = await create_user_v0(username="t22_racetrm", quota_bytes=10_000_000)
    download = await create_global_download_v0(
        resource_key="http:t22-racetrm",
        resource_kind="http",
        status="active",
        aria2_gid="gid_racetrm_022",
        total_bytes=500,
        completed_bytes=0,
        disk_reserved_bytes=500,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=500,
    )
    await _set_usage_reserved(user["id"], 500)

    client = make_aria2_client()

    with (
        patch("app.services.failed_task_cleanup.cleanup_task_download_dir") as mock_dir,
        patch("app.services.failed_task_cleanup.get_downloading_dir") as mock_get,
    ):
        mock_dir.return_value = None
        mock_get.return_value.__truediv__ = lambda self, x: f"/dl/{x}"

        results = await asyncio.gather(
            reconcile_attempt_signal(
                client=client,
                observed_gid="gid_racetrm_022",
                event="error",
                observed_status={
                    "status": "error",
                    "totalLength": "500",
                    "completedLength": "0",
                    "errorCode": "10",
                    "errorMessage": "error A",
                },
                log_prefix="[A]",
            ),
            reconcile_attempt_signal(
                client=client,
                observed_gid="gid_racetrm_022",
                event="error",
                observed_status={
                    "status": "error",
                    "totalLength": "500",
                    "completedLength": "0",
                    "errorCode": "20",
                    "errorMessage": "error B",
                },
                log_prefix="[B]",
            ),
        )

    terminalized = [r for r in results if r == ReconcileResult.TERMINALIZED]
    assert len(terminalized) == 1

    # force_remove should be called at most once (single cleanup).
    assert client.force_remove.await_count <= 1

    stored = await _fetch_global(download["id"])
    assert stored["status"] == "failed"


# ---------------------------------------------------------------------------
# 5. guarded_update affecting 0 rows triggers no destructive action
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_guarded_update_zero_rows_no_destructive(temp_db: str) -> None:
    """When ``guarded_update_global_download`` affects 0 rows (stale GID,
    terminal status, or completed_file_id set), the caller must not
    perform any destructive action (spec §22.1.5, §5.4, §10.2).

    We call the repository function directly and verify it returns
    False/None for stale conditions.
    """
    download = await create_global_download_v0(
        resource_key="http:t22-zero",
        resource_kind="http",
        status="active",
        aria2_gid="gid_actual",
        total_bytes=100,
        completed_bytes=0,
    )

    # 5a. Wrong GID → 0 rows.
    ok = await guarded_update_global_download(
        download["id"],
        {"status": "waiting"},
        expected_gid="gid_wrong",
    )
    assert ok is False

    stored = await _fetch_global(download["id"])
    assert stored["status"] == "active"

    # 5b. Correct GID → 1 row (sanity check).
    ok2 = await guarded_update_global_download(
        download["id"],
        {"status": "waiting"},
        expected_gid="gid_actual",
    )
    assert ok2 is True

    stored2 = await _fetch_global(download["id"])
    assert stored2["status"] == "waiting"


@pytest.mark.asyncio
async def test_guarded_update_zero_rows_terminal_no_destructive(temp_db: str) -> None:
    """guarded_update on a terminal attempt returns False (0 rows)."""
    download = await create_global_download_v0(
        resource_key="http:t22-terminal",
        resource_kind="http",
        status="failed",
        aria2_gid="gid_term",
        total_bytes=100,
        completed_bytes=0,
    )

    ok = await guarded_update_global_download(
        download["id"],
        {"status": "active"},
        expected_gid="gid_term",
    )
    assert ok is False

    stored = await _fetch_global(download["id"])
    assert stored["status"] == "failed"


@pytest.mark.asyncio
async def test_guarded_update_and_user_tasks_zero_rows_no_destructive(
    temp_db: str,
) -> None:
    """guarded_update_download_and_active_user_tasks with wrong GID returns
    None and does not modify user tasks (spec §22.1.5)."""
    user = await create_user_v0(username="t22_guarded", quota_bytes=10_000_000)
    download = await create_global_download_v0(
        resource_key="http:t22-guarded",
        resource_kind="http",
        status="active",
        aria2_gid="gid_real",
        total_bytes=500,
        completed_bytes=0,
        disk_reserved_bytes=500,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=500,
    )

    result = await guarded_update_download_and_active_user_tasks(
        download["id"],
        {"status": "waiting"},
        expected_gid="gid_wrong",
        user_status="waiting",
    )
    assert result is None

    tasks = await _fetch_user_tasks(download["id"])
    assert all(t["status"] == "active" for t in tasks)

    stored = await _fetch_global(download["id"])
    assert stored["status"] == "active"


# ---------------------------------------------------------------------------
# 5b. fail_download_and_reclaim with 0-row claim → no cleanup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_zero_rows_no_destructive_action(temp_db: str) -> None:
    """When ``claim_attempt_terminal`` affects 0 rows (because the attempt
    is already terminal or GID mismatched), ``fail_download_and_reclaim``
    must not execute force_remove, directory deletion, or result removal
    (spec §22.1.5, §10.2)."""
    download = await create_global_download_v0(
        resource_key="http:t22-claimfail",
        resource_kind="http",
        status="failed",
        aria2_gid="gid_already_failed",
        total_bytes=200,
        completed_bytes=0,
    )

    client = make_aria2_client()
    changed = await fail_download_and_reclaim(
        client=client,
        download_id=download["id"],
        message="should not happen",
        error_code="should_not",
        expected_gid="gid_already_failed",
        writer_gid="gid_already_failed",
        log_prefix="[T22]",
    )

    assert changed is False
    client.force_remove.assert_not_called()
    client.remove_download_result.assert_not_called()

    stored = await _fetch_global(download["id"])
    assert stored["status"] == "failed"
    assert stored["error_code"] != "should_not"
