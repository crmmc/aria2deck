from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from typing import Protocol

from ...domain.content import ContentDeletionState, require_physical_deletable
from ...domain.errors import DomainError
from ..ports import UnitOfWork
from .models import DeletionCandidate

logger = logging.getLogger(__name__)

BACKOFF_SECONDS = (1, 5, 30, 120, 600)
POLL_INTERVAL_SECONDS = 1.0


class DeletionRepository(Protocol):
    async def list_deletion_candidates(self, *, owner: str, now_ms: int) -> list[DeletionCandidate]: ...

    async def acquire_or_reclaim(
        self, content_id: int, *, now_ms: int, lease_owner: str, lease_ttl_ms: int
    ) -> bool: ...

    async def write_tombstone(self, content_id: int, *, owner: str, now_ms: int) -> bool: ...

    async def finalize_tombstoned(self, content_id: int) -> bool: ...


class PhysicalDeleteStore(Protocol):
    async def physical_delete(self, storage_path: str) -> None: ...


RepositoryFactory = Callable[[UnitOfWork], DeletionRepository]
UowFactory = Callable[[], AbstractAsyncContextManager[UnitOfWork]]
ClockMs = Callable[[], int]
Sleep = Callable[[float], Awaitable[object]]


class ContentDeletionWorker:
    """Single owner of physical content deletion: claim → tombstone → remove → finalize."""

    def __init__(
        self,
        *,
        uow_factory: UowFactory,
        repository_factory: RepositoryFactory,
        content_store: PhysicalDeleteStore,
        clock_ms: ClockMs,
        sleep: Sleep = asyncio.sleep,
        owner: str = "content-deletion",
        lease_ttl_ms: int = 300_000,
        poll_interval_seconds: float = POLL_INTERVAL_SECONDS,
        backoff_seconds: tuple[int, ...] = BACKOFF_SECONDS,
    ) -> None:
        self.uow_factory = uow_factory
        self.repository_factory = repository_factory
        self.content_store = content_store
        self.clock_ms = clock_ms
        self.sleep = sleep
        self.owner = owner
        self.lease_ttl_ms = lease_ttl_ms
        self.poll_interval_seconds = poll_interval_seconds
        self.backoff_seconds = backoff_seconds
        self.failure_counts: dict[int, int] = {}
        self.next_attempt_ms: dict[int, int] = {}

    async def run_worker(self) -> None:
        while True:
            await self.step_once()
            await self.sleep(self.poll_interval_seconds)

    async def step_once(self) -> None:
        now_ms = self.clock_ms()
        async with self.uow_factory() as uow:
            candidates = await self.repository_factory(uow).list_deletion_candidates(
                owner=self.owner, now_ms=now_ms
            )
        for candidate in candidates:
            if self.next_attempt_ms.get(candidate.content_id, 0) > now_ms:
                continue
            try:
                await self._process(candidate)
            except Exception as error:
                self._record_failure(candidate.content_id, error)
            else:
                self.failure_counts.pop(candidate.content_id, None)
                self.next_attempt_ms.pop(candidate.content_id, None)

    def _record_failure(self, content_id: int, error: Exception) -> None:
        failures = self.failure_counts.get(content_id, 0) + 1
        self.failure_counts[content_id] = failures
        backoff = self.backoff_seconds[min(failures - 1, len(self.backoff_seconds) - 1)]
        self.next_attempt_ms[content_id] = self.clock_ms() + backoff * 1000
        logger.warning(
            "内容物理删除失败 content_id=%s error_type=%s backoff_seconds=%s",
            content_id,
            type(error).__name__,
            backoff,
        )

    async def _process(self, candidate: DeletionCandidate) -> None:
        if candidate.deletion_state is ContentDeletionState.TOMBSTONED:
            await self.content_store.physical_delete(
                candidate.tombstone_path or candidate.storage_path
            )
            async with self.uow_factory() as uow:
                await self.repository_factory(uow).finalize_tombstoned(candidate.content_id)
            return
        try:
            require_physical_deletable(
                candidate.deletion_state, candidate.reference_count, candidate.read_leases
            )
        except DomainError:
            return  # 引用或读租约未排空：本轮跳过，等待下一轮
        now_ms = self.clock_ms()
        async with self.uow_factory() as uow:
            repository = self.repository_factory(uow)
            if not await repository.acquire_or_reclaim(
                candidate.content_id,
                now_ms=now_ms,
                lease_owner=self.owner,
                lease_ttl_ms=self.lease_ttl_ms,
            ):
                return  # 其他执行者持有删除租约
            if not await repository.write_tombstone(
                candidate.content_id, owner=self.owner, now_ms=now_ms
            ):  # pragma: no cover -- 同一事务内 acquire 已复核引用与租约，真实仓储下不可达
                return
        await self.content_store.physical_delete(candidate.storage_path)
        async with self.uow_factory() as uow:
            await self.repository_factory(uow).finalize_tombstoned(candidate.content_id)
