from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
import zipfile
from pathlib import Path

import pytest
from sqlalchemy import text

from aria2deck.application.packs.control import PackControlService
from aria2deck.application.packs.dispatcher import PackDispatcher
from aria2deck.application.packs.models import ArchiveResult, ArchiveSource
from aria2deck.domain.content import ContentKind
from aria2deck.domain.errors import DomainError, ErrorCode
from aria2deck.domain.packs import PackStatus, PackStep, PreparedOutput
from aria2deck.domain.quota import Quota
from aria2deck.domain.settings import SystemSettings
from aria2deck.infrastructure.archive_writer import ArchiveWriter
from aria2deck.infrastructure.content_store import DownloadContentStore
from aria2deck.infrastructure.pack_writer import PackWriterControl
from aria2deck.infrastructure.sqlite.engine import SQLiteDatabase
from aria2deck.infrastructure.sqlite.repositories.content import ContentRepository
from aria2deck.infrastructure.sqlite.repositories.packs import PackRepository
from aria2deck.infrastructure.sqlite.repositories.quota import QuotaRepository
from aria2deck.infrastructure.sqlite.unit_of_work import SQLiteUnitOfWork

from .test_library_services import FakeClock
from .test_pack_admission import (
    create_pack_user,
    make_admission,
    seed_pack_file,
    setup_pack_world,
)

RETRY_DELAYS = (250, 1000, 5000, 15000)


class FlakyWriter:
    """Delegates to a real writer after N transient failures."""

    def __init__(self, failures: int, *, chunk_size: int | None = None) -> None:
        self.failures = failures
        self.inner = ArchiveWriter(chunk_size=chunk_size) if chunk_size else ArchiveWriter()
        self.observed_options: list[tuple[str, int]] = []

    async def write_archive(
        self,
        destination: Path,
        *,
        sources,
        pack_format,
        compression_level,
        max_output_bytes: int,
        min_free_bytes: int,
        cancel_event: threading.Event,
        on_progress=None,
    ):
        self.observed_options.append((pack_format, compression_level))
        if self.failures > 0:
            self.failures -= 1
            raise OSError("simulated storage hiccup")
        return await self.inner.write_archive(
            destination,
            sources=sources,
            pack_format=pack_format,
            compression_level=compression_level,
            max_output_bytes=max_output_bytes,
            min_free_bytes=min_free_bytes,
            cancel_event=cancel_event,
            on_progress=on_progress,
        )


class BlockingWriter:
    """Blocks inside compression until the shared cancel event is set, then records the status it saw."""

    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database
        self.started = asyncio.Event()
        self.observed: list[str] = []
        self.calls = 0

    async def write_archive(
        self,
        destination: Path,
        *,
        sources,
        pack_format,
        compression_level,
        max_output_bytes: int,
        min_free_bytes: int,
        cancel_event: threading.Event,
        on_progress=None,
    ):
        self.calls += 1
        self.started.set()
        while not cancel_event.is_set():
            await asyncio.sleep(0.005)
        database = SQLiteDatabase(self.database.path)
        try:
            async with SQLiteUnitOfWork(database) as uow:
                status = (
                    await uow.connection.execute(text("SELECT status FROM pack_jobs ORDER BY id"))
                ).scalar_one()
        finally:
            await database.close()
        self.observed.append(str(status))
        raise InterruptedError("打包写入已停止")


class StubbornWriter:
    """Ignores the stop event until the test releases it, to prove the bounded wait and safe pause."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def write_archive(
        self,
        destination: Path,
        *,
        sources,
        pack_format,
        compression_level,
        max_output_bytes: int,
        min_free_bytes: int,
        cancel_event: threading.Event,
        on_progress=None,
    ) -> ArchiveResult:
        self.calls += 1
        self.started.set()
        await self.release.wait()
        raise InterruptedError("打包写入已停止")


class GatedContentStore(DownloadContentStore):
    """Blocks install_prepared until the test opens the gate."""

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.install_entered = asyncio.Event()
        self.install_gate = asyncio.Event()
        self.install_returned = False

    async def install_prepared(self, storage_path: str, digest: str):
        self.install_entered.set()
        await self.install_gate.wait()
        outcome = await super().install_prepared(storage_path, digest)
        self.install_returned = True
        return outcome


class RecordingControl:
    """Delegates to PackWriterControl and counts registrations."""

    def __init__(self, inner: PackWriterControl | None = None) -> None:
        self.inner = inner or PackWriterControl()
        self.registered: list[int] = []

    def register(
        self, pack_id: int, writer: asyncio.Task[None], cancel_event: threading.Event
    ) -> bool:
        self.registered.append(pack_id)
        return self.inner.register(pack_id, writer, cancel_event)

    async def stop_and_wait(self, pack_id: int) -> None:
        await self.inner.stop_and_wait(pack_id)

    async def wait_for_writer(self, pack_id: int, writer: asyncio.Task[None]) -> None:
        await self.inner.wait_for_writer(pack_id, writer)

    def allows_resume(self, pack_id: int) -> bool:
        return self.inner.allows_resume(pack_id)


class FrozenStepRepository(PackRepository):
    """update_step never advances: the dispatcher must stop the stage instead of guessing."""

    async def update_step(
        self, pack_id: int, *, from_step: PackStep, to_step: PackStep, now_ms: int
    ) -> bool:
        return False


class RecordingWriter:
    """Writes for real and records the frozen budget, disk floor and cancellation event."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.inner = ArchiveWriter()

    async def write_archive(
        self,
        destination: Path,
        *,
        sources,
        pack_format,
        compression_level,
        max_output_bytes: int,
        min_free_bytes: int,
        cancel_event: threading.Event,
        on_progress=None,
    ) -> ArchiveResult:
        self.calls.append(
            {
                "max_output_bytes": max_output_bytes,
                "min_free_bytes": min_free_bytes,
                "cancel_event": cancel_event,
            }
        )
        return await self.inner.write_archive(
            destination,
            sources=sources,
            pack_format=pack_format,
            compression_level=compression_level,
            max_output_bytes=max_output_bytes,
            min_free_bytes=min_free_bytes,
            cancel_event=cancel_event,
            on_progress=on_progress,
        )


class OversizedWriter:
    """Writes one byte past the frozen budget to simulate a diverged writer."""

    def __init__(self) -> None:
        self.budgets: list[int] = []

    async def write_archive(
        self,
        destination: Path,
        *,
        sources,
        pack_format,
        compression_level,
        max_output_bytes: int,
        min_free_bytes: int,
        cancel_event: threading.Event,
        on_progress=None,
    ) -> ArchiveResult:
        self.budgets.append(max_output_bytes)
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload = b"o" * (max_output_bytes + 1)
        destination.write_bytes(payload)
        return ArchiveResult(len(payload), hashlib.sha256(payload).hexdigest())


class FailingWriter:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0

    async def write_archive(
        self,
        destination: Path,
        *,
        sources,
        pack_format,
        compression_level,
        max_output_bytes: int,
        min_free_bytes: int,
        cancel_event: threading.Event,
        on_progress=None,
    ) -> ArchiveResult:
        self.calls += 1
        raise self.error


class QuotaExhaustedRepository(PackRepository):
    """完成记账时抛出配额错误，模拟受控损坏/旧数据而非正常降配额。"""

    async def complete_job(self, pack_id: int, *, now_ms: int) -> int:
        raise DomainError(ErrorCode.QUOTA_EXCEEDED, "配额账目不一致")


def make_dispatcher(
    database: SQLiteDatabase,
    clock: FakeClock,
    store: DownloadContentStore,
    *,
    writer=None,
    control: PackWriterControl | None = None,
    settings: SystemSettings | None = None,
    repository_factory=None,
) -> PackDispatcher:
    resolved_settings = settings or SystemSettings(
        pack_format="zip", pack_compression_level=3, min_free_disk=0
    )
    resolved_repository = repository_factory or (lambda uow: PackRepository(uow.connection))
    return PackDispatcher(
        uow_factory=lambda: SQLiteUnitOfWork(database),
        repository_factory=resolved_repository,
        quota_repository_factory=lambda uow: QuotaRepository(uow.connection),
        writer=writer or ArchiveWriter(),
        content_store=store,
        writer_control=control or PackWriterControl(),
        settings=lambda: resolved_settings,
        clock_ms=clock,
    )


