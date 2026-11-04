from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

import pytest
from sqlalchemy import text

from aria2deck.application.library.commands import LibraryCommandService
from aria2deck.application.library.deletion import ContentDeletionWorker
from aria2deck.application.library.download import LibraryDownloadService
from aria2deck.application.library.leases import ReadLeaseManager
from aria2deck.application.library.models import StorageDeleteOutcome
from aria2deck.application.library.queries import LibraryQueryService
from aria2deck.domain.content import ContentDeletionState, ContentKind
from aria2deck.domain.errors import DomainError, ErrorCode
from aria2deck.domain.packs import PackSourceSnapshot
from aria2deck.domain.quota import Quota
from aria2deck.infrastructure.sqlite.bootstrap import initialize_database
from aria2deck.infrastructure.sqlite.engine import SQLiteDatabase
from aria2deck.infrastructure.sqlite.repositories.accounts import AccountRepository
from aria2deck.infrastructure.sqlite.repositories.content import (
    DELETE_LEASE_OWNER,
    DELETE_LEASE_TTL_MS,
    ContentRepository,
)
from aria2deck.infrastructure.sqlite.repositories.packs import PackRepository
from aria2deck.infrastructure.sqlite.repositories.quota import QuotaRepository
from aria2deck.infrastructure.sqlite.repositories.shares import ShareRepository
from aria2deck.infrastructure.sqlite.unit_of_work import SQLiteUnitOfWork


def _digest(index: int) -> str:
    return f"{index:064x}"


class FakeClock:
    def __init__(self, now_ms: int = 1_000) -> None:
        self.now_ms = now_ms

    def __call__(self) -> int:
        return self.now_ms

    def advance(self, milliseconds: int) -> None:
        self.now_ms += milliseconds


@dataclass
class FakeStore:
    root: Path
    deleted: list[str] = field(default_factory=list)
    fail_deletes: bool = False

    def physical_path(self, storage_path: str, entry_relative_path: str | None = None) -> Path:
        target = self.root.joinpath(*storage_path.split("/"))
        return target if entry_relative_path is None else target / entry_relative_path

    async def open_read_handle(
        self, storage_path: str, entry_relative_path: str | None = None
    ) -> IO[bytes]:
        return self.physical_path(storage_path, entry_relative_path).open("rb")

    def on_disk(self, storage_path: str) -> bool:
        return self.root.joinpath(*storage_path.split("/")).exists()

    async def physical_delete(self, storage_path: str) -> None:
        self.deleted.append(storage_path)
        if self.fail_deletes:
            raise OSError("simulated storage failure")
        target = self.root.joinpath(*storage_path.split("/"))
        if target.is_dir():
            import shutil

            shutil.rmtree(target)
        elif target.exists():
            target.unlink()


@dataclass
class RaisingHandle:
    def read(self, size: int = -1) -> bytes:
        raise OSError("simulated read failure")

    def close(self) -> None:
        self.closed = True


class FailingStore(FakeStore):
    async def open_read_handle(
        self, storage_path: str, entry_relative_path: str | None = None
    ) -> IO[bytes]:
        handle = RaisingHandle()
        return handle  # type: ignore[return-value]


async def _setup(tmp_path: Path) -> tuple[SQLiteDatabase, FakeClock]:
    database = SQLiteDatabase(tmp_path / "v2.db")
    await initialize_database(database)
    return database, FakeClock()


async def _create_user(database: SQLiteDatabase, username: str, *, admin: bool = True) -> int:
    async with SQLiteUnitOfWork(database) as uow:
        return await AccountRepository(uow.connection).create_user(
            username=username,
            password_hash=_digest(1),
            is_admin=admin,
            quota_bytes=1000,
            now_ms=1,
        )


async def _seed_file(
    database: SQLiteDatabase,
    user_id: int,
    index: int,
    *,
    name: str | None = None,
    payload: bytes | None = None,
) -> tuple[int, int]:
    size = len(payload) if payload is not None else 10
    async with SQLiteUnitOfWork(database) as uow:
        repository = ContentRepository(uow.connection)
        content_id = await repository.create_object(
            kind="file", digest=_digest(index), size=size,
            storage_path=f"objects/file-{index}", now_ms=1,
        )
        file_id = await repository.add_user_file(
            user_id=user_id, content_id=content_id, display_name=name or f"file-{index}", now_ms=1
        )
    return content_id, file_id


async def _seed_directory(
    database: SQLiteDatabase,
    user_id: int,
    index: int,
    entries: list[tuple[str, str, str, int]],
) -> tuple[int, int]:
    async with SQLiteUnitOfWork(database) as uow:
        repository = ContentRepository(uow.connection)
        content_id = await repository.create_object(
            kind="directory", digest=_digest(index), size=sum(size for *_, size in entries),
            storage_path=f"objects/directory-{index}", now_ms=1,
        )
        for parent, name, entry_kind, size in entries:
            await repository.add_entry(
                content_id=content_id, parent_path=parent, name=name,
                entry_kind=entry_kind, size=size, now_ms=1,
            )
        file_id = await repository.add_user_file(
            user_id=user_id, content_id=content_id, display_name=f"dir-{index}", now_ms=1
        )
    return content_id, file_id


