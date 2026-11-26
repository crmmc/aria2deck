from __future__ import annotations

from types import TracebackType

import pytest

from aria2deck.application.accounts.models import Principal
from aria2deck.application.downloads.history import HistoryService
from aria2deck.application.downloads.models import DownloadRecord, HistoryExpiration
from aria2deck.application.downloads.projection import TaskProjector
from aria2deck.domain.accounts import AccountStatus, CredentialKind, User
from aria2deck.domain.downloads import SubscriptionStatus, effective_task_status
from aria2deck.domain.errors import DomainError, ErrorCode
from aria2deck.infrastructure.observations import InMemoryDownloadObservations
from tests.application.test_download_queries import record


class UoW:
    @property
    def connection(self) -> object:
        return object()

    async def __aenter__(self) -> UoW:
        return self

    async def __aexit__(self, exception_type: type[BaseException] | None, exception: BaseException | None, traceback: TracebackType | None) -> None:
        return None


class Repository:
    def __init__(self, records: list[DownloadRecord]) -> None:
        self.records = records
        self.cutoffs: list[int] = []

    async def list_tasks(self, *, user_id: int) -> list[DownloadRecord]:
        return self.records

    async def page_tasks(
        self,
        *,
        user_id: int,
        statuses: tuple[SubscriptionStatus, ...] | None,
        sort: str,
        order: str,
        page: int,
        page_size: int,
    ) -> tuple[list[DownloadRecord], int]:
        """假件内复刻仓储契约：复合有效态过滤 + updated_at 排序 + 分页。"""
        assert sort == "updated_at" and order == "desc"
        rows = sorted(self.records, key=lambda r: (r.updated_at_ms, r.id), reverse=True)
        if statuses is not None:
            wanted = set(statuses)
            rows = [
                r
                for r in rows
                if effective_task_status(r.subscription_status, r.attempt_status) in wanted
            ]
        start = (page - 1) * page_size
        return rows[start : start + page_size], len(rows)

    async def delete_history(self, *, user_id: int, task_id: int, now_ms: int) -> bool:
        for item in self.records:
            if item.id == task_id:
                if item.subscription_status not in {SubscriptionStatus.COMPLETED, SubscriptionStatus.FAILED, SubscriptionStatus.CANCELLED, SubscriptionStatus.EXPIRED}:
                    raise DomainError(ErrorCode.INVALID_STATE, "进行中")
                self.records.remove(item)
                return True
        raise DomainError(ErrorCode.NOT_FOUND, "不存在")

    async def expire_history(self, *, cutoff_ms: int, now_ms: int) -> HistoryExpiration:
        self.cutoffs.append(cutoff_ms)
        return HistoryExpiration(2, 1)


def make_service(records: list[DownloadRecord], *, retention: int = 30) -> tuple[HistoryService, Repository]:
    repository = Repository(records)
    return HistoryService(
        uow_factory=lambda: UoW(),
        repository_factory=lambda _uow: repository,
        projector=TaskProjector(observations=InMemoryDownloadObservations(), clock_ms=lambda: 1000),
        clock_ms=lambda: 10**12,
        retention_days=lambda: retention,
    ), repository


def principal() -> Principal:
    return Principal(
        User(1, "admin", "admin", "ab" * 32, AccountStatus.ACTIVE, True, 1000, False, False, 1, 1, 1),
        CredentialKind.SESSION,
        "session",
    )


async def test_history_query_filters_searches_and_pages_terminal_records() -> None:
    service, _repository = make_service([
        record(1, "Alpha", SubscriptionStatus.COMPLETED),
        record(2, "Beta", SubscriptionStatus.FAILED, display_uri="https://example.com/ARCHIVE"),
        record(3, "Live", SubscriptionStatus.ACTIVE),
    ])
    page = await service.list_history(user_id=1, status=None, search="", page=1, page_size=1)
    assert page.total == 2
    # 历史默认 updated_at desc：record(2) 的 updated_at_ms 更靠后。
    assert [item.id for item in page.items] == [2]
    second = await service.list_history(user_id=1, status=None, search="", page=2, page_size=1)
    assert [item.id for item in second.items] == [1]
    failed = await service.list_history(user_id=1, status=SubscriptionStatus.FAILED, search="", page=1, page_size=20)
    assert [item.id for item in failed.items] == [2]
    by_uri = await service.list_history(user_id=1, status=None, search="archive", page=1, page_size=20)
    assert [item.id for item in by_uri.items] == [2]
    with pytest.raises(DomainError):
        await service.list_history(user_id=1, status=SubscriptionStatus.ACTIVE, search="", page=1, page_size=20)


async def test_history_delete_validates_batch_and_returns_item_failures() -> None:
    service, _repository = make_service([
        record(1, "Done", SubscriptionStatus.COMPLETED),
        record(2, "Live", SubscriptionStatus.ACTIVE),
    ])
    with pytest.raises(DomainError):
        await service.delete(user_id=1, task_ids=[])
    results = await service.delete(user_id=1, task_ids=[1, 2, 999])
    assert [result.accepted for result in results] == [True, False, False]


async def test_history_worker_expires_before_sleeping(monkeypatch: pytest.MonkeyPatch) -> None:
    service, repository = make_service([])

    async def stop(delay: float) -> None:
        assert delay == 3600
        raise RuntimeError("stop")

    monkeypatch.setattr("aria2deck.application.downloads.history.asyncio.sleep", stop)
    with pytest.raises(RuntimeError, match="stop"):
        await service.run_worker()
    assert len(repository.cutoffs) == 1