def make_pack_control_service(
    database: SQLiteDatabase,
    clock: FakeClock,
    store: DownloadContentStore,
    control: PackWriterControl,
) -> PackControlService:
    return PackControlService(
        uow_factory=lambda: SQLiteUnitOfWork(database),
        repository_factory=lambda uow: PackRepository(uow.connection),
        content_repository_factory=lambda uow: ContentRepository(uow.connection),
        quota_repository_factory=lambda uow: QuotaRepository(uow.connection),
        stop_writer=control.stop_and_wait,
        clear_writer=control.clear,
        prepared_store=store,
        clock_ms=clock,
        lease_owner="content-deletion",
        lease_ttl_ms=300_000,
        lock=asyncio.Lock(),
    )


async def corrupt_quota(database: SQLiteDatabase, user_id: int) -> None:
    """受控损坏：把用户上限压到 used+reserved 以下，让账目读取抛 QUOTA_EXCEEDED。"""
    async with SQLiteUnitOfWork(database) as uow:
        await uow.connection.execute(
            text("UPDATE users SET quota_bytes = 0 WHERE id = :id"), {"id": user_id}
        )


async def get_job(database: SQLiteDatabase, pack_id: int):
    async with SQLiteUnitOfWork(database) as uow:
        return await PackRepository(uow.connection).get_job(pack_id)


async def create_pack(
    database: SQLiteDatabase,
    clock: FakeClock,
    store: DownloadContentStore,
    user_id: int,
    file_ids: list[int],
    *,
    output_name: str = "bundle.zip",
    delete_source: bool = False,
) -> int:
    service = make_admission(database, clock, store)
    return await service.create(
        user_id, user_file_ids=file_ids, output_name=output_name, delete_source=delete_source
    )


async def test_full_chain_completes_installs_output_and_converts_quota(
    tmp_path: Path,
) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    _, first = await seed_pack_file(database, root, user_id, 1, payload=b"a" * 10)
    _, second = await seed_pack_file(database, root, user_id, 2, payload=b"b" * 25)
    store = DownloadContentStore(root)
    pack_id = await create_pack(database, clock, store, user_id, [first, second])
    second_pack = await create_pack(database, clock, store, user_id, [first], output_name="later.zip")

    dispatcher = make_dispatcher(database, clock, store)
    await dispatcher.step_once()
    job = await get_job(database, pack_id)
    assert job.status is PackStatus.DONE
    assert job.step is None and job.step_progress == 100
    assert (await get_job(database, second_pack)).status is PackStatus.PENDING  # 单活跃。

    digest = job.output_content_key.split(":", 1)[1]
    canonical = root / "objects" / f"file-{digest}"
    assert canonical.is_file()
    assert not (root / "packs" / f"prepared-{pack_id}.part").exists()
    with zipfile.ZipFile(canonical) as archive:
        assert archive.read("src-1") == b"a" * 10
        assert archive.read("src-2") == b"b" * 25

    async with SQLiteUnitOfWork(database) as uow:
        content = ContentRepository(uow.connection)
        output_file = await content.find_user_file(user_id=user_id, content_id=job.output_content_id)
        quota = await QuotaRepository(uow.connection).get(user_id)
        rebuilt = await QuotaRepository(uow.connection).rebuild(user_id)
        sources = await PackRepository(uow.connection).list_sources(pack_id)
    assert output_file is not None
    # 第二个待执行任务仍占用完整输出预算（10 + 522 + 65536）；used 已含来源与输出。
    assert quota == Quota(100 * 1024 * 1024, 35 + canonical.stat().st_size, 66_068)
    assert rebuilt == quota
    assert [source.cleanup_status for source in sources] == ["kept", "kept"]

    clock.advance(RETRY_DELAYS[0])  # 第二个任务在下一轮被领取。
    await dispatcher.step_once()
    assert (await get_job(database, second_pack)).status is PackStatus.DONE


async def test_dispatcher_uses_non_default_compression_level(tmp_path: Path) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    _, file_id = await seed_pack_file(database, root, user_id, 1)
    store = DownloadContentStore(root)
    pack_id = await create_pack(database, clock, store, user_id, [file_id])
    writer = FlakyWriter(0)
    dispatcher = make_dispatcher(database, clock, store, writer=writer)

    await dispatcher.step_once()

    assert (await get_job(database, pack_id)).status is PackStatus.DONE
    assert writer.observed_options == [("zip", 3)]


async def test_compression_progress_persists_monotonically(tmp_path: Path) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    payload = bytes(range(256)) * 1024  # 256 KiB
    _, big = await seed_pack_file(database, root, user_id, 1, payload=payload)
    store = DownloadContentStore(root)
    pack_id = await create_pack(database, clock, store, user_id, [big])
    dispatcher = make_dispatcher(database, clock, store, writer=FlakyWriter(0, chunk_size=64 * 1024))
    recorded: list[int] = []
    original = dispatcher._save_progress

    async def spy(pack_id_arg: int, percent: int) -> None:
        recorded.append(percent)
        await original(pack_id_arg, percent)

    dispatcher._save_progress = spy  # type: ignore[method-assign]
    await dispatcher.step_once()
    job = await get_job(database, pack_id)
    assert job.status is PackStatus.DONE
    assert recorded[0] == 0
    assert recorded[-1] == 100
    assert recorded == sorted(recorded)
    assert any(0 < value < 100 for value in recorded)


async def test_transient_failures_backoff_sequence_then_success(tmp_path: Path) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    _, first = await seed_pack_file(database, root, user_id, 1)
    store = DownloadContentStore(root)
    pack_id = await create_pack(database, clock, store, user_id, [first])
    dispatcher = make_dispatcher(database, clock, store, writer=FlakyWriter(3))

    observed_delays: list[int] = []
    for expected in RETRY_DELAYS[:3]:
        await dispatcher.step_once()
        job = await get_job(database, pack_id)
        assert job.status is PackStatus.PENDING
        assert job.next_attempt_at_ms == clock() + expected
        observed_delays.append(expected)
        await dispatcher.step_once()  # 未到期：无事发生。
        assert (await get_job(database, pack_id)).retry_count == len(observed_delays)
        clock.advance(expected)
    await dispatcher.step_once()
    job = await get_job(database, pack_id)
    assert job.status is PackStatus.DONE
    assert observed_delays == [250, 1000, 5000]
    assert not (root / "packs" / f"prepared-{pack_id}.part").exists()


async def test_deterministic_source_drift_fails_and_releases(tmp_path: Path) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    _, first = await seed_pack_file(database, root, user_id, 1)
    store = DownloadContentStore(root)
    pack_id = await create_pack(database, clock, store, user_id, [first], delete_source=True)
    async with SQLiteUnitOfWork(database) as uow:
        await ContentRepository(uow.connection).rename_user_file(
            user_id=user_id, file_id=first, new_name="renamed.bin"
        )
    dispatcher = make_dispatcher(database, clock, store)
    await dispatcher.step_once()
    job = await get_job(database, pack_id)
    assert job.status is PackStatus.FAILED
    assert job.error_code == "invalid_state"
    assert job.reserved_bytes == 0
    async with SQLiteUnitOfWork(database) as uow:
        quota = await QuotaRepository(uow.connection).get(user_id)
        sources = await PackRepository(uow.connection).list_sources(pack_id)
    assert quota == Quota(100 * 1024 * 1024, 10, 0)
    assert [source.cleanup_status for source in sources] == ["kept"]


async def test_output_name_conflict_fails_deterministically(tmp_path: Path) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    _, first = await seed_pack_file(database, root, user_id, 1)
    _, clash = await seed_pack_file(database, root, user_id, 2, payload=b"c" * 5, name="bundle.zip")
    store = DownloadContentStore(root)
    pack_id = await create_pack(database, clock, store, user_id, [first], output_name="bundle.zip")
    dispatcher = make_dispatcher(database, clock, store)
    await dispatcher.step_once()
    job = await get_job(database, pack_id)
    assert job.status is PackStatus.FAILED
    assert job.error_code == "duplicate_resource"
    async with SQLiteUnitOfWork(database) as uow:
        quota = await QuotaRepository(uow.connection).get(user_id)
    assert quota.reserved_bytes == 0
    assert clash > 0


