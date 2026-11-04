from __future__ import annotations

import asyncio

import pytest

from aria2deck.application.system.health import HealthService
from aria2deck.application.system.supervisor import WorkerSupervisor
from aria2deck.domain.errors import DomainError, ErrorCode


async def wait_for_worker_state(
    supervisor: WorkerSupervisor, name: str, expected: str
) -> None:
    for _ in range(20):
        await asyncio.sleep(0)
        states = {item.name: item.state for item in supervisor.snapshot()}
        if states.get(name) == expected:
            return
    raise AssertionError(f"worker {name} did not reach {expected}: {supervisor.snapshot()}")


async def test_supervisor_registers_starts_stops_and_restarts_without_duplicates() -> None:
    supervisor = WorkerSupervisor()
    started = 0
    stopped = 0
    release = asyncio.Event()

    async def runner() -> None:
        nonlocal started, stopped
        started += 1
        try:
            await release.wait()
        finally:
            stopped += 1

    supervisor.register("deletion", runner)
    assert supervisor.snapshot()[0].state == "pending"
    with pytest.raises(DomainError) as caught:
        supervisor.register("deletion", runner)
    assert caught.value.code is ErrorCode.INVALID_STATE
    with pytest.raises(DomainError):
        supervisor.register("", runner)

    await supervisor.start()
    await supervisor.start()
    await asyncio.sleep(0)
    assert started == 1
    assert supervisor.readiness_errors() == []
    with pytest.raises(DomainError):
        supervisor.register("late", runner)

    stop = asyncio.create_task(supervisor.stop())
    await asyncio.sleep(0)
    assert stopped == 0
    release.set()
    await stop
    assert stopped == 1
    assert supervisor.snapshot()[0].state == "stopped"
    await supervisor.stop()

    release.clear()
    await supervisor.start()
    await asyncio.sleep(0)
    assert started == 2
    release.set()
    await supervisor.stop()
    assert stopped == 2


async def test_required_worker_failure_is_visible_to_readiness() -> None:
    supervisor = WorkerSupervisor()

    async def fail() -> None:
        raise RuntimeError("worker failure")

    supervisor.register("history", fail)
    await supervisor.start()
    await wait_for_worker_state(supervisor, "history", "failed")

    snapshot = supervisor.snapshot()[0]
    assert snapshot.required is True
    assert snapshot.error_type == "RuntimeError"
    assert supervisor.readiness_errors() == ["后台任务 history 未正常运行"]
    health = HealthService(
        database_check=lambda: asyncio.sleep(0, result=True),
        directory_check=lambda: True,
        supervisor=supervisor,
    )
    assert await health.errors() == ["后台任务 history 未正常运行"]
    assert not await health.is_ready()
    await supervisor.stop()
    assert supervisor.readiness_errors() == ["后台任务 history 未正常运行"]


async def test_normal_exit_and_optional_failure_have_distinct_readiness_semantics() -> None:
    supervisor = WorkerSupervisor()

    async def return_immediately() -> None:
        return None

    async def fail() -> None:
        raise ValueError("optional failure")

    supervisor.register("tracker", return_immediately, required=False)
    supervisor.register("optional-history", fail, required=False)
    await supervisor.start()
    await wait_for_worker_state(supervisor, "tracker", "failed")
    await wait_for_worker_state(supervisor, "optional-history", "failed")

    statuses = {item.name: item for item in supervisor.snapshot()}
    assert statuses["tracker"].error_type == "WorkerExited"
    assert statuses["optional-history"].error_type == "ValueError"
    assert supervisor.readiness_errors() == []
    await supervisor.stop()


async def test_external_cancellation_marks_worker_stopped_not_healthy() -> None:
    supervisor = WorkerSupervisor()

    async def wait_forever() -> None:
        await asyncio.Event().wait()

    supervisor.register("sync", wait_forever)
    await supervisor.start()
    await asyncio.sleep(0)
    task = next(
        task
        for task in asyncio.all_tasks()
        if task.get_name() == "aria2deck:sync"
    )
    task.cancel()
    await wait_for_worker_state(supervisor, "sync", "stopped")

    assert supervisor.snapshot()[0].error_type is None
    assert supervisor.readiness_errors() == ["后台任务 sync 未正常运行"]
    await supervisor.stop()


async def test_health_aggregates_database_and_directory_failures() -> None:
    supervisor = WorkerSupervisor()
    health = HealthService(
        database_check=lambda: asyncio.sleep(0, result=False),
        directory_check=lambda: False,
        supervisor=supervisor,
    )

    assert await health.errors() == ["数据库不可用", "下载目录不可用"]
    assert not await health.is_ready()

    healthy = HealthService(
        database_check=lambda: asyncio.sleep(0, result=True),
        directory_check=lambda: True,
        supervisor=supervisor,
    )
    assert await healthy.errors() == []
    assert await healthy.is_ready()
