from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import select

from aria2deck.application.downloads.broadcast import TaskBroadcast
from aria2deck.application.downloads.models import (
    AttemptRecord,
    BackendSubmissionResult,
    ManifestEntry,
    SourceRecord,
    Submission,
    TaskProjection,
)
from aria2deck.application.downloads.recovery import DownloadRecoveryService, RecoveryReport
from aria2deck.domain.downloads import AttemptStatus, SubscriptionStatus
from aria2deck.domain.errors import DomainError, ErrorCode
from aria2deck.infrastructure.sqlite.repositories.downloads import DownloadRepository
from aria2deck.infrastructure.sqlite.schema import content_objects, user_files
from aria2deck.infrastructure.sqlite.unit_of_work import SQLiteUnitOfWork
from tests.application.test_download_lifecycle import (
    make_lifecycle,
    manifest_files,
    observation,
    put_download,
    read,
    runtime,
)
from tests.download_fakes import FakeSubmissionBackend, FakeTrackers


def submission(gid: str = "0000000000000001") -> Submission:
    return Submission(
        1,
        gid,
        SourceRecord(1, "magnet", "magnet:key", {"uri": "magnet:?xt=urn:btih:" + "ab" * 20}, {}, None, "file", None, None),
    )


class Repository:
    def __init__(self, submissions: list[Submission], stops: list[str] | None = None) -> None:
        self.submissions = submissions
        self.stops = stops or []
        self.submitted: list[int] = []
        self.uncertain: list[int] = []
        self.failed: list[int] = []
        self.failure_codes: list[str] = []
        self.failure_gids: list[str | None] = []
        self.confirmed_stops: list[str] = []
        self.attempts = {
            item.attempt_id: AttemptRecord(
                id=item.attempt_id,
                source_id=item.source.id,
                resource_key=item.source.resource_key,
                gid=item.gid,
                status=AttemptStatus.QUEUED,
                completed_content_id=None,
                submission_state="uncertain",
                total_length=None,
                completed_length=0,
                physical_reserved_bytes=0,
                created_at_ms=item.created_at_ms,
            )
            for item in submissions
        }

    async def list_recovery_submissions(self) -> list[Submission]:
        return list(self.submissions)

    async def list_uncertain_stops(self) -> list[str]:
        return list(self.stops)

    async def mark_submitted(self, attempt_id: int) -> None:
        self.submitted.append(attempt_id)

    async def mark_uncertain(self, attempt_id: int) -> None:
        self.uncertain.append(attempt_id)

    async def get_attempt(self, attempt_id: int) -> AttemptRecord:
        return self.attempts[attempt_id]

    async def fail_attempt(
        self,
        attempt_id: int,
        *,
        error_code: str,
        now_ms: int,
        expected_gid: str | None = None,
    ) -> None:
        self.failed.append(attempt_id)
        self.failure_codes.append(error_code)
        self.failure_gids.append(expected_gid)

    async def confirm_stopped_gid(self, gid: str) -> None:
        self.confirmed_stops.append(gid)

    async def list_quota_user_ids(self) -> list[int]:
        return [7]


def service(
    repository: Repository,
    backend: FakeSubmissionBackend,
    *,
    lifecycle: RecordingLifecycle | None = None,
    quota: RecordingQuota | None = None,
) -> DownloadRecoveryService:
    return DownloadRecoveryService(
        uow_factory=lambda: UoW(),
        repository_factory=lambda _uow: repository,
        backend=backend,
        trackers=FakeTrackers(("udp://tracker.example:80",)),
        clock_ms=lambda: 100,
        lifecycle=lifecycle,
        quota_repository_factory=(lambda _uow: quota) if quota is not None else None,
    )


class UoW:
    @property
    def connection(self) -> object:
        return object()

    async def __aenter__(self) -> UoW:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


async def test_empty_recovery_is_a_noop() -> None:
    backend = FakeSubmissionBackend()
    result = await service(Repository([]), backend).recover()
    assert result == RecoveryReport()
    assert backend.submit_calls == []


async def test_existing_gid_is_confirmed_without_resubmission() -> None:
    repository = Repository([submission()])
    backend = FakeSubmissionBackend()
    result = await service(repository, backend).recover()
    assert result.recovered == 1
    assert repository.submitted == [1]
    assert backend.submit_calls == []


