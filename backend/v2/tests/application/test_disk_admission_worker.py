from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from aria2deck.application.library.admission_worker import DiskAdmissionWorker
from aria2deck.application.library.disk_scan import (
    DiskAdmissionQueue,
    ProgressCallback,
    QueueEntry,
    QueueState,
)
from aria2deck.domain.content import ContentEntryIdentity, ContentKind, directory_content_digest
from aria2deck.domain.errors import ErrorCode
from aria2deck.infrastructure.content_store import DownloadContentStore
from aria2deck.infrastructure.sqlite.bootstrap import initialize_database
from aria2deck.infrastructure.sqlite.engine import SQLiteDatabase
from aria2deck.infrastructure.sqlite.repositories.content import ContentRepository
from aria2deck.infrastructure.sqlite.unit_of_work import SQLiteUnitOfWork

MIB = 1024 * 1024


class FakeClock:
    def __init__(self, now_ms: int = 1_000) -> None:
        self.now_ms = now_ms

    def __call__(self) -> int:
        return self.now_ms


class Harness:
    """真实 SQLite + 真实磁盘 store，只替换可控的时序边界。"""

    def __init__(self, database: SQLiteDatabase, downloads: Path) -> None:
        self.database = database
        self.downloads = downloads
        self.store = DownloadContentStore(downloads)
        self.queue = DiskAdmissionQueue()
        self.clock = FakeClock()

    def worker(self, *, store: Any = None, sleep: Any = None) -> DiskAdmissionWorker:
        kwargs: dict[str, Any] = {}
        if sleep is not None:
            kwargs["sleep"] = sleep
        return DiskAdmissionWorker(
            queue=self.queue,
            store=store or self.store,
            uow_factory=lambda: SQLiteUnitOfWork(self.database),
            repository_factory=lambda uow: ContentRepository(uow.connection),
            clock_ms=self.clock,
            **kwargs,
        )

    def write_file(self, payload: bytes) -> str:
        digest = hashlib.sha256(payload).hexdigest()
        target = self.downloads / "objects" / f"file-{digest}"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        return f"objects/file-{digest}"

    def write_directory(self, files: dict[str, bytes]) -> str:
        entries: list[ContentEntryIdentity] = []
        for name, payload in files.items():
            parent = name.rpartition("/")[0]
            if parent and all(entry.path != parent for entry in entries):
                entries.append(ContentEntryIdentity(parent, ContentKind.DIRECTORY, 0))
            entries.append(
                ContentEntryIdentity(
                    name, ContentKind.FILE, len(payload), hashlib.sha256(payload).hexdigest()
                )
            )
        storage_path = f"objects/directory-{directory_content_digest(entries)}"
        for name, payload in files.items():
            target = self.downloads / storage_path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
        return storage_path

    async def enqueue(self, storage_path: str) -> None:
        measured = await self.store.stat_canonical(storage_path)
        self.queue.enqueue(
            [
                QueueEntry(
                    storage_path=measured.storage_path,
                    kind=measured.kind,
                    digest=measured.digest,
                    size=measured.size,
                    enqueued_at_ms=self.clock(),
                )
            ]
        )

    async def content_rows(self) -> list[Any]:
        async with SQLiteUnitOfWork(self.database) as uow:
            rows = await uow.connection.exec_driver_sql(
                "SELECT id, kind, digest, size, storage_path FROM content_objects ORDER BY id"
            )
            return list(rows)


@pytest.fixture
async def harness(tmp_path: Path):
    database = SQLiteDatabase(tmp_path / "v2.db")
    await initialize_database(database)
    yield Harness(database, tmp_path / "downloads")
    await database.close()


async def test_step_once_returns_false_before_start_and_without_entries(harness: Harness) -> None:
    worker = harness.worker()
    assert await worker.step_once() is False

    harness.queue.start()
    assert await worker.step_once() is False


async def test_step_once_admits_a_file_object(harness: Harness) -> None:
    payload = b"a" * (9 * MIB)
    storage_path = harness.write_file(payload)
    await harness.enqueue(storage_path)
    harness.queue.start()

    assert await harness.worker().step_once() is True

    item = harness.queue.snapshot().items[0]
    assert (item.state, item.error_code) == (QueueState.DONE, None)
    assert item.content_id is not None and item.content_id > 0
    assert item.processed_bytes == 9 * MIB, "进度按块累计，入库时停在最后一次回调"
    rows = await harness.content_rows()
    assert [(row.kind, row.size, row.storage_path) for row in rows] == [
        ("file", len(payload), storage_path)
    ]
    assert (harness.downloads / storage_path).exists(), "入库不搬动磁盘上的对象"


