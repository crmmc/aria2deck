from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Protocol

from ...domain.content import ContentEntryIdentity
from ..ports import UnitOfWork
from .models import ContentRecord, EntryDiff


class ReadModelRepository(Protocol):
    async def get_object(self, content_id: int) -> ContentRecord: ...

    async def verify_entries(
        self, *, content_id: int, entries: list[ContentEntryIdentity]
    ) -> EntryDiff: ...

    async def rebuild_entries(
        self, *, content_id: int, entries: list[ContentEntryIdentity], now_ms: int
    ) -> None: ...


class EntryScanStore(Protocol):
    async def scan_entries(self, storage_path: str) -> list[ContentEntryIdentity]: ...


RepositoryFactory = Callable[[UnitOfWork], ReadModelRepository]
UowFactory = Callable[[], AbstractAsyncContextManager[UnitOfWork]]
ClockMs = Callable[[], int]


class LibraryAdminService:
    """Read-model verification and rebuild against physical scans.

    诊断用途：不暴露公开 API，物理扫描结果即唯一事实来源。
    """

    def __init__(
        self,
        *,
        uow_factory: UowFactory,
        repository_factory: RepositoryFactory,
        content_store: EntryScanStore,
        clock_ms: ClockMs,
    ) -> None:
        self.uow_factory = uow_factory
        self.repository_factory = repository_factory
        self.content_store = content_store
        self.clock_ms = clock_ms

    async def verify_content_entries(self, content_id: int) -> EntryDiff:
        async with self.uow_factory() as uow:
            record = await self.repository_factory(uow).get_object(content_id)
        entries = await self.content_store.scan_entries(record.storage_path)
        async with self.uow_factory() as uow:
            return await self.repository_factory(uow).verify_entries(
                content_id=content_id, entries=entries
            )

    async def rebuild_content_entries(self, content_id: int) -> None:
        async with self.uow_factory() as uow:
            record = await self.repository_factory(uow).get_object(content_id)
        entries = await self.content_store.scan_entries(record.storage_path)
        async with self.uow_factory() as uow:
            await self.repository_factory(uow).rebuild_entries(
                content_id=content_id, entries=entries, now_ms=self.clock_ms()
            )