class ContentLookupFailingRepository(PackRepository):
    """回收前查内容对象失败：只记日志，不能掩盖原来的完成失败。"""

    async def content_object_exists(self, content_key: str) -> bool:
        raise OSError("内容对象查询失败")


class PhysicalDeleteFailingStore(DownloadContentStore):
    """回收删除失败：只记日志，不能掩盖原来的完成失败。"""

    async def physical_delete(self, storage_path: str) -> None:
        raise OSError("回收删除失败")


async def seed_output_name_clash(
    database: SQLiteDatabase, root: Path, user_id: int
) -> None:
    await seed_pack_file(database, root, user_id, 2, payload=b"c" * 5, name="bundle.zip")


async def test_output_name_conflict_reclaims_the_canonical_created_by_this_attempt(
    tmp_path: Path,
) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    _, first = await seed_pack_file(database, root, user_id, 1)
    await seed_output_name_clash(database, root, user_id)
    store = DownloadContentStore(root)
    pack_id = await create_pack(database, clock, store, user_id, [first], output_name="bundle.zip")

    await make_dispatcher(database, clock, store).step_once()

    job = await get_job(database, pack_id)
    assert job.status is PackStatus.FAILED
    assert job.error_code == "duplicate_resource"
    assert job.prepared is not None
    canonical = root / "objects" / f"file-{job.prepared.digest}"
    assert not canonical.exists(), "本次新建且回滚后无内容对象的 canonical 必须回收"
    assert not (root / "packs" / f"prepared-{pack_id}.part").exists()
    async with SQLiteUnitOfWork(database) as uow:
        assert (
            await ContentRepository(uow.connection).find_object_by_key(
                f"file:{job.prepared.digest}"
            )
            is None
        ), "完成结算回滚后不得留下输出内容行"
        quota = await QuotaRepository(uow.connection).get(user_id)
    assert quota == Quota(100 * 1024 * 1024, 15, 0)


async def prepare_with_shared_content(
    database: SQLiteDatabase,
    clock: FakeClock,
    store: DownloadContentStore,
    user_id: int,
    pack_id: int,
    first: int,
    *,
    canonical_installed: bool,
) -> tuple[int, int, int, str, Path]:
    """构造“同摘要内容已属于其他用户”的现场。

    返回 (其他用户, 内容 ID, 引用 ID, 摘要, canonical 路径)。
    """
    size, digest = await prepare_output(database, clock, store, user_id, pack_id, first)
    canonical = store.physical_path(f"objects/file-{digest}")
    if canonical_installed:
        canonical.parent.mkdir(parents=True, exist_ok=True)
        canonical.write_bytes(store.physical_path(f"packs/prepared-{pack_id}.part").read_bytes())
    other_user = await create_pack_user(database, "other")
    async with SQLiteUnitOfWork(database) as uow:
        content = ContentRepository(uow.connection)
        content_id = await content.create_object(
            kind="file",
            digest=digest,
            size=size,
            storage_path=f"objects/file-{digest}",
            now_ms=clock(),
        )
        other_file = await content.add_user_file(
            user_id=other_user, content_id=content_id, display_name="shared.zip", now_ms=clock()
        )
        await QuotaRepository(uow.connection).rebuild(other_user)
    return other_user, content_id, other_file, digest, canonical


async def test_reused_canonical_is_preserved_when_completion_conflicts(tmp_path: Path) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    _, first = await seed_pack_file(database, root, user_id, 1)
    await seed_output_name_clash(database, root, user_id)
    store = DownloadContentStore(root)
    pack_id = await create_pack(database, clock, store, user_id, [first], output_name="bundle.zip")
    other_user, content_id, other_file, digest, canonical = await prepare_with_shared_content(
        database, clock, store, user_id, pack_id, first, canonical_installed=True
    )
    async with SQLiteUnitOfWork(database) as uow:
        shared_quota = await QuotaRepository(uow.connection).get(other_user)

    await make_dispatcher(database, clock, store).step_once()

    job = await get_job(database, pack_id)
    assert job.status is PackStatus.FAILED and job.error_code == "duplicate_resource"
    assert canonical.is_file(), "created=False 的共享 canonical 必须保留"
    assert not (root / "packs" / f"prepared-{pack_id}.part").exists()
    async with SQLiteUnitOfWork(database) as uow:
        content = ContentRepository(uow.connection)
        assert await content.find_object_by_key(f"file:{digest}") is not None
        assert await content.find_user_file(user_id=other_user, content_id=content_id) == other_file
        assert await QuotaRepository(uow.connection).get(other_user) == shared_quota


async def test_created_canonical_with_an_existing_content_row_is_preserved(tmp_path: Path) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    _, first = await seed_pack_file(database, root, user_id, 1)
    await seed_output_name_clash(database, root, user_id)
    store = DownloadContentStore(root)
    pack_id = await create_pack(database, clock, store, user_id, [first], output_name="bundle.zip")
    other_user, content_id, other_file, digest, canonical = await prepare_with_shared_content(
        database, clock, store, user_id, pack_id, first, canonical_installed=False
    )

    await make_dispatcher(database, clock, store).step_once()

    job = await get_job(database, pack_id)
    assert job.status is PackStatus.FAILED and job.error_code == "duplicate_resource"
    assert canonical.is_file(), "回滚后仍有内容对象：不得按本次新建回收"
    assert not (root / "packs" / f"prepared-{pack_id}.part").exists()
    async with SQLiteUnitOfWork(database) as uow:
        content = ContentRepository(uow.connection)
        shared = await content.find_object_by_key(f"file:{digest}")
        assert shared is not None and shared.id == content_id
        assert await content.find_user_file(user_id=other_user, content_id=content_id) == other_file


async def test_cancel_after_a_reused_install_keeps_the_shared_canonical(tmp_path: Path) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    _, first = await seed_pack_file(database, root, user_id, 1)
    store = GatedContentStore(root)
    pack_id = await create_pack(database, clock, store, user_id, [first])
    _, digest = await prepare_output(database, clock, store, user_id, pack_id, first)
    part = root / "packs" / f"prepared-{pack_id}.part"
    canonical = root / "objects" / f"file-{digest}"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_bytes(part.read_bytes())  # 同摘要规范位已存在：本次只能复用。
    dispatcher = make_dispatcher(database, clock, store)
    installing = asyncio.create_task(dispatcher._install(pack_id))
    await asyncio.wait_for(store.install_entered.wait(), timeout=5)

    installing.cancel()
    await asyncio.sleep(0.05)
    assert not installing.done()
    store.install_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(installing, timeout=5)

    assert store.install_returned
    assert canonical.is_file(), "created=False 的共享 canonical 不得回收"
    assert not part.exists()
    assert (await get_job(database, pack_id)).status is PackStatus.PACKING


@pytest.mark.parametrize("failing", ["probe", "delete"])
async def test_reclaim_failures_do_not_mask_the_completion_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, failing: str
) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    _, first = await seed_pack_file(database, root, user_id, 1)
    await seed_output_name_clash(database, root, user_id)
    store = PhysicalDeleteFailingStore(root) if failing == "delete" else DownloadContentStore(root)
    pack_id = await create_pack(database, clock, store, user_id, [first], output_name="bundle.zip")
    caplog.set_level(logging.WARNING)
    repository_factory = (
        (lambda uow: ContentLookupFailingRepository(uow.connection))
        if failing == "probe"
        else None
    )

    await make_dispatcher(
        database, clock, store, repository_factory=repository_factory
    ).step_once()

    job = await get_job(database, pack_id)
    assert job.status is PackStatus.FAILED, "回收失败不得替换原来的确定性失败结算"
    assert job.error_code == "duplicate_resource"
    assert job.prepared is not None
    assert (root / "objects" / f"file-{job.prepared.digest}").is_file(), "无法证明归属时保留 canonical"
    assert not (root / "packs" / f"prepared-{pack_id}.part").exists()
    assert "回收" in caplog.text


