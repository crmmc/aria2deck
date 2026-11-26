from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

from ...domain.content import ContentEntryIdentity, ContentKind
from ...domain.errors import DomainError, ErrorCode
from ..ports import UnitOfWork
from .disk_scan import (
    AdmissionCancelled,
    ClockMs,
    DiskAdmissionQueue,
    ProgressCallback,
    QueueEntry,
    UowFactory,
    VerifiedDiskStore,
)
from .models import CanonicalEntry, ContentRecord

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 1.0
Sleep = Callable[[float], Awaitable[object]]


class AdmissionRepository(Protocol):
    async def find_by_storage_path(self, storage_path: str) -> ContentRecord | None: ...

    async def create_object(
        self, *, kind: str, digest: str, size: int, storage_path: str, now_ms: int
    ) -> int: ...

    async def rebuild_entries(
        self, *, content_id: int, entries: list[ContentEntryIdentity], now_ms: int
    ) -> None: ...


AdmissionRepositoryFactory = Callable[[UnitOfWork], AdmissionRepository]


class DiskAdmissionWorker:
    """从入库队列取一条、校验内容、登记入库、回写状态；一次只处理一条。"""

    def __init__(
        self,
        *,
        queue: DiskAdmissionQueue,
        store: VerifiedDiskStore,
        uow_factory: UowFactory,
        repository_factory: AdmissionRepositoryFactory,
        clock_ms: ClockMs,
        sleep: Sleep = asyncio.sleep,
        poll_interval_seconds: float = POLL_INTERVAL_SECONDS,
    ) -> None:
        self.queue = queue
        self.store = store
        self.uow_factory = uow_factory
        self.repository_factory = repository_factory
        self.clock_ms = clock_ms
        self.sleep = sleep
        self.poll_interval_seconds = poll_interval_seconds

    async def run_worker(self) -> None:
        while True:
            await self.step_once()
            await self.sleep(self.poll_interval_seconds)

    async def step_once(self) -> bool:
        if not self.queue.running:
            return False
        entry = self.queue.claim_next(now_ms=self.clock_ms())
        if entry is None:
            return False
        try:
            measured, entries = await self.store.verify_canonical(
                entry.storage_path, on_progress=self._progress(entry.storage_path)
            )
            content_id = await self._admit(entry, measured, entries)
        except AdmissionCancelled:
            self.queue.release(entry.storage_path)
            logger.info("入库校验已停止 storage_path=%s", entry.storage_path)
            return True
        except DomainError as error:
            self._fail(entry, error.code.value, str(error))
            return True
        except Exception as error:  # 单条异常不得让后台任务退出
            logger.warning(
                "登记入库异常 storage_path=%s error_type=%s",
                entry.storage_path,
                type(error).__name__,
            )
            self._fail(entry, ErrorCode.INVALID_STATE.value, "处理失败，请重新加入队列重试")
            return True
        self.queue.mark_done(entry.storage_path, content_id=content_id, now_ms=self.clock_ms())
        logger.info("登记入库成功 storage_path=%s content_id=%s", entry.storage_path, content_id)
        return True

    def _progress(self, storage_path: str) -> ProgressCallback:
        def report(processed_bytes: int) -> None:
            self.queue.update_progress(storage_path, processed_bytes)

        return report

    def _fail(self, entry: QueueEntry, code: str, message: str) -> None:
        self.queue.mark_failed(
            entry.storage_path, code=code, message=message, now_ms=self.clock_ms()
        )
        logger.warning("登记入库失败 storage_path=%s code=%s", entry.storage_path, code)

    async def _admit(
        self,
        entry: QueueEntry,
        measured: CanonicalEntry,
        entries: list[ContentEntryIdentity],
    ) -> int:
        now_ms = self.clock_ms()
        async with self.uow_factory() as uow:
            repository = self.repository_factory(uow)
            if await repository.find_by_storage_path(entry.storage_path) is not None:
                raise DomainError(ErrorCode.CONTENT_BUSY, "该路径已登记入库")
            # 复核校验期间条目未被外部改动：大小与修改时间都要与读完时一致。
            current = await self.store.stat_canonical(entry.storage_path)
            if (
                current.size != measured.size
                or current.modified_at_ns != measured.modified_at_ns
            ):
                raise DomainError(ErrorCode.INVALID_STATE, "校验期间条目发生变化，请重新扫描")
            content_id = await repository.create_object(
                kind=measured.kind.value,
                digest=measured.digest,
                size=measured.size,
                storage_path=entry.storage_path,
                now_ms=now_ms,
            )
            if measured.kind is ContentKind.DIRECTORY:
                await repository.rebuild_entries(
                    content_id=content_id, entries=entries, now_ms=now_ms
                )
        return content_id
