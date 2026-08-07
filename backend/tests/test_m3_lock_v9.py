"""T07: attempt lifecycle lock convergence tests.

Verifies that:
1. Concurrent critical sections on the same attempt serialize.
2. Locks for different attempts do not block each other.
3. Holding the lifecycle lock then calling a path with
   acquire_lifecycle_lock=False does not deadlock.
4. complete / fail paths no longer depend on get_task_complete_lock.
5. _ensure_download_submitted no longer depends on _get_download_lock.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.services.download_service import (
    complete_global_download,
    complete_global_download_locked,
    get_download_lifecycle_lock,
)
from app.services import aria2_lifecycle_service
from app.services.aria2_lifecycle_service import (
    fail_download_and_reclaim,
)


# ---------------------------------------------------------------------------
# 1. Same-attempt serialization
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_same_attempt_lock_serializes():
    """Two concurrent critical sections on the same attempt run sequentially."""
    lock = await get_download_lifecycle_lock(9991)

    execution_order: list[str] = []

    async def critical_section(name: str, delay: float):
        async with lock:
            execution_order.append(f"{name}_start")
            await asyncio.sleep(delay)
            execution_order.append(f"{name}_end")

    # Start both concurrently; A should complete before B starts.
    task_a = asyncio.create_task(critical_section("A", 0.05))
    task_b = asyncio.create_task(critical_section("B", 0.01))
    await asyncio.gather(task_a, task_b)

    assert execution_order == [
        "A_start",
        "A_end",
        "B_start",
        "B_end",
    ]


# ---------------------------------------------------------------------------
# 2. Different-attempt locks don't block
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_different_attempt_locks_independent():
    """Locks for different attempt IDs do not block each other."""
    lock_a = await get_download_lifecycle_lock(9992)
    lock_b = await get_download_lifecycle_lock(9993)

    acquired_b = asyncio.Event()

    async def hold_a():
        async with lock_a:
            await asyncio.sleep(0.1)

    async def use_b():
        async with lock_b:
            acquired_b.set()

    task_a = asyncio.create_task(hold_a())
    await asyncio.sleep(0.02)  # ensure A holds its lock

    task_b = asyncio.create_task(use_b())
    await asyncio.wait_for(acquired_b.wait(), timeout=1.0)

    await asyncio.gather(task_a, task_b)


# ---------------------------------------------------------------------------
# 3. No deadlock when acquire_lifecycle_lock=False while holding lock
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_deadlock_acquiring_false_while_holding():
    """Calling fail_download_and_reclaim with acquire_lifecycle_lock=False
    while already holding the lifecycle lock must not deadlock."""

    download_id = 9994
    lock = await get_download_lifecycle_lock(download_id)

    # Mock the internal operation so we don't need a DB or aria2 client.
    call_log: list[str] = []

    async def fake_operation(**kwargs):
        call_log.append("operation_called")
        return True

    with patch.object(
        aria2_lifecycle_service,
        "_fail_download_and_reclaim_operation",
        side_effect=fake_operation,
    ):
        async with lock:
            result = await fail_download_and_reclaim(
                client=AsyncMock(),
                download_id=download_id,
                message="test",
                error_code="test",
                expected_gid="gid-test",
                acquire_lifecycle_lock=False,
                log_prefix="[Test]",
            )

    assert result is True
    assert call_log == ["operation_called"]


# ---------------------------------------------------------------------------
# 4. complete / fail paths no longer use get_task_complete_lock
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fail_download_and_reclaim_does_not_use_completion_lock():
    """fail_download_and_reclaim must not call get_task_complete_lock."""
    download_id = 9995

    async def fake_operation(**kwargs):
        return True

    original_get_task_complete_lock = aria2_lifecycle_service.get_task_complete_lock

    def tracking_get_task_complete_lock(_download_id: int):
        pytest.fail(
            "get_task_complete_lock was called by fail_download_and_reclaim; "
            "lifecycle paths must not depend on the completion lock."
        )

    with patch.object(
        aria2_lifecycle_service,
        "_fail_download_and_reclaim_operation",
        side_effect=fake_operation,
    ), patch.object(
        aria2_lifecycle_service,
        "get_task_complete_lock",
        side_effect=tracking_get_task_complete_lock,
    ):
        result = await fail_download_and_reclaim(
            client=AsyncMock(),
            download_id=download_id,
            message="test",
            error_code="test",
            expected_gid="gid-test",
            acquire_lifecycle_lock=True,
            log_prefix="[Test]",
        )

    assert result is True


@pytest.mark.asyncio
async def test_complete_global_download_does_not_use_completion_lock():
    """complete_global_download must not call get_task_complete_lock.

    It should only use get_download_lifecycle_lock.
    """
    download_id = 9996

    # We can't easily run the full complete flow without DB + aria2,
    # so we verify at the lock acquisition level: the wrapper should
    # only call get_download_lifecycle_lock, never get_task_complete_lock.
    lifecycle_lock_called = False

    original_lifecycle_lock = get_download_lifecycle_lock

    async def tracking_lifecycle_lock(download_id_arg: int):
        nonlocal lifecycle_lock_called
        lifecycle_lock_called = True
        return await original_lifecycle_lock(download_id_arg)

    def tracking_completion_lock(_download_id: int):
        pytest.fail(
            "get_task_complete_lock was called by complete_global_download; "
            "completion paths must not depend on the completion lock."
        )

    # Mock complete_global_download_locked so we don't need DB/aria2.
    async def fake_locked(**kwargs):
        return None

    with patch.object(
        aria2_lifecycle_service,
        "get_task_complete_lock",
        side_effect=tracking_completion_lock,
    ), patch(
        "app.services.download_service.complete_global_download_locked",
        side_effect=fake_locked,
    ), patch(
        "app.services.download_service.get_download_lifecycle_lock",
        side_effect=tracking_lifecycle_lock,
    ):
        result = await complete_global_download(
            global_download_id=download_id,
            expected_gid="gid-test",
            source_path="/tmp/nonexistent",
            original_name="test.txt",
        )

    assert lifecycle_lock_called is True
    assert result is None


# ---------------------------------------------------------------------------
# 5. _ensure_download_submitted no longer uses _get_download_lock
# ---------------------------------------------------------------------------

def test_get_download_lock_removed():
    """The _get_download_lock function must no longer exist in download_service."""
    from app.services import download_service

    assert not hasattr(download_service, "_get_download_lock"), (
        "_get_download_lock should be removed; _ensure_download_submitted "
        "must rely on the caller's lifecycle lock and DB fencing."
    )


def test_download_locks_dict_removed():
    """The _download_locks dict must no longer exist in download_service."""
    from app.services import download_service

    assert not hasattr(download_service, "_download_locks"), (
        "_download_locks dict should be removed."
    )


def test_completion_locks_dict_removed():
    """The _completion_locks dict must no longer exist in aria2_lifecycle_service."""
    assert not hasattr(aria2_lifecycle_service, "_completion_locks"), (
        "_completion_locks dict should be removed."
    )