async def test_step_once_admits_a_directory_object_with_entries(harness: Harness) -> None:
    storage_path = harness.write_directory({"a.bin": b"123", "sub/b.bin": b"45"})
    await harness.enqueue(storage_path)
    harness.queue.start()

    assert await harness.worker().step_once() is True

    item = harness.queue.snapshot().items[0]
    assert (item.state, item.kind) == (QueueState.DONE, ContentKind.DIRECTORY)
    assert item.content_id is not None
    async with SQLiteUnitOfWork(harness.database) as uow:
        repository = ContentRepository(uow.connection)
        files = await repository.list_files(item.content_id)
        root_entries, _total = await repository.list_entries(
            content_id=item.content_id, parent_path="", offset=0, limit=10
        )
    assert [(entry.path, entry.entry_kind.value, entry.size) for entry in files] == [
        ("a.bin", "file", 3),
        ("sub/b.bin", "file", 2),
    ]
    assert {(entry.path, entry.entry_kind.value) for entry in root_entries} == {
        ("a.bin", "file"),
        ("sub", "directory"),
    }, "目录条目也要重建"


async def test_step_once_fails_a_tampered_object_and_allows_retry(harness: Harness) -> None:
    payload = b"real content"
    storage_path = harness.write_file(payload)
    digest = storage_path.rpartition("-")[2]
    await harness.enqueue(storage_path)
    (harness.downloads / storage_path).write_bytes(b"tampered!!!")  # 同大小、不同内容
    harness.queue.start()
    worker = harness.worker()

    assert await worker.step_once() is True
    item = harness.queue.snapshot().items[0]
    assert (item.state, item.error_code) == (QueueState.FAILED, ErrorCode.INVALID_STATE.value)
    assert item.message == "内容与文件名摘要不一致"
    assert await harness.content_rows() == []

    (harness.downloads / storage_path).write_bytes(payload)
    await harness.enqueue(storage_path)
    assert await worker.step_once() is True
    assert harness.queue.snapshot().items[0].state is QueueState.DONE
    assert len(await harness.content_rows()) == 1
    assert digest


async def test_step_once_fails_when_the_path_was_registered_meanwhile(harness: Harness) -> None:
    storage_path = harness.write_file(b"payload")
    digest = storage_path.rpartition("-")[2]
    async with SQLiteUnitOfWork(harness.database) as uow:
        await ContentRepository(uow.connection).create_object(
            kind="file", digest=digest, size=7, storage_path=storage_path, now_ms=1
        )
    await harness.enqueue(storage_path)
    harness.queue.start()

    assert await harness.worker().step_once() is True

    item = harness.queue.snapshot().items[0]
    assert (item.state, item.error_code) == (QueueState.FAILED, ErrorCode.CONTENT_BUSY.value)
    assert item.message == "该路径已登记入库"
    assert len(await harness.content_rows()) == 1


async def test_step_once_fails_on_a_duplicate_digest_at_another_path(harness: Harness) -> None:
    payload = b"payload"
    storage_path = harness.write_file(payload)
    digest = hashlib.sha256(payload).hexdigest()
    async with SQLiteUnitOfWork(harness.database) as uow:
        await ContentRepository(uow.connection).create_object(
            kind="file", digest=digest, size=7, storage_path="objects/file-legacy", now_ms=1
        )
    await harness.enqueue(storage_path)
    harness.queue.start()

    assert await harness.worker().step_once() is True

    item = harness.queue.snapshot().items[0]
    assert (item.state, item.error_code) == (QueueState.FAILED, ErrorCode.DUPLICATE_RESOURCE.value)
    assert item.message == "内容身份或存储路径已存在"
    assert (harness.downloads / storage_path).exists(), "失败时不写库也不改磁盘"


async def test_step_once_fails_when_the_entry_vanished_before_processing(
    harness: Harness,
) -> None:
    storage_path = harness.write_file(b"payload")
    await harness.enqueue(storage_path)
    (harness.downloads / storage_path).unlink()
    harness.queue.start()

    assert await harness.worker().step_once() is True

    item = harness.queue.snapshot().items[0]
    assert (item.state, item.error_code) == (QueueState.FAILED, ErrorCode.NOT_FOUND.value)
    assert item.message == "条目不存在"


