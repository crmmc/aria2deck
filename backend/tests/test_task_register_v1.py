"""Task 2 — register() blocking admission gate.

Covers AC-2 (register), AC-4 (no oversell on instant attach),
AC-6 (known-size over-quota rejection at register time).

These tests exercise ``app.modules.task_core.register.register`` directly
against a temp DB; no aria2 submission happens (Task 2 stubs submit).
"""

from __future__ import annotations

import pytest

from app.modules.task_core.register import (
    RegisterError,
    ResourceSpec,
    register,
)
from app.modules.task_core.states import ERROR_QUOTA_EXCEEDED
from tests.helpers_v0 import (
    create_global_download_v0,
    create_user_file_v0,
    create_user_task_v0,
    create_user_v0,
    get_user_v0,
)
from app.db.engine import transaction
from app.db.schema import global_downloads, user_files, user_tasks
from sqlalchemy import select


async def _count_user_files(user_id: int) -> int:
    async with transaction() as conn:
        rows = (
            await conn.execute(
                select(user_files.c.id).where(user_files.c.user_id == user_id)
            )
        ).all()
    return len(rows)


async def _get_user_task(pid: int) -> dict | None:
    async with transaction() as conn:
        row = (
            await conn.execute(select(user_tasks).where(user_tasks.c.id == pid))
        ).mappings().first()
    return dict(row) if row else None


async def _get_global(tid: int) -> dict | None:
    async with transaction() as conn:
        row = (
            await conn.execute(
                select(global_downloads).where(global_downloads.c.id == tid)
            )
        ).mappings().first()
    return dict(row) if row else None


@pytest.mark.asyncio
async def test_register_creates_new_tid_and_pid_when_no_copy(temp_db: str) -> None:
    """AC-2 case 1: no live, no completed → created."""
    user = await create_user_v0(username="u1")
    spec = ResourceSpec(
        resource_key="magnet:?xt=urn:btih:aaa",
        source_uri="magnet:?xt=urn:btih:aaa",
        resource_kind="magnet",
        display_name="file-a",
        size_bytes=0,
        size_known=False,
    )
    result = await register(user_id=user["id"], quota_bytes=user["quota_bytes"], resource=spec)

    assert result.outcome == "created"
    assert result.pid > 0
    assert result.tid > 0
    assert result.status == "queued"

    task = await _get_user_task(result.pid)
    assert task is not None
    assert task["user_id"] == user["id"]
    assert task["global_download_id"] == result.tid

    gd = await _get_global(result.tid)
    assert gd is not None
    assert gd["resource_key"] == spec.resource_key
    assert gd["status"] == "queued"


@pytest.mark.asyncio
async def test_register_joins_live_for_second_user(temp_db: str) -> None:
    """AC-2 case 2: existing live tid, second eligible user → joined_live."""
    owner = await create_user_v0(username="owner")
    other = await create_user_v0(username="other")
    gd = await create_global_download_v0(
        resource_key="magnet:?xt=urn:btih:live1",
        source_uri="magnet:?xt=urn:btih:live1",
        resource_kind="magnet",
        status="active",
        total_bytes=1024,
        size_known=True,
    )
    await create_user_task_v0(
        user_id=owner["id"], global_download_id=gd["id"], status="active"
    )

    spec = ResourceSpec(
        resource_key="magnet:?xt=urn:btih:live1",
        source_uri="magnet:?xt=urn:btih:live1",
        resource_kind="magnet",
        display_name="joined",
        size_bytes=1024,
        size_known=True,
    )
    result = await register(user_id=other["id"], quota_bytes=other["quota_bytes"], resource=spec)

    assert result.outcome == "joined_live"
    assert result.tid == gd["id"]
    assert result.pid > 0

    task = await _get_user_task(result.pid)
    assert task is not None
    assert task["user_id"] == other["id"]
    assert task["global_download_id"] == gd["id"]