def _make_services(
    database: SQLiteDatabase, clock: FakeClock, store: FakeStore
) -> tuple[
    LibraryQueryService, LibraryCommandService, ReadLeaseManager, LibraryDownloadService
]:
    def uow_factory() -> SQLiteUnitOfWork:
        return SQLiteUnitOfWork(database)

    def repository(uow: object) -> ContentRepository:
        return ContentRepository(uow.connection)  # type: ignore[attr-defined]

    def quota_repository(uow: object) -> QuotaRepository:
        return QuotaRepository(uow.connection)  # type: ignore[attr-defined]

    queries = LibraryQueryService(
        uow_factory=uow_factory,
        repository_factory=repository,
        quota_repository_factory=quota_repository,
        storage_presence=store,
    )
    commands = LibraryCommandService(
        uow_factory=uow_factory,
        repository_factory=repository,
        quota_repository_factory=quota_repository,
        clock_ms=clock,
        lease_owner=DELETE_LEASE_OWNER,
        lease_ttl_ms=DELETE_LEASE_TTL_MS,
    )
    leases = ReadLeaseManager(uow_factory=uow_factory, repository_factory=repository)
    download = LibraryDownloadService(
        uow_factory=uow_factory,
        repository_factory=repository,
        leases=leases,
        content_store=store,
    )
    return queries, commands, leases, download


async def test_list_root_files_returns_views_total_and_quota(tmp_path: Path) -> None:
    database, clock = await _setup(tmp_path)
    user_id = await _create_user(database, "admin")
    other_id = await _create_user(database, "other", admin=False)
    first, _ = await _seed_file(database, user_id, 1)
    second, _ = await _seed_file(database, user_id, 2)
    await _seed_file(database, other_id, 3)
    async with SQLiteUnitOfWork(database) as uow:
        await QuotaRepository(uow.connection).rebuild(user_id)
    store = FakeStore(tmp_path / "downloads")
    queries, _, _, _ = _make_services(database, clock, store)
    views, total, quota = await queries.list_root_files(user_id=user_id, page=1, page_size=10)
    assert total == 2
    assert [(view.display_name, view.size) for view in views] == [("file-1", 10), ("file-2", 10)]
    assert views[0].kind is ContentKind.FILE
    assert quota == Quota(1000, 20, 0)
    assert first != second
    beyond, beyond_total, _ = await queries.list_root_files(user_id=user_id, page=9, page_size=5)
    assert beyond == []
    assert beyond_total == 2


async def test_browse_directory_builds_breadcrumbs_and_rejects_non_directories(
    tmp_path: Path,
) -> None:
    database, clock = await _setup(tmp_path)
    user_id = await _create_user(database, "admin")
    _, file_id = await _seed_file(database, user_id, 1)
    _, dir_file = await _seed_directory(
        database, user_id, 2, [("", "child.bin", "file", 4), ("", "nested", "directory", 0), ("nested", "deep.bin", "file", 3)]
    )
    queries, _, _, _ = _make_services(database, clock, FakeStore(tmp_path))
    entries, breadcrumbs, total = await queries.browse_directory(
        user_id=user_id, file_id=dir_file, path="", page=1, page_size=10
    )
    nested, nested_breadcrumbs, nested_total = await queries.browse_directory(
        user_id=user_id, file_id=dir_file, path="nested", page=1, page_size=10
    )
    assert total == 2
    assert [(entry.name, entry.kind) for entry in entries][:1] == [("nested", ContentKind.DIRECTORY)]
    assert [item.path for item in breadcrumbs] == [""]
    assert nested_total == 1
    assert [(item.name, item.path) for item in nested_breadcrumbs] == [
        ("dir-2", ""),
        ("nested", "nested"),
    ]
    with pytest.raises(DomainError) as caught:
        await queries.browse_directory(user_id=user_id, file_id=file_id, path="", page=1, page_size=10)
    assert caught.value.code is ErrorCode.INVALID_INPUT
    with pytest.raises(DomainError) as caught:
        await queries.browse_directory(
            user_id=user_id, file_id=dir_file, path="child.bin", page=1, page_size=10
        )
    assert caught.value.code is ErrorCode.NOT_FOUND
    with pytest.raises(DomainError) as caught:
        await queries.browse_directory(user_id=user_id, file_id=999, path="", page=1, page_size=10)
    assert caught.value.code is ErrorCode.NOT_FOUND


