from __future__ import annotations

import asyncio
import logging
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

logger = logging.getLogger(__name__)


class PackRepositoryPort(Protocol):
    async def get_job(self, pack_id: int) -> PackJobRecord: ...

    async def count_pending_sources(self, pack_id: int) -> int: ...

    async def mark_cancelled(self, pack_id: int, *, now_ms: int) -> bool: ...

    async def delete_job(self, pack_id: int) -> bool: ...

    async def clear_terminal_records(self, *, user_id: int) -> int: ...


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
ClearWriter = Callable[[int], None]


class PackControlService:
    def __init__(
        self,
        *,
        uow_factory: UowFactory,
        repository_factory: RepositoryFactory,
        content_repository_factory: ContentRepositoryFactory,
        quota_repository_factory: QuotaRepositoryFactory,
        stop_writer: StopAndWait,
        clear_writer: ClearWriter,
        prepared_store: PreparedStorePort,
        clock_ms: ClockMs,
        lease_owner: str,
        lease_ttl_ms: int,
        lock: asyncio.Lock,
    ) -> None:
        self.uow_factory = uow_factory
        self.repository_factory = repository_factory
        self.content_repository_factory = content_repository_factory
        self.quota_repository_factory = quota_repository_factory
        self.stop_writer = stop_writer
        self.clear_writer = clear_writer
        self.prepared_store = prepared_store
        self.clock_ms = clock_ms
        self.lease_owner = lease_owner
        self.lease_ttl_ms = lease_ttl_ms
        self._lock = lock

    async def cancel(self, user_id: int, pack_id: int) -> None:
        # 与账户清理共用同一把锁：归属核对、停止、状态复核、删 part 与结算
        # 必须整体互斥，否则旧请求会跨过账户清理触碰复用 ID 的新任务。
        async with self._lock:
            await self._owned_job(user_id, pack_id)  # 归属校验先于任何停止动作。
            # 先停写入者，再落库取消：关闭中到达的写入不会再推进状态机。
            await self.stop_writer(pack_id)
            await self._require_active(pack_id)
            # 活动任务先删确定的 prepared part：删除失败不提交 cancelled，保留活动状态
            # 与取消请求，下一次取消或账户清理可重试，复用 ID 也不会继承取消。
            await self.prepared_store.remove_prepared(f"packs/prepared-{pack_id}.part")
            await self._settle_cancelled(user_id, pack_id)

    async def _require_active(self, pack_id: int) -> None:
        async with self.uow_factory() as uow:
            job = await self.repository_factory(uow).get_job(pack_id)
        if job.status in {PackStatus.PENDING, PackStatus.PACKING}:
            return
        # 停止期间任务已结束：不释放 0 预留，也不删有效输出。
        self.clear_writer(pack_id)  # 终态任务不再需要控制状态。
        raise DomainError(ErrorCode.INVALID_STATE, "打包任务已结束")

    async def _settle_cancelled(self, user_id: int, pack_id: int) -> None:
        terminal = False
        async with self.uow_factory() as uow:
            fresh = await self.repository_factory(uow).get_job(pack_id)
            if fresh.status not in {PackStatus.PENDING, PackStatus.PACKING}:
                # 删 part 期间任务已结束：状态与账目由对方结算，不重复释放。
                terminal = True
            else:
                if fresh.reserved_bytes:
                    try:
                        await self.quota_repository_factory(uow).release(
                            user_id, fresh.reserved_bytes
                        )
                    except DomainError as error:
                        if error.code is not ErrorCode.QUOTA_EXCEEDED:
                            raise
                        # 账目已损坏：不修补、不重复释放，只清该任务的预留并落终态。
                        logger.error(
                            "打包取消跳过预留释放：配额账目已损坏 pack_id=%s user_id=%s",
                            pack_id,
                            user_id,
                        )
                await self.repository_factory(uow).mark_cancelled(
                    pack_id, now_ms=self.clock_ms()
                )
        self.clear_writer(pack_id)
        if terminal:
            raise DomainError(ErrorCode.INVALID_STATE, "打包任务已结束")

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

    async def clear_records(self, user_id: int) -> int:
        """清空本人全部终态打包记录，保留输出文件引用（A3，旧版 DELETE /api/files/pack 语义）。"""
        async with self.uow_factory() as uow:
            return await self.repository_factory(uow).clear_terminal_records(user_id=user_id)

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
