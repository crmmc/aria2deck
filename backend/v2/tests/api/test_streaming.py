from __future__ import annotations

import asyncio
from functools import partial

import pytest

from aria2deck.api.streaming import release_all


async def test_release_all_no_releases_is_noop() -> None:
    await release_all(())


async def test_release_all_runs_every_release_then_raises() -> None:
    """单个释放抛错不阻断其余释放：全部落地后再传播第一个异常。"""
    ran: list[str] = []

    async def ok(name: str) -> None:
        ran.append(name)

    async def boom() -> None:
        raise RuntimeError("release failed")

    with pytest.raises(RuntimeError, match="release failed"):
        await release_all((partial(ok, "a"), boom, partial(ok, "c")))

    assert ran == ["a", "c"]


async def test_release_all_completes_releases_under_cancellation() -> None:
    """释放协程自身被取消时先等释放落地再传播取消（shield 语义）。"""
    started = asyncio.Event()
    finished = asyncio.Event()

    async def slow_release() -> None:
        started.set()
        await asyncio.sleep(0.05)
        finished.set()

    task = asyncio.create_task(release_all((slow_release,)))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()