async def test_search_validates_term_scope_and_isolates_users(tmp_path: Path) -> None:
    database, clock = await _setup(tmp_path)
    user_id = await _create_user(database, "admin")
    other_id = await _create_user(database, "other", admin=False)
    await _seed_file(database, user_id, 1, name="Report.pdf")
    _, dir_file = await _seed_directory(
        database, user_id, 2, [("", "docs", "directory", 0), ("docs", "REPORT-q1.txt", "file", 2)]
    )
    await _seed_file(database, other_id, 3, name="other-report")
    queries, _, _, _ = _make_services(database, clock, FakeStore(tmp_path))
    with pytest.raises(DomainError) as caught:
        await queries.search(user_id=user_id, term="   ", scope_file_id=None)
    assert caught.value.code is ErrorCode.INVALID_INPUT
    with pytest.raises(DomainError) as caught:
        await queries.search(user_id=user_id, term="x", scope_file_id=999)
    assert caught.value.code is ErrorCode.INVALID_INPUT
    _, plain_file = await _seed_file(database, user_id, 4, name="plain.bin")
    with pytest.raises(DomainError) as caught:
        await queries.search(user_id=user_id, term="x", scope_file_id=plain_file)
    assert caught.value.code is ErrorCode.INVALID_INPUT
    assert caught.value.args[0] == "搜索范围必须是目录文件"
    hits, truncated = await queries.search(user_id=user_id, term="report", scope_file_id=None)
    assert truncated is False
    assert [(hit.display_name, hit.path) for hit in hits] == [
        ("Report.pdf", ""),
        ("dir-2", "docs/REPORT-q1.txt"),
    ]
    scoped, _ = await queries.search(user_id=user_id, term="report", scope_file_id=dir_file)
    assert [hit.path for hit in scoped] == ["docs/REPORT-q1.txt"]
    foreign, _ = await queries.search(user_id=other_id, term="report", scope_file_id=None)
    assert [hit.display_name for hit in foreign] == ["other-report"]


async def test_storage_listing_and_referencing_users(tmp_path: Path) -> None:
    database, clock = await _setup(tmp_path)
    user_id = await _create_user(database, "admin")
    other_id = await _create_user(database, "other", admin=False)
    content_id, _ = await _seed_file(database, user_id, 1)
    orphan_id, _ = await _seed_file(database, user_id, 2)
    async with SQLiteUnitOfWork(database) as uow:
        await ContentRepository(uow.connection).remove_user_file(user_id=user_id, file_id=orphan_id)
    store = FakeStore(tmp_path / "downloads")
    (tmp_path / "downloads" / "objects").mkdir(parents=True)
    (tmp_path / "downloads" / "objects" / "file-1").write_bytes(b"data")
    queries, _, _, _ = _make_services(database, clock, store)
    views, total = await queries.list_storage_files(page=1, page_size=10)
    assert total == 2
    on_disk_by_id = {view.content_id: view.on_disk for view in views}
    assert on_disk_by_id[content_id] is True
    assert on_disk_by_id[orphan_id] is False
    orphans, orphan_total = await queries.list_storage_files(page=1, page_size=10, orphans_only=True)
    assert orphan_total == 1
    assert orphans[0].content_id == orphan_id
    referencing = await queries.list_referencing_users(content_id)
    assert sorted(row.username for row in referencing) == ["admin"]
    assert await queries.list_referencing_users(orphan_id) == []
    with pytest.raises(DomainError) as caught:
        await queries.list_referencing_users(999)
    assert caught.value.code is ErrorCode.NOT_FOUND
    assert other_id != user_id


async def test_rename_maps_repository_semantics(tmp_path: Path) -> None:
    database, clock = await _setup(tmp_path)
    user_id = await _create_user(database, "admin")
    _, file_id = await _seed_file(database, user_id, 1)
    await _seed_file(database, user_id, 2, name="taken")
    _, commands, _, _ = _make_services(database, clock, FakeStore(tmp_path))
    view = await commands.rename(user_id=user_id, file_id=file_id, new_name="renamed.bin")
    assert (view.id, view.display_name) == (file_id, "renamed.bin")
    with pytest.raises(DomainError) as caught:
        await commands.rename(user_id=user_id, file_id=file_id, new_name="taken")
    assert caught.value.code is ErrorCode.DUPLICATE_RESOURCE
    with pytest.raises(DomainError) as caught:
        await commands.rename(user_id=user_id, file_id=file_id, new_name="a/b")
    assert caught.value.code is ErrorCode.INVALID_INPUT
    with pytest.raises(DomainError) as caught:
        await commands.rename(user_id=user_id, file_id=999, new_name="x")
    assert caught.value.code is ErrorCode.NOT_FOUND


async def _seed_pack_source(database: SQLiteDatabase, user_id: int, file_id: int, content_id: int) -> None:
    async with SQLiteUnitOfWork(database) as uow:
        pack_id = await PackRepository(uow.connection).create_job(
            owner_id=user_id, output_name="a.zip", pack_format="zip",
            source_size=10, reserved_bytes=0, physical_reserved_bytes=0,
            delete_source=True, now_ms=1,
        )
        await PackRepository(uow.connection).add_source(
            pack_id,
            PackSourceSnapshot(file_id, content_id, "file:" + _digest(1), "a", 1, 10),
        )