@pytest.mark.parametrize("failure", ["count", "gid", "confirm", "transport"])
async def test_unconfirmed_recovery_keeps_candidate_uncertain(failure: str) -> None:
    repository = Repository([submission()])
    backend = FakeSubmissionBackend()
    backend.confirm_default = False
    if failure == "count":
        backend.replies = []
    elif failure == "gid":
        backend.replies = [BackendSubmissionResult(True, gid="wrong")]
    elif failure == "transport":
        backend.confirm_error = DomainError(ErrorCode.SUBMISSION_UNCERTAIN, "offline")
    result = await service(repository, backend).recover()
    assert result.uncertain == 1
    assert repository.uncertain == [1]
    assert repository.submitted == []


async def test_definite_recovery_rejection_is_failed_not_retried() -> None:
    repository = Repository([submission()])
    backend = FakeSubmissionBackend()
    backend.confirm_default = False
    backend.reject_indexes = {0}
    result = await service(repository, backend).recover()
    assert result.failed == 1
    assert repository.failed == [1]
    assert repository.failure_gids == ["0000000000000001"]
    assert repository.uncertain == []


async def test_recovery_stop_success_and_failure_are_counted() -> None:
    backend = FakeSubmissionBackend()
    repository = Repository([], ["abc"])
    result = await service(repository, backend).recover()
    assert result.stopped == 1
    assert repository.confirmed_stops == ["abc"]

    backend = FakeSubmissionBackend()
    backend.stop_error = DomainError(ErrorCode.INVALID_STATE, "offline")
    result = await service(Repository([], ["abc"]), backend).recover()
    assert result.uncertain == 1


async def test_candidates_are_frozen_before_external_side_effects() -> None:
    first = submission()
    repository = Repository([first])
    backend = FakeSubmissionBackend()
    backend.confirm_default = False
    backend.mark_confirmed_on_submit = True
    original = backend.submit_many

    async def submit_and_add(submissions: list[Submission], *, trackers: tuple[str, ...]) -> list[BackendSubmissionResult]:
        repository.submissions.append(submission("0000000000000002"))
        return await original(submissions, trackers=trackers)

    backend.submit_many = submit_and_add
    result = await service(repository, backend).recover()
    assert result.recovered == 1
    assert len(backend.submit_calls) == 1


class RecordingLifecycle:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.ingestion_ok = True
        self.reconcile_ok = True
        self.cleanup_ok = True
        self.purge_error = False

    async def broadcast_attempt(self, attempt_id: int) -> None:
        self.events.append(f"broadcast:{attempt_id}")

    async def recover_ingestions(self) -> bool:
        self.events.append("ingestions")
        return self.ingestion_ok

    async def reconcile_live(self) -> bool:
        self.events.append("live")
        return self.reconcile_ok

    async def cleanup_terminal(self) -> bool:
        self.events.append("cleanup")
        return self.cleanup_ok

    async def purge_orphans(self) -> None:
        self.events.append("orphans")
        if self.purge_error:
            raise DomainError(ErrorCode.RPC_UNAVAILABLE, "offline")


class RecordingQuota:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def rebuild(self, user_id: int) -> None:
        self.events.append(f"quota:{user_id}")


async def test_full_recovery_orders_repair_accounting_cleanup_before_orphans() -> None:
    events: list[str] = []
    lifecycle = RecordingLifecycle(events)
    quota = RecordingQuota(events)
    result = await service(Repository([]), FakeSubmissionBackend(), lifecycle=lifecycle, quota=quota).recover()
    assert result == RecoveryReport()
    assert events == ["ingestions", "live", "quota:7", "cleanup", "orphans"]


@pytest.mark.parametrize("failure", ["ingestion", "live", "cleanup", "stop"])
async def test_unconfirmed_recovery_never_purges_orphans(failure: str) -> None:
    events: list[str] = []
    lifecycle = RecordingLifecycle(events)
    backend = FakeSubmissionBackend()
    repository = Repository([], ["abc"] if failure == "stop" else [])
    if failure == "ingestion":
        lifecycle.ingestion_ok = False
    elif failure == "live":
        lifecycle.reconcile_ok = False
    elif failure == "cleanup":
        lifecycle.cleanup_ok = False
    else:
        backend.stop_error = DomainError(ErrorCode.RPC_UNAVAILABLE, "offline")
    await service(repository, backend, lifecycle=lifecycle).recover()
    assert "orphans" not in events


