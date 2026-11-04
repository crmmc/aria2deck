from __future__ import annotations

import pytest

from aria2deck.application.downloads.models import BackendSubmissionResult, SourceRecord, Submission
from aria2deck.application.downloads.recovery import DownloadRecoveryService, RecoveryReport
from aria2deck.domain.errors import DomainError, ErrorCode
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
        self.confirmed_stops: list[str] = []

    async def list_recovery_submissions(self) -> list[Submission]:
        return list(self.submissions)

    async def list_uncertain_stops(self) -> list[str]:
        return list(self.stops)

    async def mark_submitted(self, attempt_id: int) -> None:
        self.submitted.append(attempt_id)

    async def mark_uncertain(self, attempt_id: int) -> None:
        self.uncertain.append(attempt_id)

    async def fail_attempt(self, attempt_id: int, *, error_code: str, now_ms: int) -> None:
        self.failed.append(attempt_id)

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
