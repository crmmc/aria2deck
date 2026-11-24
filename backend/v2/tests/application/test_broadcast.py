from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

from aria2deck.application.downloads.broadcast import (
    BroadcastMessage,
    BroadcastSink,
    TaskBroadcast,
    task_message,
)
from aria2deck.application.downloads.lifecycle import DownloadLifecycleService
from aria2deck.application.downloads.models import TaskProjection
from aria2deck.domain.downloads import SubscriptionStatus
from aria2deck.domain.errors import DomainError, ErrorCode
from aria2deck.domain.settings import SystemSettings

from .test_rpc_projection import TASK


def _message(task_id: int = 1, status: str = "active") -> BroadcastMessage:
    return BroadcastMessage(
        type="task_update", task_id=task_id, status=status, payload={"id": task_id}
    )


async def test_publish_filters_by_user() -> None:
    bus = TaskBroadcast()
    sink_a = bus.subscribe(1)
    sink_b = bus.subscribe(2)
    bus.publish([1], _message())
    assert (await sink_a.next()).task_id == 1
    bus.publish([2], _message(task_id=9))
    assert (await sink_b.next()).task_id == 9
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(sink_a.next(), timeout=0.05)
    bus.unsubscribe(1, sink_a)
    bus.unsubscribe(2, sink_b)
    assert bus.subscriber_count(1) == 0


async def test_bounded_queue_drops_oldest_and_flags_dirty() -> None:
    bus = TaskBroadcast()
    sink = bus.subscribe(7)
    for index in range(300):
        bus.publish([7], _message(task_id=index))
    assert sink.dirty is True
    assert len(sink.queue) == 256
    first = await sink.next()
    assert first.task_id == 44, "最旧的 44 条被丢弃"

    small = bus.subscribe(8)
    small.capacity = 2
    for index in range(5):
        bus.publish([8], _message(task_id=index))
    assert small.dirty is True
    assert [m.task_id for m in small.queue] == [3, 4]


async def test_terminal_messages_and_close_semantics() -> None:
    bus = TaskBroadcast()
    sink = bus.subscribe(3)
    bus.publish([3], _message(status="removed"))
    message = await sink.next()
    assert message.status == "removed"

    sink.close()
    assert await sink.next() is None
    bus.publish([3], _message(task_id=99))  # 关闭后不回流。
    bus.unsubscribe(3, sink)
    assert bus.subscriber_count(3) == 0


def test_task_message_matches_rest_shape() -> None:
    message = task_message(TASK)
    assert message.type == "task_update"
    payload = message.payload
    assert payload["id"] == TASK.id
    assert payload["status"] == TASK.status.value
    assert payload["status_label"] == TASK.status_label
    assert payload["name"] == TASK.name
    assert payload["files"] == [
        {
            "name": file.name,
            "total_bytes": file.total_bytes,
            "completed_bytes": file.completed_bytes,
            "selected": file.selected,
        }
        for file in TASK.files
    ]
    assert set(payload) == {
        "id", "status", "status_label", "name", "uri",
        "total_bytes", "completed_bytes", "download_speed", "upload_speed",
        "peer_count", "connection_count", "files", "observation_fresh",
        "reserved_bytes", "error_code", "retryable",
        "created_at_ms", "updated_at_ms", "finished_at_ms",
    }


async def test_terminal_frame_published_exactly_once() -> None:
    """P2-9：同一尝试反复 reconcile 终态只推送一次（幂等）。"""

    bus = TaskBroadcast()
    sink = bus.subscribe(1)

    class FakeQueries:
        async def get_task(self, *, user_id: int, task_id: int) -> TaskProjection:
            return TaskProjection(
                id=task_id, status=SubscriptionStatus.FAILED, status_label="失败",
                name="t", display_uri=None, total_bytes=1, completed_bytes=0,
                download_speed=0, upload_speed=0, peer_count=0, connection_count=0,
                files=(), observation_fresh=False, reserved_bytes=0,
                error_code="x", retryable=True, created_at_ms=1, updated_at_ms=1,
                finished_at_ms=1,
            )

    class FakeRepo:
        async def list_subscription_owners(self, attempt_id: int):
            return [(attempt_id, 1)]

    class FakeUow:
        def __init__(self, repo): self.repo = repo
        @property
        def connection(self): return self.repo
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None

    repo = FakeRepo()
    lifecycle = DownloadLifecycleService(
        uow_factory=lambda: FakeUow(repo),
        repository_factory=lambda uow: repo,
        content_repository_factory=lambda uow: repo,
        backend=AsyncMock(),
        content_store=AsyncMock(),
        observations=AsyncMock(),
        connectivity=AsyncMock(),
        settings=lambda: SystemSettings(min_free_disk=0),
        clock_ms=lambda: 100,
        broadcast=bus,
        queries=FakeQueries(),
    )
    for _ in range(3):
        await lifecycle._broadcast_attempt(5)
    messages = []
    while sink.queue:
        messages.append(sink.queue.popleft())
    assert len(messages) == 1, "终态帧只推送一次"
    assert messages[0].status == "failed"