async def test_cancel_stops_writer_before_status_change(tmp_path: Path) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    _, first = await seed_pack_file(database, root, user_id, 1)
    store = DownloadContentStore(root)
    pack_id = await create_pack(database, clock, store, user_id, [first])
    control = PackWriterControl()
    blocking = BlockingWriter(database)
    dispatcher = make_dispatcher(database, clock, store, writer=blocking, control=control)
    service = make_pack_control_service(database, clock, store, control)

    stepping = asyncio.create_task(dispatcher.step_once())
    await asyncio.wait_for(blocking.started.wait(), timeout=5)
    await service.cancel(user_id, pack_id)
    await asyncio.wait_for(stepping, timeout=5)

    assert blocking.observed == ["packing"], "写入者被停止时任务必须仍是 packing（先停后改状态）"
    job = await get_job(database, pack_id)
    assert job.status is PackStatus.CANCELLED
    async with SQLiteUnitOfWork(database) as uow:
        quota = await QuotaRepository(uow.connection).get(user_id)
    assert quota.reserved_bytes == 0

    with pytest.raises(DomainError) as caught:
        await service.cancel(user_id, pack_id)
    assert caught.value.code is ErrorCode.INVALID_STATE
    foreign = await create_pack_user(database, "foreign")
    with pytest.raises(DomainError) as caught:
        await service.cancel(foreign, pack_id)
    assert caught.value.code is ErrorCode.NOT_FOUND


async def test_cancel_pending_job_without_writer(tmp_path: Path) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    _, first = await seed_pack_file(database, root, user_id, 1)
    store = DownloadContentStore(root)
    pack_id = await create_pack(database, clock, store, user_id, [first])
    control = PackWriterControl()
    service = make_pack_control_service(database, clock, store, control)
    await service.cancel(user_id, pack_id)
    job = await get_job(database, pack_id)
    assert job.status is PackStatus.CANCELLED
    async with SQLiteUnitOfWork(database) as uow:
        quota = await QuotaRepository(uow.connection).get(user_id)
    assert quota == Quota(100 * 1024 * 1024, 10, 0)


async def prepare_output(
    database: SQLiteDatabase,
    clock: FakeClock,
    store: DownloadContentStore,
    user_id: int,
    pack_id: int,
    file_id: int,
) -> tuple[int, str]:
    """Drive a job to verifying with a real archive part on disk."""
    async with SQLiteUnitOfWork(database) as uow:
        repository = PackRepository(uow.connection)
        await repository.claim_next_pending(now_ms=clock())
        await repository.update_step(
            pack_id, from_step=PackStep.VALIDATING, to_step=PackStep.COMPRESSING, now_ms=clock()
        )
        await repository.update_step(
            pack_id, from_step=PackStep.COMPRESSING, to_step=PackStep.VERIFYING, now_ms=clock()
        )
        job = await repository.get_job(pack_id)
        sources = await repository.list_admission_sources(
            user_id=user_id, user_file_ids=[file_id]
        )
    part = f"packs/prepared-{pack_id}.part"
    await ArchiveWriter().write_archive(
        store.physical_path(part),
        sources=[
            ArchiveSource(
                sources[0].display_name,
                store.physical_path(sources[0].storage_path),
                ContentKind.FILE,
            )
        ],
        pack_format="zip",
        compression_level=3,
        max_output_bytes=job.reserved_bytes,
        min_free_bytes=0,
        cancel_event=threading.Event(),
    )
    size, digest = await store.verify_prepared(part)
    async with SQLiteUnitOfWork(database) as uow:
        await PackRepository(uow.connection).set_prepared(
            pack_id, PreparedOutput(part, size, digest), now_ms=clock()
        )
    return size, digest


async def shrink_frozen_budget(
    database: SQLiteDatabase, user_id: int, pack_id: int, budget: int
) -> None:
    """受控损坏/旧数据：把已冻结预算改小，并同步减少对应的用户预留。"""
    async with SQLiteUnitOfWork(database) as uow:
        job = await PackRepository(uow.connection).get_job(pack_id)
        await uow.connection.execute(
            text(
                "UPDATE pack_jobs SET reserved_bytes = :budget, "
                "physical_reserved_bytes = :budget WHERE id = :id"
            ),
            {"budget": budget, "id": pack_id},
        )
        await QuotaRepository(uow.connection).release(user_id, job.reserved_bytes - budget)


async def test_dispatcher_passes_the_frozen_budget_and_real_disk_floor(
    tmp_path: Path,
) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    _, first = await seed_pack_file(database, root, user_id, 1)
    store = DownloadContentStore(root)
    pack_id = await create_pack(database, clock, store, user_id, [first])
    frozen = await get_job(database, pack_id)
    writer = RecordingWriter()
    dispatcher = make_dispatcher(
        database,
        clock,
        store,
        writer=writer,
        settings=SystemSettings(
            pack_format="zip", pack_compression_level=3, min_free_disk=987_654
        ),
    )

    await dispatcher.step_once()

    assert (await get_job(database, pack_id)).status is PackStatus.DONE
    assert len(writer.calls) == 1
    call = writer.calls[0]
    assert frozen.reserved_bytes == 10 + 512 + 2 * len("src-1") + 65_536
    assert call["max_output_bytes"] == frozen.reserved_bytes
    assert call["min_free_bytes"] == 987_654
    assert isinstance(call["cancel_event"], threading.Event)
    assert not call["cancel_event"].is_set()


async def test_over_budget_writer_output_fails_deterministically_at_verify(
    tmp_path: Path,
) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    _, first = await seed_pack_file(database, root, user_id, 1)
    store = DownloadContentStore(root)
    pack_id = await create_pack(database, clock, store, user_id, [first])
    frozen = await get_job(database, pack_id)
    writer = OversizedWriter()

    await make_dispatcher(database, clock, store, writer=writer).step_once()

    job = await get_job(database, pack_id)
    assert writer.budgets == [frozen.reserved_bytes]
    assert job.status is PackStatus.FAILED
    assert job.error_code == "task_too_large"
    assert job.retry_count == 0
    assert job.next_attempt_at_ms is None
    assert job.reserved_bytes == 0
    assert job.physical_reserved_bytes == 0
    assert not (root / "packs" / f"prepared-{pack_id}.part").exists()
    async with SQLiteUnitOfWork(database) as uow:
        quota = await QuotaRepository(uow.connection).get(user_id)
    assert quota.reserved_bytes == 0
    assert quota.used_bytes == 10


async def test_over_budget_prepared_output_fails_at_install_and_resume(
    tmp_path: Path,
) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    _, first = await seed_pack_file(database, root, user_id, 1)
    store = DownloadContentStore(root)
    pack_id = await create_pack(database, clock, store, user_id, [first])
    size, digest = await prepare_output(database, clock, store, user_id, pack_id, first)
    await shrink_frozen_budget(database, user_id, pack_id, size - 1)
    part = root / "packs" / f"prepared-{pack_id}.part"
    canonical = root / "objects" / f"file-{digest}"
    dispatcher = make_dispatcher(database, clock, store)

    with pytest.raises(DomainError) as caught:
        await dispatcher._install(pack_id)
    assert caught.value.code is ErrorCode.TASK_TOO_LARGE
    assert part.exists(), "预算复核必须发生在 rename 之前"
    assert not canonical.exists()

    await dispatcher.step_once()  # 重启恢复：重验后仍超预算，确定性失败。
    job = await get_job(database, pack_id)
    assert job.status is PackStatus.FAILED
    assert job.error_code == "task_too_large"
    assert job.retry_count == 0
    assert job.reserved_bytes == 0
    assert job.physical_reserved_bytes == 0
    assert not part.exists()
    async with SQLiteUnitOfWork(database) as uow:
        quota = await QuotaRepository(uow.connection).get(user_id)
    assert quota.reserved_bytes == 0


