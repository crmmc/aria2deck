from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from dataclasses import replace as dataclass_replace
from pathlib import Path

import pytest
from sqlalchemy import select, text

from aria2deck.application.downloads.broadcast import TaskBroadcast
from aria2deck.application.downloads.lifecycle import DownloadLifecycleService
from aria2deck.application.downloads.models import (
    DownloadObservation,
    ManifestEntry,
    ObservedFile,
    TaskProjection,
)
from aria2deck.application.downloads.projection import TaskProjector
from aria2deck.application.downloads.rpc_projection import tell_status_payload
from aria2deck.application.system.connectivity import BackendConnectivity
from aria2deck.domain.content import ContentKind
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
from tests.download_fakes import FakeSubmissionBackend, make_torrent

GID = "0123456789abcdef"
PAYLOAD_GID = "fedcba0987654321"


class LifecycleBackend(FakeSubmissionBackend):
    def __init__(self) -> None:
        super().__init__()
        self.snapshots: dict[str, DownloadObservation] = {}
        self.snapshot_errors: dict[str, DomainError] = {}
        self.snapshot_calls: list[str] = []
        self.unpause_calls: list[str] = []
        self.unpause_error: DomainError | None = None
        self.remove_calls: list[str] = []
        self.remove_error: DomainError | None = None
        self.live_gids: list[str] = []
        self.listen_error: DomainError | None = None
        self.listen_returns = False
        self.resume_status = "active"
        self.resume_missing = False
        self.version_calls = 0
        self.version_error: Exception | None = None
        self.version_hangs = False

    async def version(self) -> tuple[str, list[str]]:
        self.version_calls += 1
        if self.version_hangs:
            await asyncio.sleep(3600)
        if self.version_error is not None:
            raise self.version_error
        return "1.37.0", ["Async DNS"]

    async def snapshot(
        self, *, gid: str, attempt_id: int
    ) -> DownloadObservation | None:
        self.snapshot_calls.append(gid)
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
                    files=current.files,
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
    files: tuple[ObservedFile, ...] = (),
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
        files=files,
    )