async def test_step_once_fails_when_the_entry_changed_during_verification(
    harness: Harness,
) -> None:
    storage_path = harness.write_file(b"payload")

    class GrowingStore:
        def __init__(self, store: DownloadContentStore) -> None:
            self.store = store

        async def verify_canonical(
            self, path: str, *, on_progress: ProgressCallback | None = None
        ) -> tuple[Any, list[ContentEntryIdentity]]:
            result = await self.store.verify_canonical(path, on_progress=on_progress)
            (harness.downloads / path).write_bytes(b"payload-plus")
            return result

        def __getattr__(self, name: str) -> Any:
            return getattr(self.store, name)

    await harness.enqueue(storage_path)
    harness.queue.start()

    assert await harness.worker(store=GrowingStore(harness.store)).step_once() is True

    item = harness.queue.snapshot().items[0]
    assert (item.state, item.error_code) == (QueueState.FAILED, ErrorCode.INVALID_STATE.value)
    assert item.message == "校验期间条目发生变化，请重新扫描"
    assert await harness.content_rows() == []


async def test_step_once_releases_the_entry_when_stop_arrives_mid_verification(
    harness: Harness,
) -> None:
    storage_path = harness.write_file(b"a" * (9 * MIB))

    class StoppingStore:
        def __init__(self, store: DownloadContentStore, queue: DiskAdmissionQueue) -> None:
            self.store = store
            self.queue = queue

        async def verify_canonical(
            self, path: str, *, on_progress: ProgressCallback | None = None
        ) -> tuple[Any, list[ContentEntryIdentity]]:
            def progress(processed_bytes: int) -> None:
                self.queue.stop()
                assert on_progress is not None
                on_progress(processed_bytes)

            return await self.store.verify_canonical(path, on_progress=progress)

        def __getattr__(self, name: str) -> Any:
            return getattr(self.store, name)

    await harness.enqueue(storage_path)
    harness.queue.start()

    assert await harness.worker(
        store=StoppingStore(harness.store, harness.queue)
    ).step_once() is True

    item = harness.queue.snapshot().items[0]
    assert (item.state, item.started_at_ms, item.processed_bytes) == (
        QueueState.QUEUED,
        None,
        0,
    )
    assert await harness.content_rows() == [], "停止不留半登记状态"


async def test_stop_interrupts_an_entry_smaller_than_one_progress_step(
    harness: Harness,
) -> None:
    """小于进度档位的条目也必须能停在半路，否则「停止」对它们等于没有。"""
    storage_path = harness.write_file(b"a" * 4096)
    await harness.enqueue(storage_path)
    harness.queue.start()

    class StoppingStore:
        def __init__(self, store: DownloadContentStore, queue: DiskAdmissionQueue) -> None:
            self.store = store
            self.queue = queue

        async def verify_canonical(
            self, path: str, *, on_progress: ProgressCallback | None = None
        ) -> tuple[Any, list[ContentEntryIdentity]]:
            def progress(processed_bytes: int) -> None:
                self.queue.stop()
                assert on_progress is not None
                on_progress(processed_bytes)

            return await self.store.verify_canonical(path, on_progress=progress)

        def __getattr__(self, name: str) -> Any:
            return getattr(self.store, name)

    assert await harness.worker(
        store=StoppingStore(harness.store, harness.queue)
    ).step_once() is True

    item = harness.queue.snapshot().items[0]
    assert item.state is QueueState.QUEUED, "小条目点停止后必须退回排队，不能照常入库"
    assert await harness.content_rows() == [], "停止不留半登记状态"


async def test_step_once_degrades_unexpected_errors_without_dying(harness: Harness) -> None:
    storage_path = harness.write_file(b"payload")
    await harness.enqueue(storage_path)
    harness.queue.start()

    class BrokenStore:
        async def verify_canonical(
            self, path: str, *, on_progress: ProgressCallback | None = None
        ) -> tuple[Any, list[ContentEntryIdentity]]:
            raise RuntimeError("boom")

    worker = harness.worker(store=BrokenStore())

    assert await worker.step_once() is True
    item = harness.queue.snapshot().items[0]
    assert (item.state, item.error_code) == (QueueState.FAILED, ErrorCode.INVALID_STATE.value)
    assert item.message == "处理失败，请重新加入队列重试"
    assert await worker.step_once() is False, "worker 不退出，下一轮没有排队项就返回 False"


async def test_run_worker_polls_with_the_configured_interval(harness: Harness) -> None:
    sleeps: list[float] = []

    class StopLoop(Exception):
        pass

    async def sleeper(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            raise StopLoop

    worker = harness.worker(sleep=sleeper)

    with pytest.raises(StopLoop):
        await worker.run_worker()

    assert sleeps == [1.0, 1.0]