async def test_completion_quota_error_fails_without_requeue(tmp_path: Path) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    _, first = await seed_pack_file(database, root, user_id, 1)
    store = DownloadContentStore(root)
    pack_id = await create_pack(database, clock, store, user_id, [first])
    dispatcher = make_dispatcher(
        database,
        clock,
        store,
        repository_factory=lambda uow: QuotaExhaustedRepository(uow.connection),
    )

    await dispatcher.step_once()

    job = await get_job(database, pack_id)
    assert job.status is PackStatus.FAILED
    assert job.error_code == "quota_exceeded"
    assert job.retry_count == 0
    assert job.next_attempt_at_ms is None
    assert job.reserved_bytes == 0
    assert job.physical_reserved_bytes == 0
    async with SQLiteUnitOfWork(database) as uow:
        quota = await QuotaRepository(uow.connection).get(user_id)
    assert quota.reserved_bytes == 0


async def test_transient_physical_space_error_keeps_reservation_and_backs_off(
    tmp_path: Path,
) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    _, first = await seed_pack_file(database, root, user_id, 1)
    store = DownloadContentStore(root)
    pack_id = await create_pack(database, clock, store, user_id, [first])
    frozen = await get_job(database, pack_id)
    writer = FailingWriter(
        DomainError(ErrorCode.PHYSICAL_SPACE_EXCEEDED, "机器剩余空间不足")
    )

    await make_dispatcher(database, clock, store, writer=writer).step_once()

    job = await get_job(database, pack_id)
    assert writer.calls == 1
    assert job.status is PackStatus.PENDING
    assert job.error_code is None
    assert job.retry_count == 1
    assert job.next_attempt_at_ms == clock() + RETRY_DELAYS[0]
    assert job.reserved_bytes == frozen.reserved_bytes
    assert job.physical_reserved_bytes == frozen.physical_reserved_bytes
    assert not (root / "packs" / f"prepared-{pack_id}.part").exists()
    async with SQLiteUnitOfWork(database) as uow:
        quota = await QuotaRepository(uow.connection).get(user_id)
    assert quota.reserved_bytes == frozen.reserved_bytes


class InterruptingWriter:
    """Raises InterruptedError; optionally sets the stop event first."""

    def __init__(self, *, stop_requested: bool) -> None:
        self.stop_requested = stop_requested
        self.calls = 0

    async def write_archive(
        self,
        destination: Path,
        *,
        sources,
        pack_format,
        compression_level,
        max_output_bytes: int,
        min_free_bytes: int,
        cancel_event: threading.Event,
        on_progress=None,
    ) -> ArchiveResult:
        self.calls += 1
        if self.stop_requested:
            cancel_event.set()
        raise InterruptedError("打包写入已取消")


async def test_interrupted_writer_becomes_cancellation_only_when_stop_is_requested(
    tmp_path: Path,
) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    _, first = await seed_pack_file(database, root, user_id, 1)
    store = DownloadContentStore(root)
    pack_id = await create_pack(database, clock, store, user_id, [first])
    frozen = await get_job(database, pack_id)
    writer = InterruptingWriter(stop_requested=True)

    await make_dispatcher(database, clock, store, writer=writer).step_once()

    job = await get_job(database, pack_id)
    assert writer.calls == 1
    # 停止由控制服务落库；本阶段 dispatcher 只识别取消，不改状态或释放预留。
    assert job.status is PackStatus.PACKING
    assert job.retry_count == 0
    assert job.reserved_bytes == frozen.reserved_bytes
    async with SQLiteUnitOfWork(database) as uow:
        quota = await QuotaRepository(uow.connection).get(user_id)
    assert quota.reserved_bytes == frozen.reserved_bytes


async def test_interrupted_writer_without_stop_request_stays_a_transient_io_error(
    tmp_path: Path,
) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    _, first = await seed_pack_file(database, root, user_id, 1)
    store = DownloadContentStore(root)
    pack_id = await create_pack(database, clock, store, user_id, [first])
    frozen = await get_job(database, pack_id)
    writer = InterruptingWriter(stop_requested=False)

    await make_dispatcher(database, clock, store, writer=writer).step_once()

    job = await get_job(database, pack_id)
    assert job.status is PackStatus.PENDING
    assert job.retry_count == 1
    assert job.next_attempt_at_ms == clock() + RETRY_DELAYS[0]
    assert job.reserved_bytes == frozen.reserved_bytes
    async with SQLiteUnitOfWork(database) as uow:
        quota = await QuotaRepository(uow.connection).get(user_id)
    assert quota.reserved_bytes == frozen.reserved_bytes


async def test_over_budget_canonical_output_fails_when_resuming_completion(
    tmp_path: Path,
) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    _, first = await seed_pack_file(database, root, user_id, 1)
    store = DownloadContentStore(root)
    pack_id = await create_pack(database, clock, store, user_id, [first])
    size, digest = await prepare_output(database, clock, store, user_id, pack_id, first)
    # 崩溃于「移动到规范位之后、DB 完成之前」，同时预算被受控改小。
    await store.install_prepared(f"packs/prepared-{pack_id}.part", digest)
    await shrink_frozen_budget(database, user_id, pack_id, size - 1)

    await make_dispatcher(database, clock, store).step_once()

    job = await get_job(database, pack_id)
    assert job.status is PackStatus.FAILED
    assert job.error_code == "task_too_large"
    assert job.retry_count == 0
    assert job.reserved_bytes == 0
    # canonical 由写入后的安装产生，本阶段不回收 ownership。
    assert (root / "objects" / f"file-{digest}").is_file()
    async with SQLiteUnitOfWork(database) as uow:
        quota = await QuotaRepository(uow.connection).get(user_id)
    assert quota.reserved_bytes == 0


async def test_early_stop_before_claim_registers_without_running_the_writer(
    tmp_path: Path,
) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    _, first = await seed_pack_file(database, root, user_id, 1)
    store = DownloadContentStore(root)
    pack_id = await create_pack(database, clock, store, user_id, [first])
    control = PackWriterControl(stop_timeout_seconds=1.0)
    await control.stop_and_wait(pack_id)  # 取消请求早于 claim：关闭 PENDING→claim 窗口。

    writer = RecordingWriter()
    dispatcher = make_dispatcher(database, clock, store, writer=writer, control=control)
    await dispatcher.step_once()

    assert writer.calls == [], "已请求停止的任务不得启动写入"
    job = await get_job(database, pack_id)
    assert job.status is PackStatus.PACKING and job.step is PackStep.VALIDATING
    assert control.allows_resume(pack_id) is False
    assert not (root / "packs" / f"prepared-{pack_id}.part").exists()

    await make_pack_control_service(database, clock, store, control).cancel(user_id, pack_id)
    assert (await get_job(database, pack_id)).status is PackStatus.CANCELLED
    assert control.allows_resume(pack_id) is True


async def test_concurrent_stop_requests_share_the_running_writer(tmp_path: Path) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    _, first = await seed_pack_file(database, root, user_id, 1)
    store = DownloadContentStore(root)
    pack_id = await create_pack(database, clock, store, user_id, [first])
    control = PackWriterControl(stop_timeout_seconds=2.0)
    blocking = BlockingWriter(database)
    dispatcher = make_dispatcher(database, clock, store, writer=blocking, control=control)

    stepping = asyncio.create_task(dispatcher.step_once())
    await asyncio.wait_for(blocking.started.wait(), timeout=5)
    first_stop = asyncio.create_task(control.stop_and_wait(pack_id))
    second_stop = asyncio.create_task(control.stop_and_wait(pack_id))

    await asyncio.wait_for(asyncio.gather(first_stop, second_stop), timeout=5)
    await asyncio.wait_for(stepping, timeout=5)

    assert blocking.calls == 1
    assert blocking.observed == ["packing"]
    assert (await get_job(database, pack_id)).status is PackStatus.PACKING
    assert control.allows_resume(pack_id) is False

    await control.stop_and_wait(pack_id)  # 重复取消：写入者已结束，只保留取消记录。
    assert control.allows_resume(pack_id) is False

    await make_pack_control_service(database, clock, store, control).cancel(user_id, pack_id)
    assert (await get_job(database, pack_id)).status is PackStatus.CANCELLED
    assert control.allows_resume(pack_id) is True


