from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Protocol

from ...domain.accounts import Permission, authorize_admin_session
from ...domain.downloads import (
    TERMINAL_SUBSCRIPTION_STATUSES,
    SubscriptionStatus,
    effective_task_status,
)
from ...domain.errors import DomainError, ErrorCode
from ..accounts.models import Principal
from ..ports import UnitOfWork
from .models import DownloadRecord, DownloadWriteResult, HistoryExpiration, TaskPage
from .projection import TaskProjector

UowFactory = Callable[[], AbstractAsyncContextManager[UnitOfWork]]


class HistoryRepository(Protocol):
    async def list_tasks(self, *, user_id: int) -> list[DownloadRecord]: ...

    async def delete_history(self, *, user_id: int, task_id: int, now_ms: int) -> bool: ...

    async def expire_history(self, *, cutoff_ms: int, now_ms: int) -> HistoryExpiration: ...


RepositoryFactory = Callable[[UnitOfWork], HistoryRepository]


class HistoryService:
    def __init__(
        self,
        *,
        uow_factory: UowFactory,
        repository_factory: RepositoryFactory,
        projector: TaskProjector,
        clock_ms: Callable[[], int],
        retention_days: Callable[[], int],
    ) -> None:
        self.uow_factory = uow_factory
        self.repository_factory = repository_factory
        self.projector = projector
        self.clock_ms = clock_ms
        self.retention_days = retention_days

    async def list_history(
        self,
        *,
        user_id: int,
        status: SubscriptionStatus | None,
        search: str,
        page: int,
        page_size: int,
    ) -> TaskPage:
        if status is not None and status not in TERMINAL_SUBSCRIPTION_STATUSES:
            raise DomainError(ErrorCode.INVALID_INPUT, "历史状态筛选无效")
        async with self.uow_factory() as uow:
            records = await self.repository_factory(uow).list_tasks(user_id=user_id)
        tasks = [
            self.projector.project(record)
            for record in records
            if effective_task_status(record.subscription_status, record.attempt_status)
            in TERMINAL_SUBSCRIPTION_STATUSES
        ]
        if status is not None:
            tasks = [task for task in tasks if task.status is status]
        keyword = search.strip().casefold()
        if keyword:
            tasks = [
                task
                for task in tasks
                if keyword in task.name.casefold()
                or (task.display_uri is not None and keyword in task.display_uri.casefold())
            ]
        start = (page - 1) * page_size
        return TaskPage(tasks[start : start + page_size], len(tasks), page, page_size)

    async def delete(
        self, *, user_id: int, task_ids: list[int]
    ) -> list[DownloadWriteResult]:
        if not 1 <= len(task_ids) <= 1000:
            raise DomainError(ErrorCode.INVALID_INPUT, "每批必须包含 1 到 1000 条历史")
        results: list[DownloadWriteResult] = []
        for task_id in task_ids:
            try:
                async with self.uow_factory() as uow:
                    await self.repository_factory(uow).delete_history(
                        user_id=user_id, task_id=task_id, now_ms=self.clock_ms()
                    )
                results.append(DownloadWriteResult(accepted=True, task_id=task_id))
            except DomainError as error:
                results.append(
                    DownloadWriteResult(
                        accepted=False,
                        task_id=task_id,
                        error_code=error.code.value,
                        message=str(error),
                    )
                )
        return results

    async def expire(self, *, cutoff_ms: int) -> HistoryExpiration:
        async with self.uow_factory() as uow:
            return await self.repository_factory(uow).expire_history(
                cutoff_ms=cutoff_ms, now_ms=self.clock_ms()
            )

    async def purge(
        self,
        principal: Principal,
        *,
        before_ms: int | None,
        older_than_days: int | None,
    ) -> HistoryExpiration:
        authorize_admin_session(principal.user.role, principal.kind, Permission.PURGE_HISTORY)
        if before_ms is not None and older_than_days is not None:
            raise DomainError(ErrorCode.INVALID_INPUT, "历史清理条件只能选择一个")
        if before_ms is None:
            days = self.retention_days() if older_than_days is None else older_than_days
            if days < 1:
                raise DomainError(ErrorCode.INVALID_INPUT, "历史保留天数无效")
            before_ms = self.clock_ms() - days * 86400 * 1000
        if before_ms < 0:
            raise DomainError(ErrorCode.INVALID_INPUT, "历史清理时间无效")
        return await self.expire(cutoff_ms=before_ms)

    async def run_worker(self) -> None:
        while True:
            await self.expire(cutoff_ms=self.clock_ms() - self.retention_days() * 86400 * 1000)
            await asyncio.sleep(3600)
