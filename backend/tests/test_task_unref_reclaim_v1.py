"""Task 3 — unref(): user cancel + zero-ref reclaim.

Covers AC-3 (zero-ref reclaim) and AC-4 (unbind):
- User A unref only ends A's pid; tid stays live; user B's pid stays live.
- Last active subscriber unref → tid terminal (cancelled via claim) and
  ``BackendPort.remove(tid)`` is called exactly once.
- Unref of a missing / foreign / already-terminal pid raises ``UnrefError``
  with a stable code.
- Smoke: register() → unref() round-trip on a fresh tid.

These tests exercise ``app.modules.task_core.unref.unref`` directly against
a temp DB; the backend is an AsyncMock so no aria2 interaction happens.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.db.engine import transaction
from app.db.schema import global_downloads, user_tasks
from app.modules.task_core.register import ResourceSpec, register
from app.modules.task_core.unref import (
    ERROR_ALREADY_TERMINAL,
    ERROR_FORBIDDEN,
    ERROR_NOT_FOUND,
    UnrefError,
    unref,
)
from tests.helpers_v0 import (
    create_global_download_v0,
    create_user_task_v0,
    create_user_v0,
)


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


def _make_backend() -> AsyncMock:
    backend = AsyncMock()
    backend.remove = AsyncMock()
    return backend


@pytest.mark.asyncio
async def test_unref_one_of_two_subscribers_keeps_tid_live(temp_db: str) -> None:
    """AC-4: one user's cancel ends only their pid; the tid stays live."""
    user_a = await create_user_v0(username="a1")
    user_b = await create_user_v0(username="b1")
    gd = await create_global_download_v0(
        resource_key="magnet:?xt=urn:btih:unref1",
        source_uri="magnet:?xt=urn:btih:unref1",
        resource_kind="magnet",
        status="active",
        total_bytes=1024,
        size_known=True,
    )
    task_a = await create_user_task_v0(
        user_id=user_a["id"], global_download_id=gd["id"], status="active"
    )
    task_b = await create_user_task_v0(
        user_id=user_b["id"], global_download_id=gd["id"], status="active"
    )
    backend = _make_backend()

    result = await unref(user_id=user_a["id"], pid=int(task_a["id"]), backend=backend)

    assert result.pid == int(task_a["id"])
    assert result.tid == gd["id"]
    assert result.status == "cancelled"
    assert result.reclaimed is False
    assert result.tid_status is None

    a_row = await _get_user_task(int(task_a["id"]))
    assert a_row is not None
    assert a_row["status"] == "cancelled"

    b_row = await _get_user_task(int(task_b["id"]))
    assert b_row is not None
    assert b_row["status"] == "active"

    gd_row = await _get_global(gd["id"])
    assert gd_row is not None
    assert gd_row["status"] == "active"

    # Not the last ref → no reclaim → backend.remove must not fire.
    backend.remove.assert_not_called()


@pytest.mark.asyncio
async def test_unref_last_subscriber_reclaims_tid(temp_db: str) -> None:
    """AC-3: last ref unref → tid terminal; backend.remove called once."""
    user_a = await create_user_v0(username="a2")
    user_b = await create_user_v0(username="b2")
    gd = await create_global_download_v0(
        resource_key="magnet:?xt=urn:btih:unref2",
        source_uri="magnet:?xt=urn:btih:unref2",
        resource_kind="magnet",
        status="active",
        total_bytes=2048,
        size_known=True,
    )
    task_a = await create_user_task_v0(
        user_id=user_a["id"], global_download_id=gd["id"], status="active"
    )
    task_b = await create_user_task_v0(
        user_id=user_b["id"], global_download_id=gd["id"], status="active"
    )
    backend = _make_backend()

    # First unref: tid stays live, no reclaim.
    first = await unref(user_id=user_a["id"], pid=int(task_a["id"]), backend=backend)
    assert first.reclaimed is False
    backend.remove.assert_not_called()

    # Second (last) unref: tid terminalized + backend.remove fired once.
    second = await unref(user_id=user_b["id"], pid=int(task_b["id"]), backend=backend)
    assert second.reclaimed is True
    assert second.tid_status == "cancelled"
    assert second.status == "cancelled"

    gd_row = await _get_global(gd["id"])
    assert gd_row is not None
    assert gd_row["status"] == "cancelled"
    assert int(gd_row["disk_reserved_bytes"] or 0) == 0
    assert gd_row["error_code"] == "user_cancelled"

    backend.remove.assert_awaited_once_with(gd["id"])


