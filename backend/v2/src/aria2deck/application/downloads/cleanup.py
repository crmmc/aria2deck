from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Protocol

from ..ports import UnitOfWork


class DownloadCleanupRepository(Protocol):
    async def list_attempts_to_stop(self, user_id: int) -> list[str]: ...

    async def cleanup_user(self, *, user_id: int, now_ms: int) -> None: ...


class DownloadControlPort(Protocol):
    async def stop(self, *, gid: str) -> None: ...


RepositoryFactory = Callable[[UnitOfWork], DownloadCleanupRepository]
UowFactory = Callable[[], AbstractAsyncContextManager[UnitOfWork]]


class DownloadUserCleanup:
    def __init__(
        self,
        *,
        uow_factory: UowFactory,
        repository_factory: RepositoryFactory,
        control: DownloadControlPort,
        clock_ms: Callable[[], int],
    ) -> None:
        self.uow_factory = uow_factory
        self.repository_factory = repository_factory
        self.control = control
        self.clock_ms = clock_ms

    async def cleanup_user(self, user_id: int) -> None:
        async with self.uow_factory() as uow:
            gids = await self.repository_factory(uow).list_attempts_to_stop(user_id)
        for gid in gids:
            await self.control.stop(gid=gid)
        async with self.uow_factory() as uow:
            await self.repository_factory(uow).cleanup_user(
                user_id=user_id, now_ms=self.clock_ms()
            )
