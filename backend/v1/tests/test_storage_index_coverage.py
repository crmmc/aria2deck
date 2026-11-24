"""Coverage tests for app/services/storage_index.py and storage_locks.py."""

from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path

import pytest

from app.services import storage_index
from app.services.storage_index import (
    StorageScanError,
    build_entries,
    build_entry_templates,
    calculate_legacy_content_hash,
    scan_storage_path,
    scan_storage_path_async,
)
from app.services.storage_locks import (
    acquire_content_read_lease_locked,
    get_content_hash_lock,
    wait_for_content_readers_locked,
)


# ---------------------------------------------------------------------------
# storage_index
# ---------------------------------------------------------------------------


def test_scan_cancelled_before_start(tmp_path: Path) -> None:
    event = threading.Event()
    event.set()
    with pytest.raises(InterruptedError, match="cancelled"):
        scan_storage_path(tmp_path / "payload.bin", event)


def test_scan_missing_root_raises_value_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="does not exist"):
        scan_storage_path(tmp_path / "missing")
    with pytest.raises(ValueError, match="does not exist"):
        calculate_legacy_content_hash(tmp_path / "missing")


def test_scan_rejects_special_file(tmp_path: Path) -> None:
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(StorageScanError, match="特殊文件"):
        scan_storage_path(fifo)


def test_file_hash_open_failure(tmp_path: Path) -> None:
    protected = tmp_path / "protected.bin"
    protected.write_bytes(b"x")
    protected.chmod(0o000)
    try:
        with pytest.raises(StorageScanError, match="无法安全读取文件"):
            scan_storage_path(protected)
    finally:
        protected.chmod(0o600)


def test_file_hash_rejects_non_regular_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"data")
    directory_stat = os.stat(tmp_path)
    def fake_fstat(fd: int):
        return directory_stat

    monkeypatch.setattr(os, "fstat", fake_fstat)
    with pytest.raises(StorageScanError, match="特殊文件"):
        scan_storage_path(payload)


def test_validate_relative_rejects_invalid_paths() -> None:
    depth_path = "/".join(f"d{i}" for i in range(storage_index.MAX_STORAGE_PATH_DEPTH + 1)) + "/f.bin"
    long_path = "/".join(["n" * 200] * 25) + "/f.bin"
    cases = {
        "surrogate": "\udcff.bin",
        "deep": depth_path,
        "long": long_path,
        "component": "x" * (storage_index.MAX_STORAGE_COMPONENT_BYTES + 1),
    }
    for name, value in cases.items():
        with pytest.raises(StorageScanError):
            storage_index._validate_relative(value)
    storage_index._validate_relative("a/b.bin")
    storage_index._validate_relative(".")


def test_legacy_content_hash_directory_walk(tmp_path: Path) -> None:
    root = tmp_path / "payload"
    (root / "nested").mkdir(parents=True)
    (root / "a.bin").write_bytes(b"alpha")
    (root / "nested" / "b.bin").write_bytes(b"beta")

    digest = calculate_legacy_content_hash(root)
    assert len(digest) == 64

    # single-file legacy hash
    file_digest = calculate_legacy_content_hash(root / "a.bin")
    assert len(file_digest) == 64


def test_legacy_content_hash_scan_error(tmp_path: Path) -> None:
    root = tmp_path / "payload"
    root.mkdir()
    blocked = root / "blocked"
    blocked.mkdir()
    (blocked / "f.bin").write_bytes(b"x")
    blocked.chmod(0o000)
    try:
        with pytest.raises(StorageScanError, match="无法扫描目录"):
            calculate_legacy_content_hash(root)
        with pytest.raises(StorageScanError, match="无法扫描目录"):
            scan_storage_path(root)
    finally:
        blocked.chmod(0o700)


def test_scan_identity_and_build_entries(tmp_path: Path) -> None:
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"payload")
    scan = scan_storage_path(payload)
    assert scan.content_identity.version == "v2"
    templates = build_entry_templates(payload)
    assert templates == scan.entry_templates
    entries = build_entries(11, payload)
    assert entries[0]["stored_file_id"] == 11


@pytest.mark.asyncio
async def test_scan_storage_path_async_cancellation_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"payload")
    started = threading.Event()
    cancel_event = threading.Event()

    def slow_scanner(path: Path, event: threading.Event | None):
        started.set()
        assert event is not None and event.wait(2)
        return scan_storage_path(path)

    task = asyncio.create_task(
        scan_storage_path_async(payload, cancel_event, scanner=slow_scanner)
    )
    assert await asyncio.to_thread(started.wait, 2)
    task.cancel()
    cancel_event.set()
    with pytest.raises(asyncio.CancelledError):
        await task


# ---------------------------------------------------------------------------
# storage_locks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_content_hash_lock_is_reused_per_loop() -> None:
    first = await get_content_hash_lock("hash-a")
    second = await get_content_hash_lock("hash-a")
    assert first is second
    other = await get_content_hash_lock("hash-b")
    assert other is not first


@pytest.mark.asyncio
async def test_content_read_lease_drains_when_last_reader_releases() -> None:
    lease_a = acquire_content_read_lease_locked("hash-r")
    lease_b = acquire_content_read_lease_locked("hash-r")

    waiter = asyncio.create_task(wait_for_content_readers_locked("hash-r"))
    await asyncio.sleep(0.02)
    assert not waiter.done()

    await lease_a.release()
    await asyncio.sleep(0.02)
    assert not waiter.done()

    await lease_b.release()
    await asyncio.wait_for(waiter, 1)

    # double release is a no-op
    await lease_b.release()


def test_content_read_lease_release_without_readers() -> None:
    # defensive: releasing an already-zero lease keeps counter consistent
    async def scenario() -> None:
        lease = acquire_content_read_lease_locked("hash-zero")
        lease._state.active_readers = 0
        await lease.release()

    asyncio.run(scenario())


def test_scan_directory_with_nested_files(tmp_path: Path) -> None:
    root = tmp_path / "payload"
    (root / "nested").mkdir(parents=True)
    (root / "a.bin").write_bytes(b"alpha")
    (root / "nested" / "b.bin").write_bytes(b"beta")

    scan = scan_storage_path(root)
    assert scan.is_directory is True
    assert scan.size_bytes == 9
    relatives = [entry["relative_path"] for entry in scan.entry_templates]
    assert relatives[0] == "."
    assert set(relatives[1:]) == {"a.bin", "nested", "nested/b.bin"}
    assert scan.content_hash.startswith("v2:directory:")


def test_legacy_hash_rejects_entry_overflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "payload"
    root.mkdir()
    for name in ("a", "b", "c"):
        (root / name).write_bytes(b"x")
    monkeypatch.setattr(storage_index, "MAX_STORAGE_ENTRIES", 3)
    with pytest.raises(StorageScanError, match="条目过多"):
        calculate_legacy_content_hash(root)


@pytest.mark.asyncio
async def test_scan_storage_path_async_cancellation_with_interrupted_worker(
    tmp_path: Path,
) -> None:
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"payload")
    started = threading.Event()
    cancel_event = threading.Event()

    def interrupting_scanner(path: Path, event: threading.Event | None):
        started.set()
        assert event is not None and event.wait(2)
        raise InterruptedError("storage scan cancelled")

    task = asyncio.create_task(
        scan_storage_path_async(payload, cancel_event, scanner=interrupting_scanner)
    )
    assert await asyncio.to_thread(started.wait, 2)
    task.cancel()
    cancel_event.set()
    with pytest.raises(asyncio.CancelledError):
        await task
