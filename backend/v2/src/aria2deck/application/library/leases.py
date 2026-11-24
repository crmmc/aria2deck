from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Protocol

from ..ports import UnitOfWork


class ReadLeaseRepository(Protocol):
    async def acquire_read_lease(self, content_id: int) -> None: ...

    async def release_read_lease(self, content_id: int) -> None: ...


RepositoryFactory = Callable[[UnitOfWork], ReadLeaseRepository]
UowFactory = Callable[[], AbstractAsyncContextManager[UnitOfWork]]


class ReadLeaseManager:
    """Process-local in-flight counts mirrored by the database column.

    shortcut: 单实例部署下进程内计数即权威，若未来多实例需改为共享存储租约。
    """

    def __init__(self, *, uow_factory: UowFactory, repository_factory: RepositoryFactory) -> None:
        self.uow_factory = uow_factory
        self.repository_factory = repository_factory
        self._in_flight: dict[int, int] = {}

    async def acquire(self, content_id: int) -> None:
        async with self.uow_factory() as uow:
            await self.repository_factory(uow).acquire_read_lease(content_id)
        self._in_flight[content_id] = self._in_flight.get(content_id, 0) + 1

    async def release(self, content_id: int) -> None:
        async with self.uow_factory() as uow:
            await self.repository_factory(uow).release_read_lease(content_id)
        remaining = self._in_flight.get(content_id, 0) - 1
        if remaining > 0:
            self._in_flight[content_id] = remaining
        else:
            self._in_flight.pop(content_id, None)

    def in_flight(self, content_id: int) -> int:
        return self._in_flight.get(content_id, 0)