@pytest.mark.asyncio
async def test_register_rejects_known_size_over_quota(temp_db: str) -> None:
    """AC-6: size_known and size > quota_bytes → quota_exceeded, no pid."""
    user = await create_user_v0(username="smallquota", quota_bytes=100)
    spec = ResourceSpec(
        resource_key="magnet:?xt=urn:btih:big",
        source_uri="magnet:?xt=urn:btih:big",
        resource_kind="magnet",
        size_bytes=10_000,
        size_known=True,
    )
    with pytest.raises(RegisterError) as excinfo:
        await register(user_id=user["id"], quota_bytes=user["quota_bytes"], resource=spec)
    assert excinfo.value.code == ERROR_QUOTA_EXCEEDED

    # No user_task created.
    async with transaction() as conn:
        rows = (
            await conn.execute(
                select(user_tasks.c.id).where(user_tasks.c.user_id == user["id"])
            )
        ).all()
    assert rows == []


@pytest.mark.asyncio
async def test_register_attaches_completed_when_eligible(temp_db: str) -> None:
    """AC-2 case 4: completed + store → attached_completed."""
    owner = await create_user_v0(username="owner2")
    other = await create_user_v0(username="other2")

    user_file = await create_user_file_v0(
        user_id=owner["id"],
        real_path=__import__("pathlib").Path("/tmp/x.bin"),
        content_hash="hash-attach",
        display_name="done.bin",
        size_bytes=2048,
    )
    gd = await create_global_download_v0(
        resource_key="magnet:?xt=urn:btih:done1",
        source_uri="magnet:?xt=urn:btih:done1",
        resource_kind="magnet",
        status="completed",
        total_bytes=2048,
        completed_bytes=2048,
        size_known=True,
        completed_file_id=user_file["stored_file_id"],
    )

    spec = ResourceSpec(
        resource_key="magnet:?xt=urn:btih:done1",
        source_uri="magnet:?xt=urn:btih:done1",
        resource_kind="magnet",
        display_name="done.bin",
        size_bytes=2048,
        size_known=True,
    )
    result = await register(user_id=other["id"], quota_bytes=other["quota_bytes"], resource=spec)

    assert result.outcome == "attached_completed"
    assert result.tid == gd["id"]
    assert result.status == "completed"

    assert await _count_user_files(other["id"]) == 1


@pytest.mark.asyncio
async def test_register_attach_rejected_when_quota_insufficient(temp_db: str) -> None:
    """AC-4: completed+store exists but small-quota user → refused, no user_files."""
    owner = await create_user_v0(username="owner3")
    small = await create_user_v0(username="small", quota_bytes=100)

    user_file = await create_user_file_v0(
        user_id=owner["id"],
        real_path=__import__("pathlib").Path("/tmp/y.bin"),
        content_hash="hash-oversell",
        display_name="big.bin",
        size_bytes=4096,
    )
    await create_global_download_v0(
        resource_key="magnet:?xt=urn:btih:oversell",
        source_uri="magnet:?xt=urn:btih:oversell",
        resource_kind="magnet",
        status="completed",
        total_bytes=4096,
        completed_bytes=4096,
        size_known=True,
        completed_file_id=user_file["stored_file_id"],
    )

    spec = ResourceSpec(
        resource_key="magnet:?xt=urn:btih:oversell",
        source_uri="magnet:?xt=urn:btih:oversell",
        resource_kind="magnet",
        size_bytes=4096,
        size_known=True,
    )
    with pytest.raises(RegisterError) as excinfo:
        await register(user_id=small["id"], quota_bytes=small["quota_bytes"], resource=spec)
    assert excinfo.value.code == ERROR_QUOTA_EXCEEDED

    assert await _count_user_files(small["id"]) == 0


@pytest.mark.asyncio
async def test_register_rejects_duplicate_active_pid(temp_db: str) -> None:
    """AC-2 case 6: same user re-registers same live tid → duplicate error."""
    user = await create_user_v0(username="dup")
    gd = await create_global_download_v0(
        resource_key="magnet:?xt=urn:btih:dup",
        source_uri="magnet:?xt=urn:btih:dup",
        resource_kind="magnet",
        status="active",
        total_bytes=512,
        size_known=True,
    )
    await create_user_task_v0(
        user_id=user["id"], global_download_id=gd["id"], status="active"
    )

    spec = ResourceSpec(
        resource_key="magnet:?xt=urn:btih:dup",
        source_uri="magnet:?xt=urn:btih:dup",
        resource_kind="magnet",
        size_bytes=512,
        size_known=True,
    )
    with pytest.raises(RegisterError) as excinfo:
        await register(user_id=user["id"], quota_bytes=user["quota_bytes"], resource=spec)
    assert excinfo.value.code == "duplicate_task"
