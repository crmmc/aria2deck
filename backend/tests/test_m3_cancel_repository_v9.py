"""T05: cancel user task and maybe claim attempt repository contract tests."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.engine import transaction
from app.db.schema import global_downloads, user_storage_usage, user_tasks
from app.domain.lifecycle import TerminalizationClaim
from app.repositories.task.user_tasks import cancel_user_task_and_maybe_claim_attempt
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
# 1. Shared attempt: cancel one subscriber, global stays live, no claim
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_one_subscriber_global_stays_live(temp_db: str) -> None:
    user_a = await create_user_v0(username="user_a", quota_bytes=10_000)
    user_b = await create_user_v0(username="user_b", quota_bytes=10_000)
    download = await create_global_download_v0(
        resource_key="http:shared-cancel",
        status="active",
        aria2_gid="gid-shared",
        total_bytes=500,
        disk_reserved_bytes=500,
    )
    task_a = await create_user_task_v0(
        user_id=user_a["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=250,
    )
    await create_user_task_v0(
        user_id=user_b["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=250,
    )
    await _set_usage_reserved(user_a["id"], 250)
    await _set_usage_reserved(user_b["id"], 250)

    task_row, claim = await cancel_user_task_and_maybe_claim_attempt(
        user_id=user_a["id"],
        user_task_id=task_a["id"],
        expected_gid="gid-shared",
    )

    assert task_row is not None
    assert task_row["status"] == "cancelled"
    assert task_row["reserved_bytes"] == 0
    assert claim is None

    stored = await _fetch_global(download["id"])
    assert stored["status"] == "active"
    assert stored["aria2_gid"] == "gid-shared"
    assert stored["disk_reserved_bytes"] == 500

    usage_a = await _fetch_usage(user_a["id"])
    assert usage_a["reserved_bytes"] == 0
    usage_b = await _fetch_usage(user_b["id"])
    assert usage_b["reserved_bytes"] == 250

    tasks = await _fetch_user_tasks(download["id"])
    statuses = {t["status"] for t in tasks}
    assert "cancelled" in statuses
    assert "active" in statuses


# ---------------------------------------------------------------------------
# 2. Last subscriber: returns claim, global cancelled, GID preserved
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_last_subscriber_returns_claim(temp_db: str) -> None:
    user = await create_user_v0(username="last_user", quota_bytes=10_000)
    download = await create_global_download_v0(
        resource_key="http:last-cancel",
        status="active",
        aria2_gid="gid-last",
        total_bytes=300,
        disk_reserved_bytes=300,
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=300,
    )
    await _set_usage_reserved(user["id"], 300)

    task_row, claim = await cancel_user_task_and_maybe_claim_attempt(
        user_id=user["id"],
        user_task_id=task["id"],
        expected_gid="gid-last",
    )

    assert task_row is not None
    assert task_row["status"] == "cancelled"
    assert claim is not None
    assert isinstance(claim, TerminalizationClaim)
    assert claim.attempt_id == download["id"]
    assert claim.expected_current_gid == "gid-last"
    assert claim.writer_gids == ("gid-last",)
    assert claim.result_gids == ("gid-last",)
    assert claim.terminal_status == "cancelled"

    stored = await _fetch_global(download["id"])
    assert stored["status"] == "cancelled"
    assert stored["aria2_gid"] == "gid-last"
    assert stored["disk_reserved_bytes"] == 0
    assert stored["error_code"] == "user_cancelled"

    usage = await _fetch_usage(user["id"])
    assert usage["reserved_bytes"] == 0


# ---------------------------------------------------------------------------
# 3. Idempotent: cancel twice -> second returns (None, None)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_already_cancelled_returns_none(temp_db: str) -> None:
    user = await create_user_v0(username="idem_user", quota_bytes=10_000)
    download = await create_global_download_v0(
        resource_key="http:idem-cancel",
        status="active",
        aria2_gid="gid-idem",
        total_bytes=200,
        disk_reserved_bytes=200,
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=200,
    )
    await _set_usage_reserved(user["id"], 200)

    first_row, first_claim = await cancel_user_task_and_maybe_claim_attempt(
        user_id=user["id"],
        user_task_id=task["id"],
        expected_gid="gid-idem",
    )
    assert first_row is not None
    assert first_claim is not None

    second_row, second_claim = await cancel_user_task_and_maybe_claim_attempt(
        user_id=user["id"],
        user_task_id=task["id"],
        expected_gid="gid-idem",
    )
    assert second_row is None
    assert second_claim is None

    stored = await _fetch_global(download["id"])
    assert stored["status"] == "cancelled"
    assert stored["aria2_gid"] == "gid-idem"


# ---------------------------------------------------------------------------
# 4. Wrong user / nonexistent task: returns (None, None)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_wrong_user_returns_none(temp_db: str) -> None:
    user_a = await create_user_v0(username="owner", quota_bytes=10_000)
    user_b = await create_user_v0(username="intruder", quota_bytes=10_000)
    download = await create_global_download_v0(
        resource_key="http:wrong-user",
        status="active",
        aria2_gid="gid-wu",
        total_bytes=100,
        disk_reserved_bytes=100,
    )
    task = await create_user_task_v0(
        user_id=user_a["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=100,
    )

    task_row, claim = await cancel_user_task_and_maybe_claim_attempt(
        user_id=user_b["id"],
        user_task_id=task["id"],
        expected_gid="gid-wu",
    )
    assert task_row is None
    assert claim is None

    stored = await _fetch_global(download["id"])
    assert stored["status"] == "active"

    tasks = await _fetch_user_tasks(download["id"])
    assert len(tasks) == 1
    assert tasks[0]["status"] == "active"


@pytest.mark.asyncio
async def test_cancel_nonexistent_task_returns_none(temp_db: str) -> None:
    user = await create_user_v0(username="ne_user", quota_bytes=10_000)
    task_row, claim = await cancel_user_task_and_maybe_claim_attempt(
        user_id=user["id"],
        user_task_id=999_999,
        expected_gid="gid-ne",
    )
    assert task_row is None
    assert claim is None


# ---------------------------------------------------------------------------
# 5. Global already terminal: user task cancelled safely, no claim
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_when_global_already_failed(temp_db: str) -> None:
    user = await create_user_v0(username="failed_user", quota_bytes=10_000)
    download = await create_global_download_v0(
        resource_key="http:already-failed",
        status="failed",
        aria2_gid="gid-af",
        total_bytes=100,
        disk_reserved_bytes=0,
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=0,
    )

    task_row, claim = await cancel_user_task_and_maybe_claim_attempt(
        user_id=user["id"],
        user_task_id=task["id"],
        expected_gid="gid-af",
    )

    assert task_row is not None
    assert task_row["status"] == "cancelled"
    assert claim is None

    stored = await _fetch_global(download["id"])
    assert stored["status"] == "failed"
    assert stored["aria2_gid"] == "gid-af"


@pytest.mark.asyncio
async def test_cancel_when_global_already_completed(temp_db: str) -> None:
    user = await create_user_v0(username="comp_user2", quota_bytes=10_000)
    download = await create_global_download_v0(
        resource_key="http:already-completed",
        status="completed",
        aria2_gid=None,
        total_bytes=100,
        completed_bytes=100,
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=0,
    )

    task_row, claim = await cancel_user_task_and_maybe_claim_attempt(
        user_id=user["id"],
        user_task_id=task["id"],
        expected_gid=None,
    )

    assert task_row is not None
    assert task_row["status"] == "cancelled"
    assert claim is None

    stored = await _fetch_global(download["id"])
    assert stored["status"] == "completed"


# ---------------------------------------------------------------------------
# 6. GID mismatch: user task cancelled, no claim (stale snapshot)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_gid_mismatch_no_claim(temp_db: str) -> None:
    user = await create_user_v0(username="stale_user", quota_bytes=10_000)
    download = await create_global_download_v0(
        resource_key="http:gid-mismatch",
        status="active",
        aria2_gid="gid-actual",
        total_bytes=200,
        disk_reserved_bytes=200,
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=200,
    )
    await _set_usage_reserved(user["id"], 200)

    task_row, claim = await cancel_user_task_and_maybe_claim_attempt(
        user_id=user["id"],
        user_task_id=task["id"],
        expected_gid="gid-wrong",
    )

    assert task_row is not None
    assert task_row["status"] == "cancelled"
    assert claim is None

    stored = await _fetch_global(download["id"])
    assert stored["status"] == "active"
    assert stored["aria2_gid"] == "gid-actual"

    usage = await _fetch_usage(user["id"])
    assert usage["reserved_bytes"] == 0


# ---------------------------------------------------------------------------
# 7. Queued attempt with no GID: last subscriber cancels, claim with null GID
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_queued_no_gid_last_subscriber(temp_db: str) -> None:
    user = await create_user_v0(username="queued_user", quota_bytes=10_000)
    download = await create_global_download_v0(
        resource_key="http:queued-cancel",
        status="queued",
        aria2_gid=None,
        total_bytes=0,
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="queued",
        reserved_bytes=0,
    )

    task_row, claim = await cancel_user_task_and_maybe_claim_attempt(
        user_id=user["id"],
        user_task_id=task["id"],
        expected_gid=None,
    )

    assert task_row is not None
    assert task_row["status"] == "cancelled"
    assert claim is not None
    assert claim.expected_current_gid is None
    assert claim.writer_gids == ()
    assert claim.terminal_status == "cancelled"

    stored = await _fetch_global(download["id"])
    assert stored["status"] == "cancelled"
    assert stored["aria2_gid"] is None
