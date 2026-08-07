"""T03: terminal claim and repair claim repository contract tests."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from app.db.engine import transaction
from app.db.schema import global_downloads, user_storage_usage, user_tasks
from app.domain.lifecycle import RepairClaim, TerminalizationClaim
from app.repositories.downloads import (
    claim_attempt_terminal,
    claim_terminal_reclaim,
)
from app.domain.status import ACTIVE_GLOBAL_DOWNLOAD_STATUSES
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


async def _fetch_usage(user_id: int) -> dict:
    async with transaction() as conn:
        row = (
            (
                await conn.execute(
                    select(user_storage_usage).where(
                        user_storage_usage.c.user_id == user_id
                    )
                )
            )
            .mappings()
            .one()
        )
    return dict(row)


async def _set_usage_reserved(user_id: int, reserved: int) -> None:
    async with transaction() as conn:
        await conn.execute(
            user_storage_usage.update()
            .where(user_storage_usage.c.user_id == user_id)
            .values(reserved_bytes=reserved)
        )


# ---------------------------------------------------------------------------
# 1. Normal claim failed success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_failed_success(temp_db: str) -> None:
    user = await create_user_v0(username="claim_ok", quota_bytes=10_000)
    download = await create_global_download_v0(
        resource_key="http:claim-ok",
        status="active",
        aria2_gid="gid-claim-ok",
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

    claim = await claim_attempt_terminal(
        attempt_id=download["id"],
        expected_gid="gid-claim-ok",
        terminal_status="failed",
        error_code="gid_missing",
        error_message="GID not found",
        expected_statuses=ACTIVE_GLOBAL_DOWNLOAD_STATUSES,
    )

    assert claim is not None
    assert isinstance(claim, TerminalizationClaim)
    assert claim.attempt_id == download["id"]
    assert claim.expected_current_gid == "gid-claim-ok"
    assert claim.writer_gids == ("gid-claim-ok",)
    assert claim.result_gids == ("gid-claim-ok",)
    assert claim.terminal_status == "failed"

    stored = await _fetch_global(download["id"])
    assert stored["status"] == "failed"
    assert stored["error_code"] == "gid_missing"
    assert stored["error_message"] == "GID not found"
    assert stored["disk_reserved_bytes"] == 0
    assert stored["aria2_gid"] == "gid-claim-ok"


# ---------------------------------------------------------------------------
# 2. Active user_tasks synced to terminal, reservation released
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_releases_user_task_reservation(temp_db: str) -> None:
    user = await create_user_v0(username="claim_release", quota_bytes=10_000)
    download = await create_global_download_v0(
        resource_key="http:claim-release",
        status="active",
        aria2_gid="gid-rel",
        total_bytes=300,
        completed_bytes=0,
        disk_reserved_bytes=300,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=300,
    )
    await _set_usage_reserved(user["id"], 300)

    claim = await claim_attempt_terminal(
        attempt_id=download["id"],
        expected_gid="gid-rel",
        terminal_status="failed",
        error_code="test",
        error_message="err",
        expected_statuses=ACTIVE_GLOBAL_DOWNLOAD_STATUSES,
    )
    assert claim is not None

    tasks = await _fetch_user_tasks(download["id"])
    assert len(tasks) == 1
    assert tasks[0]["status"] == "failed"
    assert tasks[0]["reserved_bytes"] == 0
    assert tasks[0]["error_message"] == "err"
    assert tasks[0]["finished_at_ms"] is not None

    usage = await _fetch_usage(user["id"])
    assert usage["reserved_bytes"] == 0


# ---------------------------------------------------------------------------
# 3. expected_gid mismatch -> None, DB unchanged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_wrong_gid_returns_none(temp_db: str) -> None:
    download = await create_global_download_v0(
        resource_key="http:claim-wrong-gid",
        status="active",
        aria2_gid="gid-real",
        total_bytes=100,
        completed_bytes=0,
        disk_reserved_bytes=100,
    )

    claim = await claim_attempt_terminal(
        attempt_id=download["id"],
        expected_gid="gid-wrong",
        terminal_status="failed",
        error_code="err",
        error_message="msg",
        expected_statuses=ACTIVE_GLOBAL_DOWNLOAD_STATUSES,
    )
    assert claim is None

    stored = await _fetch_global(download["id"])
    assert stored["status"] == "active"
    assert stored["aria2_gid"] == "gid-real"


@pytest.mark.asyncio
async def test_claim_null_gid_on_non_null_row_returns_none(temp_db: str) -> None:
    download = await create_global_download_v0(
        resource_key="http:claim-null-mismatch",
        status="active",
        aria2_gid="gid-present",
        total_bytes=100,
        completed_bytes=0,
    )

    claim = await claim_attempt_terminal(
        attempt_id=download["id"],
        expected_gid=None,
        terminal_status="failed",
        error_code="err",
        error_message="msg",
        expected_statuses=ACTIVE_GLOBAL_DOWNLOAD_STATUSES,
    )
    assert claim is None


# ---------------------------------------------------------------------------
# 4. Already terminal / completed_file_id set -> None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_already_terminal_returns_none(temp_db: str) -> None:
    download = await create_global_download_v0(
        resource_key="http:claim-already-terminal",
        status="failed",
        aria2_gid="gid-term",
        total_bytes=100,
        completed_bytes=0,
    )

    claim = await claim_attempt_terminal(
        attempt_id=download["id"],
        expected_gid="gid-term",
        terminal_status="failed",
        error_code="err",
        error_message="msg",
        expected_statuses=ACTIVE_GLOBAL_DOWNLOAD_STATUSES,
    )
    assert claim is None


@pytest.mark.asyncio
async def test_claim_completed_file_id_set_returns_none(temp_db: str) -> None:
    from tests.helpers_v0 import create_user_file_v0
    from pathlib import Path
    from app.core.config import settings

    user = await create_user_v0(username="claim_completed", quota_bytes=10_000)
    store_path = Path(settings.download_dir) / "store" / "c.dat"
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_bytes(b"data")
    user_file = await create_user_file_v0(
        user_id=user["id"],
        real_path=store_path,
        content_hash="c" * 64,
        display_name="c.dat",
        size_bytes=4,
    )
    download = await create_global_download_v0(
        resource_key="http:claim-completed-fid",
        status="completed",
        aria2_gid="gid-cf",
        total_bytes=4,
        completed_bytes=4,
        completed_file_id=user_file["stored_file_id"],
    )

    claim = await claim_attempt_terminal(
        attempt_id=download["id"],
        expected_gid="gid-cf",
        terminal_status="failed",
        error_code="err",
        error_message="msg",
        expected_statuses=("completed",),
    )
    assert claim is None


# ---------------------------------------------------------------------------
# 5. Concurrent claims: only one succeeds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_claim_only_one_succeeds(temp_db: str) -> None:
    download = await create_global_download_v0(
        resource_key="http:claim-concurrent",
        status="active",
        aria2_gid="gid-conc",
        total_bytes=200,
        completed_bytes=0,
        disk_reserved_bytes=200,
    )

    claim_a, claim_b = await asyncio.gather(
        claim_attempt_terminal(
            attempt_id=download["id"],
            expected_gid="gid-conc",
            terminal_status="failed",
            error_code="err_a",
            error_message="msg_a",
            expected_statuses=ACTIVE_GLOBAL_DOWNLOAD_STATUSES,
        ),
        claim_attempt_terminal(
            attempt_id=download["id"],
            expected_gid="gid-conc",
            terminal_status="failed",
            error_code="err_b",
            error_message="msg_b",
            expected_statuses=ACTIVE_GLOBAL_DOWNLOAD_STATUSES,
        ),
    )

    results = [r for r in (claim_a, claim_b) if r is not None]
    assert len(results) == 1
    winner = results[0]
    assert winner.terminal_status == "failed"

    stored = await _fetch_global(download["id"])
    assert stored["status"] == "failed"
    # The winner's error info should be persisted
    assert stored["error_code"] in ("err_a", "err_b")


# ---------------------------------------------------------------------------
# 6. Repair claim
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repair_claim_failed_matching_gid(temp_db: str) -> None:
    download = await create_global_download_v0(
        resource_key="http:repair-failed",
        status="failed",
        aria2_gid="gid-repair-f",
        total_bytes=100,
        completed_bytes=0,
    )

    claim = await claim_terminal_reclaim(
        attempt_id=download["id"],
        expected_gid="gid-repair-f",
    )
    assert claim is not None
    assert isinstance(claim, RepairClaim)
    assert claim.attempt_id == download["id"]
    assert claim.expected_current_gid == "gid-repair-f"
    assert claim.writer_gids == ("gid-repair-f",)
    assert claim.terminal_status == "failed"

    stored = await _fetch_global(download["id"])
    assert stored["status"] == "failed"
    assert stored["aria2_gid"] == "gid-repair-f"


@pytest.mark.asyncio
async def test_repair_claim_cancelled_matching_gid(temp_db: str) -> None:
    download = await create_global_download_v0(
        resource_key="http:repair-cancelled",
        status="cancelled",
        aria2_gid="gid-repair-c",
        total_bytes=100,
        completed_bytes=0,
    )

    claim = await claim_terminal_reclaim(
        attempt_id=download["id"],
        expected_gid="gid-repair-c",
    )
    assert claim is not None
    assert claim.terminal_status == "cancelled"


@pytest.mark.asyncio
async def test_repair_claim_live_returns_none(temp_db: str) -> None:
    download = await create_global_download_v0(
        resource_key="http:repair-live",
        status="active",
        aria2_gid="gid-live",
        total_bytes=100,
        completed_bytes=0,
    )

    claim = await claim_terminal_reclaim(
        attempt_id=download["id"],
        expected_gid="gid-live",
    )
    assert claim is None


@pytest.mark.asyncio
async def test_repair_claim_wrong_gid_returns_none(temp_db: str) -> None:
    download = await create_global_download_v0(
        resource_key="http:repair-wrong",
        status="failed",
        aria2_gid="gid-actual",
        total_bytes=100,
        completed_bytes=0,
    )

    claim = await claim_terminal_reclaim(
        attempt_id=download["id"],
        expected_gid="gid-wrong",
    )
    assert claim is None


@pytest.mark.asyncio
async def test_repair_claim_does_not_change_status(temp_db: str) -> None:
    download = await create_global_download_v0(
        resource_key="http:repair-nochange",
        status="failed",
        aria2_gid="gid-nc",
        total_bytes=100,
        completed_bytes=0,
        disk_reserved_bytes=0,
    )
    before = await _fetch_global(download["id"])

    claim = await claim_terminal_reclaim(
        attempt_id=download["id"],
        expected_gid="gid-nc",
    )
    assert claim is not None

    after = await _fetch_global(download["id"])
    assert after["status"] == before["status"]
    assert after["error_code"] == before["error_code"]
    assert after["completed_file_id"] == before["completed_file_id"]
    assert after["aria2_gid"] == before["aria2_gid"]


@pytest.mark.asyncio
async def test_repair_claim_completed_file_id_set_returns_none(temp_db: str) -> None:
    from tests.helpers_v0 import create_user_file_v0
    from pathlib import Path
    from app.core.config import settings

    user = await create_user_v0(username="repair_cf", quota_bytes=10_000)
    store_path = Path(settings.download_dir) / "store" / "r.dat"
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_bytes(b"d")
    user_file = await create_user_file_v0(
        user_id=user["id"],
        real_path=store_path,
        content_hash="d" * 64,
        display_name="r.dat",
        size_bytes=1,
    )
    download = await create_global_download_v0(
        resource_key="http:repair-cf",
        status="failed",
        aria2_gid="gid-rcf",
        total_bytes=1,
        completed_bytes=0,
        completed_file_id=user_file["stored_file_id"],
    )

    claim = await claim_terminal_reclaim(
        attempt_id=download["id"],
        expected_gid="gid-rcf",
    )
    assert claim is None


# ---------------------------------------------------------------------------
# 7. Handoff-style: writer_gids / result_gids passed through to claim
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handoff_claim_writer_gids_include_source_and_payload(
    temp_db: str,
) -> None:
    download = await create_global_download_v0(
        resource_key="magnet:claim-handoff",
        status="active",
        aria2_gid="source_gid",
        total_bytes=1000,
        completed_bytes=0,
        disk_reserved_bytes=1000,
    )

    claim = await claim_attempt_terminal(
        attempt_id=download["id"],
        expected_gid="source_gid",
        terminal_status="failed",
        error_code="handoff_unknown_size",
        error_message="payload size unknown",
        expected_statuses=ACTIVE_GLOBAL_DOWNLOAD_STATUSES,
        writer_gids=("source_gid", "payload_gid"),
        result_gids=("source_gid", "payload_gid"),
    )
    assert claim is not None
    assert claim.writer_gids == ("source_gid", "payload_gid")
    assert claim.result_gids == ("source_gid", "payload_gid")
    assert claim.expected_current_gid == "source_gid"
    assert claim.error_code == "handoff_unknown_size"


@pytest.mark.asyncio
async def test_handoff_claim_result_gids_narrower_than_writer_gids(
    temp_db: str,
) -> None:
    download = await create_global_download_v0(
        resource_key="magnet:claim-narrow",
        status="active",
        aria2_gid="src",
        total_bytes=1000,
        completed_bytes=0,
    )

    claim = await claim_attempt_terminal(
        attempt_id=download["id"],
        expected_gid="src",
        terminal_status="cancelled",
        error_code=None,
        error_message=None,
        expected_statuses=ACTIVE_GLOBAL_DOWNLOAD_STATUSES,
        writer_gids=("src", "payload"),
        result_gids=("src",),
    )
    assert claim is not None
    assert claim.writer_gids == ("src", "payload")
    assert claim.result_gids == ("src",)
    assert claim.terminal_status == "cancelled"


# ---------------------------------------------------------------------------
# Bonus: cancelled terminal_status propagates to user tasks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_cancelled_propagates_to_user_tasks(temp_db: str) -> None:
    user = await create_user_v0(username="claim_cancel", quota_bytes=10_000)
    download = await create_global_download_v0(
        resource_key="http:claim-cancel",
        status="active",
        aria2_gid="gid-cancel",
        total_bytes=200,
        completed_bytes=0,
        disk_reserved_bytes=200,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=200,
    )
    await _set_usage_reserved(user["id"], 200)

    claim = await claim_attempt_terminal(
        attempt_id=download["id"],
        expected_gid="gid-cancel",
        terminal_status="cancelled",
        error_code="user_cancelled",
        error_message="用户取消",
        expected_statuses=ACTIVE_GLOBAL_DOWNLOAD_STATUSES,
    )
    assert claim is not None
    assert claim.terminal_status == "cancelled"

    stored = await _fetch_global(download["id"])
    assert stored["status"] == "cancelled"

    tasks = await _fetch_user_tasks(download["id"])
    assert len(tasks) == 1
    assert tasks[0]["status"] == "cancelled"

    usage = await _fetch_usage(user["id"])
    assert usage["reserved_bytes"] == 0
