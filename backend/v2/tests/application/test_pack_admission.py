from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import text

from aria2deck.application.downloads.commands import prepare_torrent_download
from aria2deck.application.packs.admission import (
    MachineSpacePort,
    PackAdmissionService,
)
from aria2deck.domain.downloads import AttemptStatus
from aria2deck.domain.errors import DomainError, ErrorCode
from aria2deck.domain.settings import SystemSettings
from aria2deck.domain.torrents import parse_torrent
from aria2deck.infrastructure.content_store import DownloadContentStore
from aria2deck.infrastructure.sqlite.bootstrap import initialize_database
from aria2deck.infrastructure.sqlite.engine import SQLiteDatabase
from aria2deck.infrastructure.sqlite.repositories.accounts import AccountRepository
from aria2deck.infrastructure.sqlite.repositories.content import ContentRepository
from aria2deck.infrastructure.sqlite.repositories.downloads import DownloadRepository
from aria2deck.infrastructure.sqlite.repositories.packs import PackRepository
from aria2deck.infrastructure.sqlite.repositories.quota import QuotaRepository
from aria2deck.infrastructure.sqlite.unit_of_work import SQLiteUnitOfWork
from tests.download_fakes import make_torrent

from .test_download_command_edges import FixedMachineSpace, runtime
from .test_library_services import FakeClock, _digest


def _write(root: Path, storage_path: str, payload: bytes) -> None:
    target = root.joinpath(*storage_path.split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)


async def setup_pack_world(
    tmp_path: Path, *, quota_bytes: int = 100 * 1024 * 1024
) -> tuple[SQLiteDatabase, FakeClock, Path]:
    database = SQLiteDatabase(tmp_path / "v2.db")
    await initialize_database(database)
    root = tmp_path / "downloads"
    root.mkdir(parents=True, exist_ok=True)
    return database, FakeClock(1_000), root


async def create_pack_user(
    database: SQLiteDatabase, username: str = "owner", *, quota_bytes: int = 100 * 1024 * 1024
) -> int:
    async with SQLiteUnitOfWork(database) as uow:
        return await AccountRepository(uow.connection).create_user(
            username=username,
            password_hash=_digest(1),
            is_admin=True,
            quota_bytes=quota_bytes,
            now_ms=1,
        )


async def seed_pack_file(
    database: SQLiteDatabase,
    root: Path,
    user_id: int,
    index: int,
    *,
    payload: bytes = b"x" * 10,
    name: str | None = None,
) -> tuple[int, int]:
    _write(root, f"objects/file-{index}", payload)
    async with SQLiteUnitOfWork(database) as uow:
        repository = ContentRepository(uow.connection)
        content_id = await repository.create_object(
            kind="file",
            digest=_digest(index),
            size=len(payload),
            storage_path=f"objects/file-{index}",
            now_ms=1,
        )
        file_id = await repository.add_user_file(
            user_id=user_id,
            content_id=content_id,
            display_name=name or f"src-{index}",
            now_ms=5,
        )
        await QuotaRepository(uow.connection).rebuild(user_id)
    return content_id, file_id


def make_admission(
    database: SQLiteDatabase,
    clock: FakeClock,
    store: MachineSpacePort,
    *,
    min_free_disk: int = 0,
    pack_format: str = "zip",
) -> PackAdmissionService:
    settings = SystemSettings(min_free_disk=min_free_disk, pack_format=pack_format)
    return PackAdmissionService(
        uow_factory=lambda: SQLiteUnitOfWork(database),
        repository_factory=lambda uow: PackRepository(uow.connection),
        download_repository_factory=lambda uow: DownloadRepository(uow.connection),
        quota_repository_factory=lambda uow: QuotaRepository(uow.connection),
        machine_space=store,
        settings=lambda: settings,
        clock_ms=clock,
    )


async def test_precompute_deduplicates_and_reports_item_failures(tmp_path: Path) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    _, first = await seed_pack_file(database, root, user_id, 1, payload=b"a" * 30)
    _, _second = await seed_pack_file(database, root, user_id, 2, payload=b"b" * 20)
    _, deleting = await seed_pack_file(database, root, user_id, 3, payload=b"c" * 5)
    async with SQLiteUnitOfWork(database) as uow:
        await uow.connection.execute(
            text("UPDATE content_objects SET deletion_state = 'pending_delete' WHERE id = "
                 "(SELECT content_id FROM user_files WHERE id = :id)"),
            {"id": deleting},
        )
    service = make_admission(database, clock, DownloadContentStore(root))

    result = await service.precompute(user_id, [first, first, 999, deleting, 2])
    accepted = [item for item in result.items if item.accepted]
    rejected = {item.user_file_id: item for item in result.items if not item.accepted}
    assert [item.user_file_id for item in accepted] == [first, 2]
    assert result.total_size == 50
    assert rejected[999].error_code == "not_found"
    assert rejected[deleting].error_code == "content_pending_delete"
    assert 0 < result.available_bytes <= 100 * 1024 * 1024 - 55


