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

from app.domain.locks import get_download_lifecycle_lock
from app.services.lifecycle.completion import (
    complete_global_download,
    complete_global_download_locked,
)
from app.services.lifecycle import cleanup as lifecycle_cleanup
from app.services.lifecycle.cleanup import fail_download_and_reclaim
from app.services.lifecycle import completion as lifecycle_completion


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
        lifecycle_cleanup,
        "_fail_download_and_reclaim_operation",
        side_effect=fake_operation,
    ):
        async with lock:
            result = await fail_download_and_reclaim(
                backend=AsyncMock(),
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

def test_fail_download_and_reclaim_does_not_use_completion_lock():
    """get_task_complete_lock must no longer exist in the lifecycle module."""
    for module in (lifecycle_cleanup, lifecycle_completion):
        assert not hasattr(module, "get_task_complete_lock"), (
            "get_task_complete_lock should be removed; lifecycle protection "
            "is solely via get_download_lifecycle_lock."
        )


def test_complete_global_download_does_not_use_completion_lock():
    """get_task_complete_lock must no longer exist for complete paths."""
    for module in (lifecycle_cleanup, lifecycle_completion):
        assert not hasattr(module, "get_task_complete_lock"), (
            "get_task_complete_lock should be removed; completion paths "
            "must only use get_download_lifecycle_lock."
        )


# ---------------------------------------------------------------------------
# 5. _ensure_download_submitted no longer uses _get_download_lock
# ---------------------------------------------------------------------------

def test_get_download_lock_removed():
    """The _get_download_lock function must no longer exist in the completion module."""
    from app.services.lifecycle import completion as download_service

    assert not hasattr(download_service, "_get_download_lock"), (
        "_get_download_lock should be removed; _ensure_download_submitted "
        "must rely on the caller's lifecycle lock and DB fencing."
    )


def test_download_locks_dict_removed():
    """The _download_locks dict must no longer exist in the completion module."""
    from app.services.lifecycle import completion as download_service

    assert not hasattr(download_service, "_download_locks"), (
        "_download_locks dict should be removed."
    )


def test_completion_locks_dict_removed():
    """The _completion_locks dict must no longer exist in lifecycle modules."""
    for module in (lifecycle_cleanup, lifecycle_completion):
        assert not hasattr(module, "_completion_locks"), (
            "_completion_locks dict should be removed."
        )
