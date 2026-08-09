"""T04: live write, GID submission, size and completion CAS contract tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.db.engine import transaction
from app.db.schema import global_downloads, user_storage_usage, user_tasks
from app.repositories.task.downloads import (
    assign_submitted_gid,
    complete_attempt,
    guarded_update_download_and_active_user_tasks,
    guarded_update_global_download,
    reconcile_download_size,
)
from tests.helpers_v0 import (
    create_global_download_v0,
    create_user_file_v0,
    create_user_task_v0,
    create_user_v0,
    now_ms,
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


async def _set_usage(user_id: int, *, reserved: int = 0, used: int = 0) -> None:
    async with transaction() as conn:
        await conn.execute(
            user_storage_usage.update()
            .where(user_storage_usage.c.user_id == user_id)
            .values(reserved_bytes=reserved, used_bytes=used)
        )


async def _create_stored_file(
    user_id: int, name: str, size: int, content_hash: str | None = None
) -> dict:
    store_path = Path(settings.download_dir) / "store" / name
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_bytes(b"x" * size)
    ch = content_hash or name.encode().hex() * 8
    return await create_user_file_v0(
        user_id=user_id,
        real_path=store_path,
        content_hash=ch[:64],
        display_name=name,
        size_bytes=size,
    )


# ---------------------------------------------------------------------------
# 1. assign_submitted_gid
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assign_gid_queued_no_gid_success(temp_db: str) -> None:
    download = await create_global_download_v0(
        resource_key="http:assign-ok",
        status="queued",
        aria2_gid=None,
        total_bytes=100,
    )
    row = await assign_submitted_gid(
        download_id=download["id"], gid="gid-1", status="active"
    )
    assert row is not None
    assert row["aria2_gid"] == "gid-1"
    assert row["status"] == "active"

    stored = await _fetch_global(download["id"])
    assert stored["aria2_gid"] == "gid-1"
    assert stored["status"] == "active"


@pytest.mark.asyncio
async def test_assign_gid_already_has_gid_returns_none(temp_db: str) -> None:
    download = await create_global_download_v0(
        resource_key="http:assign-has-gid",
        status="queued",
        aria2_gid="existing-gid",
        total_bytes=100,
    )
    row = await assign_submitted_gid(
        download_id=download["id"], gid="new-gid", status="active"
    )
    assert row is None

    stored = await _fetch_global(download["id"])
    assert stored["aria2_gid"] == "existing-gid"
    assert stored["status"] == "queued"


@pytest.mark.asyncio
async def test_assign_gid_non_queued_returns_none(temp_db: str) -> None:
    download = await create_global_download_v0(
        resource_key="http:assign-active",
        status="active",
        aria2_gid=None,
        total_bytes=100,
    )
    row = await assign_submitted_gid(
        download_id=download["id"], gid="gid-1", status="active"
    )
    assert row is None

    stored = await _fetch_global(download["id"])
    assert stored["aria2_gid"] is None
    assert stored["status"] == "active"


# ---------------------------------------------------------------------------
# 2. guarded_update_global_download
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_guarded_update_wrong_gid_returns_false(temp_db: str) -> None:
    download = await create_global_download_v0(
        resource_key="http:guarded-wrong",
        status="active",
        aria2_gid="gid-real",
        total_bytes=100,
    )
    ok = await guarded_update_global_download(
        download["id"],
        {"completed_bytes": 50},
        expected_gid="gid-wrong",
    )
    assert ok is False

    stored = await _fetch_global(download["id"])
    assert stored["completed_bytes"] == 0


@pytest.mark.asyncio
async def test_guarded_update_correct_gid_succeeds(temp_db: str) -> None:
    download = await create_global_download_v0(
        resource_key="http:guarded-ok",
        status="active",
        aria2_gid="gid-ok",
        total_bytes=100,
    )
    ok = await guarded_update_global_download(
        download["id"],
        {"completed_bytes": 50},
        expected_gid="gid-ok",
    )
    assert ok is True

    stored = await _fetch_global(download["id"])
    assert stored["completed_bytes"] == 50


@pytest.mark.asyncio
async def test_guarded_update_return_row_wrong_gid(temp_db: str) -> None:
    download = await create_global_download_v0(
        resource_key="http:guarded-row",
        status="active",
        aria2_gid="gid-rr",
        total_bytes=100,
    )
    row = await guarded_update_global_download(
        download["id"],
        {"completed_bytes": 30},
        expected_gid="nope",
        return_row=True,
    )
    assert row is None


# ---------------------------------------------------------------------------
# 3. guarded_update_download_and_active_user_tasks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_guarded_update_tasks_wrong_gid_returns_none(temp_db: str) -> None:
    download = await create_global_download_v0(
        resource_key="http:tasks-wrong",
        status="active",
        aria2_gid="gid-t1",
        total_bytes=100,
    )
    row = await guarded_update_download_and_active_user_tasks(
        download["id"],
        {"completed_bytes": 50},
        expected_gid="gid-wrong",
    )
    assert row is None

    stored = await _fetch_global(download["id"])
    assert stored["completed_bytes"] == 0


@pytest.mark.asyncio
async def test_guarded_update_tasks_terminal_returns_none(temp_db: str) -> None:
    download = await create_global_download_v0(
        resource_key="http:tasks-terminal",
        status="failed",
        aria2_gid="gid-term",
        total_bytes=100,
    )
    row = await guarded_update_download_and_active_user_tasks(
        download["id"],
        {"completed_bytes": 50},
        expected_gid="gid-term",
    )
    assert row is None

    stored = await _fetch_global(download["id"])
    assert stored["status"] == "failed"
    assert stored["completed_bytes"] == 0


@pytest.mark.asyncio
async def test_guarded_update_tasks_completed_file_id_set_returns_none(
    temp_db: str,
) -> None:
    user = await create_user_v0(username="cf_user", quota_bytes=10_000)
    uf = await _create_stored_file(user["id"], "cf.dat", 4)
    download = await create_global_download_v0(
        resource_key="http:tasks-cf",
        status="active",
        aria2_gid="gid-cf",
        total_bytes=4,
        completed_file_id=uf["stored_file_id"],
    )
    row = await guarded_update_download_and_active_user_tasks(
        download["id"],
        {"completed_bytes": 4},
        expected_gid="gid-cf",
    )
    assert row is None

    stored = await _fetch_global(download["id"])
    assert stored["status"] == "active"
    assert stored["completed_file_id"] == uf["stored_file_id"]
    assert stored["completed_bytes"] == 0  # unchanged — guarded update rejected


# ---------------------------------------------------------------------------
# 4. reconcile_download_size
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconcile_size_correct_gid_admitted(temp_db: str) -> None:
    user = await create_user_v0(username="reconcile_ok", quota_bytes=10_000)
    download = await create_global_download_v0(
        resource_key="http:reconcile-ok",
        status="active",
        aria2_gid="gid-rec",
        total_bytes=100,
        disk_reserved_bytes=100,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=100,
    )
    await _set_usage(user["id"], reserved=100)

    result = await reconcile_download_size(
        download_id=download["id"],
        expected_gid="gid-rec",
        candidate_bytes=200,
        completed_bytes=50,
        size_limit_bytes=10_000,
        disk_available_bytes=10 * 1024 * 1024 * 1024,
    )
    assert result.admitted is True

    stored = await _fetch_global(download["id"])
    assert stored["total_bytes"] == 200
    assert stored["completed_bytes"] == 50


@pytest.mark.asyncio
async def test_reconcile_size_wrong_gid_stale(temp_db: str) -> None:
    user = await create_user_v0(username="reconcile_stale", quota_bytes=10_000)
    download = await create_global_download_v0(
        resource_key="http:reconcile-stale",
        status="active",
        aria2_gid="gid-real",
        total_bytes=100,
        disk_reserved_bytes=100,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=100,
    )
    await _set_usage(user["id"], reserved=100)

    result = await reconcile_download_size(
        download_id=download["id"],
        expected_gid="gid-wrong",
        candidate_bytes=200,
        completed_bytes=50,
        size_limit_bytes=10_000,
        disk_available_bytes=10 * 1024 * 1024 * 1024,
    )
    assert result.get("outcome") == "stale"

    stored = await _fetch_global(download["id"])
    assert stored["total_bytes"] == 100
    assert stored["aria2_gid"] == "gid-real"


# ---------------------------------------------------------------------------
# 5. complete_attempt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_attempt_success(temp_db: str) -> None:
    size = 100
    user = await create_user_v0(username="comp_ok", quota_bytes=10_000)
    uf = await _create_stored_file(user["id"], "comp.dat", size)
    download = await create_global_download_v0(
        resource_key="http:comp-ok",
        status="active",
        aria2_gid="gid-comp",
        total_bytes=size,
        disk_reserved_bytes=size,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=size,
    )
    await _set_usage(user["id"], reserved=size)

    row = await complete_attempt(
        attempt_id=download["id"],
        expected_gid="gid-comp",
        stored_file_id=uf["stored_file_id"],
        size_bytes=size,
        original_name="comp.dat",
        completed_at_ms=now_ms(),
    )
    assert row is not None
    assert row["status"] == "completed"
    assert row["completed_file_id"] == uf["stored_file_id"]
    assert row["aria2_gid"] is None

    stored = await _fetch_global(download["id"])
    assert stored["status"] == "completed"
    assert stored["completed_file_id"] == uf["stored_file_id"]

    tasks = await _fetch_user_tasks(download["id"])
    assert len(tasks) == 1
    assert tasks[0]["status"] == "completed"


@pytest.mark.asyncio
async def test_complete_attempt_wrong_gid_returns_none(temp_db: str) -> None:
    size = 100
    user = await create_user_v0(username="comp_wg", quota_bytes=10_000)
    uf = await _create_stored_file(user["id"], "comp_wg.dat", size)
    download = await create_global_download_v0(
        resource_key="http:comp-wg",
        status="active",
        aria2_gid="gid-real",
        total_bytes=size,
        disk_reserved_bytes=size,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=size,
    )
    await _set_usage(user["id"], reserved=size)

    row = await complete_attempt(
        attempt_id=download["id"],
        expected_gid="gid-wrong",
        stored_file_id=uf["stored_file_id"],
        size_bytes=size,
        original_name="comp_wg.dat",
        completed_at_ms=now_ms(),
    )
    assert row is None

    stored = await _fetch_global(download["id"])
    assert stored["status"] == "active"
    assert stored["completed_file_id"] is None
    assert stored["aria2_gid"] == "gid-real"


@pytest.mark.asyncio
async def test_complete_attempt_duplicate_returns_none(temp_db: str) -> None:
    size = 100
    user = await create_user_v0(username="comp_dup", quota_bytes=10_000)
    uf = await _create_stored_file(user["id"], "comp_dup.dat", size)
    download = await create_global_download_v0(
        resource_key="http:comp-dup",
        status="active",
        aria2_gid="gid-dup",
        total_bytes=size,
        disk_reserved_bytes=size,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=size,
    )
    await _set_usage(user["id"], reserved=size)

    first = await complete_attempt(
        attempt_id=download["id"],
        expected_gid="gid-dup",
        stored_file_id=uf["stored_file_id"],
        size_bytes=size,
        original_name="comp_dup.dat",
        completed_at_ms=now_ms(),
    )
    assert first is not None

    second = await complete_attempt(
        attempt_id=download["id"],
        expected_gid="gid-dup",
        stored_file_id=uf["stored_file_id"],
        size_bytes=size,
        original_name="comp_dup.dat",
        completed_at_ms=now_ms(),
    )
    assert second is None

    stored = await _fetch_global(download["id"])
    assert stored["status"] == "completed"
    assert stored["completed_file_id"] == uf["stored_file_id"]


# ---------------------------------------------------------------------------
# 6. Duplicate submission isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assign_gid_does_not_affect_other_attempt(temp_db: str) -> None:
    download_a = await create_global_download_v0(
        resource_key="http:iso-a",
        status="queued",
        aria2_gid=None,
        total_bytes=100,
    )
    download_b = await create_global_download_v0(
        resource_key="http:iso-b",
        status="queued",
        aria2_gid=None,
        total_bytes=200,
    )
    row = await assign_submitted_gid(
        download_id=download_a["id"], gid="gid-a", status="active"
    )
    assert row is not None
    assert row["aria2_gid"] == "gid-a"

    stored_b = await _fetch_global(download_b["id"])
    assert stored_b["aria2_gid"] is None
    assert stored_b["status"] == "queued"
