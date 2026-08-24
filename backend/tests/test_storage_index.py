from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from app.repositories import files as files_repo
from app.services import storage_index
from app.services.hash import calculate_content_hash
from app.services.storage_index import (
    StorageScanError,
    calculate_legacy_content_hash,
    scan_storage_path,
)


def test_scan_keeps_empty_directory_entry(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    root.mkdir()

    scan = scan_storage_path(root)

    assert scan.content_hash.startswith("v2:directory:")
    assert scan.content_digest is not None
    assert scan.size_bytes == 0
    assert scan.is_directory is True
    assert [entry["relative_path"] for entry in scan.entry_templates] == ["."]


def test_scan_reports_bytes_read_for_file_and_directory(tmp_path: Path) -> None:
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"payload")
    file_reads: list[int] = []

    scan_storage_path(payload, on_bytes_read=file_reads.append)

    assert sum(file_reads) == len(b"payload")

    root = tmp_path / "directory"
    root.mkdir()
    (root / "a.bin").write_bytes(b"alpha")
    (root / "b.bin").write_bytes(b"beta")
    directory_reads: list[int] = []
    legacy_reads: list[int] = []

    scan_storage_path(root, on_bytes_read=directory_reads.append)
    calculate_legacy_content_hash(root, on_bytes_read=legacy_reads.append)

    assert sum(directory_reads) == len(b"alpha") + len(b"beta")
    assert sum(legacy_reads) == len(b"alpha") + len(b"beta")


def test_scan_rejects_symlinks_and_entry_overflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "payload"
    root.mkdir()
    (root / "target").write_bytes(b"data")
    (root / "link").symlink_to(root / "target")

    with pytest.raises(StorageScanError, match="符号链接"):
        scan_storage_path(root)

    (root / "link").unlink()
    for name in ("a", "b", "c"):
        (root / name).write_bytes(b"x")
    monkeypatch.setattr(storage_index, "MAX_STORAGE_ENTRIES", 3)
    with pytest.raises(StorageScanError, match="条目过多"):
        scan_storage_path(root)


@pytest.mark.asyncio
async def test_scan_cancellation_stops_worker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"payload")
    started = threading.Event()
    cancel_event = threading.Event()

    def blocking_hash(path: Path, event: threading.Event | None) -> str:
        started.set()
        assert event is not None and event.wait(2)
        raise InterruptedError("storage scan cancelled")

    monkeypatch.setattr(storage_index, "_file_hash", blocking_hash)
    worker = asyncio.create_task(asyncio.to_thread(scan_storage_path, payload, cancel_event))
    assert await asyncio.to_thread(started.wait, 2)
    cancel_event.set()
    with pytest.raises(InterruptedError, match="storage scan cancelled"):
        await worker


@pytest.mark.asyncio
async def test_entry_templates_are_inserted_in_bounded_batches() -> None:
    calls: list[list[dict[str, object]]] = []

    class Connection:
        async def execute(self, statement: object, values: list[dict[str, object]]) -> None:
            calls.append(values)

    templates = [{"relative_path": str(index)} for index in range(501)]
    await files_repo._insert_entry_templates(Connection(), 7, templates)

    assert [len(batch) for batch in calls] == [250, 250, 1]
    assert all(row["stored_file_id"] == 7 for batch in calls for row in batch)

def test_scan_preserves_legacy_hash_for_regular_directory(tmp_path: Path) -> None:
    root = tmp_path / "payload"
    (root / "nested").mkdir(parents=True)
    (root / "a.bin").write_bytes(b"alpha")
    (root / "nested" / "b.bin").write_bytes(b"beta")

    assert scan_storage_path(root).content_hash == calculate_content_hash(root)

