from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from ...domain.downloads import SubscriptionStatus
from .models import TaskProjection

MAX_QUEUE = 256

TERMINAL_STATUSES = frozenset(
    {
        SubscriptionStatus.COMPLETED,
        SubscriptionStatus.FAILED,
        SubscriptionStatus.CANCELLED,
        SubscriptionStatus.EXPIRED,
    }
)


@dataclass(frozen=True, slots=True)
class BroadcastMessage:
    """One task_update frame for one user; terminal messages end client tracking."""

    type: str
    task_id: int
    status: str
    payload: dict[str, object]


class BroadcastPort(Protocol):
    def publish(self, user_ids: Iterable[int], message: BroadcastMessage) -> None: ...


def task_message(projection: TaskProjection) -> BroadcastMessage:
    """Serialize a TaskProjection exactly like REST TaskResponse (WS-03 同构)."""
    return BroadcastMessage(
        type="task_update",
        task_id=projection.id,
        status=projection.status.value,
        payload={
            "id": projection.id,
            "status": projection.status.value,
            "status_label": projection.status_label,
            "name": projection.name,
            "uri": projection.display_uri,
            "total_bytes": projection.total_bytes,
            "completed_bytes": projection.completed_bytes,
            "download_speed": projection.download_speed,
            "upload_speed": projection.upload_speed,
            "peer_count": projection.peer_count,
            "connection_count": projection.connection_count,
            "files": [
                {
                    "name": file.name,
                    "total_bytes": file.total_bytes,
                    "completed_bytes": file.completed_bytes,
                    "selected": file.selected,
                }
                for file in projection.files
            ],
            "observation_fresh": projection.observation_fresh,
            "reserved_bytes": projection.reserved_bytes,
            "error_code": projection.error_code,
            "retryable": projection.retryable,
            "created_at_ms": projection.created_at_ms,
            "updated_at_ms": projection.updated_at_ms,
            "finished_at_ms": projection.finished_at_ms,
        },
    )


class BroadcastSink:
    """Per-subscriber bounded queue; overflow drops oldest and flags dirty.

    push 可从任意线程调用：消费者所在事件循环由 next() 绑定，
    跨线程投递经 call_soon_threadsafe 保证唤醒安全。
    """

    def __init__(self, capacity: int = MAX_QUEUE) -> None:
        self.capacity = capacity
        self.queue: deque[BroadcastMessage] = deque()
        self.event = asyncio.Event()
        self.dirty = False
        self.closed = False
        self.loop: asyncio.AbstractEventLoop | None = None

    def push(self, message: BroadcastMessage) -> None:
        if self.closed:
            return
        if self.loop is not None and self.loop is not _get_running_loop_or_none():
            self.loop.call_soon_threadsafe(self._push_nowait, message)
            return
        self._push_nowait(message)

    def _push_nowait(self, message: BroadcastMessage) -> None:
        if self.closed:
            return
        self.queue.append(message)
        while len(self.queue) > self.capacity:
            self.queue.popleft()
            self.dirty = True  # 溢出：客户端需要全量补偿（WS-05 前端重读 REST）。
        self.event.set()

    async def next(self) -> BroadcastMessage | None:
        if self.loop is None:
            self.loop = asyncio.get_running_loop()
        while not self.queue:
            if self.closed:
                return None
            self.event.clear()
            await self.event.wait()
        return self.queue.popleft()

    def close(self) -> None:
        self.closed = True
        self.event.set()


class TaskBroadcast:
    """In-process publish/subscribe bus filtered by user id.

    shortcut: 单实例内存权威；多实例部署需替换为共享通道（与 WS 限额同一升级点）。
    """

    def __init__(self) -> None:
        self._subscribers: dict[int, set[BroadcastSink]] = {}

    def subscribe(self, user_id: int) -> BroadcastSink:
        sink = BroadcastSink()
        self._subscribers.setdefault(user_id, set()).add(sink)
        return sink

    def unsubscribe(self, user_id: int, sink: BroadcastSink) -> None:
        sinks = self._subscribers.get(user_id)
        if sinks is None:
            return
        sinks.discard(sink)
        sink.close()
        if not sinks:
            self._subscribers.pop(user_id, None)

    def publish(self, user_ids: Iterable[int], message: BroadcastMessage) -> None:
        for user_id in user_ids:
            for sink in list(self._subscribers.get(user_id, ())):
                sink.push(message)

    def subscriber_count(self, user_id: int) -> int:
        return len(self._subscribers.get(user_id, ()))


def _get_running_loop_or_none() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None
