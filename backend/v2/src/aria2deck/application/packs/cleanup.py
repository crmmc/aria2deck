from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Protocol

from ..ports import UnitOfWork


class PackCleanupRepository(Protocol):
    async def list_user_job_ids(self, user_id: int) -> list[int]: ...

    async def mark_user_jobs_cancelled(self, *, user_id: int, now_ms: int) -> None: ...

    async def delete_user_jobs_and_rebuild_quota(self, *, user_id: int) -> None: ...


class PackControlPort(Protocol):
    async def stop_and_wait(self, pack_id: int) -> None: ...

    def clear(self, pack_id: int) -> None: ...


class PreparedStorePort(Protocol):
    async def remove_prepared(self, storage_path: str) -> None: ...


RepositoryFactory = Callable[[UnitOfWork], PackCleanupRepository]
UowFactory = Callable[[], AbstractAsyncContextManager[UnitOfWork]]


class PackUserCleanup:
    def __init__(
        self,
        *,
        uow_factory: UowFactory,
        repository_factory: RepositoryFactory,
        control: PackControlPort,
        prepared_store: PreparedStorePort,
        clock_ms: Callable[[], int],
        lock: asyncio.Lock,
    ) -> None:
        self.uow_factory = uow_factory
        self.repository_factory = repository_factory
        self.control = control
        self.prepared_store = prepared_store
        self.clock_ms = clock_ms
        # 与 PackControlService.cancel 共用同一把锁：列 ID、停止、删 part、清控制状态
        # 与删行必须整体互斥，否则旧取消会跨过账户清理触碰复用 ID 的新任务。
        self._lock = lock

    async def cleanup_user(self, user_id: int) -> None:
        async with self._lock:
            await self._cleanup_user_locked(user_id)

    async def _cleanup_user_locked(self, user_id: int) -> None:
        # 获得锁后才列出全部未删除记录（含已 cancelled），否则上一轮的旧 ID 会在本轮
        # 记录已删除、ID 已复用时指向其他用户的新任务。
        async with self.uow_factory() as uow:
            pack_ids = await self.repository_factory(uow).list_user_job_ids(user_id)
        for pack_id in pack_ids:
            await self.control.stop_and_wait(pack_id)
        # 独立提交 cancelled 门闩：此后任何领取都无法再把任务转回 packing。
        async with self.uow_factory() as uow:
            await self.repository_factory(uow).mark_user_jobs_cancelled(
                user_id=user_id, now_ms=self.clock_ms()
            )
        for pack_id in pack_ids:
            await self.prepared_store.remove_prepared(f"packs/prepared-{pack_id}.part")
            self.control.clear(pack_id)
        # 最后才删行并重建配额：前面的物理与控制状态清理失败时记录仍在，下一轮可重试。
        async with self.uow_factory() as uow:
            await self.repository_factory(uow).delete_user_jobs_and_rebuild_quota(user_id=user_id)
