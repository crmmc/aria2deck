from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from aria2deck.application.library.commands import LibraryCommandService
from aria2deck.application.library.disk_scan import (
    DiskAdmissionQueue,
    DiskOrphanService,
    QueueEntry,
    QueueState,
)
from aria2deck.domain.content import (
    ContentEntryIdentity,
    ContentKind,
    directory_content_digest,
)
from aria2deck.domain.errors import ErrorCode
from aria2deck.infrastructure.content_store import DownloadContentStore
from aria2deck.infrastructure.sqlite.bootstrap import initialize_database
from aria2deck.infrastructure.sqlite.engine import SQLiteDatabase
from aria2deck.infrastructure.sqlite.repositories.content import (
    DELETE_LEASE_OWNER,
    DELETE_LEASE_TTL_MS,
    ContentRepository,
)
from aria2deck.infrastructure.sqlite.repositories.quota import QuotaRepository
from aria2deck.infrastructure.sqlite.unit_of_work import SQLiteUnitOfWork


class FakeClock:
    def __init__(self, now_ms: int = 1_000) -> None:
        self.now_ms = now_ms

    def __call__(self) -> int:
        return self.now_ms


class Harness:
    def __init__(self, database: SQLiteDatabase, downloads: Path) -> None:
        self.database = database
        self.downloads = downloads
        self.store = DownloadContentStore(downloads)
        self.queue = DiskAdmissionQueue()
        self.clock = FakeClock()
        self.library_commands = LibraryCommandService(
            uow_factory=lambda: SQLiteUnitOfWork(self.database),
            repository_factory=lambda uow: ContentRepository(uow.connection),
            quota_repository_factory=lambda uow: QuotaRepository(uow.connection),
            clock_ms=self.clock,
            lease_owner=DELETE_LEASE_OWNER,
            lease_ttl_ms=DELETE_LEASE_TTL_MS,
        )

    def service(self) -> DiskOrphanService:
        return DiskOrphanService(
            store=self.store,
            uow_factory=lambda: SQLiteUnitOfWork(self.database),
            repository_factory=lambda uow: ContentRepository(uow.connection),
            queue=self.queue,
            library_commands=self.library_commands,
            clock_ms=self.clock,
        )

    def write_file(self, payload: bytes) -> str:
        digest = hashlib.sha256(payload).hexdigest()
        target = self.downloads / "objects" / f"file-{digest}"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        return f"objects/file-{digest}"

    def write_directory(self, payloads: dict[str, bytes]) -> str:
        manifest = [
            ContentEntryIdentity(
                name, ContentKind.FILE, len(payload), hashlib.sha256(payload).hexdigest()
            )
            for name, payload in payloads.items()
        ]
        storage_path = f"objects/directory-{directory_content_digest(manifest)}"
        for name, payload in payloads.items():
            target = self.downloads / storage_path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
        return storage_path

    def enqueue_entry(self, storage_path: str, *, size: int = 1) -> None:
        self.queue.enqueue(
            [
                QueueEntry(
                    storage_path=storage_path,
                    kind=ContentKind.FILE,
                    digest=storage_path.rpartition("-")[2],
                    size=size,
                    enqueued_at_ms=self.clock(),
                )
            ]
        )


@pytest.fixture
async def harness(tmp_path: Path):
    database = SQLiteDatabase(tmp_path / "v2.db")
    await initialize_database(database)
    yield Harness(database, tmp_path / "downloads")
    await database.close()