async def test_orphan_enumeration_failure_is_non_destructive_and_does_not_abort_startup() -> None:
    events: list[str] = []
    lifecycle = RecordingLifecycle(events)
    lifecycle.purge_error = True
    result = await service(Repository([]), FakeSubmissionBackend(), lifecycle=lifecycle).recover()
    assert result == RecoveryReport()
    assert events[-1] == "orphans"


async def test_submission_timeout_keeps_stop_uncertain_and_broadcasts_failure() -> None:
    repository = Repository([replace(submission(), created_at_ms=100)])
    backend = FakeSubmissionBackend()
    backend.confirm_error = DomainError(ErrorCode.SUBMISSION_UNCERTAIN, "offline")
    events: list[str] = []
    recovery = service(repository, backend, lifecycle=RecordingLifecycle(events))
    recovery.clock_ms = lambda: 60_100

    result = await recovery.recover_pending()

    assert result.failed == 1
    assert result.uncertain == 1
    assert repository.failed == [1]
    assert repository.failure_codes == ["submission_timeout"]
    assert repository.failure_gids == ["0000000000000001"]
    assert repository.uncertain == [1]
    assert events == ["broadcast:1"]


async def test_submission_retries_do_not_refresh_total_age() -> None:
    repository = Repository([replace(submission(), created_at_ms=100)])
    backend = FakeSubmissionBackend()
    backend.confirm_default = False
    now = [50_100]
    recovery = service(repository, backend)
    recovery.clock_ms = lambda: now[0]

    first = await recovery.recover_pending()
    assert first.uncertain == 1
    assert repository.failed == []

    now[0] = 60_100
    second = await recovery.recover_pending()
    assert second.failed == 1
    assert repository.failure_codes == ["submission_timeout"]
    assert len(backend.submit_calls) == 2


async def test_expired_submission_still_recovers_when_confirmation_succeeds() -> None:
    repository = Repository([replace(submission(), created_at_ms=100)])
    recovery = service(repository, FakeSubmissionBackend())
    recovery.clock_ms = lambda: 60_100

    result = await recovery.recover_pending()

    assert result.recovered == 1
    assert result.failed == 0
    assert repository.submitted == [1]


async def test_pending_worker_never_runs_full_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    recovery = service(
        Repository([]),
        FakeSubmissionBackend(),
        lifecycle=RecordingLifecycle(events),
        quota=RecordingQuota(events),
    )

    async def pending() -> RecoveryReport:
        events.append("pending")
        return RecoveryReport()

    async def full() -> RecoveryReport:
        events.append("full")
        return RecoveryReport()

    async def stop(delay: float) -> None:
        assert delay == 2.0
        raise asyncio.CancelledError

    monkeypatch.setattr(recovery, "recover_pending", pending, raising=False)
    monkeypatch.setattr(recovery, "recover", full)
    monkeypatch.setattr(recovery, "sleep", stop, raising=False)
    with pytest.raises(asyncio.CancelledError):
        await recovery.run_worker()
    assert events == ["pending"]


@pytest.mark.parametrize("failure", ["rejection", "timeout"])
@pytest.mark.parametrize(
    "stale",
    ["cancelled", "handoff", "failed", "completed", "content_intent", "ingestion_intent"],
)
async def test_submission_failure_ignores_stale_or_ingesting_attempt(
    failure: str, stale: str
) -> None:
    candidate = replace(submission(), created_at_ms=100)
    repository = Repository([candidate])
    current = repository.attempts[candidate.attempt_id]
    if stale == "cancelled":
        current = replace(current, status=AttemptStatus.CANCELLED)
    elif stale == "handoff":
        current = replace(current, gid="0000000000000002")
    elif stale == "failed":
        current = replace(current, status=AttemptStatus.FAILED)
    elif stale == "completed":
        current = replace(current, status=AttemptStatus.COMPLETED)
    elif stale == "content_intent":
        current = replace(current, completed_content_id=9)
    else:
        current = replace(current, ingestion_started_at_ms=100)
    repository.attempts[candidate.attempt_id] = current
    backend = FakeSubmissionBackend()
    events: list[str] = []
    recovery = service(repository, backend, lifecycle=RecordingLifecycle(events))
    if failure == "rejection":
        backend.confirm_default = False
        backend.reject_indexes = {0}
    else:
        backend.confirm_error = DomainError(ErrorCode.SUBMISSION_UNCERTAIN, "offline")
        recovery.clock_ms = lambda: 60_100

    result = await recovery.recover_pending()

    assert result.recovered == 0
    assert result.failed == 0
    assert repository.failed == []
    assert events == []


