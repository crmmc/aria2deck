from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Protocol

from ...domain.downloads import LIVE_ATTEMPT_STATUSES
from ...domain.errors import DomainError, ErrorCode
from ..ports import UnitOfWork
from .commands import SubmissionBackend, TrackerProvider
from .lifecycle import DownloadLifecycleService
from .models import AttemptRecord, Submission

UowFactory = Callable[[], AbstractAsyncContextManager[UnitOfWork]]
Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    recovered: int = 0
    uncertain: int = 0
    stopped: int = 0
    failed: int = 0


class RecoveryRepository(Protocol):
    async def list_recovery_submissions(self) -> list[Submission]: ...

    async def list_uncertain_stops(self) -> list[str]: ...

    async def get_attempt(self, attempt_id: int) -> AttemptRecord: ...

    async def mark_submitted(self, attempt_id: int) -> None: ...

    async def mark_uncertain(self, attempt_id: int) -> None: ...

    async def fail_attempt(
        self,
        attempt_id: int,
        *,
        error_code: str,
        now_ms: int,
        expected_gid: str | None = None,
    ) -> None: ...

    async def mark_stop_confirmed(self, attempt_id: int) -> None: ...

    async def confirm_stopped_gid(self, gid: str) -> None: ...

    async def list_quota_user_ids(self) -> list[int]: ...


class RecoveryQuotaRepository(Protocol):
    async def rebuild(self, user_id: int) -> object: ...


RepositoryFactory = Callable[[UnitOfWork], RecoveryRepository]
QuotaRepositoryFactory = Callable[[UnitOfWork], RecoveryQuotaRepository]


class DownloadRecoveryService:
    def __init__(
        self,
        *,
        uow_factory: UowFactory,
        repository_factory: RepositoryFactory,
        backend: SubmissionBackend,
        trackers: TrackerProvider,
        clock_ms: Callable[[], int],
        lifecycle: DownloadLifecycleService | None = None,
        quota_repository_factory: QuotaRepositoryFactory | None = None,
        submission_lock: asyncio.Lock | None = None,
        submission_timeout_ms: int = 60_000,
        poll_interval_seconds: float = 2.0,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.uow_factory = uow_factory
        self.repository_factory = repository_factory
        self.backend = backend
        self.trackers = trackers
        self.clock_ms = clock_ms
        self.lifecycle = lifecycle
        self.quota_repository_factory = quota_repository_factory
        self.submission_lock = submission_lock or asyncio.Lock()
        self.submission_timeout_ms = submission_timeout_ms
        self.poll_interval_seconds = poll_interval_seconds
        self.sleep = sleep

    async def _fail_submission(self, submission: Submission, error_code: str) -> bool:
        async with self.uow_factory() as uow:
            repository = self.repository_factory(uow)
            # 当前记录校验与失败释放必须共用同一个 BEGIN IMMEDIATE 事务。
            attempt = await repository.get_attempt(submission.attempt_id)
            if (
                attempt.gid != submission.gid
                or attempt.status not in LIVE_ATTEMPT_STATUSES
                or attempt.completed_content_id is not None
                or attempt.ingestion_started_at_ms is not None
            ):
                return False
            await repository.fail_attempt(
                submission.attempt_id,
                error_code=error_code,
                now_ms=self.clock_ms(),
                expected_gid=submission.gid,
            )
        if self.lifecycle is not None:
            await self.lifecycle.broadcast_attempt(submission.attempt_id)
        return True

    async def recover_pending(self) -> RecoveryReport:
        async with self.submission_lock:
            # 候选和外部提交都在共享锁内，首次提交完成后这里会重新读取状态。
            async with self.uow_factory() as uow:
                repository = self.repository_factory(uow)
                submissions = await repository.list_recovery_submissions()
                stops = await repository.list_uncertain_stops()
            recovered = uncertain = stopped = failed = 0
            for submission in submissions:
                try:
                    if not await self.backend.confirm(gid=submission.gid):
                        replies = await self.backend.submit_many(
                            [submission], trackers=await self.trackers.current()
                        )
                        if (
                            len(replies) != 1
                            or not replies[0].accepted
                            or replies[0].gid != submission.gid
                        ):
                            if replies and not replies[0].accepted:
                                if await self._fail_submission(
                                    submission, replies[0].error_code or "aria2_rejected"
                                ):
                                    failed += 1
                                continue
                            raise DomainError(ErrorCode.SUBMISSION_UNCERTAIN, "恢复提交无法确认")
                        if not await self.backend.confirm(gid=submission.gid):
                            raise DomainError(ErrorCode.SUBMISSION_UNCERTAIN, "恢复提交后无法确认")
                    async with self.uow_factory() as uow:
                        await self.repository_factory(uow).mark_submitted(submission.attempt_id)
                    recovered += 1
                except DomainError:
                    async with self.uow_factory() as uow:
                        await self.repository_factory(uow).mark_uncertain(submission.attempt_id)
                    uncertain += 1
                    if self.clock_ms() - submission.created_at_ms >= self.submission_timeout_ms:
                        if await self._fail_submission(submission, "submission_timeout"):
                            failed += 1
            for gid in stops:
                try:
                    await self.backend.stop(gid=gid)
                except DomainError:
                    uncertain += 1
                    continue
                async with self.uow_factory() as uow:
                    repository = self.repository_factory(uow)
                    # 已取消或失败的 GID 都不是 live 资源，按稳定值确认停止。
                    await repository.confirm_stopped_gid(gid)
                stopped += 1
            return RecoveryReport(recovered, uncertain, stopped, failed)

    async def recover(self) -> RecoveryReport:
        report = await self.recover_pending()
        candidates_complete = report.uncertain == 0
        if self.lifecycle is not None:
            if not await self.lifecycle.recover_ingestions():
                candidates_complete = False
            if await self.lifecycle.reconcile_live() is False:
                candidates_complete = False
        if self.quota_repository_factory is not None:
            async with self.uow_factory() as uow:
                user_ids = await self.repository_factory(uow).list_quota_user_ids()
            for user_id in user_ids:
                async with self.uow_factory() as uow:
                    await self.quota_repository_factory(uow).rebuild(user_id)
        if self.lifecycle is not None:
            if not await self.lifecycle.cleanup_terminal():
                candidates_complete = False
            if candidates_complete:
                try:
                    await self.lifecycle.purge_orphans()
                except DomainError:
                    pass
        return report

    async def run_worker(self) -> None:
        while True:
            await self.recover_pending()
            await self.sleep(self.poll_interval_seconds)
