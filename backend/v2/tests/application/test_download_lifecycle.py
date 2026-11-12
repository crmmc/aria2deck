from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import select, text

from aria2deck.application.downloads.lifecycle import DownloadLifecycleService
from aria2deck.application.downloads.models import DownloadObservation
from aria2deck.application.downloads.projection import TaskProjector
from aria2deck.application.system.connectivity import BackendConnectivity
from aria2deck.domain.downloads import (
    ADMISSION_PAUSED,
    EXTERNAL_PAUSED,
    METADATA_ADMISSION_PAUSED,
    AttemptStatus,
    SubscriptionStatus,
)
from aria2deck.domain.errors import DomainError, ErrorCode
from aria2deck.domain.settings import SystemSettings
from aria2deck.infrastructure.content_store import DownloadContentStore
from aria2deck.infrastructure.observations import InMemoryDownloadObservations
from aria2deck.infrastructure.sqlite.bootstrap import initialize_database
from aria2deck.infrastructure.sqlite.engine import SQLiteDatabase
from aria2deck.infrastructure.sqlite.repositories.accounts import AccountRepository
from aria2deck.infrastructure.sqlite.repositories.content import ContentRepository
from aria2deck.infrastructure.sqlite.repositories.downloads import DownloadRepository
from aria2deck.infrastructure.sqlite.repositories.quota import QuotaRepository
from aria2deck.infrastructure.sqlite.schema import content_entries, content_objects, user_files
from aria2deck.infrastructure.sqlite.unit_of_work import SQLiteUnitOfWork
from tests.download_fakes import FakeSubmissionBackend

GID = "0123456789abcdef"
PAYLOAD_GID = "fedcba0987654321"


class LifecycleBackend(FakeSubmissionBackend):
    def __init__(self) -> None:
        super().__init__()
        self.snapshots: dict[str, DownloadObservation] = {}
        self.snapshot_errors: dict[str, DomainError] = {}
        self.unpause_calls: list[str] = []
        self.unpause_error: DomainError | None = None
        self.remove_calls: list[str] = []
        self.remove_error: DomainError | None = None
        self.live_gids: list[str] = []
        self.listen_error: DomainError | None = None
        self.listen_returns = False
        self.resume_status = "active"
        self.resume_missing = False

    async def snapshot(self, *, gid: str) -> DownloadObservation | None:
        if gid in self.snapshot_errors:
            raise self.snapshot_errors[gid]
        return self.snapshots.get(gid)

    async def unpause(self, *, gid: str) -> None:
        self.unpause_calls.append(gid)
        if self.unpause_error is not None:
            raise self.unpause_error
        current = self.snapshots.get(gid)
        if current is not None and current.status == "paused":
            if self.resume_missing:
                self.snapshots.pop(gid)
            else:
                self.snapshots[gid] = observation(
                    gid,
                    self.resume_status,
                    completed=(
                        current.total_length or 0
                        if self.resume_status == "complete"
                        else current.completed_length
                    ),
                )

    async def remove_result(self, *, gid: str) -> None:
        self.remove_calls.append(gid)
        if self.remove_error is not None:
            raise self.remove_error

    async def list_live_gids(self) -> list[str]:
        return list(self.live_gids)

    async def listen(self, on_gid: Callable[[str], Awaitable[None]]) -> None:
        if self.listen_returns:
            return
        raise self.listen_error or DomainError(ErrorCode.RPC_UNAVAILABLE, "offline")


def observation(
    gid: str = GID,
    status: str = "active",
    *,
    total: int | None = 3,
    completed: int = 0,
    followed_by: tuple[str, ...] = (),
) -> DownloadObservation:
    return DownloadObservation(
        gid=gid,
        observed_at_ms=100,
        status=status,
        download_speed=9,
        upload_speed=2,
        peer_count=1,
        connection_count=1,
        total_length=total,
        completed_length=completed,
        followed_by=followed_by,
    )


@dataclass
class Runtime:
    database: SQLiteDatabase
    lifecycle: DownloadLifecycleService
    backend: LifecycleBackend
    observations: InMemoryDownloadObservations
    store: DownloadContentStore
    connectivity: BackendConnectivity
    user_id: int
    attempt_id: int
    task_id: int
    root: Path


def make_lifecycle(runtime: Runtime) -> DownloadLifecycleService:
    return DownloadLifecycleService(
        uow_factory=lambda: SQLiteUnitOfWork(runtime.database),
        repository_factory=lambda uow: DownloadRepository(uow.connection),
        content_repository_factory=lambda uow: ContentRepository(uow.connection),
        backend=runtime.backend,
        content_store=runtime.store,
        observations=runtime.observations,
        connectivity=runtime.connectivity,
        settings=lambda: SystemSettings(min_free_disk=0),
        clock_ms=lambda: 100,
        jitter=lambda: 0.5,
    )


@asynccontextmanager
async def runtime(
    tmp_path: Path,
    *,
    kind: str = "http",
    total: int | None = 3,
    quota: int = 100,
    pause_code: str | None = ADMISSION_PAUSED,
) -> AsyncIterator[Runtime]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    database = SQLiteDatabase(tmp_path / "v2.db")
    await initialize_database(database)
    root = tmp_path / "downloads"
    root.mkdir()
    backend = LifecycleBackend()
    observations = InMemoryDownloadObservations()
    store = DownloadContentStore(root)
    connectivity = BackendConnectivity()
    async with SQLiteUnitOfWork(database) as uow:
        user_id = await AccountRepository(uow.connection).create_user(
            username="admin",
            password_hash="ab" * 32,
            is_admin=True,
            is_initial_password=False,
            quota_bytes=quota,
            now_ms=1,
        )
        downloads = DownloadRepository(uow.connection)
        payload = (
            {"uri": "magnet:?xt=urn:btih:" + "ab" * 20}
            if kind == "magnet"
            else {"uris": ["https://example.com/file.bin"]}
        )
        import json

        source_id = await downloads.create_source(
            kind=kind,
            resource_key=f"{kind}:key",
            payload_json=json.dumps(payload),
            options_json="{}",
            selected_file_indexes_json=None,
            display_name="file.bin",
            display_uri=None,
            now_ms=1,
        )
        reserved = total or 0
        attempt_id = await downloads.create_attempt(
            source_id=source_id,
            resource_key=f"{kind}:key",
            gid=GID,
            total_length=total,
            physical_reserved_bytes=reserved,
            error_code=pause_code,
            now_ms=1,
        )
        await downloads.mark_submitted(attempt_id)
        task_id = await downloads.create_subscription(
            attempt_id=attempt_id,
            user_id=user_id,
            reserved_bytes=reserved,
            display_name="file.bin",
            display_uri=None,
            now_ms=1,
        )
        if reserved:
            await QuotaRepository(uow.connection).reserve(user_id, reserved)
    state = Runtime(
        database, None, backend, observations, store, connectivity, user_id, attempt_id, task_id, root
    )
    state.lifecycle = make_lifecycle(state)
    try:
        yield state
    finally:
        await state.database.close()