async def test_scan_subtracts_registered_paths_and_sorts_by_size_then_path(
    harness: Harness,
) -> None:
    small = harness.write_file(b"1")
    large = harness.write_file(b"x" * 10)
    same_size_a = harness.write_file(b"aa")
    same_size_b = harness.write_file(b"bb")
    registered = harness.write_file(b"registered")
    async with SQLiteUnitOfWork(harness.database) as uow:
        await ContentRepository(uow.connection).create_object(
            kind="file",
            digest=registered.rpartition("-")[2],
            size=10,
            storage_path=registered,
            now_ms=1,
        )

    page = await harness.service().scan(search=None, page=1, page_size=10)

    assert [item.storage_path for item in page.items] == [
        large,
        min(same_size_a, same_size_b),
        max(same_size_a, same_size_b),
        small,
    ]
    assert (page.total, page.page, page.page_size, page.unrecognized_count) == (4, 1, 10, 0)


async def test_scan_filters_by_path_keyword_and_paginates(harness: Harness) -> None:
    first = harness.write_file(b"a" * 10)
    second = harness.write_file(b"b" * 10)
    third = harness.write_file(b"c" * 5)
    service = harness.service()

    filtered = await service.scan(search="FILE-", page=1, page_size=10)
    assert filtered.total == 3, "过滤不区分大小写"
    assert [item.storage_path for item in filtered.items] == sorted([first, second]) + [third]
    unmatched = await service.scan(search="nope", page=1, page_size=10)
    assert (unmatched.items, unmatched.total) == ((), 0)
    assert (await service.scan(search="   ", page=1, page_size=10)).total == 3

    page_two = await service.scan(search=None, page=2, page_size=2)
    assert (page_two.total, len(page_two.items)) == (3, 1)
    page_three = await service.scan(search=None, page=3, page_size=2)
    assert page_three.items == ()


async def test_scan_reports_unrecognized_entries_and_handles_missing_objects(
    harness: Harness,
) -> None:
    assert (await harness.service().scan(search=None, page=1, page_size=10)).total == 0
    harness.write_file(b"canonical")
    objects = harness.downloads / "objects"
    (objects / "random.txt").write_bytes(b"x")
    (objects / f"file-{'d' * 64}").symlink_to(harness.downloads / "outside")

    page = await harness.service().scan(search=None, page=1, page_size=10)

    assert (page.total, page.unrecognized_count) == (1, 2)


async def test_enqueue_reports_per_item_results_and_is_idempotent(harness: Harness) -> None:
    valid = harness.write_file(b"payload")
    service = harness.service()

    outcomes = await service.enqueue(["../etc/passwd", valid, f"objects/file-{'e' * 64}"])

    assert [(item.storage_path, item.accepted) for item in outcomes] == [
        ("../etc/passwd", False),
        (valid, True),
        (f"objects/file-{'e' * 64}", False),
    ]
    assert outcomes[0].error_code == ErrorCode.INVALID_INPUT.value
    assert outcomes[2].error_code == ErrorCode.NOT_FOUND.value
    assert outcomes[1].state is QueueState.QUEUED
    entry = harness.queue.get(valid)
    assert entry is not None
    assert (entry.kind, entry.size, entry.enqueued_at_ms) == (ContentKind.FILE, 7, harness.clock())

    again = await service.enqueue([valid])
    assert [(item.accepted, item.state) for item in again] == [(True, QueueState.QUEUED)]


async def test_delete_removes_an_unregistered_entry_and_reports_freed_bytes(
    harness: Harness,
) -> None:
    payload = b"x" * 20
    storage_path = harness.write_file(payload)
    harness.enqueue_entry(storage_path, size=len(payload))

    outcomes = await harness.service().delete([storage_path])

    assert [(item.accepted, item.mode, item.freed_bytes) for item in outcomes] == [
        (True, "immediate", 20)
    ]
    assert not (harness.downloads / storage_path).exists()
    assert harness.queue.get(storage_path) is None


async def test_delete_removes_an_entry_that_was_never_enqueued(harness: Harness) -> None:
    storage_path = harness.write_file(b"abc")

    outcomes = await harness.service().delete([storage_path])

    assert [(item.accepted, item.mode, item.freed_bytes) for item in outcomes] == [
        (True, "immediate", 3)
    ]
    assert not (harness.downloads / storage_path).exists()


