from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Literal, Protocol

from ...domain.content import ContentEntryIdentity, ContentKind
from ...domain.errors import DomainError, ErrorCode
from ..ports import UnitOfWork
from .models import CanonicalEntry, ContentRecord, StorageDeleteOutcome

logger = logging.getLogger(__name__)

MAX_PENDING = 1000
UowFactory = Callable[[], AbstractAsyncContextManager[UnitOfWork]]
ClockMs = Callable[[], int]
ProgressCallback = Callable[[int], None]


class QueueState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class AdmissionCancelled(Exception):
    """校验被「停止处理」中断：由进度回调从工作线程抛出，worker 退回排队。"""


@dataclass(slots=True)
class QueueEntry:
    storage_path: str
    kind: ContentKind
    digest: str
    size: int
    enqueued_at_ms: int
    state: QueueState = QueueState.QUEUED
    started_at_ms: int | None = None
    finished_at_ms: int | None = None
    processed_bytes: int = 0
    content_id: int | None = None
    error_code: str | None = None
    message: str | None = None


@dataclass(frozen=True, slots=True)
class QueueSnapshot:
    running: bool
    items: tuple[QueueEntry, ...]
    queued_count: int
    running_count: int
    done_count: int
    failed_count: int
    pending_bytes: int


@dataclass(frozen=True, slots=True)
class EnqueueOutcome:
    storage_path: str
    accepted: bool
    state: QueueState | None = None
    error_code: str | None = None
    message: str | None = None


@dataclass(frozen=True, slots=True)
class DeleteOutcome:
    storage_path: str
    accepted: bool
    mode: Literal["immediate", "deferred"] | None = None
    freed_bytes: int = 0
    error_code: str | None = None
    message: str | None = None


@dataclass(frozen=True, slots=True)
class DiskOrphanPage:
    items: tuple[CanonicalEntry, ...]
    total: int
    page: int
    page_size: int
    unrecognized_count: int


def _snapshot_order(entry: QueueEntry) -> tuple[int, int]:
    if entry.state is QueueState.RUNNING:
        return (0, 0)
    if entry.state is QueueState.QUEUED:
        return (1, entry.enqueued_at_ms)
    return (2, -(entry.finished_at_ms or 0))


class DiskAdmissionQueue:
    """入库队列：进程内状态机，先进先出，一次处理一条。

    shortcut: 进程内内存队列，服务重启即清空（V2 无迁移能力，不落表）；
    需要重启后续跑时升级为持久化队列。
    """

    def __init__(self, *, max_pending: int = MAX_PENDING) -> None:
        self.max_pending = max_pending
        self._items: dict[str, QueueEntry] = {}
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> None:
        self._running = True

    def stop(self) -> None:
        self._running = False

    def get(self, storage_path: str) -> QueueEntry | None:
        return self._items.get(storage_path)

    def enqueue(self, entries: list[QueueEntry]) -> list[EnqueueOutcome]:
        outcomes: list[EnqueueOutcome] = []
        queued = sum(1 for item in self._items.values() if item.state is QueueState.QUEUED)
        for entry in entries:
            existing = self._items.get(entry.storage_path)
            if existing is not None and existing.state is not QueueState.FAILED:
                outcomes.append(EnqueueOutcome(entry.storage_path, True, existing.state))
                continue
            if queued >= self.max_pending:
                outcomes.append(
                    EnqueueOutcome(
                        entry.storage_path,
                        False,
                        error_code=ErrorCode.INVALID_STATE.value,
                        message=f"入库队列已满（上限 {self.max_pending} 条），请先处理或删除已有条目",
                    )
                )
                continue
            if existing is not None:
                del self._items[entry.storage_path]  # 失败项重新排队，按新加入顺序排到队尾
            self._items[entry.storage_path] = entry
            queued += 1
            outcomes.append(EnqueueOutcome(entry.storage_path, True, QueueState.QUEUED))
        return outcomes

    def claim_next(self, *, now_ms: int) -> QueueEntry | None:
        for entry in self._items.values():
            if entry.state is not QueueState.QUEUED:
                continue
            entry.state = QueueState.RUNNING
            entry.started_at_ms = now_ms
            entry.processed_bytes = 0
            entry.error_code = None
            entry.message = None
            return entry
        return None

    def update_progress(self, storage_path: str, processed_bytes: int) -> None:
        entry = self._items.get(storage_path)
        if entry is None or entry.state is not QueueState.RUNNING:
            return
        entry.processed_bytes = processed_bytes
        if not self._running:
            raise AdmissionCancelled

    def mark_done(self, storage_path: str, *, content_id: int, now_ms: int) -> None:
        entry = self._items[storage_path]
        entry.state = QueueState.DONE
        entry.content_id = content_id
        entry.finished_at_ms = now_ms
        entry.error_code = None
        entry.message = None

    def mark_failed(self, storage_path: str, *, code: str, message: str, now_ms: int) -> None:
        entry = self._items[storage_path]
        entry.state = QueueState.FAILED
        entry.finished_at_ms = now_ms
        entry.error_code = code
        entry.message = message

    def release(self, storage_path: str) -> None:
        entry = self._items[storage_path]
        entry.state = QueueState.QUEUED
        entry.started_at_ms = None
        entry.processed_bytes = 0

    def remove(self, storage_path: str) -> QueueEntry | None:
        return self._items.pop(storage_path, None)

    def snapshot(self) -> QueueSnapshot:
        items = list(self._items.values())
        counts = {state: 0 for state in QueueState}
        for item in items:
            counts[item.state] += 1
        return QueueSnapshot(
            running=self._running,
            items=tuple(replace(item) for item in sorted(items, key=_snapshot_order)),
            queued_count=counts[QueueState.QUEUED],
            running_count=counts[QueueState.RUNNING],
            done_count=counts[QueueState.DONE],
            failed_count=counts[QueueState.FAILED],
            pending_bytes=sum(
                item.size for item in items if item.state is QueueState.QUEUED
            ),
        )