async def read(runtime: Runtime) -> tuple[object, object, object]:
    async with SQLiteUnitOfWork(runtime.database) as uow:
        downloads = DownloadRepository(uow.connection)
        return (
            await downloads.get_attempt(runtime.attempt_id),
            await downloads.get_task(user_id=runtime.user_id, task_id=runtime.task_id),
            await QuotaRepository(uow.connection).get(runtime.user_id),
        )


def put_download(runtime: Runtime, content: bytes = b"abc") -> Path:
    directory = runtime.root / str(runtime.attempt_id)
    directory.mkdir()
    (directory / "file.bin").write_bytes(content)
    return directory


async def test_system_pause_resume_uses_shared_projection_and_monotonic_progress(
    tmp_path: Path,
) -> None:
    async with runtime(tmp_path) as state:
        paused = observation(status="paused")
        state.backend.snapshots[GID] = paused
        await state.lifecycle.reconcile(paused)

        attempt, task, quota = await read(state)
        assert attempt.status is AttemptStatus.ACTIVE
        assert attempt.error_code is None
        assert task.subscription_status is SubscriptionStatus.ACTIVE
        assert quota.reserved_bytes == 3
        assert state.backend.unpause_calls == [GID]
        projected = TaskProjector(
            observations=state.observations, clock_ms=lambda: 100
        ).project(task)
        assert projected.download_speed == 9

        await state.lifecycle.reconcile(observation(completed=2))
        await state.lifecycle.reconcile(observation(completed=1))
        attempt, task, _ = await read(state)
        assert attempt.completed_length == 2
        assert task.completed_bytes == 2


async def test_external_pause_is_never_auto_resumed_and_external_resume_is_visible(
    tmp_path: Path,
) -> None:
    async with runtime(tmp_path, pause_code=None) as state:
        await state.lifecycle.reconcile(observation(status="paused"))
        attempt, _, _ = await read(state)
        assert attempt.status is AttemptStatus.PAUSED
        assert attempt.error_code == EXTERNAL_PAUSED
        assert state.backend.unpause_calls == []

        await state.lifecycle.reconcile(observation(status="active"))
        attempt, _, _ = await read(state)
        assert attempt.status is AttemptStatus.ACTIVE
        assert attempt.error_code is None


async def test_rpc_unavailable_discards_old_snapshot_without_writing_old_state(
    tmp_path: Path,
) -> None:
    async with runtime(tmp_path) as state:
        state.observations.put(observation(status="active", completed=2))
        state.backend.unpause_error = DomainError(ErrorCode.RPC_UNAVAILABLE, "offline")
        await state.lifecycle.reconcile(observation(status="paused"))

        attempt, _, _ = await read(state)
        assert attempt.status is AttemptStatus.QUEUED
        assert attempt.completed_length == 0
        assert attempt.error_code == ADMISSION_PAUSED
        assert state.observations.get(GID) is None


async def test_completion_installs_content_and_atomically_completes_quota_and_subscription(
    tmp_path: Path,
) -> None:
    async with runtime(tmp_path) as state:
        source = put_download(state)
        complete = observation(status="complete", completed=3)
        await asyncio.gather(state.lifecycle.reconcile(complete), state.lifecycle.reconcile(complete))

        attempt, task, quota = await read(state)
        assert attempt.status is AttemptStatus.COMPLETED
        assert attempt.completed_content_id is not None
        assert attempt.result_consumed_at_ms == 100
        assert task.subscription_status is SubscriptionStatus.COMPLETED
        assert quota.used_bytes == 3
        assert quota.reserved_bytes == 0
        assert not source.exists()
        assert state.backend.remove_calls == [GID]
        async with SQLiteUnitOfWork(state.database) as uow:
            content = await ContentRepository(uow.connection).get_object(
                attempt.completed_content_id
            )
            assert (state.root / content.storage_path).read_bytes() == b"abc"
            assert await ContentRepository(uow.connection).find_user_file(
                user_id=state.user_id, content_id=content.id
            ) is not None


async def test_completion_failure_rolls_back_references_and_releases_reservation(
    tmp_path: Path,
) -> None:
    async with runtime(tmp_path) as state:
        async with SQLiteUnitOfWork(state.database) as uow:
            content = ContentRepository(uow.connection)
            existing_id = await content.create_object(
                kind="file", digest="cd" * 32, size=1, storage_path="objects/existing", now_ms=1
            )
            await content.add_user_file(
                user_id=state.user_id,
                content_id=existing_id,
                display_name="file.bin",
                now_ms=1,
            )
            await QuotaRepository(uow.connection).complete_download(
                state.user_id, 0, content_size=1, already_owned=False
            )
        put_download(state)
        await state.lifecycle.reconcile(observation(status="complete", completed=3))

        attempt, task, quota = await read(state)
        assert attempt.status is AttemptStatus.FAILED
        assert task.subscription_status is SubscriptionStatus.FAILED
        assert quota.used_bytes == 1
        assert quota.reserved_bytes == 0
        async with SQLiteUnitOfWork(state.database) as uow:
            assert (
                await uow.connection.execute(
                    select(user_files).where(user_files.c.user_id == state.user_id)
                )
            ).all()[0].content_id == existing_id


async def test_completion_wins_a_concurrent_failure_without_double_accounting(
    tmp_path: Path,
) -> None:
    async with runtime(tmp_path) as state:
        put_download(state)
        started = asyncio.Event()
        release = asyncio.Event()
        original_scan = state.store.scan

        async def slow_scan(attempt_id: int):
            started.set()
            await release.wait()
            return await original_scan(attempt_id)

        state.store.scan = slow_scan  # type: ignore[method-assign]
        completing = asyncio.create_task(
            state.lifecycle.reconcile(observation(status="complete", completed=3))
        )
        await started.wait()
        failing = asyncio.create_task(state.lifecycle.reconcile(observation(status="error")))
        release.set()
        await asyncio.gather(completing, failing)

        attempt, _, quota = await read(state)
        assert attempt.status is AttemptStatus.COMPLETED
        assert quota.used_bytes == 3
        assert quota.reserved_bytes == 0


