from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from typing import Protocol

from ...domain.content import plan_reference_removal
from ...domain.errors import DomainError
from ...domain.packs import SourceCleanupDecision, plan_source_cleanup
from ...domain.quota import Quota
from ..library.models import UserFileRecord
from ..ports import UnitOfWork
from .models import (
    PackAdmissionSource,
    PackJobRecord,
    PackSourceRecord,
    SourceCleanupOutcome,
)

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 1.0


class PackRepositoryPort(Protocol):
    async def list_cleanup_pending_jobs(self) -> list[PackJobRecord]: ...

    async def list_sources(self, pack_id: int) -> list[PackSourceRecord]: ...

    async def count_pending_sources(self, pack_id: int) -> int: ...

    async def set_source_cleanup(
        self, source_id: int, *, status: str, error_code: str | None = None
    ) -> bool: ...

    async def finish_source_cleanup(self, pack_id: int, *, now_ms: int) -> bool: ...

    async def list_admission_sources(
        self, *, user_id: int, user_file_ids: list[int]
    ) -> list[PackAdmissionSource]: ...


class ContentRepositoryPort(Protocol):
    async def get_user_file(self, *, user_id: int, file_id: int) -> UserFileRecord: ...

    async def count_references(self, content_id: int) -> int: ...

    async def has_pending_pack_sources(self, content_id: int) -> bool: ...

    async def revoke_shares_for_user_file(self, *, user_file_id: int, now_ms: int) -> int: ...

    async def remove_user_file(self, *, user_id: int, file_id: int) -> int: ...

    async def claim_orphan_delete(
        self, content_id: int, *, now_ms: int, lease_owner: str, lease_ttl_ms: int
    ) -> bool: ...


class QuotaRepositoryPort(Protocol):
    async def rebuild(self, user_id: int) -> Quota: ...


RepositoryFactory = Callable[[UnitOfWork], PackRepositoryPort]
ContentRepositoryFactory = Callable[[UnitOfWork], ContentRepositoryPort]
QuotaRepositoryFactory = Callable[[UnitOfWork], QuotaRepositoryPort]
UowFactory = Callable[[], AbstractAsyncContextManager[UnitOfWork]]
ClockMs = Callable[[], int]
Sleep = Callable[[float], Awaitable[object]]


class SourceCleanupWorker:
    """Deletes pack sources after completion, keyed on snapshot identity checks."""

    def __init__(
        self,
        *,
        uow_factory: UowFactory,
        repository_factory: RepositoryFactory,
        content_repository_factory: ContentRepositoryFactory,
        quota_repository_factory: QuotaRepositoryFactory,
        clock_ms: ClockMs,
        lease_owner: str,
        lease_ttl_ms: int,
        sleep: Sleep = asyncio.sleep,
        poll_interval_seconds: float = POLL_INTERVAL_SECONDS,
    ) -> None:
        self.uow_factory = uow_factory
        self.repository_factory = repository_factory
        self.content_repository_factory = content_repository_factory
        self.quota_repository_factory = quota_repository_factory
        self.clock_ms = clock_ms
        self.lease_owner = lease_owner
        self.lease_ttl_ms = lease_ttl_ms
        self.sleep = sleep
        self.poll_interval_seconds = poll_interval_seconds

    async def run_worker(self) -> None:
        while True:
            await self.step_once()
            await self.sleep(self.poll_interval_seconds)

    async def step_once(self) -> list[SourceCleanupOutcome]:
        async with self.uow_factory() as uow:
            jobs = await self.repository_factory(uow).list_cleanup_pending_jobs()
        outcomes: list[SourceCleanupOutcome] = []
        for job in jobs:
            outcomes.extend(await self._cleanup_job(job))
        return outcomes

    async def _cleanup_job(self, job: PackJobRecord) -> list[SourceCleanupOutcome]:
        async with self.uow_factory() as uow:
            sources = await self.repository_factory(uow).list_sources(job.id)
        outcomes: list[SourceCleanupOutcome] = []
        for source in sources:
            if source.cleanup_status != "pending":
                continue
            try:
                outcomes.append(await self._process_source(job, source))
            except DomainError as error:
                # 保留 pending 状态等待下一轮重试（例如其他打包仍占用最后引用）。
                logger.warning(
                    "来源清理暂缓 source_id=%s error_code=%s", source.id, error.code
                )
                outcomes.append(
                    SourceCleanupOutcome(
                        source_id=source.id,
                        pack_id=job.id,
                        decision=None,
                        cleanup_status="pending",
                        error_code=str(error.code),
                    )
                )
        async with self.uow_factory() as uow:
            repository = self.repository_factory(uow)
            if await repository.count_pending_sources(job.id) == 0:
                await repository.finish_source_cleanup(job.id, now_ms=self.clock_ms())
        return outcomes

    async def _process_source(self, job: PackJobRecord, source: PackSourceRecord) -> SourceCleanupOutcome:
        now_ms = self.clock_ms()
        async with self.uow_factory() as uow:
            pack_repository = self.repository_factory(uow)
            current_rows = await pack_repository.list_admission_sources(
                user_id=job.owner_id, user_file_ids=[source.snapshot.user_file_id]
            )
            current = current_rows[0] if current_rows else None
            decision = plan_source_cleanup(
                source.snapshot,
                None if current is None else current.snapshot(),
                job.output_content_key or "",
            )
            if decision is SourceCleanupDecision.DELETE:
                # 同一事务：先把本来源标为 deleted（解除自身对引用删除的阻塞），
                # 再走切片①的引用删除语义；任一步失败整体回滚，下一轮重试。
                await pack_repository.set_source_cleanup(source.id, status="deleted")
                await self._remove_reference(uow, job.owner_id, source.snapshot.user_file_id, now_ms)
            elif decision is SourceCleanupDecision.KEEP:
                await pack_repository.set_source_cleanup(source.id, status="kept")
            else:
                await pack_repository.set_source_cleanup(
                    source.id, status="deleted", error_code="already_missing"
                )
        return SourceCleanupOutcome(
            source_id=source.id,
            pack_id=job.id,
            decision=decision,
            cleanup_status="deleted" if decision is not SourceCleanupDecision.KEEP else "kept",
        )

    async def _remove_reference(
        self, uow: UnitOfWork, user_id: int, file_id: int, now_ms: int
    ) -> None:
        content = self.content_repository_factory(uow)
        record = await content.get_user_file(user_id=user_id, file_id=file_id)
        decision = plan_reference_removal(
            reference_count=await content.count_references(record.content_id),
            pending_pack_sources=await content.has_pending_pack_sources(record.content_id),
        )
        # 先撤销分享再删行：user_files 外键 SET NULL 会让事后定位失效。
        await content.revoke_shares_for_user_file(user_file_id=file_id, now_ms=now_ms)
        await content.remove_user_file(user_id=user_id, file_id=file_id)
        await self.quota_repository_factory(uow).rebuild(user_id)
        if decision.schedule_physical_delete:
            await content.claim_orphan_delete(
                record.content_id,
                now_ms=now_ms,
                lease_owner=self.lease_owner,
                lease_ttl_ms=self.lease_ttl_ms,
            )