def _projection(task_id: int, status: SubscriptionStatus) -> TaskProjection:
    return TaskProjection(
        id=task_id,
        status=status,
        status_label=status.value,
        name=f"task-{task_id}",
        display_uri=None,
        total_bytes=1,
        completed_bytes=0,
        download_speed=0,
        upload_speed=0,
        peer_count=0,
        connection_count=0,
        files=(),
        observation_fresh=False,
        reserved_bytes=0,
        error_code=None,
        retryable=False,
        created_at_ms=1,
        updated_at_ms=1,
        finished_at_ms=1,
    )


class _FakeQueries:
    """按 (user_id, task_id) 返回可变投影，模拟订阅先后进入终态与单项失败。"""

    def __init__(self) -> None:
        self.statuses: dict[tuple[int, int], SubscriptionStatus] = {}
        self.missing: set[tuple[int, int]] = set()
        self.errors: dict[tuple[int, int], DomainError] = {}

    async def get_task(self, *, user_id: int, task_id: int) -> TaskProjection:
        key = (user_id, task_id)
        if key in self.errors:
            raise self.errors[key]
        if key in self.missing:
            raise DomainError(ErrorCode.NOT_FOUND, "任务不存在")
        return _projection(task_id, self.statuses[key])


class _FakeOwners:
    def __init__(self, owners: list[tuple[int, int]]) -> None:
        self.owners = owners

    async def list_subscription_owners(self, attempt_id: int) -> list[tuple[int, int]]:
        return list(self.owners)


class _FakeUow:
    async def __aenter__(self) -> _FakeUow:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


def _broadcasting_lifecycle(
    bus: TaskBroadcast,
    owners: _FakeOwners,
    queries: _FakeQueries,
    *,
    terminal_broadcast_capacity: int | None = None,
) -> DownloadLifecycleService:
    options: dict[str, Any] = {}
    if terminal_broadcast_capacity is not None:
        options["terminal_broadcast_capacity"] = terminal_broadcast_capacity
    return DownloadLifecycleService(
        uow_factory=lambda: _FakeUow(),
        repository_factory=lambda uow: owners,
        content_repository_factory=lambda uow: owners,
        backend=AsyncMock(),
        content_store=AsyncMock(),
        observations=AsyncMock(),
        connectivity=AsyncMock(),
        settings=lambda: SystemSettings(min_free_disk=0),
        clock_ms=lambda: 100,
        broadcast=bus,
        queries=queries,
        **options,
    )


def _frames(sink: BroadcastSink) -> list[tuple[int, str]]:
    frames: list[tuple[int, str]] = []
    while sink.queue:
        message = sink.queue.popleft()
        frames.append((message.task_id, message.status))
    return frames


@pytest.mark.parametrize("subscribers", [2, 3])
async def test_terminal_frame_is_fanned_out_to_every_subscription(subscribers: int) -> None:
    """共享同一 attempt 的每个订阅都收到自己的一次终态帧。"""
    bus = TaskBroadcast()
    owners = _FakeOwners([(index * 10, index) for index in range(1, subscribers + 1)])
    sinks = {index: bus.subscribe(index) for index in range(1, subscribers + 1)}
    queries = _FakeQueries()
    for task_id, user_id in owners.owners:
        queries.statuses[(user_id, task_id)] = SubscriptionStatus.FAILED
    lifecycle = _broadcasting_lifecycle(bus, owners, queries)

    await lifecycle._broadcast_attempt(5)

    expected = {user_id: [(task_id, "failed")] for task_id, user_id in owners.owners}
    assert {user_id: _frames(sink) for user_id, sink in sinks.items()} == expected

    await lifecycle._broadcast_attempt(5)
    assert {user_id: _frames(sink) for user_id, sink in sinks.items()} == {
        user_id: [] for user_id in sinks
    }, "窗口内重复广播不得重复推送"