async def test_magnet_handoff_fences_old_gid_and_recovers_payload_queue(tmp_path: Path) -> None:
    async with runtime(tmp_path, kind="magnet", total=None, pause_code=METADATA_ADMISSION_PAUSED) as state:
        state.backend.snapshots[PAYLOAD_GID] = observation(PAYLOAD_GID, "paused")
        parent = observation(status="complete", total=0, followed_by=(PAYLOAD_GID,))
        await state.lifecycle.reconcile(parent)

        attempt, task, _ = await read(state)
        assert attempt.gid == PAYLOAD_GID
        assert attempt.status is AttemptStatus.ACTIVE
        assert attempt.error_code is None
        assert task.subscription_status is SubscriptionStatus.ACTIVE
        assert state.backend.remove_calls == [GID]
        assert state.backend.unpause_calls == [PAYLOAD_GID]

        await state.lifecycle.reconcile(observation(status="error", completed=3))
        attempt, _, _ = await read(state)
        assert attempt.status is AttemptStatus.ACTIVE


async def test_handoff_transport_failure_keeps_recoverable_payload_intent(tmp_path: Path) -> None:
    async with runtime(tmp_path, kind="magnet", total=None, pause_code=METADATA_ADMISSION_PAUSED) as state:
        state.backend.snapshot_errors[PAYLOAD_GID] = DomainError(
            ErrorCode.RPC_UNAVAILABLE, "offline"
        )
        state.observations.put(observation(status="active", total=None))
        await state.lifecycle.reconcile(
            observation(status="complete", total=0, followed_by=(PAYLOAD_GID,))
        )

        attempt, _, _ = await read(state)
        assert attempt.gid == PAYLOAD_GID
        assert attempt.status is AttemptStatus.WAITING
        assert attempt.error_code == ADMISSION_PAUSED
        assert state.observations.get(GID) is None
        assert state.observations.get(PAYLOAD_GID) is None


async def test_invalid_handoff_and_metadata_without_payload_do_not_ingest(tmp_path: Path) -> None:
    async with runtime(tmp_path, kind="magnet", total=None, pause_code=METADATA_ADMISSION_PAUSED) as state:
        with pytest.raises(DomainError) as caught:
            await state.lifecycle.reconcile(
                observation(status="complete", total=0, followed_by=(PAYLOAD_GID, "another"))
            )
        assert caught.value.code is ErrorCode.INVALID_STATE
        attempt, _, _ = await read(state)
        assert attempt.gid == GID

        await state.lifecycle.reconcile(observation(status="complete", total=0))
        attempt, _, _ = await read(state)
        assert attempt.status is AttemptStatus.QUEUED
        assert attempt.completed_content_id is None


async def test_failure_releases_once_and_admission_stop_failure_remains_recoverable(
    tmp_path: Path,
) -> None:
    async with runtime(tmp_path) as state:
        source = put_download(state)
        await state.lifecycle.reconcile(observation(status="error"))
        await state.lifecycle.reconcile(observation(status="error"))
        attempt, _, quota = await read(state)
        assert attempt.status is AttemptStatus.FAILED
        assert attempt.submission_state == "submitted"
        assert quota.reserved_bytes == 0
        assert not source.exists()
        assert state.backend.remove_calls == [GID]

    async with runtime(tmp_path / "stop", total=None, quota=2) as state:
        state.backend.stop_error = DomainError(ErrorCode.RPC_UNAVAILABLE, "offline")
        await state.lifecycle.reconcile(observation(status="active", total=3))
        attempt, _, quota = await read(state)
        assert attempt.status is AttemptStatus.FAILED
        assert attempt.submission_state == "uncertain"
        assert quota.reserved_bytes == 0
        assert state.backend.remove_calls == []


async def test_poll_isolates_item_failure_and_orphan_cleanup_only_stops_unknown_gids(
    tmp_path: Path,
) -> None:
    async with runtime(tmp_path) as state:
        async with SQLiteUnitOfWork(state.database) as uow:
            downloads = DownloadRepository(uow.connection)
            source_id = await downloads.create_source(
                kind="http", resource_key="http:second", payload_json="{}", options_json="{}",
                selected_file_indexes_json=None, display_name="second", display_uri=None, now_ms=2,
            )
            second_id = await downloads.create_attempt(
                source_id=source_id, resource_key="http:second", gid=PAYLOAD_GID,
                total_length=0, now_ms=2, error_code=ADMISSION_PAUSED,
            )
            await downloads.mark_submitted(second_id)
            await downloads.create_subscription(
                attempt_id=second_id, user_id=state.user_id, reserved_bytes=0,
                display_name="second", display_uri=None, now_ms=2,
            )
        state.backend.snapshot_errors[GID] = DomainError(ErrorCode.RPC_UNAVAILABLE, "offline")
        state.backend.snapshots[PAYLOAD_GID] = observation(PAYLOAD_GID, "active", total=0)

        assert await state.lifecycle.reconcile_live() is False
        async with SQLiteUnitOfWork(state.database) as uow:
            second = await DownloadRepository(uow.connection).get_attempt(second_id)
            assert second.status is AttemptStatus.ACTIVE

        state.backend.live_gids = [GID, "orphan"]
        await state.lifecycle.purge_orphans()
        assert state.backend.stop_calls == ["orphan"]


async def test_listener_reconnects_with_backoff_and_polling_worker_is_cancellable(
    tmp_path: Path,
) -> None:
    async with runtime(tmp_path) as state:
        delays: list[float] = []

        async def stop_after_two(delay: float) -> None:
            delays.append(delay)
            if len(delays) == 2:
                raise asyncio.CancelledError

        state.lifecycle.sleep = stop_after_two
        with pytest.raises(asyncio.CancelledError):
            await state.lifecycle.run_listener()
        assert delays == [1.5, 2.5]

        state.backend.snapshots[GID] = observation(status="active")
        polling_delays: list[float] = []

        async def stop_polling(delay: float) -> None:
            polling_delays.append(delay)
            raise asyncio.CancelledError

        state.lifecycle.sleep = stop_polling
        with pytest.raises(asyncio.CancelledError):
            await state.lifecycle.run_polling()
        state.backend.snapshots.clear()
        with pytest.raises(asyncio.CancelledError):
            await state.lifecycle.run_polling()
        assert polling_delays == [2.0, 2.0]


async def test_restart_resumes_installed_completion_intent(tmp_path: Path) -> None:
    async with runtime(tmp_path) as state:
        put_download(state)
        preparation = await state.store.scan(state.attempt_id)
        async with SQLiteUnitOfWork(state.database) as uow:
            content_id = await ContentRepository(uow.connection).create_object(
                kind=preparation.identity.kind.value,
                digest=preparation.identity.digest,
                size=preparation.size,
                storage_path=preparation.storage_path,
                now_ms=2,
            )
            await DownloadRepository(uow.connection).begin_completion(
                state.attempt_id, expected_gid=GID, content_id=content_id, now_ms=2
            )
        await state.store.install(state.attempt_id, preparation)

        await state.database.close()
        state.database = SQLiteDatabase(tmp_path / "v2.db")
        restarted = make_lifecycle(state)
        assert await restarted.recover_ingestions() is True

        attempt, _, quota = await read(state)
        assert attempt.status is AttemptStatus.COMPLETED
        assert attempt.completed_content_id == content_id
        assert quota.used_bytes == 3
        assert quota.reserved_bytes == 0