async def test_delete_removes_a_failed_entry(harness: Harness) -> None:
    storage_path = harness.write_file(b"abc")
    harness.enqueue_entry(storage_path, size=3)
    claimed = harness.queue.claim_next(now_ms=1)
    assert claimed is not None
    harness.queue.mark_failed(
        storage_path, code=ErrorCode.INVALID_STATE.value, message="摘要不一致", now_ms=2
    )

    outcomes = await harness.service().delete([storage_path])

    assert [(item.accepted, item.mode) for item in outcomes] == [(True, "immediate")]
    assert harness.queue.get(storage_path) is None


async def test_delete_rejects_a_running_entry(harness: Harness) -> None:
    storage_path = harness.write_file(b"abc")
    harness.enqueue_entry(storage_path, size=3)
    harness.queue.start()
    assert harness.queue.claim_next(now_ms=1) is not None

    outcomes = await harness.service().delete([storage_path])

    assert [(item.accepted, item.error_code) for item in outcomes] == [
        (False, ErrorCode.CONTENT_BUSY.value)
    ]
    assert outcomes[0].message == "正在校验中，请先停止处理"
    assert (harness.downloads / storage_path).exists(), "处理中不得删字节"
    assert harness.queue.get(storage_path) is not None


async def test_delete_hands_an_admitted_entry_to_the_async_deletion_chain(
    harness: Harness,
) -> None:
    storage_path = harness.write_file(b"abc")
    content_id = await _admit(harness, storage_path)
    harness.enqueue_entry(storage_path, size=3)
    claimed = harness.queue.claim_next(now_ms=1)
    assert claimed is not None
    harness.queue.mark_done(storage_path, content_id=content_id, now_ms=2)

    outcomes = await harness.service().delete([storage_path])

    assert [(item.accepted, item.mode, item.freed_bytes) for item in outcomes] == [
        (True, "deferred", 0)
    ]
    assert harness.queue.get(storage_path) is None
    assert (harness.downloads / storage_path).exists(), "物理删除交给既有后台任务"
    async with SQLiteUnitOfWork(harness.database) as uow:
        record = await ContentRepository(uow.connection).get_object(content_id)
    assert record.deletion_state.value == "pending_delete"


async def test_delete_reports_a_rejection_from_the_async_chain_without_touching_disk(
    harness: Harness,
) -> None:
    storage_path = harness.write_file(b"abc")
    content_id = await _admit(harness, storage_path)
    harness.enqueue_entry(storage_path, size=3)
    claimed = harness.queue.claim_next(now_ms=1)
    assert claimed is not None
    harness.queue.mark_done(storage_path, content_id=content_id, now_ms=2)
    async with SQLiteUnitOfWork(harness.database) as uow:
        await uow.connection.exec_driver_sql(
            "UPDATE content_objects SET deletion_state = 'pending_delete' WHERE id = ?",
            (content_id,),
        )

    outcomes = await harness.service().delete([storage_path])

    assert [(item.accepted, item.error_code) for item in outcomes] == [
        (False, ErrorCode.CONTENT_PENDING_DELETE.value)
    ]
    assert (harness.downloads / storage_path).exists()
    assert harness.queue.get(storage_path) is not None


async def test_delete_falls_back_to_disk_when_the_admitted_row_is_gone(harness: Harness) -> None:
    storage_path = harness.write_file(b"abc")
    content_id = await _admit(harness, storage_path)
    harness.enqueue_entry(storage_path, size=3)
    claimed = harness.queue.claim_next(now_ms=1)
    assert claimed is not None
    harness.queue.mark_done(storage_path, content_id=content_id, now_ms=2)
    async with SQLiteUnitOfWork(harness.database) as uow:
        await uow.connection.exec_driver_sql(
            "DELETE FROM content_objects WHERE id = ?", (content_id,)
        )

    outcomes = await harness.service().delete([storage_path])

    assert [(item.accepted, item.mode, item.freed_bytes) for item in outcomes] == [
        (True, "immediate", 3)
    ]
    assert not (harness.downloads / storage_path).exists()
    assert harness.queue.get(storage_path) is None