@pytest.mark.asyncio
async def test_unref_without_backend_still_terminalizes(temp_db: str) -> None:
    """AC-3: reclaim works when no BackendPort is supplied (DB terminal only)."""
    user = await create_user_v0(username="solo")
    gd = await create_global_download_v0(
        resource_key="magnet:?xt=urn:btih:unref3",
        source_uri="magnet:?xt=urn:btih:unref3",
        resource_kind="magnet",
        status="active",
        total_bytes=512,
        size_known=True,
    )
    task = await create_user_task_v0(
        user_id=user["id"], global_download_id=gd["id"], status="active"
    )

    result = await unref(user_id=user["id"], pid=int(task["id"]), backend=None)

    assert result.reclaimed is True
    assert result.tid_status == "cancelled"
    gd_row = await _get_global(gd["id"])
    assert gd_row is not None
    assert gd_row["status"] == "cancelled"


@pytest.mark.asyncio
async def test_unref_nonexistent_pid_raises_not_found(temp_db: str) -> None:
    user = await create_user_v0(username="nf")
    with pytest.raises(UnrefError) as excinfo:
        await unref(user_id=user["id"], pid=999_999, backend=_make_backend())
    assert excinfo.value.code == ERROR_NOT_FOUND


@pytest.mark.asyncio
async def test_unref_foreign_pid_raises_forbidden(temp_db: str) -> None:
    owner = await create_user_v0(username="owner-x")
    other = await create_user_v0(username="other-x")
    gd = await create_global_download_v0(
        resource_key="magnet:?xt=urn:btih:unref4",
        source_uri="magnet:?xt=urn:btih:unref4",
        resource_kind="magnet",
        status="active",
        total_bytes=128,
        size_known=True,
    )
    task = await create_user_task_v0(
        user_id=owner["id"], global_download_id=gd["id"], status="active"
    )

    with pytest.raises(UnrefError) as excinfo:
        await unref(user_id=other["id"], pid=int(task["id"]), backend=_make_backend())
    assert excinfo.value.code == ERROR_FORBIDDEN

    # The foreign attempt must not disturb the owner's pid or the tid.
    task_row = await _get_user_task(int(task["id"]))
    assert task_row is not None
    assert task_row["status"] == "active"
    gd_row = await _get_global(gd["id"])
    assert gd_row is not None
    assert gd_row["status"] == "active"


@pytest.mark.asyncio
async def test_unref_already_terminal_pid_raises(temp_db: str) -> None:
    user = await create_user_v0(username="term")
    gd = await create_global_download_v0(
        resource_key="magnet:?xt=urn:btih:unref5",
        source_uri="magnet:?xt=urn:btih:unref5",
        resource_kind="magnet",
        status="cancelled",
        total_bytes=64,
        size_known=True,
    )
    task = await create_user_task_v0(
        user_id=user["id"], global_download_id=gd["id"], status="cancelled"
    )

    with pytest.raises(UnrefError) as excinfo:
        await unref(user_id=user["id"], pid=int(task["id"]), backend=_make_backend())
    assert excinfo.value.code == ERROR_ALREADY_TERMINAL


@pytest.mark.asyncio
async def test_register_then_unref_round_trip(temp_db: str) -> None:
    """Smoke: register() creates pid+tid; unref() cancels and reclaims."""
    user = await create_user_v0(username="rt")
    # size_known=False keeps reserved_bytes=0: register() only pre-checks
    # quota and does not mutate user_storage_usage, so a known-size pid would
    # drift on unref release (reservation is wired up in a later task).
    spec = ResourceSpec(
        resource_key="magnet:?xt=urn:btih:rt1",
        source_uri="magnet:?xt=urn:btih:rt1",
        resource_kind="magnet",
        display_name="rt.bin",
        size_bytes=0,
        size_known=False,
    )
    reg = await register(user_id=user["id"], quota_bytes=user["quota_bytes"], resource=spec)
    assert reg.outcome == "created"

    backend = _make_backend()
    result = await unref(user_id=user["id"], pid=reg.pid, backend=backend)

    assert result.tid == reg.tid
    assert result.status == "cancelled"
    assert result.reclaimed is True
    assert result.tid_status == "cancelled"

    gd_row = await _get_global(reg.tid)
    assert gd_row is not None
    assert gd_row["status"] == "cancelled"
    backend.remove.assert_awaited_once_with(reg.tid)
