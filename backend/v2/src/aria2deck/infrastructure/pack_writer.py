from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass

from ..domain.errors import DomainError, ErrorCode

DEFAULT_STOP_TIMEOUT_SECONDS = 30.0


@dataclass(slots=True)
class _PackWriterRecord:
    """某 pack 在本进程内的控制状态；取消请求黏住到 clear 为止。"""

    cancel_requested: bool = False
    writer: asyncio.Task[None] | None = None
    cancel_event: threading.Event | None = None
    stop_wait: asyncio.Task[bool] | None = None


class PackWriterControl:
    """每 pack 写入任务、协作取消与有上限停止等待的生命周期。"""

    def __init__(self, stop_timeout_seconds: float = DEFAULT_STOP_TIMEOUT_SECONDS) -> None:
        self._stop_timeout_seconds = stop_timeout_seconds
        self._records: dict[int, _PackWriterRecord] = {}

    def register(
        self, pack_id: int, writer: asyncio.Task[None], cancel_event: threading.Event
    ) -> bool:
        record = self._records.get(pack_id)
        if record is None:
            record = _PackWriterRecord()
            self._records[pack_id] = record
        elif record.writer is not None and not record.writer.done():
            raise DomainError(ErrorCode.INVALID_STATE, "打包写入者仍在运行")
        record.writer = writer
        record.cancel_event = cancel_event
        # 旧 writer 的等待不得污染替换者。
        record.stop_wait = None
        if not record.cancel_requested:
            return True
        cancel_event.set()
        return False

    async def stop_and_wait(self, pack_id: int) -> None:
        record = self._records.get(pack_id)
        if record is None:
            # 尚无 writer 也先记录，关闭 PENDING→claim 后注册的窗口。
            self._records[pack_id] = _PackWriterRecord(cancel_requested=True)
            return
        record.cancel_requested = True
        if record.cancel_event is not None:
            record.cancel_event.set()
        writer = record.writer
        if writer is None or writer.done():
            return
        wait = record.stop_wait
        if wait is None or wait.done():
            # 超时后的显式重试开启新的有上限等待，不缓存上一次结果。
            wait = asyncio.create_task(self._await_writer(writer))
            record.stop_wait = wait
        # 并发调用共享同一截止窗口；等待者被取消不得取消共享等待。
        if not await asyncio.shield(wait):
            raise DomainError(ErrorCode.RECOVERY_UNSAFE, "打包写入者停止超时")

    async def wait_for_writer(self, pack_id: int, writer: asyncio.Task[None]) -> None:
        """只观察：共享进行中的等待，不新开等待，也不重复请求取消。"""
        record = self._records.get(pack_id)
        if record is None or record.writer is not writer:
            return
        if not record.cancel_requested:
            if writer.done() and self._records.get(pack_id) is record:
                del self._records[pack_id]
            return
        wait = record.stop_wait
        if wait is not None and not wait.done():
            await asyncio.shield(wait)

    def clear(self, pack_id: int) -> None:
        record = self._records.get(pack_id)
        if record is None:
            return
        if record.writer is not None and not record.writer.done():
            raise DomainError(ErrorCode.INVALID_STATE, "打包写入者仍在运行")
        del self._records[pack_id]

    def allows_resume(self, pack_id: int) -> bool:
        return pack_id not in self._records

    async def _await_writer(self, writer: asyncio.Task[None]) -> bool:
        done, _ = await asyncio.wait({writer}, timeout=self._stop_timeout_seconds)
        return bool(done)