async def test_later_terminal_subscription_is_not_blocked_by_the_first() -> None:
    bus = TaskBroadcast()
    sink_one = bus.subscribe(1)
    sink_two = bus.subscribe(2)
    owners = _FakeOwners([(11, 1), (12, 2)])
    queries = _FakeQueries()
    queries.statuses[(1, 11)] = SubscriptionStatus.COMPLETED
    queries.statuses[(2, 12)] = SubscriptionStatus.ACTIVE
    lifecycle = _broadcasting_lifecycle(bus, owners, queries)

    await lifecycle._broadcast_attempt(5)
    assert _frames(sink_one) == [(11, "completed")]
    assert _frames(sink_two) == [(12, "active")]

    queries.statuses[(2, 12)] = SubscriptionStatus.COMPLETED
    await lifecycle._broadcast_attempt(5)
    assert _frames(sink_one) == []
    assert _frames(sink_two) == [(12, "completed")]


async def test_cancelled_subscription_does_not_swallow_later_completion() -> None:
    bus = TaskBroadcast()
    sink_one = bus.subscribe(1)
    sink_two = bus.subscribe(2)
    owners = _FakeOwners([(11, 1), (12, 2)])
    queries = _FakeQueries()
    queries.statuses[(1, 11)] = SubscriptionStatus.CANCELLED
    queries.statuses[(2, 12)] = SubscriptionStatus.ACTIVE
    lifecycle = _broadcasting_lifecycle(bus, owners, queries)

    await lifecycle._broadcast_attempt(5)
    assert _frames(sink_one) == [(11, "cancelled")]
    assert _frames(sink_two) == [(12, "active")]

    queries.statuses[(2, 12)] = SubscriptionStatus.COMPLETED
    await lifecycle._broadcast_attempt(5)
    assert _frames(sink_one) == []
    assert _frames(sink_two) == [(12, "completed")], "先取消的订阅不得吞掉另一订阅的完成帧"


async def test_not_found_projection_only_skips_that_subscription() -> None:
    bus = TaskBroadcast()
    sink_one = bus.subscribe(1)
    sink_two = bus.subscribe(2)
    owners = _FakeOwners([(11, 1), (12, 2)])
    queries = _FakeQueries()
    queries.missing.add((1, 11))
    queries.statuses[(2, 12)] = SubscriptionStatus.COMPLETED
    lifecycle = _broadcasting_lifecycle(bus, owners, queries)

    await lifecycle._broadcast_attempt(5)

    assert _frames(sink_one) == []
    assert _frames(sink_two) == [(12, "completed")], "已删除的订阅不得中断其他订阅的广播"


async def test_other_projection_error_keeps_the_existing_outer_boundary() -> None:
    bus = TaskBroadcast()
    sink_one = bus.subscribe(1)
    sink_two = bus.subscribe(2)
    owners = _FakeOwners([(11, 1), (12, 2)])
    queries = _FakeQueries()
    queries.errors[(1, 11)] = DomainError(ErrorCode.INVALID_STATE, "投影失败")
    queries.statuses[(2, 12)] = SubscriptionStatus.COMPLETED
    lifecycle = _broadcasting_lifecycle(bus, owners, queries)

    await lifecycle._broadcast_attempt(5)  # 不向生命周期决策抛错

    assert _frames(sink_one) == []
    assert _frames(sink_two) == [], "非 NOT_FOUND 异常仍按现有外层边界记录"


async def test_terminal_dedup_evicts_oldest_insertion_at_capacity() -> None:
    bus = TaskBroadcast()
    sink = bus.subscribe(1)
    owners = _FakeOwners([(11, 1)])
    queries = _FakeQueries()
    queries.statuses[(1, 11)] = SubscriptionStatus.COMPLETED
    lifecycle = _broadcasting_lifecycle(
        bus, owners, queries, terminal_broadcast_capacity=2
    )

    await lifecycle._broadcast_attempt(1)
    assert _frames(sink) == [(11, "completed")]
    await lifecycle._broadcast_attempt(1)
    assert _frames(sink) == [], "窗口内重复广播不重复推送"

    await lifecycle._broadcast_attempt(2)
    await lifecycle._broadcast_attempt(3)
    assert _frames(sink) == [(11, "completed"), (11, "completed")]
    assert len(lifecycle._terminal_broadcast) == 2, "去重表按容量有界"
    assert list(lifecycle._terminal_broadcast) == [(2, 11), (3, 11)], "按插入顺序保留"

    await lifecycle._broadcast_attempt(3)
    assert _frames(sink) == [], "最近插入的键仍在窗口内"
    await lifecycle._broadcast_attempt(1)
    assert _frames(sink) == [(11, "completed")], "最早的键先被淘汰，重发由前端与 REST 兜底"
