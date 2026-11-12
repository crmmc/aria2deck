from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from aria2deck.application.downloads.commands import prepare_torrent_download
from aria2deck.application.packs.admission import (
    MachineSpacePort,
    PackAdmissionService,
)
from aria2deck.application.packs.models import PackAdmissionSource
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
    # 预算 = 50 来源 + 2*(512+2*5) 元数据 + 65536 冗余。
    assert result.total_size == 66_630
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
        "pending", 66_615, 66_615, True,
    )
    assert job.source_size == 35, "source_size 保留真实来源内容字节"
    assert len(sources) == 2
    assert quota.reserved_bytes == 66_615

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
    # 已有下载 80：S = B + 80 - 1，单独看来源可容，合并后恰好卡掉打包预算。
    service = make_admission(database, clock, FixedMachineSpace(66_167))

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
    assert result.available_bytes == 66_087, "机器余量必须扣除已有下载的 80 字节承诺"
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
            # 60 字节下载与 66_118 字节打包预算各自可容，合并后只允许一个成功。
            packs = make_admission(database, FakeClock(2_000), FixedMachineSpace(66_177))
            commands.machine_space = FixedMachineSpace(66_177)
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
        # 60 字节增长与 66_118 字节打包预算各自可容，合并后只允许一个成功。
        packs = make_admission(database, clock, FixedMachineSpace(66_177))

        async def grow() -> DomainError | None:
            async with SQLiteUnitOfWork(database) as uow:
                return await DownloadRepository(uow.connection).admit_size(
                    attempt_id=attempt_id,
                    observed_bytes=60,
                    known_total=False,
                    disk_free_bytes=66_177,
                    min_free_bytes=0,
                    max_task_bytes=0,
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
        assert committed <= 66_177
    finally:
        await database.close()


LONG_DIRECTORY_NAME = "d" * 200
# 单条 4019 字节的合法目录条目路径（20 段 × 200 字节）：加前缀才会越过 4096。
LONG_DIRECTORY_ENTRY = "/".join(["a" * 200] * 20)


async def seed_pack_directory(
    database: SQLiteDatabase,
    user_id: int,
    *,
    index: int,
    name: str,
    entries: list[tuple[str, str, int]],
) -> tuple[int, int]:
    """建一个真实目录内容（含 content_entries），供聚合预算使用。"""
    async with SQLiteUnitOfWork(database) as uow:
        content = ContentRepository(uow.connection)
        content_id = await content.create_object(
            kind="directory",
            digest=_digest(index),
            size=sum(size for _path, kind, size in entries if kind == "file"),
            storage_path=f"objects/directory-{index}",
            now_ms=1,
        )
        for entry_path, kind, size in entries:
            parent, _, leaf = entry_path.rpartition("/")
            await content.add_entry(
                content_id=content_id,
                parent_path=parent,
                name=leaf,
                entry_kind=kind,
                size=size,
                now_ms=1,
            )
        file_id = await content.add_user_file(
            user_id=user_id, content_id=content_id, display_name=name, now_ms=5
        )
        await QuotaRepository(uow.connection).rebuild(user_id)
    return content_id, file_id


class DuplicateAdmissionRepository:
    """受控仓储：把一条真实来源按不同 display_name 复制为多个候选。

    当前 schema 的 UNIQUE(user_id, content_id) 不允许同一用户正常建立这种重复归属，
    这里只在内存里复制 PackAdmissionSource 验证预算代数，不改 schema，也不冒充正常 API。
    """

    def __init__(
        self, inner: PackRepository, *, real_file_id: int, variants: dict[int, str]
    ) -> None:
        self.inner = inner
        self.real_file_id = real_file_id
        self.variants = variants

    async def list_admission_sources(
        self, *, user_id: int, user_file_ids: list[int]
    ) -> list[PackAdmissionSource]:
        real = await self.inner.list_admission_sources(
            user_id=user_id, user_file_ids=[self.real_file_id]
        )
        if not real:
            return []
        return [
            replace(real[0], user_file_id=file_id, display_name=self.variants[file_id])
            for file_id in user_file_ids
            if file_id in self.variants
        ]

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


async def test_empty_directory_still_reserves_the_minimum_slack(tmp_path: Path) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    _, file_id = await seed_pack_directory(database, user_id, index=20, name="empty-dir", entries=[])
    service = make_admission(database, clock, DownloadContentStore(root))

    result = await service.precompute(user_id, [file_id])
    assert result.items[0].accepted and result.items[0].size == 0
    assert result.total_size == 66_068, "空目录仍需要根条目与 64 KiB 固定冗余"

    pack_id = await service.create(
        user_id, user_file_ids=[file_id], output_name="empty.zip", delete_source=False
    )
    async with SQLiteUnitOfWork(database) as uow:
        job = await PackRepository(uow.connection).get_job(pack_id)
        quota = await QuotaRepository(uow.connection).get(user_id)
    assert (job.source_size, job.reserved_bytes, job.physical_reserved_bytes) == (
        0,
        66_068,
        66_068,
    )
    assert quota.reserved_bytes == 66_068


async def test_precompute_rejects_only_the_source_that_breaks_the_path_limit(
    tmp_path: Path,
) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    _, valid_file = await seed_pack_file(database, root, user_id, 1, payload=b"x" * 10)
    _, oversized = await seed_pack_directory(
        database,
        user_id,
        index=21,
        name="x" * 80,
        entries=[(LONG_DIRECTORY_ENTRY, "file", 0)],
    )
    _, valid_directory = await seed_pack_directory(
        database,
        user_id,
        index=22,
        name="ok",
        entries=[(LONG_DIRECTORY_ENTRY, "file", 0)],
    )
    service = make_admission(database, clock, DownloadContentStore(root))

    result = await service.precompute(user_id, [valid_file, oversized, valid_directory])

    accepted = [item.user_file_id for item in result.items if item.accepted]
    rejected = {item.user_file_id: item for item in result.items if not item.accepted}
    assert accepted == [valid_file, valid_directory]
    assert rejected[oversized].error_code == "invalid_input"
    # 10 来源 + 3*512 条目 + 2*(5 + 3 + 4022) 路径 + 65536 冗余。
    assert result.total_size == 75_142, "被拒绝来源不得污染已接受来源的累计预算"

    with pytest.raises(DomainError) as caught:
        await service.create(
            user_id,
            user_file_ids=[valid_file, oversized, valid_directory],
            output_name="mixed.zip",
            delete_source=False,
        )
    assert caught.value.code is ErrorCode.INVALID_INPUT
    async with SQLiteUnitOfWork(database) as uow:
        assert (
            await uow.connection.execute(text("SELECT COUNT(*) FROM pack_jobs"))
        ).scalar_one() == 0


async def test_directory_projection_drift_is_an_invalid_state(tmp_path: Path) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    content_id, file_id = await seed_pack_directory(
        database, user_id, index=23, name="drift", entries=[("a", "file", 4), ("b", "file", 4)]
    )
    async with SQLiteUnitOfWork(database) as uow:
        await uow.connection.execute(
            text("UPDATE content_objects SET size = size + 1 WHERE id = :id"),
            {"id": content_id},
        )
    service = make_admission(database, clock, DownloadContentStore(root))

    result = await service.precompute(user_id, [file_id])
    assert result.items[0].error_code == "invalid_state"
    assert result.total_size == 0

    with pytest.raises(DomainError) as caught:
        await service.create(
            user_id, user_file_ids=[file_id], output_name="drift.zip", delete_source=False
        )
    assert caught.value.code is ErrorCode.INVALID_STATE
    async with SQLiteUnitOfWork(database) as uow:
        assert (
            await uow.connection.execute(text("SELECT COUNT(*) FROM pack_jobs"))
        ).scalar_one() == 0
        assert (await QuotaRepository(uow.connection).get(user_id)).reserved_bytes == 0


async def test_controlled_duplicate_content_charges_each_arcname(tmp_path: Path) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    _, real_id = await seed_pack_directory(
        database, user_id, index=24, name="a", entries=[("f", "file", 135)]
    )
    fake_id = real_id + 1000
    service = make_admission(database, clock, DownloadContentStore(root))
    service.repository_factory = lambda uow: DuplicateAdmissionRepository(
        PackRepository(uow.connection),
        real_file_id=real_id,
        variants={real_id: "a", fake_id: LONG_DIRECTORY_NAME},
    )

    result = await service.precompute(user_id, [real_id, fake_id])

    assert all(item.accepted for item in result.items)
    assert [item.size for item in result.items] == [135, 135]
    # 270 来源 + [a/(516)+a/f(518)] + [200 字节目录根(914)+条目(916)] + 65536。
    assert result.total_size == 68_670


async def test_create_rejects_pending_delete_owner_without_leaving_a_job(
    tmp_path: Path,
) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    _, file_id = await seed_pack_file(database, root, user_id, 1)
    async with SQLiteUnitOfWork(database) as uow:
        await AccountRepository(uow.connection).mark_pending_delete(user_id=user_id, now_ms=5)
    service = make_admission(database, clock, DownloadContentStore(root))

    with pytest.raises(DomainError) as caught:
        await service.create(
            user_id, user_file_ids=[file_id], output_name="late.zip", delete_source=False
        )
    assert caught.value.code is ErrorCode.CONTENT_PENDING_DELETE
    async with SQLiteUnitOfWork(database) as uow:
        assert (
            await uow.connection.execute(text("SELECT COUNT(*) FROM pack_jobs"))
        ).scalar_one() == 0
        assert (await QuotaRepository(uow.connection).get(user_id)).reserved_bytes == 0
