from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Protocol

from ..ports import UnitOfWork


class LibraryCleanupRepository(Protocol):
    async def cleanup_user(self, *, user_id: int, now_ms: int) -> None: ...


RepositoryFactory = Callable[[UnitOfWork], LibraryCleanupRepository]
UowFactory = Callable[[], AbstractAsyncContextManager[UnitOfWork]]


class LibraryUserCleanup:
    def __init__(
        self,
        *,
        uow_factory: UowFactory,
        repository_factory: RepositoryFactory,
        clock_ms: Callable[[], int],
    ) -> None:
        self.uow_factory = uow_factory
        self.repository_factory = repository_factory
        self.clock_ms = clock_ms

    async def cleanup_user(self, user_id: int) -> None:
        async with self.uow_factory() as uow:
            await self.repository_factory(uow).cleanup_user(
                user_id=user_id, now_ms=self.clock_ms()
            )