class DiskOrphanRepository(Protocol):
    async def find_by_storage_path(self, storage_path: str) -> ContentRecord | None: ...

    async def list_all_storage_paths(self) -> set[str]: ...


class DiskOrphanStore(Protocol):
    async def list_canonical_entries(self) -> tuple[list[CanonicalEntry], int]: ...

    async def stat_canonical(self, storage_path: str) -> CanonicalEntry: ...

    async def physical_delete(self, storage_path: str) -> None: ...


class VerifiedDiskStore(DiskOrphanStore, Protocol):
    async def verify_canonical(
        self, storage_path: str, *, on_progress: ProgressCallback | None = None
    ) -> tuple[CanonicalEntry, list[ContentEntryIdentity]]: ...


class OrphanDeleteCommands(Protocol):
    async def storage_delete_orphans(
        self, content_ids: list[int]
    ) -> list[StorageDeleteOutcome]: ...


RepositoryFactory = Callable[[UnitOfWork], DiskOrphanRepository]


class DiskOrphanService:
    """磁盘未登记文件的清单、入队与删除分流。"""

    def __init__(
        self,
        *,
        store: VerifiedDiskStore,
        uow_factory: UowFactory,
        repository_factory: RepositoryFactory,
        queue: DiskAdmissionQueue,
        library_commands: OrphanDeleteCommands,
        clock_ms: ClockMs,
    ) -> None:
        self.store = store
        self.uow_factory = uow_factory
        self.repository_factory = repository_factory
        self.queue = queue
        self.library_commands = library_commands
        self.clock_ms = clock_ms

    async def scan(self, *, search: str | None, page: int, page_size: int) -> DiskOrphanPage:
        entries, unrecognized = await self.store.list_canonical_entries()
        async with self.uow_factory() as uow:
            registered = await self.repository_factory(uow).list_all_storage_paths()
        term = (search or "").strip().casefold()
        orphans = [
            entry
            for entry in entries
            if entry.storage_path not in registered
            and (not term or term in entry.storage_path.casefold())
        ]
        orphans.sort(key=lambda entry: (-entry.size, entry.storage_path))
        start = (page - 1) * page_size
        return DiskOrphanPage(
            items=tuple(orphans[start : start + page_size]),
            total=len(orphans),
            page=page,
            page_size=page_size,
            unrecognized_count=unrecognized,
        )

    async def enqueue(self, storage_paths: list[str]) -> list[EnqueueOutcome]:
        now_ms = self.clock_ms()
        prepared: list[QueueEntry] = []
        rejected: dict[int, EnqueueOutcome] = {}
        for index, storage_path in enumerate(storage_paths):
            try:
                measured = await self.store.stat_canonical(storage_path)
            except DomainError as error:
                rejected[index] = EnqueueOutcome(
                    storage_path, False, error_code=error.code.value, message=str(error)
                )
                continue
            prepared.append(
                QueueEntry(
                    storage_path=measured.storage_path,
                    kind=measured.kind,
                    digest=measured.digest,
                    size=measured.size,
                    enqueued_at_ms=now_ms,
                )
            )
        accepted = iter(self.queue.enqueue(prepared))
        outcomes = [
            rejected[index] if index in rejected else next(accepted)
            for index in range(len(storage_paths))
        ]
        for outcome in outcomes:
            logger.info(
                "加入入库队列 storage_path=%s accepted=%s state=%s",
                outcome.storage_path,
                outcome.accepted,
                outcome.state,
            )
        return outcomes

    async def delete(self, storage_paths: list[str]) -> list[DeleteOutcome]:
        outcomes = [await self._delete_one(storage_path) for storage_path in storage_paths]
        for outcome in outcomes:
            logger.info(
                "删除未登记文件 storage_path=%s accepted=%s mode=%s freed_bytes=%s",
                outcome.storage_path,
                outcome.accepted,
                outcome.mode,
                outcome.freed_bytes,
            )
        return outcomes

    def start(self) -> QueueSnapshot:
        self.queue.start()
        logger.info("开始处理入库队列")
        return self.queue.snapshot()

    def stop(self) -> QueueSnapshot:
        self.queue.stop()
        logger.info("停止处理入库队列")
        return self.queue.snapshot()

    def snapshot(self) -> QueueSnapshot:
        return self.queue.snapshot()

    async def _delete_one(self, storage_path: str) -> DeleteOutcome:
        entry = self.queue.get(storage_path)
        if entry is not None and entry.state is QueueState.RUNNING:
            return DeleteOutcome(
                storage_path,
                False,
                error_code=ErrorCode.CONTENT_BUSY.value,
                message="正在校验中，请先停止处理",
            )
        if entry is not None and entry.state is QueueState.DONE and entry.content_id is not None:
            return await self._deferred_delete(storage_path, entry.content_id)
        return await self._immediate_delete(storage_path)

    async def _deferred_delete(self, storage_path: str, content_id: int) -> DeleteOutcome:
        outcome = (await self.library_commands.storage_delete_orphans([content_id]))[0]
        if outcome.accepted:
            self.queue.remove(storage_path)
            return DeleteOutcome(storage_path, True, mode="deferred")
        if outcome.error_code == ErrorCode.NOT_FOUND.value:
            # 登记行已被别处删除：条目退回未登记分支，磁盘字节仍由本流程清理。
            return await self._immediate_delete(storage_path)
        return DeleteOutcome(
            storage_path, False, error_code=outcome.error_code, message=outcome.message
        )

    async def _immediate_delete(self, storage_path: str) -> DeleteOutcome:
        try:
            measured = await self.store.stat_canonical(storage_path)
        except DomainError as error:
            if error.code is ErrorCode.NOT_FOUND:
                self.queue.remove(storage_path)  # 磁盘上已不存在，队列记录没有意义
            return DeleteOutcome(
                storage_path, False, error_code=error.code.value, message=str(error)
            )
        try:
            # 事务内复核「仍无登记行」，防止判定与删字节之间被并发登记插队。
            async with self.uow_factory() as uow:
                repository = self.repository_factory(uow)
                if await repository.find_by_storage_path(storage_path) is not None:
                    raise DomainError(
                        ErrorCode.CONTENT_BUSY, "该路径已登记入库，请在存储管理页删除"
                    )
                await self.store.physical_delete(storage_path)
        except DomainError as error:
            return DeleteOutcome(
                storage_path, False, error_code=error.code.value, message=str(error)
            )
        self.queue.remove(storage_path)
        return DeleteOutcome(storage_path, True, mode="immediate", freed_bytes=measured.size)
