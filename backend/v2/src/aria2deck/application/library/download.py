from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import IO, Protocol

from ...domain.content import (
    ContentDeletionState,
    ContentKind,
    RangeFallback,
    RangeSpec,
    normalize_relative_path,
    parse_range_header,
)
from ...domain.errors import DomainError, ErrorCode
from ..ports import UnitOfWork
from .leases import ReadLeaseManager
from .models import ContentEntryRecord, DownloadTarget, UserFileRecord

CHUNK_SIZE = 1024 * 1024


class DownloadRepository(Protocol):
    async def get_user_file(self, *, user_id: int, file_id: int) -> UserFileRecord: ...

    async def get_entry(self, *, content_id: int, path: str) -> ContentEntryRecord: ...


class ContentReadStore(Protocol):
    def physical_path(self, storage_path: str, entry_relative_path: str | None = None) -> Path: ...

    async def open_read_handle(
        self, storage_path: str, entry_relative_path: str | None = None
    ) -> IO[bytes]: ...


RepositoryFactory = Callable[[UnitOfWork], DownloadRepository]
UowFactory = Callable[[], AbstractAsyncContextManager[UnitOfWork]]


class LibraryDownloadService:
    def __init__(
        self,
        *,
        uow_factory: UowFactory,
        repository_factory: RepositoryFactory,
        leases: ReadLeaseManager,
        content_store: ContentReadStore,
        chunk_size: int = CHUNK_SIZE,
    ) -> None:
        self.uow_factory = uow_factory
        self.repository_factory = repository_factory
        self.leases = leases
        self.content_store = content_store
        self.chunk_size = chunk_size

    async def open_download(
        self,
        *,
        user_id: int,
        file_id: int,
        path: str | None = None,
        range_header: str | None = None,
    ) -> DownloadTarget:
        async with self.uow_factory() as uow:
            repository = self.repository_factory(uow)
            record = await repository.get_user_file(user_id=user_id, file_id=file_id)
            entry_relative_path: str | None = None
            if path is None:
                if record.kind is not ContentKind.FILE:
                    raise DomainError(ErrorCode.INVALID_INPUT, "目录不能整体下载")
                display_name = record.display_name
                total_size = record.size
            else:
                if record.kind is not ContentKind.DIRECTORY:
                    raise DomainError(ErrorCode.INVALID_INPUT, "文件不包含子路径")
                entry = await repository.get_entry(
                    content_id=record.content_id, path=normalize_relative_path(path)
                )
                if entry.entry_kind is not ContentKind.FILE:
                    raise DomainError(ErrorCode.INVALID_INPUT, "目录条目不能整体下载")
                entry_relative_path = entry.path
                display_name = entry.name
                total_size = entry.size
            if record.deletion_state is not ContentDeletionState.ACTIVE:
                raise DomainError(ErrorCode.CONTENT_PENDING_DELETE, "内容正在删除，不能下载")
            range_spec = self._resolve_range(range_header, total_size)
        await self.leases.acquire(record.content_id)
        return DownloadTarget(
            content_id=record.content_id,
            storage_path=record.storage_path,
            entry_relative_path=entry_relative_path,
            physical_path=self.content_store.physical_path(
                record.storage_path, entry_relative_path
            ),
            display_name=display_name,
            total_size=total_size,
            range_spec=range_spec,
        )

    async def iter_chunks(self, target: DownloadTarget) -> AsyncIterator[bytes]:
        handle: IO[bytes] | None = None
        try:
            # 打开句柄也在租约保护内：打开失败同样必须释放租约，否则内容会被残留租约阻塞删除。
            handle = await self.content_store.open_read_handle(
                target.storage_path, target.entry_relative_path
            )
            if target.range_spec is not None:
                await asyncio.to_thread(handle.seek, target.range_spec.start)
                remaining = target.range_spec.length
                while remaining > 0:
                    chunk = await asyncio.to_thread(
                        handle.read, min(self.chunk_size, remaining)
                    )
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk
            else:
                while True:
                    chunk = await asyncio.to_thread(handle.read, self.chunk_size)
                    if not chunk:
                        break
                    yield chunk
        finally:
            if handle is not None:
                await asyncio.to_thread(handle.close)
            # 租约释放只走响应层 releases=（ASGI finally 覆盖正常/异常/断开）；
            # 不在生成器 finally 里释放：生成器关闭上下文里的连接获取会悬挂，
            # 且其先置 released 标志会让响应层释放被跳过（实测读租约只增不减）。

    async def release(self, target: DownloadTarget) -> None:
        """Exactly-once release：唯一调用方是响应层 releases=（ASGI finally）。"""
        if target.released:
            return
        target.released = True
        await self.leases.release(target.content_id)

    @staticmethod
    def _resolve_range(range_header: str | None, total_size: int) -> RangeSpec | None:
        if range_header is None:
            return None
        decision = parse_range_header(range_header, total_size)
        if isinstance(decision, RangeFallback):
            return None
        return decision
