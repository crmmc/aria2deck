from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Protocol

from ..ports import UnitOfWork


class PackCleanupRepository(Protocol):
    async def list_active_jobs(self, user_id: int) -> list[int]: ...

    async def cleanup_user(self, *, user_id: int, now_ms: int) -> None: ...


class PackControlPort(Protocol):
    async def stop_and_wait(self, pack_id: int) -> None: ...


RepositoryFactory = Callable[[UnitOfWork], PackCleanupRepository]
UowFactory = Callable[[], AbstractAsyncContextManager[UnitOfWork]]


class PackUserCleanup:
    def __init__(
        self,
        *,
        uow_factory: UowFactory,
        repository_factory: RepositoryFactory,
        control: PackControlPort,
        clock_ms: Callable[[], int],
    ) -> None:
        self.uow_factory = uow_factory
        self.repository_factory = repository_factory
        self.control = control
        self.clock_ms = clock_ms

    async def cleanup_user(self, user_id: int) -> None:
        async with self.uow_factory() as uow:
            pack_ids = await self.repository_factory(uow).list_active_jobs(user_id)
        for pack_id in pack_ids:
            await self.control.stop_and_wait(pack_id)
        async with self.uow_factory() as uow:
            await self.repository_factory(uow).cleanup_user(
                user_id=user_id, now_ms=self.clock_ms()
            )