async def test_create_reserves_and_snapshots_atomically(tmp_path: Path) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    _, first = await seed_pack_file(database, root, user_id, 1)
    _, second = await seed_pack_file(database, root, user_id, 2, payload=b"y" * 25)
    service = make_admission(database, clock, DownloadContentStore(root))

    pack_id = await service.create(
        user_id,
        user_file_ids=[first, second, first],
        output_name="bundle.zip",
        delete_source=True,
    )
    async with SQLiteUnitOfWork(database) as uow:
        job = await PackRepository(uow.connection).get_job(pack_id)
        sources = await PackRepository(uow.connection).list_sources(pack_id)
        quota = await QuotaRepository(uow.connection).get(user_id)
    assert (job.status, job.reserved_bytes, job.physical_reserved_bytes, job.delete_source) == (
        "pending", 35, 35, True,
    )
    assert len(sources) == 2
    assert quota.reserved_bytes == 35

    with pytest.raises(DomainError) as caught:
        await service.create(user_id, user_file_ids=[999], output_name="x.zip", delete_source=False)
    assert caught.value.code is ErrorCode.NOT_FOUND
    with pytest.raises(DomainError) as caught:
        await service.create(user_id, user_file_ids=[first], output_name="a/b", delete_source=False)
    assert caught.value.code is ErrorCode.INVALID_INPUT
    with pytest.raises(DomainError) as caught:
        await service.create(
            user_id, user_file_ids=[first] * 101, output_name="many.zip", delete_source=False
        )
    assert caught.value.code is ErrorCode.INVALID_INPUT
    with pytest.raises(DomainError) as caught:
        await service.create(user_id, user_file_ids=[], output_name="none.zip", delete_source=False)
    assert caught.value.code is ErrorCode.INVALID_INPUT
    async with SQLiteUnitOfWork(database) as uow:
        count = (
            await uow.connection.execute(text("SELECT COUNT(*) FROM pack_jobs"))
        ).scalar_one()
    assert count == 1, "失败的创建不得留下半任务"


async def test_create_snapshots_non_default_pack_format(tmp_path: Path) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    _, file_id = await seed_pack_file(database, root, user_id, 1)
    service = make_admission(
        database, clock, DownloadContentStore(root), pack_format="tar.zst"
    )

    pack_id = await service.create(
        user_id, user_file_ids=[file_id], output_name="bundle.tar.zst", delete_source=False
    )

    async with SQLiteUnitOfWork(database) as uow:
        job = await PackRepository(uow.connection).get_job(pack_id)
    assert job.pack_format == "tar.zst"


async def test_create_rejects_when_quota_or_disk_insufficient(tmp_path: Path) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    tight_user = await create_pack_user(database, "tight", quota_bytes=150)
    _, big = await seed_pack_file(database, root, tight_user, 1, payload=b"z" * 100)
    service = make_admission(database, clock, DownloadContentStore(root))
    with pytest.raises(DomainError) as caught:
        await service.create(tight_user, user_file_ids=[big], output_name="big.zip", delete_source=False)
    assert caught.value.code is ErrorCode.QUOTA_EXCEEDED

    roomy = await create_pack_user(database, "roomy")
    _, small = await seed_pack_file(database, root, roomy, 2, payload=b"z" * 10)
    starving = make_admission(
        database, clock, DownloadContentStore(root), min_free_disk=100 * 1024**3
    )
    with pytest.raises(DomainError) as caught:
        await starving.create(roomy, user_file_ids=[small], output_name="s.zip", delete_source=False)
    assert caught.value.code is ErrorCode.PHYSICAL_SPACE_EXCEEDED
    result = await starving.precompute(roomy, [small])
    assert result.available_bytes == 0


async def test_pack_admission_counts_active_download_physical_commitment(
    tmp_path: Path,
) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    _, file_id = await seed_pack_file(
        database, root, user_id, 1, payload=b"x" * 30
    )
    async with SQLiteUnitOfWork(database) as uow:
        downloads = DownloadRepository(uow.connection)
        source_id = await downloads.create_source(
            kind="http",
            resource_key="http:existing",
            payload_json="{}",
            options_json="{}",
            selected_file_indexes_json=None,
            display_name="existing",
            display_uri=None,
            now_ms=1,
        )
        await downloads.create_attempt(
            source_id=source_id,
            resource_key="http:existing",
            gid="0123456789abcdef",
            status=AttemptStatus.ACTIVE,
            total_length=80,
            physical_reserved_bytes=80,
            now_ms=1,
        )
    service = make_admission(database, clock, FixedMachineSpace(100))

    uow_entries = 0
    original_uow_factory = service.uow_factory

    def counted_uow() -> SQLiteUnitOfWork:
        nonlocal uow_entries
        uow_entries += 1
        return original_uow_factory()

    service.uow_factory = counted_uow
    with pytest.raises(DomainError) as caught:
        await service.create(
            user_id,
            user_file_ids=[file_id],
            output_name="new.zip",
            delete_source=False,
        )
    assert caught.value.code is ErrorCode.PHYSICAL_SPACE_EXCEEDED
    assert uow_entries == 1
    result = await service.precompute(user_id, [file_id])
    assert result.available_bytes == 20
    assert uow_entries == 2