async def test_shutdown_cancellation_stops_the_writer_within_the_budget(
    tmp_path: Path,
) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    _, first = await seed_pack_file(database, root, user_id, 1)
    store = DownloadContentStore(root)
    pack_id = await create_pack(database, clock, store, user_id, [first])
    frozen = await get_job(database, pack_id)
    control = PackWriterControl(stop_timeout_seconds=2.0)
    blocking = BlockingWriter(database)
    dispatcher = make_dispatcher(database, clock, store, writer=blocking, control=control)

    stepping = asyncio.create_task(dispatcher.step_once())
    await asyncio.wait_for(blocking.started.wait(), timeout=5)
    stepping.cancel()  # supervisor 关停：父任务外部取消。
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(stepping, timeout=5)

    assert blocking.observed == ["packing"], "关停必须先让写入者停下，再传播原取消"
    job = await get_job(database, pack_id)
    assert job.status is PackStatus.PACKING
    assert job.reserved_bytes == frozen.reserved_bytes
    assert control.allows_resume(pack_id) is False
    assert not (root / "packs" / f"prepared-{pack_id}.part").exists()


async def test_timed_out_stop_pauses_the_queue_and_is_not_resumed(tmp_path: Path) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    _, first = await seed_pack_file(database, root, user_id, 1)
    store = DownloadContentStore(root)
    pack_id = await create_pack(database, clock, store, user_id, [first])
    frozen = await get_job(database, pack_id)
    control = PackWriterControl(stop_timeout_seconds=0.05)
    stubborn = StubbornWriter()
    dispatcher = make_dispatcher(database, clock, store, writer=stubborn, control=control)

    stepping = asyncio.create_task(dispatcher.step_once())
    await asyncio.wait_for(stubborn.started.wait(), timeout=5)
    stepping.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(stepping, timeout=5)

    job = await get_job(database, pack_id)
    assert job.status is PackStatus.PACKING, "超时是安全暂停：不落库、不释放、不重排"
    assert job.reserved_bytes == frozen.reserved_bytes
    assert control.allows_resume(pack_id) is False
    record = control._records[pack_id]
    writer_task = record.writer
    assert writer_task is not None and not writer_task.done()

    stubborn.release.set()
    await asyncio.wait_for(asyncio.gather(writer_task, return_exceptions=True), timeout=5)

    # 写入者结束不等于取消已落库：记录黏住，下一轮不得自动恢复。
    await dispatcher.step_once()
    assert stubborn.calls == 1
    assert (await get_job(database, pack_id)).status is PackStatus.PACKING

    await make_pack_control_service(database, clock, store, control).cancel(user_id, pack_id)
    assert control.allows_resume(pack_id) is True


async def test_update_step_false_stops_the_stage_without_writing(tmp_path: Path) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    _, first = await seed_pack_file(database, root, user_id, 1)
    store = DownloadContentStore(root)
    pack_id = await create_pack(database, clock, store, user_id, [first])
    writer = RecordingWriter()
    dispatcher = make_dispatcher(
        database,
        clock,
        store,
        writer=writer,
        repository_factory=lambda uow: FrozenStepRepository(uow.connection),
    )

    await dispatcher.step_once()

    assert writer.calls == [], "阶段迁移失败后不得继续压缩"
    job = await get_job(database, pack_id)
    assert job.status is PackStatus.PACKING and job.step is PackStep.VALIDATING
    await make_pack_control_service(database, clock, store, PackWriterControl()).cancel(
        user_id, pack_id
    )
    assert (await get_job(database, pack_id)).status is PackStatus.CANCELLED


async def test_stop_during_install_waits_for_the_rename_to_finish(tmp_path: Path) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    _, first = await seed_pack_file(database, root, user_id, 1)
    store = GatedContentStore(root)
    pack_id = await create_pack(database, clock, store, user_id, [first])
    _, digest = await prepare_output(database, clock, store, user_id, pack_id, first)
    control = PackWriterControl(stop_timeout_seconds=2.0)
    dispatcher = make_dispatcher(database, clock, store, writer=RecordingWriter(), control=control)

    stepping = asyncio.create_task(dispatcher.step_once())
    await asyncio.wait_for(store.install_entered.wait(), timeout=5)
    stopping = asyncio.create_task(control.stop_and_wait(pack_id))
    await asyncio.sleep(0.05)
    assert not stopping.done(), "安装进行中：停止必须等真实安装返回"

    store.install_gate.set()
    await asyncio.wait_for(stopping, timeout=5)
    await asyncio.wait_for(stepping, timeout=5)

    canonical = root / "objects" / f"file-{digest}"
    assert not canonical.exists(), "本次新建且无内容对象的 canonical 必须在取消后回收"
    assert not (root / "packs" / f"prepared-{pack_id}.part").exists()
    job = await get_job(database, pack_id)
    assert job.status is PackStatus.PACKING, "安装返回后观察到取消：不完成结算"
    assert control.allows_resume(pack_id) is False

    await make_pack_control_service(database, clock, store, control).cancel(user_id, pack_id)
    assert (await get_job(database, pack_id)).status is PackStatus.CANCELLED
    assert control.allows_resume(pack_id) is True


async def test_register_happens_once_for_a_normal_attempt(tmp_path: Path) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    _, first = await seed_pack_file(database, root, user_id, 1)
    store = DownloadContentStore(root)
    pack_id = await create_pack(database, clock, store, user_id, [first])
    control = RecordingControl()
    dispatcher = make_dispatcher(database, clock, store, control=control)

    await dispatcher.step_once()

    assert control.registered == [pack_id], "正常执行只注册一次"
    assert (await get_job(database, pack_id)).status is PackStatus.DONE
    assert control.allows_resume(pack_id) is True


async def test_failure_releases_once_and_the_worker_keeps_working(tmp_path: Path) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    _, first = await seed_pack_file(database, root, user_id, 1)
    _, second = await seed_pack_file(database, root, user_id, 2, payload=b"b" * 7)
    store = DownloadContentStore(root)
    failing = await create_pack(database, clock, store, user_id, [first], output_name="fail.zip")
    async with SQLiteUnitOfWork(database) as uow:
        await ContentRepository(uow.connection).rename_user_file(
            user_id=user_id, file_id=first, new_name="renamed.bin"
        )
    dispatcher = make_dispatcher(database, clock, store)

    await dispatcher.step_once()
    assert (await get_job(database, failing)).status is PackStatus.FAILED
    async with SQLiteUnitOfWork(database) as uow:
        released = await QuotaRepository(uow.connection).get(user_id)
    assert released.reserved_bytes == 0

    await dispatcher._handle_failure(
        failing, DomainError(ErrorCode.INVALID_STATE, "迟到的重复失败事件")
    )
    async with SQLiteUnitOfWork(database) as uow:
        assert await QuotaRepository(uow.connection).get(user_id) == released, "终态不得再次释放"

    later = await create_pack(database, clock, store, user_id, [second], output_name="later.zip")
    await dispatcher.step_once()
    assert (await get_job(database, later)).status is PackStatus.DONE, "worker 必须继续工作"