@pytest.mark.parametrize("confirmed", ["paused", "waiting", "complete", "error"])
async def test_unpause_is_only_confirmed_by_a_fresh_followup_snapshot(
    tmp_path: Path, confirmed: str
) -> None:
    async with runtime(tmp_path) as state:
        if confirmed == "complete":
            put_download(state)
        state.backend.resume_status = confirmed
        state.backend.snapshots[GID] = observation(status="paused")
        await state.lifecycle.reconcile(observation(status="paused"))
        attempt, _, quota = await read(state)
        if confirmed == "paused":
            assert attempt.status is AttemptStatus.PAUSED
            assert attempt.error_code == ADMISSION_PAUSED
        elif confirmed == "waiting":
            assert attempt.status is AttemptStatus.WAITING
            assert attempt.error_code is None
        elif confirmed == "complete":
            assert attempt.status is AttemptStatus.COMPLETED
            assert quota.used_bytes == 3
        else:
            assert attempt.status is AttemptStatus.FAILED
            assert quota.reserved_bytes == 0


async def test_unpause_missing_snapshot_discards_observation_without_state_write(
    tmp_path: Path,
) -> None:
    async with runtime(tmp_path) as state:
        state.backend.resume_missing = True
        state.observations.put(observation(status="active"))
        await state.lifecycle.reconcile(observation(status="paused"))
        attempt, _, _ = await read(state)
        assert attempt.status is AttemptStatus.QUEUED
        assert state.observations.get(GID) is None


async def test_metadata_parent_runs_without_payload_accounting_and_keeps_ownership(
    tmp_path: Path,
) -> None:
    async with runtime(tmp_path, kind="magnet", total=None, pause_code=METADATA_ADMISSION_PAUSED) as state:
        await state.lifecycle.reconcile(observation(status="active", total=None))
        attempt, _, quota = await read(state)
        assert attempt.status is AttemptStatus.ACTIVE
        assert attempt.error_code == METADATA_ADMISSION_PAUSED
        assert quota.reserved_bytes == 0

        state.backend.snapshots[GID] = observation(status="paused", total=None)
        await state.lifecycle.reconcile(observation(status="paused", total=None))
        attempt, _, _ = await read(state)
        assert attempt.status is AttemptStatus.ACTIVE
        assert attempt.error_code == METADATA_ADMISSION_PAUSED


async def test_paused_admission_failure_does_not_send_unpause(tmp_path: Path) -> None:
    async with runtime(tmp_path, total=None, quota=2) as state:
        await state.lifecycle.reconcile(observation(status="paused", total=3))
        attempt, _, _ = await read(state)
        assert attempt.status is AttemptStatus.FAILED
        assert state.backend.unpause_calls == []
        assert state.backend.stop_calls == [GID]


async def test_paused_resume_checks_full_remaining_machine_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with runtime(tmp_path, total=None, quota=100) as state:
        state.lifecycle.settings = lambda: SystemSettings(min_free_disk=0)
        monkeypatch.setattr(state.store, "machine_free_bytes", lambda: 2)

        await state.lifecycle.reconcile(observation(status="paused", total=3))

        attempt, _, quota = await read(state)
        assert attempt.status is AttemptStatus.FAILED
        assert attempt.error_code == ErrorCode.PHYSICAL_SPACE_EXCEEDED.value
        assert quota.reserved_bytes == 0
        assert state.backend.unpause_calls == []


async def test_directory_completion_persists_entries_and_reuses_existing_content(
    tmp_path: Path,
) -> None:
    async with runtime(tmp_path) as state:
        source = state.root / str(state.attempt_id)
        (source / "nested").mkdir(parents=True)
        (source / "file.bin").write_bytes(b"abc")
        (source / "nested" / "child.bin").write_bytes(b"def")
        await state.lifecycle.reconcile(observation(status="complete", total=6, completed=6))
        first, _, first_quota = await read(state)
        assert first.status is AttemptStatus.COMPLETED
        assert first_quota.used_bytes == 6
        async with SQLiteUnitOfWork(state.database) as uow:
            rows = (
                await uow.connection.execute(
                    select(content_entries.c.path)
                    .where(content_entries.c.content_id == first.completed_content_id)
                    .order_by(content_entries.c.path)
                )
            ).scalars().all()
            assert rows == ["file.bin", "nested", "nested/child.bin"]

            second_user = await AccountRepository(uow.connection).create_user(
                username="second", password_hash="cd" * 32, is_admin=False,
                is_initial_password=False, quota_bytes=100, now_ms=2,
            )
            downloads = DownloadRepository(uow.connection)
            source_id = await downloads.create_source(
                kind="http", resource_key="http:other", payload_json="{}", options_json="{}",
                selected_file_indexes_json=None, display_name="copy", display_uri=None, now_ms=2,
            )
            second_id = await downloads.create_attempt(
                source_id=source_id, resource_key="http:other", gid=PAYLOAD_GID,
                total_length=6, physical_reserved_bytes=6, now_ms=2,
            )
            await downloads.mark_submitted(second_id)
            await downloads.create_subscription(
                attempt_id=second_id, user_id=second_user, reserved_bytes=6,
                display_name="copy", display_uri=None, now_ms=2,
            )
            await QuotaRepository(uow.connection).reserve(second_user, 6)
        duplicate = state.root / str(second_id)
        (duplicate / "nested").mkdir(parents=True)
        (duplicate / "file.bin").write_bytes(b"abc")
        (duplicate / "nested" / "child.bin").write_bytes(b"def")
        await state.lifecycle.reconcile(
            observation(PAYLOAD_GID, "complete", total=6, completed=6)
        )
        async with SQLiteUnitOfWork(state.database) as uow:
            second = await DownloadRepository(uow.connection).get_attempt(second_id)
            assert second.completed_content_id == first.completed_content_id
            assert second.status is AttemptStatus.COMPLETED
            assert (await QuotaRepository(uow.connection).get(second_user)).used_bytes == 6
            assert (await uow.connection.execute(select(content_objects))).all().__len__() == 1
            assert not duplicate.exists()


async def test_existing_content_identity_conflict_is_not_overwritten(tmp_path: Path) -> None:
    async with runtime(tmp_path) as state:
        put_download(state)
        preparation = await state.store.scan(state.attempt_id)
        async with SQLiteUnitOfWork(state.database) as uow:
            await ContentRepository(uow.connection).create_object(
                kind="file", digest=preparation.identity.digest, size=99,
                storage_path="objects/conflict", now_ms=2,
            )
        with pytest.raises(DomainError) as caught:
            await state.lifecycle.reconcile(observation(status="complete", completed=3))
        assert caught.value.code is ErrorCode.INVALID_STATE
        attempt, _, _ = await read(state)
        assert attempt.status is AttemptStatus.QUEUED
        assert attempt.completed_content_id is None