async def test_delete_user_file_decision_matrix(tmp_path: Path) -> None:
    database, clock = await _setup(tmp_path)
    user_id = await _create_user(database, "admin")
    other_id = await _create_user(database, "other", admin=False)

    # 共享引用：删除一个引用不影响其他用户，也不触发物理删除。
    shared_content, owner_file = await _seed_file(database, user_id, 1)
    async with SQLiteUnitOfWork(database) as uow:
        await ContentRepository(uow.connection).add_user_file(
            user_id=other_id, content_id=shared_content, display_name="shared", now_ms=1
        )
        await QuotaRepository(uow.connection).rebuild(user_id)
    store = FakeStore(tmp_path / "downloads")
    _, commands, _, _ = _make_services(database, clock, store)
    await commands.delete_user_file(user_id=user_id, file_id=owner_file)
    async with SQLiteUnitOfWork(database) as uow:
        record = await ContentRepository(uow.connection).get_object(shared_content)
        quota = await QuotaRepository(uow.connection).get(user_id)
    assert record.deletion_state is ContentDeletionState.ACTIVE
    assert quota.used_bytes == 0
    assert store.deleted == []

    # 最后引用：认领物理删除并撤销分享。
    last_content, last_file = await _seed_file(database, user_id, 2)
    async with SQLiteUnitOfWork(database) as uow:
        await QuotaRepository(uow.connection).rebuild(user_id)
        await ShareRepository(uow.connection).create_share(
            user_id=user_id, user_file_id=last_file, code="c1", password_hash=None,
            expires_at_ms=None, max_downloads=None, now_ms=10,
        )
    await commands.delete_user_file(user_id=user_id, file_id=last_file)
    async with SQLiteUnitOfWork(database) as uow:
        record = await ContentRepository(uow.connection).get_object(last_content)
        revoked = await ShareRepository(uow.connection).get_share(1)
        quota = await QuotaRepository(uow.connection).get(user_id)
    assert record.deletion_state is ContentDeletionState.PENDING_DELETE
    assert record.delete_lease_owner == DELETE_LEASE_OWNER
    assert revoked.revoked_at_ms is not None
    assert quota.used_bytes == 0

    # 最后引用 + 待清理打包来源：拒绝且不产生任何删除副作用。
    busy_content, busy_file = await _seed_file(database, user_id, 3)
    await _seed_pack_source(database, user_id, busy_file, busy_content)
    async with SQLiteUnitOfWork(database) as uow:
        await QuotaRepository(uow.connection).rebuild(user_id)
    with pytest.raises(DomainError) as caught:
        await commands.delete_user_file(user_id=user_id, file_id=busy_file)
    assert caught.value.code is ErrorCode.CONTENT_BUSY
    async with SQLiteUnitOfWork(database) as uow:
        repository = ContentRepository(uow.connection)
        record = await repository.get_object(busy_content)
        assert await repository.count_references(busy_content) == 1
        quota = await QuotaRepository(uow.connection).get(user_id)
    assert record.deletion_state is ContentDeletionState.ACTIVE
    assert quota.used_bytes == 10
    assert store.deleted == []

    with pytest.raises(DomainError) as caught:
        await commands.delete_user_file(user_id=user_id, file_id=999)
    assert caught.value.code is ErrorCode.NOT_FOUND


async def test_storage_delete_orphans_reports_per_item_outcomes(tmp_path: Path) -> None:
    database, clock = await _setup(tmp_path)
    user_id = await _create_user(database, "admin")
    orphan_content, orphan_file = await _seed_file(database, user_id, 1)
    referenced_content, _ = await _seed_file(database, user_id, 2)
    async with SQLiteUnitOfWork(database) as uow:
        await ContentRepository(uow.connection).remove_user_file(user_id=user_id, file_id=orphan_file)
    _, commands, _, _ = _make_services(database, clock, FakeStore(tmp_path))
    outcomes = await commands.storage_delete_orphans(
        [orphan_content, referenced_content, 999, orphan_content]
    )
    assert outcomes == [
        StorageDeleteOutcome(orphan_content, True),
        StorageDeleteOutcome(referenced_content, False, "content_busy", "内容仍有用户引用"),
        StorageDeleteOutcome(999, False, "not_found", "内容对象不存在"),
        # 重复受理：同批第二次时对象已进入删除流程。
        StorageDeleteOutcome(orphan_content, False, "content_pending_delete", "内容已在删除流程中"),
    ]


async def test_read_lease_manager_tracks_in_flight_and_floors_at_zero(tmp_path: Path) -> None:
    database, clock = await _setup(tmp_path)
    user_id = await _create_user(database, "admin")
    content_id, _ = await _seed_file(database, user_id, 1)
    _, _, leases, _ = _make_services(database, clock, FakeStore(tmp_path))
    await leases.acquire(content_id)
    await leases.acquire(content_id)
    assert leases.in_flight(content_id) == 2
    async with SQLiteUnitOfWork(database) as uow:
        assert (await ContentRepository(uow.connection).get_object(content_id)).read_leases == 2
    await leases.release(content_id)
    await leases.release(content_id)
    await leases.release(content_id)
    assert leases.in_flight(content_id) == 0
    async with SQLiteUnitOfWork(database) as uow:
        assert (await ContentRepository(uow.connection).get_object(content_id)).read_leases == 0

    async with SQLiteUnitOfWork(database) as uow:
        await uow.connection.execute(
            text("UPDATE content_objects SET deletion_state = 'pending_delete' WHERE id = :id"),
            {"id": content_id},
        )
    with pytest.raises(DomainError) as caught:
        await leases.acquire(content_id)
    assert caught.value.code is ErrorCode.CONTENT_PENDING_DELETE
    assert leases.in_flight(content_id) == 0