async def test_submission_timeout_defers_to_persisted_ingestion_and_settles_once(
    tmp_path: Path,
) -> None:
    async with runtime(tmp_path) as state:
        put_download(state)
        async with SQLiteUnitOfWork(state.database) as uow:
            downloads = DownloadRepository(uow.connection)
            await downloads.mark_uncertain(state.attempt_id)
            await downloads.start_ingestion(
                state.attempt_id, expected_gid="0123456789abcdef", now_ms=50
            )
            # 完成意图与清单同一事务落库：恢复路径按清单入库，不再扫磁盘。
            await downloads.record_manifest(
                state.attempt_id, entries=(ManifestEntry("file.bin", 3),), now_ms=50
            )
        state.backend.confirm_error = DomainError(
            ErrorCode.SUBMISSION_UNCERTAIN, "offline"
        )
        recovery = DownloadRecoveryService(
            uow_factory=lambda: SQLiteUnitOfWork(state.database),
            repository_factory=lambda uow: DownloadRepository(uow.connection),
            backend=state.backend,
            trackers=FakeTrackers(),
            clock_ms=lambda: 60_100,
            lifecycle=state.lifecycle,
        )

        report = await recovery.recover_pending()

        assert report.failed == 0
        attempt, _, quota = await read(state)
        assert attempt.status is AttemptStatus.QUEUED
        assert attempt.ingestion_started_at_ms == 50
        assert quota.reserved_bytes == 3

        assert await state.lifecycle.recover_ingestions() is True
        assert await state.lifecycle.recover_ingestions() is True
        await state.lifecycle.reconcile(
            observation(
                status="complete", completed=3, files=manifest_files(("file.bin", 3))
            )
        )

        attempt, task, quota = await read(state)
        assert attempt.status is AttemptStatus.COMPLETED
        assert task.subscription_status is SubscriptionStatus.COMPLETED
        assert quota.used_bytes == 3
        assert quota.reserved_bytes == 0
        async with SQLiteUnitOfWork(state.database) as uow:
            assert len((await uow.connection.execute(select(content_objects))).all()) == 1
            assert len((await uow.connection.execute(select(user_files))).all()) == 1


class _FailedProjection:
    """恢复入口用例的终态投影：只验证锁与单帧，不重复真实投影断言。"""

    async def get_task(self, *, user_id: int, task_id: int) -> TaskProjection:
        return TaskProjection(
            id=task_id,
            status=SubscriptionStatus.FAILED,
            status_label="失败",
            name="file.bin",
            display_uri=None,
            total_bytes=3,
            completed_bytes=0,
            download_speed=0,
            upload_speed=0,
            peer_count=0,
            connection_count=0,
            files=(),
            observation_fresh=False,
            reserved_bytes=0,
            error_code="failed_elsewhere",
            retryable=True,
            created_at_ms=1,
            updated_at_ms=1,
            finished_at_ms=1,
        )


async def test_broadcast_entry_waits_for_the_attempt_lock_and_publishes_once(
    tmp_path: Path,
) -> None:
    async with runtime(tmp_path) as state:
        bus = TaskBroadcast()
        sink = bus.subscribe(state.user_id)
        async with SQLiteUnitOfWork(state.database) as uow:
            await DownloadRepository(uow.connection).fail_attempt(
                state.attempt_id, error_code="failed_elsewhere", now_ms=100
            )
        lifecycle = make_lifecycle(state)
        lifecycle.broadcast = bus
        lifecycle.queries = _FailedProjection()
        lock = lifecycle._locks.setdefault(state.attempt_id, asyncio.Lock())
        await lock.acquire()
        started = asyncio.Event()

        async def call() -> None:
            started.set()
            await lifecycle.broadcast_attempt(state.attempt_id)

        task = asyncio.create_task(call())
        await asyncio.wait_for(started.wait(), timeout=1)
        await asyncio.sleep(0.05)
        assert not task.done(), "公开广播入口必须先取得 attempt 锁"
        lock.release()
        await asyncio.wait_for(task, timeout=2)

        frames = []
        while sink.queue:
            frames.append(sink.queue.popleft())
        assert [(message.task_id, message.status) for message in frames] == [
            (state.task_id, "failed")
        ]

        await lifecycle.broadcast_attempt(state.attempt_id)
        assert list(sink.queue) == [], "去重窗口内再次广播不重复推送"
