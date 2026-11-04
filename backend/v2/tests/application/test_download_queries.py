from __future__ import annotations

from types import TracebackType

import pytest

from aria2deck.application.downloads.models import (
    DownloadObservation,
    DownloadRecord,
    TaskPage,
    TaskStats,
)
from aria2deck.application.downloads.projection import TaskProjector
from aria2deck.application.downloads.queries import DownloadQueryService
from aria2deck.domain.downloads import AttemptStatus, SubscriptionStatus
from aria2deck.domain.errors import DomainError, ErrorCode
from aria2deck.domain.quota import Quota, available_user_bytes
from aria2deck.infrastructure.observations import InMemoryDownloadObservations


class FakeUoW:
    @property
    def connection(self) -> object:
        return object()

    async def __aenter__(self) -> FakeUoW:
        return self

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None


class ReadRepository:
    def __init__(self, records: list[DownloadRecord]) -> None:
        self.records = records

    async def list_tasks(self, *, user_id: int) -> list[DownloadRecord]:
        assert user_id == 7
        return self.records

    async def get_task(self, *, user_id: int, task_id: int) -> DownloadRecord:
        assert user_id == 7
        for record in self.records:
            if record.id == task_id:
                return record
        raise DomainError(ErrorCode.NOT_FOUND, "任务不存在")


class QuotaRepository:
    async def get_quota(self, user_id: int) -> Quota:
        assert user_id == 7
        return Quota(quota_bytes=1000, used_bytes=200, reserved_bytes=300)


def record(
    task_id: int,
    name: str,
    status: SubscriptionStatus,
    *,
    display_uri: str | None = None,
) -> DownloadRecord:
    return DownloadRecord(
        id=task_id,
        attempt_id=task_id,
        gid=f"gid-{task_id}",
        subscription_status=status,
        attempt_status=AttemptStatus.ACTIVE,
        name=name,
        display_uri=display_uri,
        total_bytes=100,
        completed_bytes=25,
        reserved_bytes=50,
        error_code=None,
        created_at_ms=task_id,
        updated_at_ms=task_id,
        finished_at_ms=None,
    )


def service(records: list[DownloadRecord]) -> DownloadQueryService:
    observations = InMemoryDownloadObservations()
    observations.put(
        DownloadObservation(
            gid="gid-1",
            observed_at_ms=99_000,
            status="active",
            download_speed=100,
            upload_speed=10,
            peer_count=2,
            connection_count=1,
        )
    )
    return DownloadQueryService(
        uow_factory=FakeUoW,
        repository_factory=lambda _uow: ReadRepository(records),
        account_repository_factory=lambda _uow: QuotaRepository(),
        projector=TaskProjector(observations=observations, clock_ms=lambda: 100_000),
    )


async def test_query_service_owns_filter_search_and_page_boundaries() -> None:
    queries = service(
        [
            record(1, "Alpha", SubscriptionStatus.ACTIVE),
            record(
                2,
                "Beta",
                SubscriptionStatus.PAUSED,
                display_uri="https://example.com/ARCHIVE",
            ),
            record(3, "Gamma", SubscriptionStatus.CANCELLED),
        ]
    )

    first = await queries.list_tasks(
        user_id=7, status=None, search="", page=1, page_size=2
    )
    assert isinstance(first, TaskPage)
    assert [task.id for task in first.items] == [1, 2]
    assert first.total == 3
    second = await queries.list_tasks(
        user_id=7, status=None, search="", page=2, page_size=2
    )
    assert [task.id for task in second.items] == [3]
    paused = await queries.list_tasks(
        user_id=7, status=SubscriptionStatus.PAUSED, search="", page=1, page_size=20
    )
    assert [task.id for task in paused.items] == [2]
    by_name = await queries.list_tasks(
        user_id=7, status=None, search=" alpha ", page=1, page_size=20
    )
    assert [task.id for task in by_name.items] == [1]
    by_uri = await queries.list_tasks(
        user_id=7, status=None, search="archive", page=1, page_size=20
    )
    assert [task.id for task in by_uri.items] == [2]
    missing = await queries.list_tasks(
        user_id=7, status=None, search="missing", page=1, page_size=20
    )
    assert missing.items == []


async def test_query_service_detail_and_stats_use_the_same_projection() -> None:
    queries = service(
        [
            record(1, "Alpha", SubscriptionStatus.ACTIVE),
            record(2, "Beta", SubscriptionStatus.PAUSED),
            record(3, "Gamma", SubscriptionStatus.CANCELLED),
        ]
    )

    detail = await queries.get_task(user_id=7, task_id=1)
    stats = await queries.stats(user_id=7)

    assert detail.download_speed == 100
    assert stats.counts[SubscriptionStatus.ACTIVE] == 1
    assert stats.counts[SubscriptionStatus.PAUSED] == 1
    assert stats.counts[SubscriptionStatus.CANCELLED] == 1
    assert stats.counts[SubscriptionStatus.COMPLETED] == 0
    assert stats.total == 3
    assert stats.download_speed == 100
    assert stats.upload_speed == 10
    assert isinstance(stats, TaskStats)
    assert available_user_bytes(stats.quota) == 500
    with pytest.raises(DomainError) as caught:
        await queries.get_task(user_id=7, task_id=999)
    assert caught.value.code is ErrorCode.NOT_FOUND