async def test_unsafe_content_fails_without_leaking_a_user_file(tmp_path: Path) -> None:
    async with runtime(tmp_path) as state:
        source = state.root / str(state.attempt_id)
        source.mkdir()
        external = tmp_path / "external"
        external.write_bytes(b"abc")
        (source / "file.bin").symlink_to(external)
        await state.lifecycle.reconcile(observation(status="complete", completed=3))
        attempt, _, quota = await read(state)
        assert attempt.status is AttemptStatus.FAILED
        assert quota.reserved_bytes == 0
        assert not source.exists()
        assert external.read_bytes() == b"abc"


async def test_cleanup_failure_is_retried_and_handoff_parent_cleanup_is_nonfatal(
    tmp_path: Path,
) -> None:
    async with runtime(tmp_path) as state:
        state.backend.remove_error = DomainError(ErrorCode.RPC_UNAVAILABLE, "offline")
        with pytest.raises(DomainError):
            await state.lifecycle.reconcile(observation(status="error"))
        attempt, _, _ = await read(state)
        assert attempt.status is AttemptStatus.FAILED
        assert attempt.result_consumed_at_ms is None
        assert await state.lifecycle.cleanup_terminal() is False
        state.backend.remove_error = None
        assert await state.lifecycle.cleanup_terminal() is True
        attempt, _, _ = await read(state)
        assert attempt.result_consumed_at_ms == 100

    async with runtime(tmp_path / "handoff", kind="magnet", total=None, pause_code=METADATA_ADMISSION_PAUSED) as state:
        state.backend.remove_error = DomainError(ErrorCode.RPC_UNAVAILABLE, "offline")
        state.backend.snapshots[PAYLOAD_GID] = observation(PAYLOAD_GID, "active")
        await state.lifecycle.reconcile(
            observation(status="complete", total=0, followed_by=(PAYLOAD_GID,))
        )
        attempt, _, _ = await read(state)
        assert attempt.gid == PAYLOAD_GID
        assert attempt.status is AttemptStatus.ACTIVE


async def test_recovery_failure_is_retained_and_stale_completion_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with runtime(tmp_path) as state:
        preparation = put_download(state)
        scanned = await state.store.scan(state.attempt_id)
        async with SQLiteUnitOfWork(state.database) as uow:
            content_id = await ContentRepository(uow.connection).create_object(
                kind="file", digest=scanned.identity.digest, size=3,
                storage_path=scanned.storage_path, now_ms=2,
            )
            await DownloadRepository(uow.connection).begin_completion(
                state.attempt_id, expected_gid=GID, content_id=content_id, now_ms=2
            )
        import shutil
        shutil.rmtree(preparation)
        assert await state.lifecycle.recover_ingestions() is False
        attempt, _, _ = await read(state)
        assert attempt.status is AttemptStatus.QUEUED

        put_download(state)
        async def stale(*args: object, **kwargs: object) -> bool:
            raise DomainError(ErrorCode.STALE_EVENT, "stale")
        monkeypatch.setattr(DownloadRepository, "finalize_completion", stale)
        await state.lifecycle.reconcile(observation(status="complete", completed=3))
        attempt, _, _ = await read(state)
        assert attempt.status is AttemptStatus.QUEUED
        assert state.observations.get(GID) is None


async def test_notification_missing_failure_and_success_share_the_same_reconciler(
    tmp_path: Path,
) -> None:
    async with runtime(tmp_path) as state:
        state.observations.put(observation(status="active"))
        await state.lifecycle._on_gid(GID)
        assert state.observations.get(GID) is None

        state.backend.snapshots[GID] = observation(status="active")
        await state.lifecycle._on_gid(GID)
        attempt, _, _ = await read(state)
        assert attempt.status is AttemptStatus.ACTIVE

        state.backend.snapshot_errors[GID] = DomainError(ErrorCode.RPC_UNAVAILABLE, "offline")
        await state.lifecycle._on_gid(GID)
        assert state.observations.get(GID) is None


async def test_missing_gid_requires_grace_and_a_second_missing_snapshot(
    tmp_path: Path,
) -> None:
    async with runtime(tmp_path) as state:
        now = [100]
        state.lifecycle.clock_ms = lambda: now[0]

        assert await state.lifecycle.reconcile_live() is False
        attempt, _, quota = await read(state)
        assert attempt.status is AttemptStatus.QUEUED
        assert quota.reserved_bytes == 3

        now[0] += 9_999
        assert await state.lifecycle.reconcile_live() is False
        attempt, _, _ = await read(state)
        assert attempt.status is AttemptStatus.QUEUED

        now[0] += 1
        assert await state.lifecycle.reconcile_live() is False
        attempt, task, quota = await read(state)
        assert attempt.status is AttemptStatus.FAILED
        assert attempt.error_code == "aria2_gid_missing"
        assert task.subscription_status is SubscriptionStatus.FAILED
        assert quota.reserved_bytes == 0


async def test_missing_gid_query_errors_do_not_start_or_confirm_failure(
    tmp_path: Path,
) -> None:
    async with runtime(tmp_path) as state:
        now = [100]
        state.lifecycle.clock_ms = lambda: now[0]
        state.backend.snapshot_errors[GID] = DomainError(ErrorCode.RPC_UNAVAILABLE, "offline")

        assert await state.lifecycle.reconcile_live() is False
        now[0] += 60_000
        state.backend.snapshot_errors.clear()
        assert await state.lifecycle.reconcile_live() is False
        attempt, _, _ = await read(state)
        assert attempt.status is AttemptStatus.QUEUED

        state.backend.snapshot_errors[GID] = DomainError(ErrorCode.RPC_UNAVAILABLE, "offline")
        now[0] += 10_000
        assert await state.lifecycle.reconcile_live() is False
        attempt, _, _ = await read(state)
        assert attempt.status is AttemptStatus.QUEUED