def _write_store_file(root: Path, storage_path: str, payload: bytes) -> None:
    target = root.joinpath(*storage_path.split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)


async def test_open_download_resolves_targets_ranges_and_ownership(tmp_path: Path) -> None:
    database, clock = await _setup(tmp_path)
    user_id = await _create_user(database, "admin")
    other_id = await _create_user(database, "other", admin=False)
    file_content, file_id = await _seed_file(database, user_id, 1)
    dir_content, dir_file = await _seed_directory(
        database, user_id, 2, [("", "docs", "directory", 0), ("docs", "child.bin", "file", 4)]
    )
    pending_content, pending_file = await _seed_file(database, user_id, 3)
    async with SQLiteUnitOfWork(database) as uow:
        await uow.connection.execute(
            text("UPDATE content_objects SET deletion_state = 'pending_delete' WHERE id = :id"),
            {"id": pending_content},
        )
    root = tmp_path / "downloads"
    _write_store_file(root, "objects/file-1", b"0123456789")
    _write_store_file(root, "objects/directory-2/docs/child.bin", b"abcd")
    store = FakeStore(root)
    _, _, _, download = _make_services(database, clock, store)

    target = await download.open_download(user_id=user_id, file_id=file_id)
    assert (target.display_name, target.total_size, target.range_spec) == ("file-1", 10, None)
    assert target.physical_path == root / "objects" / "file-1"
    await download.release(target)

    target = await download.open_download(
        user_id=user_id, file_id=file_id, range_header="bytes=2-5"
    )
    assert target.range_spec is not None and (target.range_spec.start, target.range_spec.end) == (2, 5)
    await download.release(target)

    target = await download.open_download(
        user_id=user_id, file_id=file_id, range_header="bytes=0-"
    )
    assert target.range_spec is not None and target.range_spec.length == 10
    await download.release(target)

    target = await download.open_download(
        user_id=user_id, file_id=file_id, range_header="items=0-1"
    )
    assert target.range_spec is None
    await download.release(target)

    with pytest.raises(DomainError) as caught:
        await download.open_download(
            user_id=user_id, file_id=file_id, range_header="bytes=99-"
        )
    assert caught.value.code is ErrorCode.RANGE_NOT_SATISFIABLE

    entry_target = await download.open_download(
        user_id=user_id, file_id=dir_file, path="docs/child.bin"
    )
    assert (entry_target.display_name, entry_target.total_size) == ("child.bin", 4)
    assert entry_target.entry_relative_path == "docs/child.bin"
    await download.release(entry_target)

    for kwargs in (
        {"file_id": dir_file},
        {"file_id": file_id, "path": "nested/x"},
        {"file_id": dir_file, "path": "docs"},
        {"file_id": pending_file},
        {"file_id": 999},
    ):
        with pytest.raises(DomainError):
            await download.open_download(user_id=user_id, **kwargs)
    with pytest.raises(DomainError) as caught:
        await download.open_download(user_id=other_id, file_id=file_id)
    assert caught.value.code is ErrorCode.NOT_FOUND
    _, _, leases = _make_services(database, clock, store)[:3]
    assert leases.in_flight(file_content) == 0
    assert leases.in_flight(dir_content) == 0


async def test_iter_chunks_streams_exact_bytes_and_releases_on_completion(
    tmp_path: Path,
) -> None:
    database, clock = await _setup(tmp_path)
    user_id = await _create_user(database, "admin")
    payload = bytes(range(256)) * 64  # 16 KiB，chunk_size 8 字节时强制分多块。
    _, file_id = await _seed_file(database, user_id, 1)
    root = tmp_path / "downloads"
    _write_store_file(root, "objects/file-1", payload)
    _, _, leases, download = _make_services(database, clock, FakeStore(root))
    download.chunk_size = 8

    target = await download.open_download(user_id=user_id, file_id=file_id)
    assert target.range_spec is None
    body = b"".join([chunk async for chunk in download.iter_chunks(target)])
    assert body == payload
    assert target.released is True
    assert leases.in_flight(target.content_id) == 0

    ranged = await download.open_download(
        user_id=user_id, file_id=file_id, range_header="bytes=2-7"
    )
    assert ranged.range_spec is not None
    window = b"".join([chunk async for chunk in download.iter_chunks(ranged)])
    assert window == payload[2:8]

    early = await download.open_download(user_id=user_id, file_id=file_id)
    stream = download.iter_chunks(early)
    async for _chunk in stream:
        pass  # 消费一个块后提前断开。
        break
    await stream.aclose()  # 断连等价于关闭生成器：finally 必须释放租约。
    assert early.released is True
    assert leases.in_flight(early.content_id) == 0
    await download.release(early)  # 幂等：不重复释放。
    async with SQLiteUnitOfWork(database) as uow:
        assert (await ContentRepository(uow.connection).get_object(early.content_id)).read_leases == 0


