from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from typing import Protocol

from ...domain.content import plan_reference_removal
from ...domain.errors import DomainError, ErrorCode
from ...domain.packs import PackStatus, can_delete_pack
from ...domain.quota import Quota
from ..library.models import UserFileRecord
from ..ports import UnitOfWork
from .models import PackJobRecord


class PackRepositoryPort(Protocol):
    async def get_job(self, pack_id: int) -> PackJobRecord: ...

    async def count_pending_sources(self, pack_id: int) -> int: ...

    async def mark_cancelled(self, pack_id: int, *, now_ms: int) -> bool: ...

    async def delete_job(self, pack_id: int) -> bool: ...


class ContentRepositoryPort(Protocol):
    async def get_user_file(self, *, user_id: int, file_id: int) -> UserFileRecord: ...

    async def find_user_file(self, *, user_id: int, content_id: int) -> int | None: ...

    async def count_references(self, content_id: int) -> int: ...

    async def has_pending_pack_sources(self, content_id: int) -> bool: ...

    async def revoke_shares_for_user_file(self, *, user_file_id: int, now_ms: int) -> int: ...

    async def remove_user_file(self, *, user_id: int, file_id: int) -> int: ...

    async def claim_orphan_delete(
        self, content_id: int, *, now_ms: int, lease_owner: str, lease_ttl_ms: int
    ) -> bool: ...


class QuotaRepositoryPort(Protocol):
    async def release(self, user_id: int, amount: int) -> Quota: ...

    async def rebuild(self, user_id: int) -> Quota: ...


class WriterControlPort(Protocol):
    async def stop_and_wait(self, pack_id: int) -> None: ...


class PreparedStorePort(Protocol):
    async def remove_prepared(self, storage_path: str) -> None: ...


RepositoryFactory = Callable[[UnitOfWork], PackRepositoryPort]
ContentRepositoryFactory = Callable[[UnitOfWork], ContentRepositoryPort]
QuotaRepositoryFactory = Callable[[UnitOfWork], QuotaRepositoryPort]
UowFactory = Callable[[], AbstractAsyncContextManager[UnitOfWork]]
ClockMs = Callable[[], int]
StopAndWait = Callable[[int], Awaitable[None]]


class PackControlService:
    def __init__(
        self,
        *,
        uow_factory: UowFactory,
        repository_factory: RepositoryFactory,
        content_repository_factory: ContentRepositoryFactory,
        quota_repository_factory: QuotaRepositoryFactory,
        stop_writer: StopAndWait,
        prepared_store: PreparedStorePort,
        clock_ms: ClockMs,
        lease_owner: str,
        lease_ttl_ms: int,
    ) -> None:
        self.uow_factory = uow_factory
        self.repository_factory = repository_factory
        self.content_repository_factory = content_repository_factory
        self.quota_repository_factory = quota_repository_factory
        self.stop_writer = stop_writer
        self.prepared_store = prepared_store
        self.clock_ms = clock_ms
        self.lease_owner = lease_owner
        self.lease_ttl_ms = lease_ttl_ms

    async def cancel(self, user_id: int, pack_id: int) -> None:
        job = await self._owned_job(user_id, pack_id)
        if job.status not in {PackStatus.PENDING, PackStatus.PACKING}:
            raise DomainError(ErrorCode.INVALID_STATE, "只有进行中的打包任务可以取消")
        # 先停写入者，再落库取消：关闭中到达的写入不会再推进状态机。
        await self.stop_writer(pack_id)
        async with self.uow_factory() as uow:
            fresh = await self.repository_factory(uow).get_job(pack_id)
            if fresh.status not in {PackStatus.PENDING, PackStatus.PACKING}:
                raise DomainError(ErrorCode.INVALID_STATE, "打包任务已结束")
            if fresh.reserved_bytes:
                await self.quota_repository_factory(uow).release(user_id, fresh.reserved_bytes)
            await self.repository_factory(uow).mark_cancelled(pack_id, now_ms=self.clock_ms())
        await self.prepared_store.remove_prepared(f"packs/prepared-{pack_id}.part")

    async def delete_pack(self, user_id: int, pack_id: int) -> None:
        """Delete the job record together with the output file reference."""
        job = await self._owned_job(user_id, pack_id)
        await self._ensure_deletable(job)
        if job.output_content_id is not None:
            await self._remove_reference(user_id, job.output_content_id)
        async with self.uow_factory() as uow:
            await self.repository_factory(uow).delete_job(pack_id)

    async def clear_record(self, user_id: int, pack_id: int) -> None:
        """Delete the job record but keep the output file reference."""
        job = await self._owned_job(user_id, pack_id)
        await self._ensure_deletable(job)
        async with self.uow_factory() as uow:
            await self.repository_factory(uow).delete_job(pack_id)

    async def _owned_job(self, user_id: int, pack_id: int) -> PackJobRecord:
        async with self.uow_factory() as uow:
            job = await self.repository_factory(uow).get_job(pack_id)
        if job.owner_id != user_id:
            raise DomainError(ErrorCode.NOT_FOUND, "打包任务不存在")
        return job

    async def _ensure_deletable(self, job: PackJobRecord) -> None:
        async with self.uow_factory() as uow:
            pending = await self.repository_factory(uow).count_pending_sources(job.id)
        if not can_delete_pack(job.status, pending):
            raise DomainError(ErrorCode.INVALID_STATE, "来源清理未完成或任务仍在进行")

    async def _remove_reference(self, user_id: int, content_id: int) -> None:
        async with self.uow_factory() as uow:
            content = self.content_repository_factory(uow)
            file_id = await content.find_user_file(user_id=user_id, content_id=content_id)
            if file_id is None:
                return
            await content.get_user_file(user_id=user_id, file_id=file_id)  # 归属校验。
            decision = plan_reference_removal(
                reference_count=await content.count_references(content_id),
                pending_pack_sources=await content.has_pending_pack_sources(content_id),
            )
            await content.revoke_shares_for_user_file(
                user_file_id=file_id, now_ms=self.clock_ms()
            )
            await content.remove_user_file(user_id=user_id, file_id=file_id)
            await self.quota_repository_factory(uow).rebuild(user_id)
            if decision.schedule_physical_delete:
                await content.claim_orphan_delete(
                    content_id,
                    now_ms=self.clock_ms(),
                    lease_owner=self.lease_owner,
                    lease_ttl_ms=self.lease_ttl_ms,
                )