async def test_ingestion_started_before_scan_and_missing_gid_recovers_after_restart(
    tmp_path: Path,
) -> None:
    async with runtime(tmp_path) as state:
        put_download(state)
        original_scan = state.store.scan

        async def scan_failed(attempt_id: int) -> object:
            raise OSError("scan offline")

        state.store.scan = scan_failed  # type: ignore[method-assign]
        with pytest.raises(OSError, match="scan offline"):
            await state.lifecycle.reconcile(observation(status="complete", completed=3))
        attempt, _, _ = await read(state)
        assert attempt.ingestion_started_at_ms == 100
        assert attempt.completed_content_id is None

        state.store.scan = original_scan  # type: ignore[method-assign]
        await state.database.close()
        state.database = SQLiteDatabase(tmp_path / "v2.db")
        restarted = make_lifecycle(state)
        now = [100]
        restarted.clock_ms = lambda: now[0]
        restarted.ingestion_timeout_ms = 10

        assert await restarted.reconcile_live() is False
        now[0] += 10_000
        assert await restarted.reconcile_live() is False
        attempt, task, quota = await read(state)
        assert attempt.status is AttemptStatus.COMPLETED
        assert attempt.ingestion_started_at_ms == 100
        assert task.subscription_status is SubscriptionStatus.COMPLETED
        assert quota.used_bytes == 3
        assert quota.reserved_bytes == 0