async def test_concurrent_download_and_pack_admission_share_one_machine_budget(
    tmp_path: Path,
) -> None:
    async with runtime(tmp_path) as (commands, user_id, _principal, _backend):
        database = SQLiteDatabase(tmp_path / "v2.db")
        try:
            root = tmp_path / "downloads"
            _, file_id = await seed_pack_file(
                database, root, user_id, 1, payload=b"x" * 60
            )
            packs = make_admission(database, FakeClock(2_000), FixedMachineSpace(100))
            commands.machine_space = FixedMachineSpace(100)
            commands.settings = lambda: SystemSettings(min_free_disk=0)
            prepared = prepare_torrent_download(
                parse_torrent(make_torrent([("new.bin", 60)]))
            )

            results = await asyncio.gather(
                commands._register(user_id=user_id, prepared=prepared),
                packs.create(
                    user_id,
                    user_file_ids=[file_id],
                    output_name="new.zip",
                    delete_source=False,
                ),
                return_exceptions=True,
            )
            async with SQLiteUnitOfWork(database) as uow:
                quota = await QuotaRepository(uow.connection).get(user_id)
                rebuilt = await QuotaRepository(uow.connection).rebuild(user_id)
            assert quota == rebuilt
        finally:
            await database.close()

    successes = [result for result in results if not isinstance(result, Exception)]
    failures = [result for result in results if isinstance(result, DomainError)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert failures[0].code is ErrorCode.PHYSICAL_SPACE_EXCEEDED


async def test_concurrent_download_growth_and_pack_admission_share_one_machine_budget(
    tmp_path: Path,
) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    try:
        _, file_id = await seed_pack_file(
            database, root, user_id, 1, payload=b"x" * 60
        )
        async with SQLiteUnitOfWork(database) as uow:
            downloads = DownloadRepository(uow.connection)
            source_id = await downloads.create_source(
                kind="http",
                resource_key="http:growing",
                payload_json="{}",
                options_json="{}",
                selected_file_indexes_json=None,
                display_name="growing",
                display_uri=None,
                now_ms=1,
            )
            attempt_id = await downloads.create_attempt(
                source_id=source_id,
                resource_key="http:growing",
                gid="0123456789abcdef",
                status=AttemptStatus.ACTIVE,
                total_length=None,
                physical_reserved_bytes=0,
                now_ms=1,
            )
            await downloads.create_subscription(
                attempt_id=attempt_id,
                user_id=user_id,
                reserved_bytes=0,
                display_name="growing",
                display_uri=None,
                now_ms=1,
            )
        packs = make_admission(database, clock, FixedMachineSpace(100))

        async def grow() -> DomainError | None:
            async with SQLiteUnitOfWork(database) as uow:
                return await DownloadRepository(uow.connection).admit_size(
                    attempt_id=attempt_id,
                    observed_bytes=60,
                    known_total=False,
                    disk_free_bytes=100,
                    min_free_bytes=0,
                    now_ms=2,
                )

        growth, pack = await asyncio.gather(
            grow(),
            packs.create(
                user_id,
                user_file_ids=[file_id],
                output_name="growth.zip",
                delete_source=False,
            ),
            return_exceptions=True,
        )
        assert (growth is None) + (not isinstance(pack, Exception)) == 1
        failures = [
            result for result in (growth, pack) if isinstance(result, DomainError)
        ]
        assert len(failures) == 1
        assert failures[0].code is ErrorCode.PHYSICAL_SPACE_EXCEEDED
        async with SQLiteUnitOfWork(database) as uow:
            quota = await QuotaRepository(uow.connection).get(user_id)
            rebuilt = await QuotaRepository(uow.connection).rebuild(user_id)
            physical_downloads = await DownloadRepository(
                uow.connection
            ).list_active_physical()
            physical_packs = await PackRepository(uow.connection).list_active_physical()
        assert quota == rebuilt
        committed = sum(item.reserved_bytes for item in physical_downloads) + sum(
            item.reserved_bytes for item in physical_packs
        )
        assert committed <= 100
    finally:
        await database.close()