async def test_iter_chunks_releases_lease_when_read_fails(tmp_path: Path) -> None:
    database, clock = await _setup(tmp_path)
    user_id = await _create_user(database, "admin")
    _, file_id = await _seed_file(database, user_id, 1)
    failing = FailingStore(tmp_path / "downloads")
    _, _, leases, download = _make_services(database, clock, failing)
    target = await download.open_download(user_id=user_id, file_id=file_id)
    with pytest.raises(OSError):
        async for _chunk in download.iter_chunks(target):
            pass
    assert target.released is True
    assert leases.in_flight(target.content_id) == 0


async def test_iter_chunks_releases_lease_when_open_fails(tmp_path: Path) -> None:
    """物理文件缺失（ACTIVE 内容）：打开句柄失败也必须释放租约，否则删除被永久阻塞。"""
    database, clock = await _setup(tmp_path)
    user_id = await _create_user(database, "admin")
    content_id, file_id = await _seed_file(database, user_id, 1)  # 不写物理文件。
    _, _, leases, download = _make_services(database, clock, FakeStore(tmp_path / "downloads"))
    target = await download.open_download(user_id=user_id, file_id=file_id)
    assert leases.in_flight(content_id) == 1
    async with SQLiteUnitOfWork(database) as uow:
        assert (await ContentRepository(uow.connection).get_object(content_id)).read_leases == 1
    with pytest.raises(OSError):
        async for _chunk in download.iter_chunks(target):
            pass
    assert target.released is True
    assert leases.in_flight(content_id) == 0
    async with SQLiteUnitOfWork(database) as uow:
        assert (await ContentRepository(uow.connection).get_object(content_id)).read_leases == 0


async def test_deletion_worker_executes_full_chain_from_pending(tmp_path: Path) -> None:
    database, clock = await _setup(tmp_path)
    user_id = await _create_user(database, "admin")
    content_id, file_id = await _seed_file(database, user_id, 1)
    root = tmp_path / "downloads"
    _write_store_file(root, "objects/file-1", b"payload")
    store = FakeStore(root)
    worker = ContentDeletionWorker(
        uow_factory=lambda: SQLiteUnitOfWork(database),
        repository_factory=lambda uow: ContentRepository(uow.connection),
        content_store=store,
        clock_ms=clock,
        owner=DELETE_LEASE_OWNER,
        lease_ttl_ms=DELETE_LEASE_TTL_MS,
    )
    _, commands, _, _ = _make_services(database, clock, store)
    await commands.delete_user_file(user_id=user_id, file_id=file_id)
    await worker.step_once()
    assert store.deleted == ["objects/file-1"]
    assert not (root / "objects" / "file-1").exists()
    async with SQLiteUnitOfWork(database) as uow:
        with pytest.raises(DomainError) as caught:
            await ContentRepository(uow.connection).get_object(content_id)
    assert caught.value.code is ErrorCode.NOT_FOUND
    await worker.step_once()  # 收敛后无候选，重复执行无副作用。


async def test_deletion_worker_resumes_tombstoned_and_skips_busy(tmp_path: Path) -> None:
    database, clock = await _setup(tmp_path)
    user_id = await _create_user(database, "admin")
    tombstoned_content, tombstoned_file = await _seed_file(database, user_id, 1)
    leased_content, leased_file = await _seed_file(database, user_id, 2)
    referenced_content, referenced_file = await _seed_file(database, user_id, 3)
    root = tmp_path / "downloads"
    _write_store_file(root, "objects/file-1", b"gone")
    _write_store_file(root, "objects/file-2", b"busy")
    _write_store_file(root, "objects/file-3", b"kept")
    store = FakeStore(root)
    _, commands, _, _ = _make_services(database, clock, store)
    # 读租约必须在认领前建立（pending 内容拒绝新读取）。
    async with SQLiteUnitOfWork(database) as uow:
        await ContentRepository(uow.connection).acquire_read_lease(leased_content)
    await commands.delete_user_file(user_id=user_id, file_id=tombstoned_file)
    await commands.delete_user_file(user_id=user_id, file_id=leased_file)
    await commands.delete_user_file(user_id=user_id, file_id=referenced_file)
    async with SQLiteUnitOfWork(database) as uow:
        await ContentRepository(uow.connection).write_tombstone(
            tombstoned_content, owner=DELETE_LEASE_OWNER, now_ms=clock()
        )
        # 认领后出现新引用（竞态残留）：安全保留。
        await uow.connection.execute(
            text(
                "INSERT INTO user_files (user_id, content_id, display_name, created_at_ms) "
                "VALUES (:user_id, :content_id, 'ghost', 1)"
            ),
            {"user_id": user_id, "content_id": referenced_content},
        )
    worker = ContentDeletionWorker(
        uow_factory=lambda: SQLiteUnitOfWork(database),
        repository_factory=lambda uow: ContentRepository(uow.connection),
        content_store=store,
        clock_ms=clock,
        owner=DELETE_LEASE_OWNER,
        lease_ttl_ms=DELETE_LEASE_TTL_MS,
    )
    await worker.step_once()
    assert store.deleted == ["objects/file-1"]
    async with SQLiteUnitOfWork(database) as uow:
        repository = ContentRepository(uow.connection)
        assert (await repository.get_object(leased_content)).deletion_state is ContentDeletionState.PENDING_DELETE
        assert (await repository.get_object(referenced_content)).deletion_state is ContentDeletionState.PENDING_DELETE
    assert (root / "objects" / "file-2").exists()
    assert (root / "objects" / "file-3").exists()


