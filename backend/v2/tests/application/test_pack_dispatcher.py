from __future__ import annotations

import asyncio
import zipfile
from pathlib import Path

import pytest
from sqlalchemy import text

from aria2deck.application.packs.control import PackControlService
from aria2deck.application.packs.dispatcher import PackDispatcher
from aria2deck.domain.errors import DomainError, ErrorCode
from aria2deck.domain.packs import PackStatus
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

    async def write_archive(self, destination, *, sources, pack_format, compression_level, on_progress=None):
        self.observed_options.append((pack_format, compression_level))
        if self.failures > 0:
            self.failures -= 1
            raise OSError("simulated storage hiccup")
        return await self.inner.write_archive(
            destination,
            sources=sources,
            pack_format=pack_format,
            compression_level=compression_level,
            on_progress=on_progress,
        )


class BlockingWriter:
    """Blocks inside compression until cancelled; records the job status it observed."""

    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database
        self.started = asyncio.Event()
        self.observed: list[str] = []

    async def write_archive(self, destination, *, sources, pack_format, compression_level, on_progress=None):
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            database = SQLiteDatabase(self.database.path)
            try:
                async with SQLiteUnitOfWork(database) as uow:
                    status = (
                        await uow.connection.execute(text("SELECT status FROM pack_jobs ORDER BY id"))
                    ).scalar_one()
            finally:
                await database.close()
            self.observed.append(str(status))
            raise


def make_dispatcher(
    database: SQLiteDatabase,
    clock: FakeClock,
    store: DownloadContentStore,
    *,
    writer=None,
    control: PackWriterControl | None = None,
) -> PackDispatcher:
    return PackDispatcher(
        uow_factory=lambda: SQLiteUnitOfWork(database),
        repository_factory=lambda uow: PackRepository(uow.connection),
        quota_repository_factory=lambda uow: QuotaRepository(uow.connection),
        writer=writer or ArchiveWriter(),
        content_store=store,
        writer_control=control or PackWriterControl(),
        settings=lambda: SystemSettings(pack_format="zip", pack_compression_level=3),
        clock_ms=clock,
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
    # 第二个待执行任务仍占用 10 字节预留；used 已含来源与输出。
    assert quota == Quota(100 * 1024 * 1024, 35 + canonical.stat().st_size, 10)
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


async def test_cancel_stops_writer_before_status_change(tmp_path: Path) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    _, first = await seed_pack_file(database, root, user_id, 1)
    store = DownloadContentStore(root)
    pack_id = await create_pack(database, clock, store, user_id, [first])
    control = PackWriterControl()
    blocking = BlockingWriter(database)
    dispatcher = make_dispatcher(database, clock, store, writer=blocking, control=control)
    service = PackControlService(
        uow_factory=lambda: SQLiteUnitOfWork(database),
        repository_factory=lambda uow: PackRepository(uow.connection),
        content_repository_factory=lambda uow: ContentRepository(uow.connection),
        quota_repository_factory=lambda uow: QuotaRepository(uow.connection),
        stop_writer=control.stop_and_wait,
        prepared_store=store,
        clock_ms=clock,
        lease_owner="content-deletion",
        lease_ttl_ms=300_000,
    )

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
    service = PackControlService(
        uow_factory=lambda: SQLiteUnitOfWork(database),
        repository_factory=lambda uow: PackRepository(uow.connection),
        content_repository_factory=lambda uow: ContentRepository(uow.connection),
        quota_repository_factory=lambda uow: QuotaRepository(uow.connection),
        stop_writer=PackWriterControl().stop_and_wait,
        prepared_store=store,
        clock_ms=clock,
        lease_owner="content-deletion",
        lease_ttl_ms=300_000,
    )
    await service.cancel(user_id, pack_id)
    job = await get_job(database, pack_id)
    assert job.status is PackStatus.CANCELLED
    async with SQLiteUnitOfWork(database) as uow:
        quota = await QuotaRepository(uow.connection).get(user_id)
    assert quota == Quota(100 * 1024 * 1024, 10, 0)
