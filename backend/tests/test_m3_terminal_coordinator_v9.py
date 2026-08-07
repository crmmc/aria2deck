"""T09: terminal claim/reclaim coordinator integration tests.

Verifies that ``fail_download_and_reclaim`` now follows the two-step model
(spec §10.1, §10.2, §10.6):

    Claim terminal state
    → Reclaim attempt resources (cleanup_with_claim)

Test matrix:
1. Claim succeeds → cleanup_with_claim called, task is failed, GID cleared.
2. Concurrent fail → only one claim wins, loser does no destructive cleanup.
3. expected_gid mismatch → claim returns None, no destructive cleanup.
4. Handoff style: writer_gids = [source, payload] passed to claim/cleanup.
5. Claim succeeds but cleanup partially fails → DB stays terminal (no rollback).
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.db.engine import transaction
from app.db.schema import global_downloads, user_tasks
from app.services.aria2_lifecycle_service import fail_download_and_reclaim
from tests.fakes import make_aria2_client
from tests.helpers_v0 import (
    create_global_download_v0,
    create_user_task_v0,
    create_user_v0,
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


async def _set_usage_reserved(user_id: int, reserved: int) -> None:
    from app.db.schema import user_storage_usage

    async with transaction() as conn:
        await conn.execute(
            user_storage_usage.update()
            .where(user_storage_usage.c.user_id == user_id)
            .values(reserved_bytes=reserved)
        )


# ---------------------------------------------------------------------------
# 1. Claim succeeds → cleanup_with_claim runs, task is failed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_success_triggers_cleanup_and_terminalizes(temp_db: str) -> None:
    user = await create_user_v0(username="t09_ok", quota_bytes=10_000)
    download = await create_global_download_v0(
        resource_key="http:t09-ok",
        status="active",
        aria2_gid="gid-t09-ok",
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
    with (
        patch("app.services.failed_task_cleanup.cleanup_task_download_dir") as mock_dir,
        patch("app.services.failed_task_cleanup.get_downloading_dir") as mock_get,
    ):
        mock_dir.return_value = None
        mock_get.return_value.__truediv__ = lambda self, x: f"/dl/{x}"

        changed = await fail_download_and_reclaim(
            client=client,
            download_id=download["id"],
            message="test failure",
            error_code="test_err",
            expected_gid="gid-t09-ok",
            writer_gid="gid-t09-ok",
            log_prefix="[T09]",
        )

    assert changed is True

    stored = await _fetch_global(download["id"])
    assert stored["status"] == "failed"
    assert stored["error_code"] == "test_err"
    assert stored["error_message"] == "test failure"
    assert stored["disk_reserved_bytes"] == 0
    # GID should be cleared by cleanup_with_claim step 4
    assert stored["aria2_gid"] is None

    # force_remove was called for the writer
    client.force_remove.assert_awaited_once_with("gid-t09-ok")
    # remove_download_result was called
    client.remove_download_result.assert_awaited_once_with("gid-t09-ok")
    # directory was cleaned
    mock_dir.assert_awaited_once_with(download["id"])

    # user task synced to failed
    tasks = await _fetch_user_tasks(download["id"])
    assert len(tasks) == 1
    assert tasks[0]["status"] == "failed"


# ---------------------------------------------------------------------------
# 2. Concurrent fail: only one claim wins, loser does no destructive cleanup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_fail_only_one_claim_wins(temp_db: str) -> None:
    user = await create_user_v0(username="t09_conc", quota_bytes=10_000)
    download = await create_global_download_v0(
        resource_key="http:t09-conc",
        status="active",
        aria2_gid="gid-t09-conc",
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
    cleanup_spy = AsyncMock(wraps=__import__(
        "app.services.failed_task_cleanup", fromlist=["cleanup_with_claim"]
    ).cleanup_with_claim)

    with (
        patch("app.services.failed_task_cleanup.cleanup_task_download_dir") as mock_dir,
        patch("app.services.failed_task_cleanup.get_downloading_dir") as mock_get,
        patch(
            "app.services.aria2_lifecycle_service.cleanup_with_claim", cleanup_spy
        ),
    ):
        mock_dir.return_value = None
        mock_get.return_value.__truediv__ = lambda self, x: f"/dl/{x}"

        result_a, result_b = await asyncio.gather(
            fail_download_and_reclaim(
                client=client,
                download_id=download["id"],
                message="fail_a",
                error_code="err_a",
                expected_gid="gid-t09-conc",
                writer_gid="gid-t09-conc",
                log_prefix="[A]",
            ),
            fail_download_and_reclaim(
                client=client,
                download_id=download["id"],
                message="fail_b",
                error_code="err_b",
                expected_gid="gid-t09-conc",
                writer_gid="gid-t09-conc",
                log_prefix="[B]",
            ),
        )

    results = [r for r in (result_a, result_b) if r]
    assert len(results) == 1, "exactly one fail should succeed"

    # cleanup_with_claim must have been called exactly once (by the winner)
    assert cleanup_spy.await_count == 1

    # force_remove should only be called once (single cleanup)
    assert client.force_remove.await_count == 1

    stored = await _fetch_global(download["id"])
    assert stored["status"] == "failed"
    assert stored["error_code"] in ("err_a", "err_b")


# ---------------------------------------------------------------------------
# 3. expected_gid mismatch → no destructive cleanup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expected_gid_mismatch_no_destructive_cleanup(temp_db: str) -> None:
    download = await create_global_download_v0(
        resource_key="http:t09-mismatch",
        status="active",
        aria2_gid="gid-actual",
        total_bytes=200,
        completed_bytes=0,
    )

    client = make_aria2_client()
    changed = await fail_download_and_reclaim(
        client=client,
        download_id=download["id"],
        message="stale gid",
        error_code="stale",
        expected_gid="gid-wrong",
        writer_gid="gid-wrong",
        log_prefix="[T09]",
    )

    assert changed is False

    # No destructive operations
    client.force_remove.assert_not_called()
    client.remove_download_result.assert_not_called()

    # DB unchanged
    stored = await _fetch_global(download["id"])
    assert stored["status"] == "active"
    assert stored["aria2_gid"] == "gid-actual"


# ---------------------------------------------------------------------------
# 3b. Already terminal → claim returns None, no destructive cleanup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_already_terminal_no_cleanup(temp_db: str) -> None:
    download = await create_global_download_v0(
        resource_key="http:t09-terminal",
        status="failed",
        aria2_gid="gid-terminal",
        total_bytes=200,
        completed_bytes=0,
    )

    client = make_aria2_client()
    changed = await fail_download_and_reclaim(
        client=client,
        download_id=download["id"],
        message="double fail",
        error_code="err",
        expected_gid="gid-terminal",
        writer_gid="gid-terminal",
        log_prefix="[T09]",
    )

    assert changed is False
    client.force_remove.assert_not_called()
    client.remove_download_result.assert_not_called()


# ---------------------------------------------------------------------------
# 4. Handoff: writer_gids = [source, payload]
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handoff_writer_gids_include_source_and_payload(temp_db: str) -> None:
    download = await create_global_download_v0(
        resource_key="magnet:t09-handoff",
        status="active",
        aria2_gid="source_gid",
        total_bytes=1000,
        completed_bytes=0,
        disk_reserved_bytes=1000,
    )

    client = make_aria2_client()
    captured_claim: list[Any] = []

    async def _capture_cleanup(c: Any, claim: Any, *, log_prefix: str) -> Any:
        captured_claim.append(claim)
        # Simulate successful cleanup
        from app.services.failed_task_cleanup import CleanupResult
        return CleanupResult(True, True, True)

    with patch(
        "app.services.aria2_lifecycle_service.cleanup_with_claim",
        side_effect=_capture_cleanup,
    ):
        changed = await fail_download_and_reclaim(
            client=client,
            download_id=download["id"],
            message="handoff fail",
            error_code="handoff_err",
            expected_gid="source_gid",
            writer_gid="payload_gid",
            log_prefix="[T09]",
        )

    assert changed is True
    assert len(captured_claim) == 1
    claim = captured_claim[0]
    assert claim.writer_gids == ("source_gid", "payload_gid")
    assert claim.result_gids == ("source_gid", "payload_gid")
    assert claim.expected_current_gid == "source_gid"

    stored = await _fetch_global(download["id"])
    assert stored["status"] == "failed"


# ---------------------------------------------------------------------------
# 4b. Handoff with narrower result_gids (only source)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handoff_with_only_writer_gid(temp_db: str) -> None:
    """When writer_gid == expected_gid, single GID in claim."""
    download = await create_global_download_v0(
        resource_key="http:t09-single",
        status="active",
        aria2_gid="gid-single",
        total_bytes=300,
        completed_bytes=0,
    )

    client = make_aria2_client()
    captured_claim: list[Any] = []

    async def _capture_cleanup(c: Any, claim: Any, *, log_prefix: str) -> Any:
        captured_claim.append(claim)
        from app.services.failed_task_cleanup import CleanupResult
        return CleanupResult(True, True, True)

    with patch(
        "app.services.aria2_lifecycle_service.cleanup_with_claim",
        side_effect=_capture_cleanup,
    ):
        changed = await fail_download_and_reclaim(
            client=client,
            download_id=download["id"],
            message="single fail",
            error_code="err",
            expected_gid="gid-single",
            writer_gid="gid-single",
            log_prefix="[T09]",
        )

    assert changed is True
    claim = captured_claim[0]
    assert claim.writer_gids == ("gid-single",)
    assert claim.result_gids == ("gid-single",)


# ---------------------------------------------------------------------------
# 5. Claim succeeds but cleanup partially fails → DB stays terminal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cleanup_failure_does_not_rollback_terminal(temp_db: str) -> None:
    download = await create_global_download_v0(
        resource_key="http:t09-partial",
        status="active",
        aria2_gid="gid-partial",
        total_bytes=300,
        completed_bytes=0,
        disk_reserved_bytes=300,
    )

    client = make_aria2_client()
    # force_remove fails with a non-not-found error
    client.force_remove.side_effect = ConnectionError("network down")

    with patch("app.services.failed_task_cleanup.cleanup_task_download_dir") as mock_dir:
        changed = await fail_download_and_reclaim(
            client=client,
            download_id=download["id"],
            message="cleanup will fail",
            error_code="partial_err",
            expected_gid="gid-partial",
            writer_gid="gid-partial",
            log_prefix="[T09]",
        )

    # Claim succeeded, so the function returns True
    assert changed is True

    stored = await _fetch_global(download["id"])
    # DB stays terminal despite cleanup failure
    assert stored["status"] == "failed"
    assert stored["error_code"] == "partial_err"
    assert stored["disk_reserved_bytes"] == 0
    # GID is NOT cleared because cleanup_with_claim step 1 failed
    assert stored["aria2_gid"] == "gid-partial"

    # Directory not deleted because writer wasn't stopped
    mock_dir.assert_not_called()


# ---------------------------------------------------------------------------
# 6. No writer_gid (queued timeout / legacy HTTP) — directory-only cleanup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_writer_gid_still_claims_and_cleans_dir(temp_db: str) -> None:
    download = await create_global_download_v0(
        resource_key="http:t09-nowriter",
        status="queued",
        aria2_gid=None,
        total_bytes=0,
        completed_bytes=0,
    )

    client = make_aria2_client()
    captured_claim: list[Any] = []

    async def _capture_cleanup(c: Any, claim: Any, *, log_prefix: str) -> Any:
        captured_claim.append(claim)
        from app.services.failed_task_cleanup import CleanupResult
        return CleanupResult(True, True, True)

    with (
        patch(
            "app.services.aria2_lifecycle_service.cleanup_with_claim",
            side_effect=_capture_cleanup,
        ),
        patch("app.services.failed_task_cleanup.cleanup_task_download_dir") as mock_dir,
    ):
        mock_dir.return_value = None
        changed = await fail_download_and_reclaim(
            client=client,
            download_id=download["id"],
            message="submit timeout",
            error_code="submit_timeout",
            expected_gid=None,
            writer_gid=None,
            expected_statuses=("queued",),
            log_prefix="[T09]",
        )

    assert changed is True
    claim = captured_claim[0]
    assert claim.writer_gids == ()
    assert claim.result_gids == ()
    assert claim.expected_current_gid is None

    # No force_remove since no writers
    client.force_remove.assert_not_called()

    stored = await _fetch_global(download["id"])
    assert stored["status"] == "failed"
    assert stored["aria2_gid"] is None