def manifest_files(*entries: tuple[str, int]) -> tuple[ObservedFile, ...]:
    """构造 aria2 完成报告的 files：relpath 是 attempt 内相对路径，name 取 basename。"""
    return tuple(
        ObservedFile(path.rsplit("/", 1)[-1], size, size, True, path) for path, size in entries
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
    display_name: str = "file.bin",
    display_uri: str | None = None,
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
            else (
                {"torrent": make_torrent([("bundle/file.bin", 3)], name="bundle")}
                if kind == "torrent"
                else {"uris": ["https://example.com/file.bin"]}
            )
        )
        import json

        source_id = await downloads.create_source(
            kind=kind,
            resource_key=f"{kind}:key",
            payload_json=json.dumps(payload),
            options_json="{}",
            selected_file_indexes_json=None,
            display_name=display_name,
            display_uri=display_uri,
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
            display_name=display_name,
            display_uri=display_uri,
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


async def read_manifest(runtime: Runtime) -> tuple[ManifestEntry, ...] | None:
    async with SQLiteUnitOfWork(runtime.database) as uow:
        return await DownloadRepository(uow.connection).get_manifest(runtime.attempt_id)


class DatabaseProjection:
    """广播入口用的真实投影：错误码与中文文案必须来自落库记录，而不是测试编的对象。"""

    def __init__(self, state: Runtime) -> None:
        self.state = state

    async def get_task(self, *, user_id: int, task_id: int) -> TaskProjection:
        async with SQLiteUnitOfWork(self.state.database) as uow:
            record = await DownloadRepository(uow.connection).get_task(
                user_id=user_id, task_id=task_id
            )
        return TaskProjector(
            observations=self.state.observations, clock_ms=lambda: 100
        ).project(record)


def put_download(runtime: Runtime, content: bytes = b"abc") -> Path:
    directory = runtime.root / str(runtime.attempt_id)
    directory.mkdir()
    (directory / "file.bin").write_bytes(content)
    return directory


class RecordingStore:
    """记录入库入口收到的清单：用于断言“清单外的东西根本没被看”。"""

    def __init__(self, inner: DownloadContentStore) -> None:
        self._inner = inner
        self.collect_calls: list[tuple[int, tuple[ManifestEntry, ...]]] = []
        self.install_calls: list[tuple[int, tuple[ManifestEntry, ...]]] = []

    def machine_free_bytes(self) -> int:
        return self._inner.machine_free_bytes()

    async def collect(self, attempt_id: int, manifest: tuple[ManifestEntry, ...]):
        self.collect_calls.append((attempt_id, manifest))
        return await self._inner.collect(attempt_id, manifest)

    async def install(
        self, attempt_id: int, expected, manifest: tuple[ManifestEntry, ...]
    ) -> None:
        self.install_calls.append((attempt_id, manifest))
        await self._inner.install(attempt_id, expected, manifest)

    async def cleanup_attempt(self, attempt_id: int) -> None:
        await self._inner.cleanup_attempt(attempt_id)


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
        complete = observation(
            status="complete", completed=3, files=manifest_files(("file.bin", 3))
        )
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


async def test_completion_persists_manifest_in_the_same_transaction_as_the_intent(
    tmp_path: Path,
) -> None:
    """HTTP 单文件：观测到完成那一刻，清单与完成意图一起落库。"""
    async with runtime(tmp_path) as state:
        put_download(state)
        await state.lifecycle.reconcile(
            observation(
                status="complete",
                completed=3,
                files=(ObservedFile("file.bin", 3, 3, True, "file.bin"),),
            )
        )

        attempt, task, quota = await read(state)
        assert attempt.status is AttemptStatus.COMPLETED
        assert attempt.ingestion_started_at_ms is not None
        assert attempt.completed_content_id is not None
        assert task.subscription_status is SubscriptionStatus.COMPLETED
        assert quota.used_bytes == 3
        assert await read_manifest(state) == (ManifestEntry("file.bin", 3),)


async def test_bt_multi_file_completion_persists_relative_paths_for_subdirectories(
    tmp_path: Path,
) -> None:
    """BT 多文件：清单存 attempt 内相对路径——basename 无法定位 `{种子名}/子目录/文件`。"""
    async with runtime(tmp_path, kind="torrent", total=5) as state:
        directory = state.root / str(state.attempt_id)
        (directory / "bundle" / "sub").mkdir(parents=True)
        (directory / "bundle" / "sub" / "a.bin").write_bytes(b"abc")
        (directory / "bundle" / "b.bin").write_bytes(b"de")
        await state.lifecycle.reconcile(
            observation(
                status="complete",
                total=5,
                completed=5,
                files=(
                    ObservedFile("a.bin", 3, 3, True, "bundle/sub/a.bin"),
                    ObservedFile("b.bin", 2, 2, True, "bundle/b.bin"),
                ),
            )
        )

        attempt, _, quota = await read(state)
        assert attempt.status is AttemptStatus.COMPLETED
        assert quota.used_bytes == 5
        assert await read_manifest(state) == (
            ManifestEntry("bundle/sub/a.bin", 3),
            ManifestEntry("bundle/b.bin", 2),
        )


async def test_manifest_keeps_only_selected_files(tmp_path: Path) -> None:
    """子集下载：未选中项 aria2 不落盘（bt-remove-unselected-file），清单只记应存在的文件。"""
    async with runtime(tmp_path, kind="torrent", total=3) as state:
        directory = state.root / str(state.attempt_id)
        (directory / "bundle").mkdir(parents=True)
        (directory / "bundle" / "picked.bin").write_bytes(b"abc")
        await state.lifecycle.reconcile(
            observation(
                status="complete",
                total=3,
                completed=3,
                files=(
                    ObservedFile("picked.bin", 3, 3, True, "bundle/picked.bin"),
                    ObservedFile("skipped.bin", 1, 0, False, "bundle/skipped.bin"),
                ),
            )
        )

        attempt, _, _ = await read(state)
        assert attempt.status is AttemptStatus.COMPLETED
        assert await read_manifest(state) == (ManifestEntry("bundle/picked.bin", 3),)


@pytest.mark.parametrize(
    "files",
    [
        (),
        (ObservedFile("file.bin", 3, 3, False, "file.bin"),),
    ],
    ids=["no-files", "all-unselected"],
)
async def test_completion_without_usable_manifest_settles_as_manifest_unavailable(
    tmp_path: Path, files: tuple[ObservedFile, ...]
) -> None:
    """凑不出清单的完成观测不再无限重试：重观测也拿不到可信文件就判死并保留文件。

    完成态的 aria2 报告必然有文件列表，空观测/全未选中只能说明“不知道”，所以不落
    空清单；重观测仍是同样结果时不猜内容，按 manifest_unavailable 收尾。
    """
    async with runtime(tmp_path) as state:
        directory = put_download(state)
        state.backend.snapshots[GID] = observation(
            status="complete", completed=3, files=files
        )

        await state.lifecycle.reconcile(
            observation(status="complete", completed=3, files=files)
        )

        attempt, task, quota = await read(state)
        assert attempt.status is AttemptStatus.FAILED
        assert attempt.error_code == "manifest_unavailable"
        assert attempt.completed_content_id is None
        assert task.subscription_status is SubscriptionStatus.FAILED
        assert quota.reserved_bytes == 0
        assert await read_manifest(state) is None
        async with SQLiteUnitOfWork(state.database) as uow:
            assert (await uow.connection.execute(select(content_objects))).all() == []
        assert (directory / "file.bin").read_bytes() == b"abc", "无法核对的文件留在盘上"


async def test_missing_manifest_is_restored_from_a_fresh_snapshot(tmp_path: Path) -> None:
    """无清单（升级前的未完成入库）但 aria2 仍可观测：补写清单后同轮入库，不进终态。"""
    async with runtime(tmp_path) as state:
        put_download(state)
        state.backend.snapshots[GID] = observation(
            status="complete", completed=3, files=manifest_files(("file.bin", 3))
        )
        async with SQLiteUnitOfWork(state.database) as uow:
            await DownloadRepository(uow.connection).start_ingestion(
                state.attempt_id, expected_gid=GID, now_ms=100
            )

        assert await state.lifecycle.recover_ingestions() is True

        attempt, task, quota = await read(state)
        assert attempt.status is AttemptStatus.COMPLETED
        assert task.subscription_status is SubscriptionStatus.COMPLETED
        assert quota.used_bytes == 3
        assert quota.reserved_bytes == 0
        assert await read_manifest(state) == (ManifestEntry("file.bin", 3),)


async def test_manifest_unavailable_keeps_files_releases_quota_and_broadcasts(
    tmp_path: Path,
) -> None:
    """aria2 丢失该任务且过了宽限：失败终态 + 文件保留 + 配额释放 + 终态帧。"""
    async with runtime(tmp_path) as state:
        directory = put_download(state)
        async with SQLiteUnitOfWork(state.database) as uow:
            await DownloadRepository(uow.connection).start_ingestion(
                state.attempt_id, expected_gid=GID, now_ms=100
            )
        now = [100]
        state.lifecycle.clock_ms = lambda: now[0]
        bus = TaskBroadcast()
        sink = bus.subscribe(state.user_id)
        state.lifecycle.broadcast = bus
        state.lifecycle.queries = DatabaseProjection(state)

        assert await state.lifecycle.recover_ingestions() is True
        attempt, _, quota = await read(state)
        assert attempt.status is AttemptStatus.QUEUED, "宽限内不判死"
        assert quota.reserved_bytes == 3
        sink.queue.clear()  # 宽限期内的非终态帧不参与下面的终态断言

        now[0] += 10_000
        assert await state.lifecycle.recover_ingestions() is True

        attempt, task, quota = await read(state)
        assert attempt.status is AttemptStatus.FAILED
        assert attempt.error_code == "manifest_unavailable"
        assert attempt.result_consumed_at_ms is not None
        assert task.subscription_status is SubscriptionStatus.FAILED
        assert quota.reserved_bytes == 0
        assert [message.payload["error_message"] for message in sink.queue] == [
            "下载完成后无法核对文件清单，文件已保留在下载目录中，请联系管理员处理"
        ]

        # 终态清理循环不得删掉保留的文件：清理入口只看“已收尾”标记，这里已经记下。
        assert await state.lifecycle.cleanup_terminal() is True
        assert (directory / "file.bin").read_bytes() == b"abc"


async def test_manifest_restore_waits_out_the_missing_grace_and_then_recovers(
    tmp_path: Path,
) -> None:
    """aria2 重启 reload 的瞬间查不到任务：宽限内只重试，恢复可观测后照常补清单入库。"""
    async with runtime(tmp_path) as state:
        put_download(state)
        async with SQLiteUnitOfWork(state.database) as uow:
            await DownloadRepository(uow.connection).start_ingestion(
                state.attempt_id, expected_gid=GID, now_ms=100
            )
        now = [100]
        state.lifecycle.clock_ms = lambda: now[0]

        assert await state.lifecycle.recover_ingestions() is True
        attempt, _, quota = await read(state)
        assert attempt.status is AttemptStatus.QUEUED, "首次查不到任务不算判死"
        assert quota.reserved_bytes == 3

        now[0] += 5_000
        state.backend.snapshots[GID] = observation(
            status="complete", completed=3, files=manifest_files(("file.bin", 3))
        )
        assert await state.lifecycle.recover_ingestions() is True

        attempt, task, quota = await read(state)
        assert attempt.status is AttemptStatus.COMPLETED
        assert task.subscription_status is SubscriptionStatus.COMPLETED
        assert quota.used_bytes == 3
        assert quota.reserved_bytes == 0
        assert await read_manifest(state) == (ManifestEntry("file.bin", 3),)


async def test_manifest_restore_transport_failure_waits_for_the_ingestion_timeout(
    tmp_path: Path,
) -> None:
    """连不上 aria2 不等于任务丢失：先保持可重试，超时判死的错误码是 manifest_unavailable。"""
    async with runtime(tmp_path) as state:
        directory = put_download(state)
        async with SQLiteUnitOfWork(state.database) as uow:
            await DownloadRepository(uow.connection).start_ingestion(
                state.attempt_id, expected_gid=GID, now_ms=100
            )
        now = [100]
        state.lifecycle.clock_ms = lambda: now[0]
        state.lifecycle.ingestion_timeout_ms = 10_000
        state.backend.snapshot_errors[GID] = DomainError(ErrorCode.RPC_UNAVAILABLE, "offline")

        assert await state.lifecycle.recover_ingestions() is False
        attempt, _, quota = await read(state)
        assert attempt.status is AttemptStatus.QUEUED, "暂时连不上 aria2 不判死"
        assert attempt.ingestion_started_at_ms == 100
        assert quota.reserved_bytes == 3

        now[0] += 9_999
        assert await state.lifecycle.recover_ingestions() is False
        attempt, _, _ = await read(state)
        assert attempt.status is AttemptStatus.QUEUED

        now[0] += 1
        assert await state.lifecycle.recover_ingestions() is False
        attempt, task, quota = await read(state)
        assert attempt.status is AttemptStatus.FAILED
        assert attempt.error_code == "manifest_unavailable"
        assert task.subscription_status is SubscriptionStatus.FAILED
        assert quota.reserved_bytes == 0
        assert (directory / "file.bin").read_bytes() == b"abc"


async def test_manifest_size_mismatch_is_retryable_and_never_judged_dead(
    tmp_path: Path,
) -> None:
    """aria2 报告 5 字节而磁盘只有 3 字节（还在写/异常）：按可重试处理，补齐后照常入库。"""
    async with runtime(tmp_path, total=5) as state:
        directory = put_download(state, b"abc")

        with pytest.raises(DomainError) as caught:
            await state.lifecycle.reconcile(
                observation(
                    status="complete",
                    total=5,
                    completed=5,
                    files=manifest_files(("file.bin", 5)),
                )
            )

        assert caught.value.code is ErrorCode.INVALID_STATE
        attempt, task, quota = await read(state)
        assert attempt.status is AttemptStatus.QUEUED
        assert attempt.ingestion_started_at_ms == 100, "完成意图保留，等下一轮重试"
        assert attempt.completed_content_id is None
        assert task.subscription_status is SubscriptionStatus.QUEUED
        assert quota.used_bytes == 0
        async with SQLiteUnitOfWork(state.database) as uow:
            assert (await uow.connection.execute(select(content_objects))).all() == []

        (directory / "file.bin").write_bytes(b"abcde")  # 磁盘补齐到清单大小
        assert await state.lifecycle.recover_ingestions() is True
        attempt, task, quota = await read(state)
        assert attempt.status is AttemptStatus.COMPLETED
        assert task.subscription_status is SubscriptionStatus.COMPLETED
        assert quota.used_bytes == 5


async def test_manifest_write_failure_rolls_back_the_completion_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failing_manifest(
        self: DownloadRepository,
        attempt_id: int,
        *,
        entries: tuple[ManifestEntry, ...],
        now_ms: int,
    ) -> None:
        raise RuntimeError("清单写入失败")

    async with runtime(tmp_path) as state:
        source = put_download(state)
        monkeypatch.setattr(DownloadRepository, "record_manifest", failing_manifest)
        with pytest.raises(RuntimeError):
            await state.lifecycle.reconcile(
                observation(
                    status="complete",
                    completed=3,
                    files=(ObservedFile("file.bin", 3, 3, True, "file.bin"),),
                )
            )

        attempt, task, _ = await read(state)
        # 清单写入失败 → 同一事务里先写的完成意图一起回滚，不可能出现“意图在、清单丢”。
        assert attempt.ingestion_started_at_ms is None
        assert attempt.completed_content_id is None
        assert attempt.status is AttemptStatus.QUEUED
        assert task.subscription_status is SubscriptionStatus.QUEUED
        assert await read_manifest(state) is None
        assert source.is_dir()  # 半成品未被当成成功结果删掉


async def test_restart_recovery_ingestion_keeps_the_manifest_written_at_completion(
    tmp_path: Path,
) -> None:
    """恢复路径的完成意图写入（_ingest(attempt, None)）不改写也不丢失已有清单。"""
    async with runtime(tmp_path) as state:
        put_download(state)
        async with SQLiteUnitOfWork(state.database) as uow:
            repository = DownloadRepository(uow.connection)
            await repository.start_ingestion(state.attempt_id, expected_gid=GID, now_ms=2)
            await repository.record_manifest(
                state.attempt_id, entries=(ManifestEntry("file.bin", 3),), now_ms=2
            )

        await state.database.close()
        state.database = SQLiteDatabase(tmp_path / "v2.db")
        restarted = make_lifecycle(state)
        assert await restarted.recover_ingestions() is True

        attempt, _, quota = await read(state)
        assert attempt.status is AttemptStatus.COMPLETED
        assert quota.used_bytes == 3
        assert await read_manifest(state) == (ManifestEntry("file.bin", 3),)


async def test_torrent_subset_completion_ignores_control_file_residue(tmp_path: Path) -> None:
    """回归（缺陷2）：选择子集 torrent 完成后 aria2 按 <种子名>.aria2 留下控制文件；
    清单只含选中的文件，残留既不参与入库也没被删（不再是“清扫后重扫描”）。"""
    async with runtime(tmp_path, kind="torrent", total=3) as state:
        store = RecordingStore(state.store)
        state.lifecycle.content_store = store  # type: ignore[assignment]
        directory = state.root / str(state.attempt_id)
        (directory / "bundle").mkdir(parents=True)
        (directory / "bundle" / "file.bin").write_bytes(b"abc")
        (directory / "bundle.aria2").write_bytes(b"aria2 control")
        await state.lifecycle.reconcile(
            observation(
                status="complete",
                completed=3,
                files=manifest_files(("bundle/file.bin", 3)),
            )
        )

        attempt, task, quota = await read(state)
        assert attempt.status is AttemptStatus.COMPLETED
        assert store.collect_calls == [
            (state.attempt_id, (ManifestEntry("bundle/file.bin", 3),))
        ]
        assert store.install_calls == [
            (state.attempt_id, (ManifestEntry("bundle/file.bin", 3),))
        ]
        assert task.subscription_status is SubscriptionStatus.COMPLETED
        assert quota.used_bytes == 3
        async with SQLiteUnitOfWork(state.database) as uow:
            rows = (
                await uow.connection.execute(
                    select(content_entries.c.path)
                    .where(content_entries.c.content_id == attempt.completed_content_id)
                    .order_by(content_entries.c.path)
                )
            ).scalars().all()
        assert rows == ["bundle", "bundle/file.bin"], "控制文件不进内容"


async def test_magnet_completion_ingests_the_reported_name_and_ignores_residue(
    tmp_path: Path,
) -> None:
    """生产回归（ingestion_timeout）：无 dn 磁力的 display_name 是 info 哈希，
    aria2 却用种子真名落盘；入库只听清单，清单外残留（含 .aria2）不参与也不被删。"""
    async with runtime(
        tmp_path, kind="magnet", total=3, display_name="ab" * 20, pause_code=None
    ) as state:
        store = RecordingStore(state.store)
        state.lifecycle.content_store = store  # type: ignore[assignment]
        directory = state.root / str(state.attempt_id)
        directory.mkdir()
        (directory / "Real Torrent Name.bin").write_bytes(b"abc")
        (directory / "Real Torrent Name.aria2").write_bytes(b"aria2 control")

        await state.lifecycle.reconcile(
            observation(
                status="complete",
                completed=3,
                files=manifest_files(("Real Torrent Name.bin", 3)),
            )
        )

        attempt, task, quota = await read(state)
        assert attempt.status is AttemptStatus.COMPLETED
        assert attempt.completed_content_id is not None
        assert store.collect_calls == [
            (state.attempt_id, (ManifestEntry("Real Torrent Name.bin", 3),))
        ]
        async with SQLiteUnitOfWork(state.database) as uow:
            content = await ContentRepository(uow.connection).get_object(
                attempt.completed_content_id
            )
        assert content.kind is ContentKind.FILE
        assert content.digest == hashlib.sha256(b"abc").hexdigest()
        assert (state.root / content.storage_path).read_bytes() == b"abc"
        assert task.subscription_status is SubscriptionStatus.COMPLETED
        assert quota.used_bytes == 3


async def test_bt_completion_ingests_only_the_reported_multi_file_manifest(
    tmp_path: Path,
) -> None:
    """BT 多文件：清单带子目录路径，主目录与子文件旁的控制文件残留都不进内容。"""
    async with runtime(tmp_path, kind="torrent", total=6) as state:
        store = RecordingStore(state.store)
        state.lifecycle.content_store = store  # type: ignore[assignment]
        directory = state.root / str(state.attempt_id)
        (directory / "bundle").mkdir(parents=True)
        (directory / "bundle" / "file.bin").write_bytes(b"abc")
        (directory / "bundle" / "extra.bin").write_bytes(b"def")
        (directory / "bundle.aria2").write_bytes(b"aria2 control")
        (directory / "bundle" / "file.bin.aria2").write_bytes(b"aria2 control")

        await state.lifecycle.reconcile(
            observation(
                status="complete",
                completed=6,
                files=manifest_files(("bundle/file.bin", 3), ("bundle/extra.bin", 3)),
            )
        )

        attempt, _, quota = await read(state)
        assert attempt.status is AttemptStatus.COMPLETED
        assert store.collect_calls == [
            (
                state.attempt_id,
                (ManifestEntry("bundle/file.bin", 3), ManifestEntry("bundle/extra.bin", 3)),
            )
        ]
        assert quota.used_bytes == 6
        async with SQLiteUnitOfWork(state.database) as uow:
            content = await ContentRepository(uow.connection).get_object(
                attempt.completed_content_id
            )
            rows = (
                await uow.connection.execute(
                    select(content_entries.c.path)
                    .where(content_entries.c.content_id == content.id)
                    .order_by(content_entries.c.path)
                )
            ).scalars().all()
        assert rows == ["bundle", "bundle/extra.bin", "bundle/file.bin"]
        assert not (state.root / content.storage_path / "bundle.aria2").exists()


async def test_http_user_content_named_aria2_suffix_is_content_when_reported(
    tmp_path: Path,
) -> None:
    """回归（评审阻断项）：`.aria2` 后缀不再是判据。

    清单报了它 → 就是用户内容，照常入库，绝不删；清单没报它 → 不参与入库。
    """
    async with runtime(tmp_path) as state:
        directory = state.root / str(state.attempt_id)
        directory.mkdir()
        (directory / "user-data.aria2").write_bytes(b"user content")

        await state.lifecycle.reconcile(
            observation(
                status="complete",
                completed=12,
                files=manifest_files(("user-data.aria2", 12)),
            )
        )

        attempt, _, quota = await read(state)
        assert attempt.status is AttemptStatus.COMPLETED
        assert quota.used_bytes == 12
        async with SQLiteUnitOfWork(state.database) as uow:
            content = await ContentRepository(uow.connection).get_object(
                attempt.completed_content_id
            )
        assert content.digest == hashlib.sha256(b"user content").hexdigest()
        assert (state.root / content.storage_path).read_bytes() == b"user content"


async def test_ingestion_never_parses_the_source_payload(tmp_path: Path) -> None:
    """入库只看清单，不解析种子载荷：载荷坏掉也不会变成新的致命错误来源。"""
    async with runtime(tmp_path, kind="torrent", total=3) as state:
        async with SQLiteUnitOfWork(state.database) as uow:
            await uow.connection.execute(
                text("UPDATE download_sources SET payload_json = :payload WHERE id = 1"),
                {"payload": '{"torrent": 123}'},
            )
        directory = state.root / str(state.attempt_id)
        directory.mkdir()
        (directory / "file.bin").write_bytes(b"abc")
        (directory / "file.bin.aria2").write_bytes(b"aria2 control")

        await state.lifecycle.reconcile(
            observation(
                status="complete",
                completed=3,
                files=manifest_files(("file.bin", 3)),
            )
        )

        attempt, _, _ = await read(state)
        assert attempt.status is AttemptStatus.COMPLETED


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
        await state.lifecycle.reconcile(
            observation(
                status="complete", completed=3, files=manifest_files(("file.bin", 3))
            )
        )

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
        original_collect = state.store.collect

        async def slow_collect(attempt_id: int, manifest):
            started.set()
            await release.wait()
            return await original_collect(attempt_id, manifest)

        state.store.collect = slow_collect  # type: ignore[method-assign]
        completing = asyncio.create_task(
            state.lifecycle.reconcile(
                observation(
                    status="complete", completed=3, files=manifest_files(("file.bin", 3))
                )
            )
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


async def leave_no_tasks(state: Runtime) -> None:
    """清空任务表：构造真正空闲的轮询起点（无 live 任务、无入库意图）。"""
    async with SQLiteUnitOfWork(state.database) as uow:
        await uow.connection.execute(text("DELETE FROM task_subscriptions"))
        await uow.connection.execute(text("DELETE FROM download_attempts"))


async def poll_cycles(lifecycle: DownloadLifecycleService, cycles: int) -> None:
    delays: list[float] = []

    async def stop(delay: float) -> None:
        delays.append(delay)
        if len(delays) == cycles:
            raise asyncio.CancelledError

    lifecycle.sleep = stop
    with pytest.raises(asyncio.CancelledError):
        await lifecycle.run_polling()


async def test_idle_polling_probe_flips_backend_status_both_directions(
    tmp_path: Path,
) -> None:
    async with runtime(tmp_path) as state:
        await leave_no_tasks(state)
        state.connectivity.record_failure()
        state.connectivity.record_failure()
        assert state.connectivity.status(is_admin=True).status == "degraded"

        await poll_cycles(state.lifecycle, 2)
        assert state.backend.version_calls == 2
        assert state.connectivity.status(is_admin=True).status == "ok"

        state.backend.version_error = DomainError(ErrorCode.RPC_UNAVAILABLE, "offline")
        await poll_cycles(state.lifecycle, 1)
        assert state.connectivity.status(is_admin=True).status == "ok"
        await poll_cycles(state.lifecycle, 1)
        assert state.connectivity.status(is_admin=True).status == "degraded"

        state.backend.version_error = None
        await poll_cycles(state.lifecycle, 1)
        assert state.connectivity.status(is_admin=True).status == "ok"


async def test_polling_probe_timeout_does_not_stall_the_loop(tmp_path: Path) -> None:
    async with runtime(tmp_path) as state:
        await leave_no_tasks(state)
        state.lifecycle.probe_timeout_seconds = 0.01
        state.backend.version_hangs = True
        started = time.monotonic()
        await poll_cycles(state.lifecycle, 2)
        assert time.monotonic() - started < 2
        assert state.backend.version_calls == 2
        assert state.connectivity.status(is_admin=True).status == "degraded"


async def test_restart_resumes_installed_completion_intent(tmp_path: Path) -> None:
    async with runtime(tmp_path) as state:
        put_download(state)
        entries = (ManifestEntry("file.bin", 3),)
        preparation = await state.store.collect(state.attempt_id, entries)
        async with SQLiteUnitOfWork(state.database) as uow:
            content_id = await ContentRepository(uow.connection).create_object(
                kind=preparation.identity.kind.value,
                digest=preparation.identity.digest,
                size=preparation.size,
                storage_path=preparation.storage_path,
                now_ms=2,
            )
            repository = DownloadRepository(uow.connection)
            await repository.begin_completion(
                state.attempt_id, expected_gid=GID, content_id=content_id, now_ms=2
            )
            await repository.record_manifest(state.attempt_id, entries=entries, now_ms=2)
        await state.store.install(state.attempt_id, preparation, entries)

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
        state.backend.snapshots[GID] = observation(
            status="paused", files=manifest_files(("file.bin", 3))
        )
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
        await state.lifecycle.reconcile(
            observation(
                status="complete",
                total=6,
                completed=6,
                files=manifest_files(("file.bin", 3), ("nested/child.bin", 3)),
            )
        )
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
            observation(
                PAYLOAD_GID,
                "complete",
                total=6,
                completed=6,
                files=manifest_files(("file.bin", 3), ("nested/child.bin", 3)),
            )
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
        entries = (ManifestEntry("file.bin", 3),)
        preparation = await state.store.collect(state.attempt_id, entries)
        async with SQLiteUnitOfWork(state.database) as uow:
            await ContentRepository(uow.connection).create_object(
                kind="file", digest=preparation.identity.digest, size=99,
                storage_path="objects/conflict", now_ms=2,
            )
        with pytest.raises(DomainError) as caught:
            await state.lifecycle.reconcile(
                observation(
                    status="complete", completed=3, files=manifest_files(("file.bin", 3))
                )
            )
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
        await state.lifecycle.reconcile(
            observation(
                status="complete", completed=3, files=manifest_files(("file.bin", 3))
            )
        )
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
        entries = (ManifestEntry("file.bin", 3),)
        collected = await state.store.collect(state.attempt_id, entries)
        async with SQLiteUnitOfWork(state.database) as uow:
            content_id = await ContentRepository(uow.connection).create_object(
                kind="file", digest=collected.identity.digest, size=3,
                storage_path=collected.storage_path, now_ms=2,
            )
            repository = DownloadRepository(uow.connection)
            await repository.begin_completion(
                state.attempt_id, expected_gid=GID, content_id=content_id, now_ms=2
            )
            await repository.record_manifest(state.attempt_id, entries=entries, now_ms=2)
        import shutil
        shutil.rmtree(preparation)
        assert await state.lifecycle.recover_ingestions() is False
        attempt, _, _ = await read(state)
        assert attempt.status is AttemptStatus.QUEUED

        put_download(state)
        async def stale(*args: object, **kwargs: object) -> bool:
            raise DomainError(ErrorCode.STALE_EVENT, "stale")
        monkeypatch.setattr(DownloadRepository, "finalize_completion", stale)
        await state.lifecycle.reconcile(
            observation(
                status="complete", completed=3, files=manifest_files(("file.bin", 3))
            )
        )
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


async def test_notification_for_an_unmanaged_gid_never_asks_aria2(tmp_path: Path) -> None:
    """未知 GID（不是本平台的 attempt）直接忽略：没有 attempt 就没有相对路径的基准目录。"""
    async with runtime(tmp_path) as state:
        state.backend.snapshots["unmanaged"] = observation("unmanaged", "active")

        await state.lifecycle._on_gid("unmanaged")

        assert state.backend.snapshot_calls == []
        assert state.observations.get("unmanaged") is None


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


async def test_ingestion_started_before_collect_and_missing_gid_recovers_after_restart(
    tmp_path: Path,
) -> None:
    async with runtime(tmp_path) as state:
        put_download(state)
        original_collect = state.store.collect

        async def collect_failed(attempt_id: int, manifest: object) -> object:
            raise OSError("collect offline")

        state.store.collect = collect_failed  # type: ignore[method-assign]
        with pytest.raises(OSError, match="collect offline"):
            await state.lifecycle.reconcile(
                observation(
                    status="complete", completed=3, files=manifest_files(("file.bin", 3))
                )
            )
        attempt, _, _ = await read(state)
        assert attempt.ingestion_started_at_ms == 100
        assert attempt.completed_content_id is None

        state.store.collect = original_collect  # type: ignore[method-assign]
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


@pytest.mark.parametrize("stage", ["collect", "install", "finalize"])
async def test_persistent_ingestion_failure_times_out_without_resetting_age(
    tmp_path: Path, stage: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with runtime(tmp_path / stage) as state:
        put_download(state)

        async def failed(*args: object, **kwargs: object) -> object:
            raise OSError(f"{stage} offline")

        if stage in {"collect", "install"}:
            monkeypatch.setattr(state.store, stage, failed)
        else:
            monkeypatch.setattr(DownloadRepository, "finalize_completion", failed)
        now = [100]
        state.lifecycle.clock_ms = lambda: now[0]
        state.lifecycle.ingestion_timeout_ms = 10_000
        with pytest.raises(OSError, match=f"{stage} offline"):
            await state.lifecycle.reconcile(
                observation(
                    status="complete", completed=3, files=manifest_files(("file.bin", 3))
                )
            )
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
        original_collect = state.store.collect

        async def collect_failed(attempt_id: int, manifest: object) -> object:
            raise OSError("collect offline")

        state.store.collect = collect_failed  # type: ignore[method-assign]
        with pytest.raises(OSError, match="collect offline"):
            await state.lifecycle.reconcile(
                observation(
                    status="complete", completed=3, files=manifest_files(("file.bin", 3))
                )
            )
        state.store.collect = original_collect  # type: ignore[method-assign]

        await state.lifecycle.reconcile(observation(status=late_status, completed=3))
        attempt, _, quota = await read(state)
        assert attempt.status is AttemptStatus.COMPLETED
        assert quota.used_bytes == 3


async def test_ingestion_timeout_cleanup_failure_remains_retryable(tmp_path: Path) -> None:
    async with runtime(tmp_path) as state:
        put_download(state)

        async def collect_failed(attempt_id: int, manifest: object) -> object:
            raise OSError("collect offline")

        state.store.collect = collect_failed  # type: ignore[method-assign]
        with pytest.raises(OSError, match="collect offline"):
            await state.lifecycle.reconcile(
                observation(
                    status="complete", completed=3, files=manifest_files(("file.bin", 3))
                )
            )
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
        original_collect = state.store.collect

        async def collect_failed(attempt_id: int, manifest: object) -> object:
            raise OSError("collect offline")

        state.store.collect = collect_failed  # type: ignore[method-assign]
        with pytest.raises(OSError, match="collect offline"):
            await state.lifecycle.reconcile(
                observation(
                    status="complete", completed=3, files=manifest_files(("file.bin", 3))
                )
            )
        state.store.collect = original_collect  # type: ignore[method-assign]

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
        await state.lifecycle.reconcile(
            observation(
                PAYLOAD_GID,
                "complete",
                completed=3,
                files=manifest_files(("file.bin", 3)),
            )
        )

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

        async def snapshots(*, gid: str, attempt_id: int) -> DownloadObservation | None:
            nonlocal calls
            calls += 1
            if calls == 3:
                state.backend.snapshots[GID] = observation(status="active")
            return await original(gid=gid, attempt_id=attempt_id)

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

        async def snapshots(*, gid: str, attempt_id: int) -> DownloadObservation | None:
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

        async def snapshots(*, gid: str, attempt_id: int) -> DownloadObservation | None:
            nonlocal calls
            calls += 1
            if calls == 2:
                async with SQLiteUnitOfWork(state.database) as uow:
                    await DownloadRepository(uow.connection).fail_attempt(
                        state.attempt_id, error_code="cancelled_elsewhere", now_ms=now[0]
                    )
            return await original(gid=gid, attempt_id=attempt_id)

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
        original_collect = state.store.collect
        entered = asyncio.Event()
        release = asyncio.Event()

        async def gated_collect(attempt_id: int, manifest: object):
            entered.set()
            await release.wait()
            return await original_collect(attempt_id, manifest)

        state.store.collect = gated_collect  # type: ignore[method-assign]
        completing = asyncio.create_task(
            state.lifecycle.reconcile(
                observation(
                    status="complete", completed=3, files=manifest_files(("file.bin", 3))
                )
            )
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
        assert not (state.root / ".ingest").exists(), "入库没开始就不该有暂存目录"


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

        async def gated_install(attempt_id: int, preparation: object, manifest: object):
            entered.set()
            await release.wait()
            return await original_install(attempt_id, preparation, manifest)

        state.store.install = gated_install  # type: ignore[method-assign]
        completing = asyncio.create_task(
            state.lifecycle.reconcile(
                observation(
                    status="complete", completed=3, files=manifest_files(("file.bin", 3))
                )
            )
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

        async def gated_install(attempt_id: int, preparation: object, manifest: object):
            entered.set()
            await release.wait()
            return await original_install(attempt_id, preparation, manifest)

        state.store.install = gated_install  # type: ignore[method-assign]
        completing = asyncio.create_task(
            state.lifecycle.reconcile(
                observation(
                    status="complete", completed=3, files=manifest_files(("file.bin", 3))
                )
            )
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


async def test_actual_size_settlement_uses_content_size_not_declared_total(
    tmp_path: Path,
) -> None:
    """R4 回归锁定：声明 3 字节但实际入库 5 字节时，按实际大小记账。"""

    async with runtime(tmp_path, total=3, quota=100) as state:
        put_download(state, b"abcde")
        complete = DownloadObservation(
            gid=GID,
            observed_at_ms=100,
            status="complete",
            download_speed=0,
            upload_speed=0,
            peer_count=0,
            connection_count=0,
            total_length=3,
            completed_length=3,
            followed_by=(),
            files=manifest_files(("file.bin", 5)),
        )
        await state.lifecycle.reconcile(complete)

        attempt, task, quota = await read(state)
        assert attempt.status is AttemptStatus.COMPLETED
        assert quota.used_bytes == 5
        assert quota.reserved_bytes == 0
        assert task.subscription_status is SubscriptionStatus.COMPLETED


async def test_actual_size_over_quota_fails_cleans_and_keeps_accounting_legal(
    tmp_path: Path,
) -> None:
    """R4/R5：实测超出剩余配额 → 任务失败、预留释放、用量不增、磁盘不残留。"""

    async with runtime(tmp_path, total=3, quota=4) as state:
        source = put_download(state, b"abcde")
        complete = DownloadObservation(
            gid=GID,
            observed_at_ms=100,
            status="complete",
            download_speed=0,
            upload_speed=0,
            peer_count=0,
            connection_count=0,
            total_length=3,
            completed_length=3,
            followed_by=(),
            files=manifest_files(("file.bin", 5)),
        )
        await state.lifecycle.reconcile(complete)

        attempt, task, quota = await read(state)
        assert attempt.status is AttemptStatus.FAILED
        assert attempt.error_code == ErrorCode.QUOTA_EXCEEDED.value
        assert task.subscription_status is SubscriptionStatus.FAILED
        assert task.reserved_bytes == 0
        assert quota.used_bytes == 0
        assert quota.reserved_bytes == 0
        assert not source.exists()
        # 超额失败不留下任何用户文件引用
        async with SQLiteUnitOfWork(state.database) as uow:
            owned = (
                await uow.connection.execute(
                    select(user_files.c.id).where(user_files.c.user_id == state.user_id)
                )
            ).all()
        assert owned == []


INFO_HASH = "ab" * 20
MAGNET_URI = f"magnet:?xt=urn:btih:{INFO_HASH}"


def named_observation(name: str | None, *, status: str = "active") -> DownloadObservation:
    return dataclass_replace(observation(status=status, total=None), name=name)


async def test_magnet_hash_fallback_name_is_replaced_by_the_seeded_name(tmp_path: Path) -> None:
    """缺陷 2：无 dn 磁力任务元数据到达后，哈希名自动变成种子真名（三端同源）。"""
    async with runtime(
        tmp_path,
        kind="magnet",
        total=None,
        pause_code=None,
        display_name=INFO_HASH,
        display_uri=MAGNET_URI,
    ) as state:
        await state.lifecycle.reconcile(named_observation("Big Buck Bunny"))

        _, task, _ = await read(state)
        assert task.name == "Big Buck Bunny"
        projection = TaskProjector(
            observations=state.observations, clock_ms=lambda: 100
        ).project(task)
        assert projection.name == "Big Buck Bunny"
        payload = tell_status_payload(projection, "g")
        files = payload["files"]
        assert isinstance(files, list)
        assert files[0]["path"] == "./Big Buck Bunny", "RPC files[0].path 与 REST name 同步"

        # 重复观测同名是幂等的（不会反复改 updated_at_ms 造出重复推送）。
        await state.lifecycle.reconcile(named_observation("Big Buck Bunny"))
        _, again, _ = await read(state)
        assert again.name == "Big Buck Bunny"


async def test_user_supplied_display_name_is_never_overwritten(tmp_path: Path) -> None:
    """out 自定义名（以及任何非哈希兜底名）不参与改名。"""
    async with runtime(
        tmp_path,
        kind="magnet",
        total=None,
        pause_code=None,
        display_name="my-own-name.mkv",
        display_uri=MAGNET_URI,
    ) as state:
        await state.lifecycle.reconcile(named_observation("Big Buck Bunny"))

        _, task, _ = await read(state)
        assert task.name == "my-own-name.mkv"


async def test_observation_without_name_keeps_the_fallback_name(tmp_path: Path) -> None:
    async with runtime(
        tmp_path,
        kind="magnet",
        total=None,
        pause_code=None,
        display_name=INFO_HASH,
        display_uri=MAGNET_URI,
    ) as state:
        await state.lifecycle.reconcile(named_observation(None))

        _, task, _ = await read(state)
        assert task.name == INFO_HASH


async def test_rename_happens_before_ingestion_so_the_file_gets_the_real_name(
    tmp_path: Path,
) -> None:
    """入库文件名取的是订阅显示名：改名的挂载点必须在终态处理之前。"""
    async with runtime(
        tmp_path,
        kind="magnet",
        total=None,
        pause_code=None,
        display_name=INFO_HASH,
        display_uri=MAGNET_URI,
    ) as state:
        put_download(state)
        await state.lifecycle.reconcile(
            dataclass_replace(
                named_observation("Big Buck Bunny"),
                status="complete",
                files=manifest_files(("file.bin", 3)),
            )
        )

        attempt, task, _ = await read(state)
        assert attempt.status is AttemptStatus.COMPLETED
        assert task.name == "Big Buck Bunny"
        async with SQLiteUnitOfWork(state.database) as uow:
            rows = (
                await uow.connection.execute(
                    select(user_files).where(user_files.c.user_id == state.user_id)
                )
            ).all()
        assert [row.display_name for row in rows] == ["Big Buck Bunny"]