async def test_failure_with_corrupted_quota_skips_release_and_zeroes_the_job(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    _, first = await seed_pack_file(database, root, user_id, 1)
    store = DownloadContentStore(root)
    pack_id = await create_pack(database, clock, store, user_id, [first])
    async with SQLiteUnitOfWork(database) as uow:
        await ContentRepository(uow.connection).rename_user_file(
            user_id=user_id, file_id=first, new_name="renamed.bin"
        )
    await corrupt_quota(database, user_id)
    caplog.set_level(logging.WARNING)

    await make_dispatcher(database, clock, store).step_once()  # 账目损坏不得击穿 worker

    job = await get_job(database, pack_id)
    assert job.status is PackStatus.FAILED
    assert job.error_code == "invalid_state"
    assert job.reserved_bytes == 0
    assert job.physical_reserved_bytes == 0
    assert "配额账目已损坏" in caplog.text


class LateFrozenStepRepository(PackRepository):
    """只允许进入压缩阶段，之后的阶段迁移一律失败。"""

    async def update_step(
        self, pack_id: int, *, from_step: PackStep, to_step: PackStep, now_ms: int
    ) -> bool:
        if to_step is PackStep.COMPRESSING:
            return await super().update_step(
                pack_id, from_step=from_step, to_step=to_step, now_ms=now_ms
            )
        return False


async def test_stage_move_failure_after_compression_stops_before_verify(
    tmp_path: Path,
) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    _, first = await seed_pack_file(database, root, user_id, 1)
    store = DownloadContentStore(root)
    pack_id = await create_pack(database, clock, store, user_id, [first])
    writer = RecordingWriter()
    dispatcher = make_dispatcher(
        database,
        clock,
        store,
        writer=writer,
        repository_factory=lambda uow: LateFrozenStepRepository(uow.connection),
    )

    await dispatcher.step_once()

    assert len(writer.calls) == 1, "压缩已经发生，但阶段没推进"
    job = await get_job(database, pack_id)
    assert job.status is PackStatus.PACKING and job.step is PackStep.COMPRESSING
    assert job.prepared is None, "未进入验收阶段就不得登记 prepared"
    await make_pack_control_service(database, clock, store, PackWriterControl()).cancel(
        user_id, pack_id
    )
    assert not (root / "packs" / f"prepared-{pack_id}.part").exists()


async def test_install_rejects_a_prepared_output_missing_from_both_locations(
    tmp_path: Path,
) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    _, first = await seed_pack_file(database, root, user_id, 1)
    store = DownloadContentStore(root)
    pack_id = await create_pack(database, clock, store, user_id, [first])
    await prepare_output(database, clock, store, user_id, pack_id, first)
    (root / "packs" / f"prepared-{pack_id}.part").unlink()

    with pytest.raises(DomainError) as caught:
        await make_dispatcher(database, clock, store)._install(pack_id)
    assert caught.value.code is ErrorCode.INVALID_STATE


async def test_install_rejects_a_conflicting_canonical_when_the_part_is_gone(
    tmp_path: Path,
) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    _, first = await seed_pack_file(database, root, user_id, 1)
    store = DownloadContentStore(root)
    pack_id = await create_pack(database, clock, store, user_id, [first])
    _, digest = await prepare_output(database, clock, store, user_id, pack_id, first)
    (root / "packs" / f"prepared-{pack_id}.part").unlink()
    canonical = root / "objects" / f"file-{digest}"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_bytes(b"a different object")

    with pytest.raises(DomainError) as caught:
        await make_dispatcher(database, clock, store)._install(pack_id)
    assert caught.value.code is ErrorCode.INVALID_STATE
    assert canonical.read_bytes() == b"a different object"


async def test_resume_fails_deterministically_when_the_canonical_path_is_a_directory(
    tmp_path: Path,
) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    _, first = await seed_pack_file(database, root, user_id, 1, payload=b"a" * 10)
    store = DownloadContentStore(root)
    pack_id = await create_pack(database, clock, store, user_id, [first])
    _, digest = await prepare_output(database, clock, store, user_id, pack_id, first)
    frozen = await get_job(database, pack_id)
    assert frozen.reserved_bytes > 0
    part = root / "packs" / f"prepared-{pack_id}.part"
    part.unlink()  # part 缺失且规范位不是普通文件：不得误判为「双缺失」而重排。
    canonical = root / "objects" / f"file-{digest}"
    canonical.mkdir(parents=True)
    (canonical / "keep.bin").write_bytes(b"keep")

    await make_dispatcher(database, clock, store).step_once()

    job = await get_job(database, pack_id)
    assert job.status is PackStatus.FAILED, "目录规范位是持久冲突，不能重排重压缩"
    assert job.error_code == "invalid_state"
    assert job.retry_count == 0
    assert job.next_attempt_at_ms is None
    assert job.reserved_bytes == 0 and job.physical_reserved_bytes == 0
    assert not part.exists()
    assert canonical.is_dir() and (canonical / "keep.bin").read_bytes() == b"keep"
    async with SQLiteUnitOfWork(database) as uow:
        quota = await QuotaRepository(uow.connection).get(user_id)
    assert quota.reserved_bytes == 0


class BrokenVerifyStore(DownloadContentStore):
    """非「文件不存在」的存储错误必须如实上抛。"""

    async def verify_prepared(self, storage_path: str) -> tuple[int, str]:
        raise DomainError(ErrorCode.INVALID_INPUT, "存储路径无效")


async def test_probe_lets_unexpected_storage_errors_escape(tmp_path: Path) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    _, first = await seed_pack_file(database, root, user_id, 1)
    store = DownloadContentStore(root)
    pack_id = await create_pack(database, clock, store, user_id, [first])
    await prepare_output(database, clock, store, user_id, pack_id, first)
    dispatcher = make_dispatcher(database, clock, BrokenVerifyStore(root))

    with pytest.raises(DomainError) as caught:
        await dispatcher._install(pack_id)
    assert caught.value.code is ErrorCode.INVALID_INPUT


async def test_install_waits_for_the_rename_when_its_own_task_is_cancelled(
    tmp_path: Path,
) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    _, first = await seed_pack_file(database, root, user_id, 1)
    store = GatedContentStore(root)
    pack_id = await create_pack(database, clock, store, user_id, [first])
    _, digest = await prepare_output(database, clock, store, user_id, pack_id, first)
    dispatcher = make_dispatcher(database, clock, store)
    installing = asyncio.create_task(dispatcher._install(pack_id))
    await asyncio.wait_for(store.install_entered.wait(), timeout=5)

    installing.cancel()
    await asyncio.sleep(0.05)
    assert not installing.done(), "外层取消也必须等真实安装返回"
    store.install_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(installing, timeout=5)
    assert store.install_returned, "必须拿到 InstallOutcome 后才允许清理"
    assert not (root / "objects" / f"file-{digest}").exists(), "本次新建且无内容对象：回收"
    assert not (root / "packs" / f"prepared-{pack_id}.part").exists()
    assert (await get_job(database, pack_id)).status is PackStatus.PACKING


async def test_second_cancellation_still_waits_for_the_install_outcome(tmp_path: Path) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    _, first = await seed_pack_file(database, root, user_id, 1)
    store = GatedContentStore(root)
    pack_id = await create_pack(database, clock, store, user_id, [first])
    _, digest = await prepare_output(database, clock, store, user_id, pack_id, first)
    dispatcher = make_dispatcher(database, clock, store)
    installing = asyncio.create_task(dispatcher._install(pack_id))
    await asyncio.wait_for(store.install_entered.wait(), timeout=5)

    installing.cancel()
    await asyncio.sleep(0.05)
    installing.cancel()  # 第二次取消不能提前中断对真实安装的等待。
    await asyncio.sleep(0.05)
    assert not installing.done(), "二次取消仍必须等真实安装返回"
    assert not store.install_returned

    store.install_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(installing, timeout=5)
    assert store.install_returned, "二次取消后仍必须取得安装结果"
    assert not (root / "objects" / f"file-{digest}").exists()
    assert not (root / "packs" / f"prepared-{pack_id}.part").exists()
    assert (await get_job(database, pack_id)).status is PackStatus.PACKING


class CancelledInstallStore(DownloadContentStore):
    async def install_prepared(self, storage_path: str, digest: str):
        raise asyncio.CancelledError


async def test_install_task_cancellation_does_not_trap_the_wait_loop(tmp_path: Path) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    _, first = await seed_pack_file(database, root, user_id, 1)
    store = DownloadContentStore(root)
    pack_id = await create_pack(database, clock, store, user_id, [first])
    _, digest = await prepare_output(database, clock, store, user_id, pack_id, first)
    dispatcher = make_dispatcher(database, clock, CancelledInstallStore(root))

    with pytest.raises(asyncio.CancelledError):
        await dispatcher._install_shielded(PreparedOutput(f"packs/prepared-{pack_id}.part", 1, digest))


class ExplodingControl:
    def __init__(self) -> None:
        self.writer: asyncio.Task[None] | None = None

    def register(
        self, pack_id: int, writer: asyncio.Task[None], cancel_event: threading.Event
    ) -> bool:
        self.writer = writer
        raise DomainError(ErrorCode.INVALID_STATE, "打包写入者仍在运行")

    async def stop_and_wait(self, pack_id: int) -> None:
        return None

    async def wait_for_writer(self, pack_id: int, writer: asyncio.Task[None]) -> None:
        return None

    def allows_resume(self, pack_id: int) -> bool:
        return True


async def test_register_failure_reaps_the_new_task(tmp_path: Path) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    _, first = await seed_pack_file(database, root, user_id, 1)
    store = DownloadContentStore(root)
    pack_id = await create_pack(database, clock, store, user_id, [first])
    control = ExplodingControl()
    dispatcher = PackDispatcher(
        uow_factory=lambda: SQLiteUnitOfWork(database),
        repository_factory=lambda uow: PackRepository(uow.connection),
        quota_repository_factory=lambda uow: QuotaRepository(uow.connection),
        writer=RecordingWriter(),
        content_store=store,
        writer_control=control,
        settings=lambda: SystemSettings(pack_format="zip", pack_compression_level=3, min_free_disk=0),
        clock_ms=clock,
    )

    with pytest.raises(DomainError) as caught:
        await dispatcher.step_once()
    assert caught.value.code is ErrorCode.INVALID_STATE
    assert (await get_job(database, pack_id)).status is PackStatus.PACKING
    assert control.writer is not None
    assert control.writer.done() and control.writer.cancelled(), "注册失败后必须回收新建任务"


class StopFailingControl:
    """停止等待抛错：DomainError 不伪装成取消，CancelledError 原样传播。"""

    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.task: asyncio.Task[None] | None = None
        self.cancel_event: threading.Event | None = None

    def register(
        self, pack_id: int, writer: asyncio.Task[None], cancel_event: threading.Event
    ) -> bool:
        self.task = writer
        self.cancel_event = cancel_event
        return True

    async def stop_and_wait(self, pack_id: int) -> None:
        if self.cancel_event is not None:
            self.cancel_event.set()
        raise self.error

    async def wait_for_writer(self, pack_id: int, writer: asyncio.Task[None]) -> None:
        return None

    def allows_resume(self, pack_id: int) -> bool:
        return True


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (DomainError(ErrorCode.INVALID_STATE, "停止失败"), DomainError),
        (asyncio.CancelledError(), asyncio.CancelledError),
    ],
)
async def test_shutdown_stop_failure_is_not_swallowed(
    tmp_path: Path, error: BaseException, expected: type[BaseException]
) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    _, first = await seed_pack_file(database, root, user_id, 1)
    store = DownloadContentStore(root)
    await create_pack(database, clock, store, user_id, [first])
    control = StopFailingControl(error)
    blocking = BlockingWriter(database)
    dispatcher = make_dispatcher(database, clock, store, writer=blocking, control=control)

    stepping = asyncio.create_task(dispatcher.step_once())
    await asyncio.wait_for(blocking.started.wait(), timeout=5)
    stepping.cancel()
    with pytest.raises(expected):
        await asyncio.wait_for(stepping, timeout=5)
    if control.task is not None:
        await asyncio.wait_for(asyncio.gather(control.task, return_exceptions=True), timeout=5)


