from __future__ import annotations

import asyncio

import pytest

from aria2deck.application.downloads.broadcast import (
    BroadcastMessage,
    TaskBroadcast,
    task_message,
)

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
    from unittest.mock import AsyncMock

    from aria2deck.application.downloads.lifecycle import DownloadLifecycleService
    from aria2deck.application.downloads.models import TaskProjection
    from aria2deck.domain.downloads import SubscriptionStatus
    from aria2deck.domain.settings import SystemSettings

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