async def test_delete_rejects_a_path_registered_by_someone_else(harness: Harness) -> None:
    storage_path = harness.write_file(b"abc")
    async with SQLiteUnitOfWork(harness.database) as uow:
        await ContentRepository(uow.connection).create_object(
            kind="file",
            digest=storage_path.rpartition("-")[2],
            size=3,
            storage_path=storage_path,
            now_ms=1,
        )

    outcomes = await harness.service().delete([storage_path])

    assert [(item.accepted, item.error_code) for item in outcomes] == [
        (False, ErrorCode.CONTENT_BUSY.value)
    ]
    assert outcomes[0].message == "该路径已登记入库，请在存储管理页删除"
    assert (harness.downloads / storage_path).read_bytes() == b"abc", "一个字节都不能少"


@pytest.mark.parametrize(
    "storage_path",
    [
        "../etc/passwd",
        "/etc/passwd",
        "objects/file-" + "a" * 63,
        "objects/../objects/file-" + "a" * 64,
        "objects\\\\file-" + "a" * 64,
        "objects/file-" + "a" * 64 + "\x00",
    ],
)
async def test_delete_rejects_paths_outside_the_canonical_layout(
    harness: Harness, storage_path: str
) -> None:
    before = sorted(path.name for path in (harness.downloads).glob("**/*"))

    outcomes = await harness.service().delete([storage_path])

    assert [(item.accepted, item.error_code) for item in outcomes] == [
        (False, ErrorCode.INVALID_INPUT.value)
    ]
    assert outcomes[0].message == "存储路径格式无效"
    assert sorted(path.name for path in harness.downloads.glob("**/*")) == before


async def test_delete_reports_a_missing_entry_and_drops_the_stale_queue_record(
    harness: Harness,
) -> None:
    storage_path = harness.write_file(b"abc")
    harness.enqueue_entry(storage_path, size=3)
    (harness.downloads / storage_path).unlink()

    outcomes = await harness.service().delete([storage_path])

    assert [(item.accepted, item.error_code) for item in outcomes] == [
        (False, ErrorCode.NOT_FOUND.value)
    ]
    assert harness.queue.get(storage_path) is None


async def test_delete_refuses_a_symlink_without_following_it(harness: Harness) -> None:
    harness.downloads.mkdir(parents=True, exist_ok=True)
    outside = harness.downloads / "outside.bin"
    outside.write_bytes(b"do not touch")
    storage_path = f"objects/file-{'f' * 64}"
    (harness.downloads / "objects").mkdir(parents=True, exist_ok=True)
    (harness.downloads / storage_path).symlink_to(outside)

    outcomes = await harness.service().delete([storage_path])

    assert [(item.accepted, item.error_code) for item in outcomes] == [
        (False, ErrorCode.INVALID_INPUT.value)
    ]
    assert (harness.downloads / storage_path).is_symlink()
    assert outside.read_bytes() == b"do not touch"


async def test_start_stop_and_snapshot_delegate_to_the_queue(harness: Harness) -> None:
    service = harness.service()
    storage_path = harness.write_file(b"abc")
    harness.enqueue_entry(storage_path, size=3)

    started = service.start()
    assert started.running is True
    assert (started.queued_count, started.pending_bytes) == (1, 3)
    assert service.snapshot().running is True

    assert service.stop().running is False
    assert service.snapshot().running is False


async def _admit(harness: Harness, storage_path: str) -> int:
    async with SQLiteUnitOfWork(harness.database) as uow:
        return await ContentRepository(uow.connection).create_object(
            kind="file",
            digest=storage_path.rpartition("-")[2],
            size=3,
            storage_path=storage_path,
            now_ms=1,
        )