async def test_deletion_worker_backoff_sequence_and_reset(tmp_path: Path) -> None:
    database, clock = await _setup(tmp_path)
    user_id = await _create_user(database, "admin")
    content_id, file_id = await _seed_file(database, user_id, 1)
    store = FakeStore(tmp_path / "downloads")
    store.fail_deletes = True
    _, commands, _, _ = _make_services(database, clock, store)
    await commands.delete_user_file(user_id=user_id, file_id=file_id)
    worker = ContentDeletionWorker(
        uow_factory=lambda: SQLiteUnitOfWork(database),
        repository_factory=lambda uow: ContentRepository(uow.connection),
        content_store=store,
        clock_ms=clock,
        owner=DELETE_LEASE_OWNER,
        lease_ttl_ms=DELETE_LEASE_TTL_MS,
        backoff_seconds=(1, 5, 30, 120, 600),
    )

    expected_backoff = [1, 5, 30, 120, 600, 600]
    for index, backoff in enumerate(expected_backoff):
        await worker.step_once()
        assert len(store.deleted) == index + 1
        assert worker.failure_counts[content_id] == index + 1
        assert worker.next_attempt_ms[content_id] == clock() + backoff * 1000
        await worker.step_once()  # 退避期内跳过。
        assert len(store.deleted) == index + 1
        clock.advance(backoff * 1000)

    store.fail_deletes = False
    clock.advance(600 * 1000)
    await worker.step_once()
    assert store.deleted[-1] == "objects/file-1"
    assert content_id not in worker.failure_counts
    async with SQLiteUnitOfWork(database) as uow:
        with pytest.raises(DomainError):
            await ContentRepository(uow.connection).get_object(content_id)


async def test_deletion_worker_skips_when_lease_owned_elsewhere(tmp_path: Path) -> None:
    database, clock = await _setup(tmp_path)
    user_id = await _create_user(database, "admin")
    content_id, file_id = await _seed_file(database, user_id, 1)
    store = FakeStore(tmp_path / "downloads")
    _, commands, _, _ = _make_services(database, clock, store)
    await commands.delete_user_file(user_id=user_id, file_id=file_id)
    clock.advance(DELETE_LEASE_TTL_MS + 1)  # 本方租约到期，他方才可抢占。
    async with SQLiteUnitOfWork(database) as uow:
        assert await ContentRepository(uow.connection).acquire_or_reclaim(
            content_id, now_ms=clock(), lease_owner="other-worker", lease_ttl_ms=600_000
        )
    worker = ContentDeletionWorker(
        uow_factory=lambda: SQLiteUnitOfWork(database),
        repository_factory=lambda uow: ContentRepository(uow.connection),
        content_store=store,
        clock_ms=clock,
        owner=DELETE_LEASE_OWNER,
        lease_ttl_ms=DELETE_LEASE_TTL_MS,
    )
    await worker.step_once()
    assert store.deleted == []
    assert worker.failure_counts == {}


async def test_deletion_worker_loop_stops_on_cancel(tmp_path: Path) -> None:
    database, clock = await _setup(tmp_path)
    worker = ContentDeletionWorker(
        uow_factory=lambda: SQLiteUnitOfWork(database),
        repository_factory=lambda uow: ContentRepository(uow.connection),
        content_store=FakeStore(tmp_path),
        clock_ms=clock,
    )

    def immediate_cancel(seconds: float) -> object:
        raise asyncio.CancelledError

    worker.sleep = immediate_cancel  # type: ignore[assignment]
    with pytest.raises(asyncio.CancelledError):
        await worker.run_worker()


async def test_delete_user_file_tolerates_unclaimable_content(tmp_path: Path) -> None:
    # 崩溃残留：内容已进入 pending_delete 但引用行仍在（写入方被中断）。
    # 删除流程仍应完成引用清理，认领失败只记录日志、不向用户报错。
    database, clock = await _setup(tmp_path)
    user_id = await _create_user(database, "admin")
    content_id, file_id = await _seed_file(database, user_id, 1)
    async with SQLiteUnitOfWork(database) as uow:
        await uow.connection.execute(
            text("UPDATE content_objects SET deletion_state = 'pending_delete' WHERE id = :id"),
            {"id": content_id},
        )
        await QuotaRepository(uow.connection).rebuild(user_id)
    _, commands, _, _ = _make_services(database, clock, FakeStore(tmp_path))
    await commands.delete_user_file(user_id=user_id, file_id=file_id)
    async with SQLiteUnitOfWork(database) as uow:
        repository = ContentRepository(uow.connection)
        record = await repository.get_object(content_id)
        assert await repository.count_references(content_id) == 0
        assert await QuotaRepository(uow.connection).get(user_id) == Quota(1000, 0, 0)
    assert record.deletion_state is ContentDeletionState.PENDING_DELETE