@pytest.mark.parametrize("stage", ["scan", "install", "finalize"])
async def test_persistent_ingestion_failure_times_out_without_resetting_age(
    tmp_path: Path, stage: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with runtime(tmp_path / stage) as state:
        put_download(state)

        async def failed(*args: object, **kwargs: object) -> object:
            raise OSError(f"{stage} offline")

        if stage == "scan":
            monkeypatch.setattr(state.store, "scan", failed)
        elif stage == "install":
            monkeypatch.setattr(state.store, "install", failed)
        else:
            monkeypatch.setattr(DownloadRepository, "finalize_completion", failed)
        now = [100]
        state.lifecycle.clock_ms = lambda: now[0]
        state.lifecycle.ingestion_timeout_ms = 10_000
        with pytest.raises(OSError, match=f"{stage} offline"):
            await state.lifecycle.reconcile(observation(status="complete", completed=3))
        first, _, _ = await read(state)

        now[0] += 9_999
        assert await state.lifecycle.recover_ingestions() is False
        attempt, _, quota = await read(state)
        assert attempt.status is AttemptStatus.QUEUED
        assert attempt.ingestion_started_at_ms == first.ingestion_started_at_ms
        assert quota.reserved_bytes == 3

        now[0] += 1
        assert await state.lifecycle.recover_ingestions() is False
        attempt, task, quota = await read(state)
        assert attempt.status is AttemptStatus.FAILED
        assert attempt.error_code == "ingestion_timeout"
        assert task.subscription_status is SubscriptionStatus.FAILED
        assert quota.reserved_bytes == 0


@pytest.mark.parametrize("late_status", ["active", "error"])
async def test_ingestion_intent_wins_over_late_non_completion_snapshots(
    tmp_path: Path, late_status: str
) -> None:
    async with runtime(tmp_path) as state:
        put_download(state)
        original_scan = state.store.scan

        async def scan_failed(attempt_id: int) -> object:
            raise OSError("scan offline")

        state.store.scan = scan_failed  # type: ignore[method-assign]
        with pytest.raises(OSError, match="scan offline"):
            await state.lifecycle.reconcile(observation(status="complete", completed=3))
        state.store.scan = original_scan  # type: ignore[method-assign]

        await state.lifecycle.reconcile(observation(status=late_status, completed=3))
        attempt, _, quota = await read(state)
        assert attempt.status is AttemptStatus.COMPLETED
        assert quota.used_bytes == 3


async def test_ingestion_timeout_cleanup_failure_remains_retryable(tmp_path: Path) -> None:
    async with runtime(tmp_path) as state:
        put_download(state)

        async def scan_failed(attempt_id: int) -> object:
            raise OSError("scan offline")

        state.store.scan = scan_failed  # type: ignore[method-assign]
        with pytest.raises(OSError, match="scan offline"):
            await state.lifecycle.reconcile(observation(status="complete", completed=3))
        now = [100]
        state.lifecycle.clock_ms = lambda: now[0]
        now[0] += 60_000
        state.backend.remove_error = DomainError(ErrorCode.RPC_UNAVAILABLE, "offline")

        assert await state.lifecycle.recover_ingestions() is False
        attempt, _, quota = await read(state)
        assert attempt.status is AttemptStatus.FAILED
        assert attempt.error_code == "ingestion_timeout"
        assert attempt.result_consumed_at_ms is None
        assert quota.reserved_bytes == 0

        state.backend.remove_error = None
        assert await state.lifecycle.cleanup_terminal() is True
        attempt, _, _ = await read(state)
        assert attempt.result_consumed_at_ms == now[0]


async def test_polling_recovers_pending_ingestion_before_sleeping(tmp_path: Path) -> None:
    async with runtime(tmp_path) as state:
        put_download(state)
        original_scan = state.store.scan

        async def scan_failed(attempt_id: int) -> object:
            raise OSError("scan offline")

        state.store.scan = scan_failed  # type: ignore[method-assign]
        with pytest.raises(OSError, match="scan offline"):
            await state.lifecycle.reconcile(observation(status="complete", completed=3))
        state.store.scan = original_scan  # type: ignore[method-assign]

        async def stop(delay: float) -> None:
            raise asyncio.CancelledError

        state.lifecycle.sleep = stop
        with pytest.raises(asyncio.CancelledError):
            await state.lifecycle.run_polling()
        attempt, _, _ = await read(state)
        assert attempt.status is AttemptStatus.COMPLETED


@pytest.mark.parametrize(
    ("kind", "pause_code"),
    [
        ("http", ADMISSION_PAUSED),
        ("magnet", METADATA_ADMISSION_PAUSED),
    ],
)
async def test_followed_by_handoff_completes_actual_payload(
    tmp_path: Path, kind: str, pause_code: str
) -> None:
    async with runtime(tmp_path, kind=kind, total=None, pause_code=pause_code) as state:
        state.backend.snapshots[PAYLOAD_GID] = observation(PAYLOAD_GID, "paused")
        await state.lifecycle.reconcile(
            observation(status="complete", total=0, followed_by=(PAYLOAD_GID,))
        )
        put_download(state)
        await state.lifecycle.reconcile(observation(PAYLOAD_GID, "complete", completed=3))

        attempt, task, quota = await read(state)
        assert attempt.gid == PAYLOAD_GID
        assert attempt.status is AttemptStatus.COMPLETED
        assert task.subscription_status is SubscriptionStatus.COMPLETED
        assert quota.used_bytes == 3
        async with SQLiteUnitOfWork(state.database) as uow:
            content = await ContentRepository(uow.connection).get_object(
                attempt.completed_content_id
            )
            assert content.digest == hashlib.sha256(b"abc").hexdigest()


async def test_missing_gid_recheck_uses_a_valid_second_snapshot(tmp_path: Path) -> None:
    async with runtime(tmp_path) as state:
        now = [100]
        state.lifecycle.clock_ms = lambda: now[0]
        original = state.backend.snapshot
        calls = 0

        async def snapshots(*, gid: str) -> DownloadObservation | None:
            nonlocal calls
            calls += 1
            if calls == 3:
                state.backend.snapshots[GID] = observation(status="active")
            return await original(gid=gid)

        state.backend.snapshot = snapshots  # type: ignore[method-assign]
        assert await state.lifecycle.reconcile_live() is False
        now[0] += 10_000
        assert await state.lifecycle.reconcile_live() is False
        attempt, _, quota = await read(state)
        assert attempt.status is AttemptStatus.ACTIVE
        assert quota.reserved_bytes == 3
        assert calls == 3


async def test_missing_gid_recheck_transport_error_is_not_terminal(tmp_path: Path) -> None:
    async with runtime(tmp_path) as state:
        now = [100]
        state.lifecycle.clock_ms = lambda: now[0]
        await state.lifecycle.reconcile_live()
        calls = 0

        async def snapshots(*, gid: str) -> DownloadObservation | None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise DomainError(ErrorCode.RPC_UNAVAILABLE, "offline")
            return None

        state.backend.snapshot = snapshots  # type: ignore[method-assign]
        now[0] += 10_000
        assert await state.lifecycle.reconcile_live() is False
        attempt, _, quota = await read(state)
        assert attempt.status is AttemptStatus.QUEUED
        assert quota.reserved_bytes == 3
        assert calls == 2


async def test_missing_failure_uses_the_lifecycle_broadcast_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with runtime(tmp_path) as state:
        now = [100]
        state.lifecycle.clock_ms = lambda: now[0]
        broadcasts: list[int] = []

        async def record(attempt_id: int) -> None:
            broadcasts.append(attempt_id)

        monkeypatch.setattr(state.lifecycle, "_broadcast_attempt", record)
        await state.lifecycle.reconcile_live()
        assert broadcasts == []
        now[0] += 10_000
        await state.lifecycle.reconcile_live()
        assert broadcasts == [state.attempt_id]


async def test_missing_recheck_does_not_overwrite_a_terminal_attempt(tmp_path: Path) -> None:
    async with runtime(tmp_path) as state:
        now = [100]
        state.lifecycle.clock_ms = lambda: now[0]
        original = state.backend.snapshot
        calls = 0

        async def snapshots(*, gid: str) -> DownloadObservation | None:
            nonlocal calls
            calls += 1
            if calls == 2:
                async with SQLiteUnitOfWork(state.database) as uow:
                    await DownloadRepository(uow.connection).fail_attempt(
                        state.attempt_id, error_code="cancelled_elsewhere", now_ms=now[0]
                    )
            return await original(gid=gid)

        state.backend.snapshot = snapshots  # type: ignore[method-assign]
        await state.lifecycle.reconcile_live()
        now[0] += 10_000
        await state.lifecycle.reconcile_live()
        attempt, _, quota = await read(state)
        assert attempt.status is AttemptStatus.FAILED
        assert attempt.error_code == "cancelled_elsewhere"
        assert quota.reserved_bytes == 0


async def test_ingestion_recovery_rereads_a_terminal_candidate_under_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with runtime(tmp_path) as state:
        async with SQLiteUnitOfWork(state.database) as uow:
            await DownloadRepository(uow.connection).start_ingestion(
                state.attempt_id, expected_gid=GID, now_ms=100
            )
        listed = asyncio.Event()
        original = DownloadRepository.list_completion_intents

        async def list_candidates(repository: DownloadRepository):
            attempts = await original(repository)
            listed.set()
            return attempts

        monkeypatch.setattr(DownloadRepository, "list_completion_intents", list_candidates)
        lock = state.lifecycle._locks.setdefault(state.attempt_id, asyncio.Lock())
        await lock.acquire()
        recovering = asyncio.create_task(state.lifecycle.recover_ingestions())
        await listed.wait()
        async with SQLiteUnitOfWork(state.database) as uow:
            await DownloadRepository(uow.connection).fail_attempt(
                state.attempt_id, error_code="failed_elsewhere", now_ms=101
            )
        lock.release()

        assert await recovering is True
        attempt, _, quota = await read(state)
        assert attempt.status is AttemptStatus.FAILED
        assert attempt.error_code == "failed_elsewhere"
        assert quota.reserved_bytes == 0


async def test_listener_clean_disconnect_also_reconnects(tmp_path: Path) -> None:
    async with runtime(tmp_path) as state:
        state.backend.listen_returns = True
        async def stop(delay: float) -> None:
            raise asyncio.CancelledError
        state.lifecycle.sleep = stop
        with pytest.raises(asyncio.CancelledError):
            await state.lifecycle.run_listener()


async def test_stale_lock_read_and_unapplied_snapshot_never_update_observations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with runtime(tmp_path) as state:
        state.observations.put(observation(status="active"))
        original = DownloadRepository.get_attempt
        async def changed(self: DownloadRepository, attempt_id: int):
            attempt = await original(self, attempt_id)
            from dataclasses import replace
            return replace(attempt, gid="different")
        monkeypatch.setattr(DownloadRepository, "get_attempt", changed)
        await state.lifecycle.reconcile(observation(status="paused"))
        assert state.observations.get(GID) is None

    async with runtime(tmp_path / "not-applied") as state:
        async def not_applied(*args: object, **kwargs: object) -> bool:
            return False
        monkeypatch.setattr(DownloadRepository, "apply_observation", not_applied)
        await state.lifecycle.reconcile(observation(status="active"))
        assert state.observations.get(GID) is None


# --- 回归（D2）：建立意图前取消必须回滚 content/entries 与完成意图，且不再 install ---
async def test_cancel_before_completion_intent_rolls_back_content_and_stops_install(
    tmp_path: Path,
) -> None:
    async with runtime(tmp_path) as state:
        put_download(state)
        original_scan = state.store.scan
        entered = asyncio.Event()
        release = asyncio.Event()

        async def gated_scan(attempt_id: int):
            entered.set()
            await release.wait()
            return await original_scan(attempt_id)

        state.store.scan = gated_scan  # type: ignore[method-assign]
        completing = asyncio.create_task(
            state.lifecycle.reconcile(observation(status="complete", completed=3))
        )
        await entered.wait()
        async with SQLiteUnitOfWork(state.database) as uow:
            repository = DownloadRepository(uow.connection)
            cancellation = await repository.cancel_subscription(
                user_id=state.user_id, task_id=state.task_id, now_ms=101
            )
            await repository.mark_stop_confirmed(cancellation.attempt_id)
        release.set()
        await completing

        attempt, task, quota = await read(state)
        assert attempt.status is AttemptStatus.CANCELLED
        assert task.subscription_status is SubscriptionStatus.CANCELLED
        assert quota.reserved_bytes == 0
        assert attempt.completed_content_id is None
        async with SQLiteUnitOfWork(state.database) as uow:
            rows = (await uow.connection.execute(select(content_objects))).all()
        assert rows == []
        assert not (state.root / "objects").exists()


# --- 回归（D3/D4）：安装期间取消（停止已确认）在 install 返回后必须原子终态完成 ---
async def test_cancel_during_install_reclaims_orphan_content_after_install_returns(
    tmp_path: Path,
) -> None:
    from aria2deck.domain.content import ContentDeletionState

    async with runtime(tmp_path) as state:
        put_download(state)
        original_install = state.store.install
        entered = asyncio.Event()
        release = asyncio.Event()

        async def gated_install(attempt_id: int, preparation: object):
            entered.set()
            await release.wait()
            return await original_install(attempt_id, preparation)

        state.store.install = gated_install  # type: ignore[method-assign]
        completing = asyncio.create_task(
            state.lifecycle.reconcile(observation(status="complete", completed=3))
        )
        await entered.wait()
        async with SQLiteUnitOfWork(state.database) as uow:
            repository = DownloadRepository(uow.connection)
            cancellation = await repository.cancel_subscription(
                user_id=state.user_id, task_id=state.task_id, now_ms=101
            )
            await repository.mark_stop_confirmed(cancellation.attempt_id)
        release.set()
        await completing

        attempt, _, quota = await read(state)
        assert attempt.status is AttemptStatus.CANCELLED
        assert quota.reserved_bytes == 0
        assert attempt.result_consumed_at_ms is not None
        assert attempt.completed_content_id is not None
        async with SQLiteUnitOfWork(state.database) as uow:
            content = await ContentRepository(uow.connection).get_object(
                attempt.completed_content_id
            )
        assert content.deletion_state is ContentDeletionState.PENDING_DELETE


# --- 回归（D3）：停止未确认期间不清理、不认领；确认后由终态清理链完成认领 ---
async def test_uncertain_stop_waits_for_confirmation_before_reclaim(tmp_path: Path) -> None:
    from aria2deck.domain.content import ContentDeletionState

    async with runtime(tmp_path) as state:
        put_download(state)
        original_install = state.store.install
        entered = asyncio.Event()
        release = asyncio.Event()

        async def gated_install(attempt_id: int, preparation: object):
            entered.set()
            await release.wait()
            return await original_install(attempt_id, preparation)

        state.store.install = gated_install  # type: ignore[method-assign]
        completing = asyncio.create_task(
            state.lifecycle.reconcile(observation(status="complete", completed=3))
        )
        await entered.wait()
        async with SQLiteUnitOfWork(state.database) as uow:
            await DownloadRepository(uow.connection).cancel_subscription(
                user_id=state.user_id, task_id=state.task_id, now_ms=101
            )
        release.set()
        await completing

        attempt, _, _ = await read(state)
        assert attempt.status is AttemptStatus.CANCELLED
        assert attempt.submission_state == "uncertain"
        assert attempt.result_consumed_at_ms is None

        assert await state.lifecycle.cleanup_terminal() is True
        uncertain, _, _ = await read(state)
        assert uncertain.result_consumed_at_ms is None  # 未确认：不清理、不认领、不标 consumed

        async with SQLiteUnitOfWork(state.database) as uow:
            await DownloadRepository(uow.connection).confirm_stopped_gid(GID)
        assert await state.lifecycle.cleanup_terminal() is True
        confirmed, _, _ = await read(state)
        async with SQLiteUnitOfWork(state.database) as uow:
            content = await ContentRepository(uow.connection).get_object(
                confirmed.completed_content_id
            )
        assert content.deletion_state is ContentDeletionState.PENDING_DELETE


# --- 回归（D6）：cleanup_terminal 必须与 reconcile/recover_ingestions 共用 attempt 锁 ---
async def test_cleanup_terminal_waits_for_the_attempt_lock_instead_of_racing(
    tmp_path: Path,
) -> None:
    async with runtime(tmp_path) as state:
        put_download(state)
        async with SQLiteUnitOfWork(state.database) as uow:
            await DownloadRepository(uow.connection).fail_attempt(
                state.attempt_id, error_code="failed", now_ms=101
            )
        lock = state.lifecycle._locks.setdefault(state.attempt_id, asyncio.Lock())
        await lock.acquire()
        cleaning = asyncio.create_task(state.lifecycle.cleanup_terminal())
        # attempt 锁被占（如 install 进行中）时，终态清理必须等待，不得与安装并发触碰同一目录。
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 2.0
        while not cleaning.done() and loop.time() < deadline:
            await asyncio.sleep(0.01)
        assert not cleaning.done()  # 修复前：不取 attempt 锁，清理直接完成

        lock.release()
        assert await cleaning is True
        attempt, _, _ = await read(state)
        assert attempt.result_consumed_at_ms is not None
        assert not (state.root / str(state.attempt_id)).exists()
        assert state.backend.remove_calls == [GID]


# --- 回归（D4/R4）：建立意图遇 STALE 时，若最新 attempt 的 GID 已变化（如 Magnet ---
# --- handoff 换 GID）或仍 live，必须保留原异常向上传播，而不是吞成清理。 ---
async def test_stale_ingest_abort_reraises_when_gid_changed(tmp_path: Path) -> None:
    async with runtime(tmp_path) as state:
        async with SQLiteUnitOfWork(state.database) as uow:
            stale_snapshot = await DownloadRepository(uow.connection).get_attempt(
                state.attempt_id
            )
        # 模拟 handoff：DB 中 GID 已换新，完成事件携带的还是旧 GID 快照。
        async with SQLiteUnitOfWork(state.database) as uow:
            await uow.connection.execute(
                text("UPDATE download_attempts SET gid = 'newgid-handoff' WHERE id = :id"),
                {"id": state.attempt_id},
            )
        error = DomainError(ErrorCode.STALE_EVENT, "下载已结束，忽略旧完成事件")
        with pytest.raises(DomainError) as caught:
            await state.lifecycle._abort_stale_ingest(stale_snapshot, error)
        assert caught.value is error  # 原异常保留，不按取消清理


async def test_stale_ingest_abort_reraises_when_attempt_still_live(tmp_path: Path) -> None:
    async with runtime(tmp_path) as state:
        async with SQLiteUnitOfWork(state.database) as uow:
            stale_snapshot = await DownloadRepository(uow.connection).get_attempt(
                state.attempt_id
            )
        # 并发交错：事件处理期间 attempt 仍是 live（未完成取消）。
        error = DomainError(ErrorCode.STALE_EVENT, "下载已结束，忽略旧完成事件")
        with pytest.raises(DomainError) as caught:
            await state.lifecycle._abort_stale_ingest(stale_snapshot, error)
        assert caught.value is error
        # 未按取消清理：consumed 未写、aria2 结果未删。
        attempt, _, _ = await read(state)
        assert attempt.result_consumed_at_ms is None
        assert state.backend.remove_calls == []