class PartCleanupFailureStore(DownloadContentStore):
    """前 N 次 prepared 清理失败；可选记录每次清理时的任务状态。"""

    def __init__(
        self,
        root: Path,
        *,
        failures: int = 1,
        database: SQLiteDatabase | None = None,
    ) -> None:
        super().__init__(root)
        self.failures = failures
        self.database = database
        self.removed: list[str] = []
        self.statuses: list[PackStatus] = []

    async def remove_prepared(self, storage_path: str) -> None:
        self.removed.append(storage_path)
        if self.database is not None:
            pack_id = int(storage_path.removeprefix("packs/prepared-").removesuffix(".part"))
            async with SQLiteUnitOfWork(self.database) as uow:
                self.statuses.append((await PackRepository(uow.connection).get_job(pack_id)).status)
        if self.failures:
            self.failures -= 1
            raise OSError("模拟 prepared 清理失败")
        await super().remove_prepared(storage_path)


async def test_deterministic_failure_keeps_packing_until_the_part_cleanup_succeeds(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    _, first = await seed_pack_file(database, root, user_id, 1)
    store = PartCleanupFailureStore(root)
    pack_id = await create_pack(database, clock, store, user_id, [first])
    frozen = await get_job(database, pack_id)
    part = root / "packs" / f"prepared-{pack_id}.part"
    dispatcher = make_dispatcher(database, clock, store, writer=OversizedWriter())
    caplog.set_level(logging.WARNING)

    await dispatcher.step_once()  # 超预算确定性失败，但现场清理失败。

    job = await get_job(database, pack_id)
    assert job.status is PackStatus.PACKING, "清理失败不得先提交 FAILED"
    assert job.reserved_bytes == frozen.reserved_bytes
    assert job.physical_reserved_bytes == frozen.physical_reserved_bytes
    assert part.exists(), "现场必须留给恢复重试"
    assert "现场清理失败" in caplog.text
    async with SQLiteUnitOfWork(database) as uow:
        quota = await QuotaRepository(uow.connection).get(user_id)
    assert quota.reserved_bytes == frozen.reserved_bytes

    await dispatcher.step_once()  # 恢复：清掉 part 后暂态重排。

    job = await get_job(database, pack_id)
    assert job.status is PackStatus.PENDING
    assert job.retry_count == 1
    assert not part.exists()

    clock.advance(15_000)
    await dispatcher.step_once()  # 再次领取仍超预算，这次清理成功才结算失败。

    job = await get_job(database, pack_id)
    assert job.status is PackStatus.FAILED
    assert job.error_code == "task_too_large"
    assert job.reserved_bytes == 0 and job.physical_reserved_bytes == 0
    assert not part.exists()
    assert store.removed == [f"packs/prepared-{pack_id}.part"] * 3
    async with SQLiteUnitOfWork(database) as uow:
        quota = await QuotaRepository(uow.connection).get(user_id)
    assert quota.reserved_bytes == 0


async def test_transient_failure_keeps_packing_when_part_cleanup_fails_and_worker_survives(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    _, first = await seed_pack_file(database, root, user_id, 1)
    store = PartCleanupFailureStore(root, failures=2, database=database)
    pack_id = await create_pack(database, clock, store, user_id, [first])
    frozen = await get_job(database, pack_id)
    part = root / "packs" / f"prepared-{pack_id}.part"
    dispatcher = make_dispatcher(database, clock, store, writer=FlakyWriter(1))
    caplog.set_level(logging.WARNING)

    # 第一轮：写入暂态失败，现场清理也失败；worker 必须正常返回并保留 PACKING 与预留。
    await dispatcher.step_once()

    job = await get_job(database, pack_id)
    assert job.status is PackStatus.PACKING, "清理失败必须保留 PACKING 供恢复重试"
    assert job.reserved_bytes == frozen.reserved_bytes
    assert job.physical_reserved_bytes == frozen.physical_reserved_bytes
    assert job.retry_count == 0
    assert "打包任务暂态失败" in caplog.text
    assert "现场清理失败" in caplog.text
    async with SQLiteUnitOfWork(database) as uow:
        quota = await QuotaRepository(uow.connection).get(user_id)
    assert quota.reserved_bytes == frozen.reserved_bytes

    # 第二轮：恢复清理第二次失败；同一轮收尾的第三次清理成功后才重排。
    await dispatcher.step_once()

    job = await get_job(database, pack_id)
    assert job.status is PackStatus.PENDING
    assert job.retry_count == 1
    assert store.removed == [f"packs/prepared-{pack_id}.part"] * 3
    assert store.statuses == [PackStatus.PACKING] * 3, "两次清理失败都发生在 PACKING 状态"
    assert not part.exists()

    # 恢复后真实写入成功：worker 未退出，后续任务仍可继续完成。
    clock.advance(RETRY_DELAYS[0])
    await dispatcher.step_once()
    assert (await get_job(database, pack_id)).status is PackStatus.DONE

    _, second = await seed_pack_file(database, root, user_id, 2)
    later = await create_pack(
        database, clock, store, user_id, [second], output_name="later.zip"
    )
    await dispatcher.step_once()
    assert (await get_job(database, later)).status is PackStatus.DONE