async def test_deletion_worker_skips_when_reference_reappears_between_steps(
    tmp_path: Path,
) -> None:
    # list 与 process 之间存在真实竞态窗口：新引用出现时 acquire 拒绝，安全跳过。
    database, clock = await _setup(tmp_path)
    user_id = await _create_user(database, "admin")
    content_id, file_id = await _seed_file(database, user_id, 1)
    store = FakeStore(tmp_path / "downloads")
    _, commands, _, _ = _make_services(database, clock, store)
    await commands.delete_user_file(user_id=user_id, file_id=file_id)
    async with SQLiteUnitOfWork(database) as uow:
        candidates = await ContentRepository(uow.connection).list_deletion_candidates(
            owner=DELETE_LEASE_OWNER, now_ms=clock()
        )
        await uow.connection.execute(
            text(
                "INSERT INTO user_files (user_id, content_id, display_name, created_at_ms) "
                "VALUES (:user_id, :content_id, 'ghost', 1)"
            ),
            {"user_id": user_id, "content_id": content_id},
        )
    worker = ContentDeletionWorker(
        uow_factory=lambda: SQLiteUnitOfWork(database),
        repository_factory=lambda uow: ContentRepository(uow.connection),
        content_store=store,
        clock_ms=clock,
        owner=DELETE_LEASE_OWNER,
        lease_ttl_ms=DELETE_LEASE_TTL_MS,
    )
    await worker._process(candidates[0])
    assert store.deleted == []
    async with SQLiteUnitOfWork(database) as uow:
        record = await ContentRepository(uow.connection).get_object(content_id)
    assert record.deletion_state is ContentDeletionState.PENDING_DELETE


async def test_iter_chunks_stops_at_physical_short_read(tmp_path: Path) -> None:
    # 记录大小与物理文件不一致（截断/篡改）：区间读取提前命中 EOF，按已读字节返回。
    database, clock = await _setup(tmp_path)
    user_id = await _create_user(database, "admin")
    _, file_id = await _seed_file(database, user_id, 1)  # 记录大小 10。
    root = tmp_path / "downloads"
    _write_store_file(root, "objects/file-1", b"abcd")  # 物理只有 4 字节。
    _, _, leases, download = _make_services(database, clock, FakeStore(root))
    target = await download.open_download(
        user_id=user_id, file_id=file_id, range_header="bytes=0-9"
    )
    assert target.range_spec is not None
    body = b"".join([chunk async for chunk in download.iter_chunks(target)])
    assert body == b"abcd"
    assert target.released is True
    assert leases.in_flight(target.content_id) == 0


async def test_browse_empty_directory_returns_no_items_with_real_total(
    tmp_path: Path,
) -> None:
    database, clock = await _setup(tmp_path)
    user_id = await _create_user(database, "admin")
    _, dir_file = await _seed_directory(database, user_id, 1, [])
    queries, _, _, _ = _make_services(database, clock, FakeStore(tmp_path))
    entries, breadcrumbs, total = await queries.browse_directory(
        user_id=user_id, file_id=dir_file, path="", page=1, page_size=10
    )
    assert entries == []
    assert total == 0
    assert [(crumb.name, crumb.path) for crumb in breadcrumbs] == [("dir-1", "")]


async def test_content_key_is_released_after_finalize(tmp_path: Path) -> None:
    """删除收敛后同内容可秒传重建：同 digest/kind 再次入库得到全新 content_id。"""
    database, clock = await _setup(tmp_path)
    user_id = await _create_user(database, "admin")
    content_id, file_id = await _seed_file(database, user_id, 1, payload=b"again")
    root = tmp_path / "downloads"
    _write_store_file(root, "objects/file-1", b"again")
    store = FakeStore(root)
    _, commands, _, _ = _make_services(database, clock, store)
    async with SQLiteUnitOfWork(database) as uow:
        repository = ContentRepository(uow.connection)
        with pytest.raises(DomainError) as caught:  # 删除前：同 content_key 仍被占用。
            await repository.create_object(
                kind="file", digest=_digest(1), size=5, storage_path="objects/dupe", now_ms=2
            )
        assert caught.value.code is ErrorCode.DUPLICATE_RESOURCE

    await commands.delete_user_file(user_id=user_id, file_id=file_id)
    worker = ContentDeletionWorker(
        uow_factory=lambda: SQLiteUnitOfWork(database),
        repository_factory=lambda uow: ContentRepository(uow.connection),
        content_store=store,
        clock_ms=clock,
        owner=DELETE_LEASE_OWNER,
        lease_ttl_ms=DELETE_LEASE_TTL_MS,
    )
    await worker.step_once()
    assert store.deleted == ["objects/file-1"]

    async with SQLiteUnitOfWork(database) as uow:
        repository = ContentRepository(uow.connection)
        recreated = await repository.create_object(
            kind="file", digest=_digest(1), size=5, storage_path="objects/file-1", now_ms=3
        )
        new_file = await repository.add_user_file(
            user_id=user_id, content_id=recreated, display_name="again.bin", now_ms=3
        )
        record = await repository.get_object(recreated)
    assert new_file > 0
    assert record.content_key == f"file:{_digest(1)}"
