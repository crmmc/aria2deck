from __future__ import annotations

from pathlib import Path

import pytest

from aria2deck.domain.content import ContentKind
from aria2deck.domain.errors import DomainError, ErrorCode
from aria2deck.infrastructure.content_store import DownloadContentStore


async def test_scan_rejects_invalid_ids_missing_and_symlink_roots(tmp_path: Path) -> None:
    store = DownloadContentStore(tmp_path)
    with pytest.raises(DomainError) as caught:
        await store.scan(0)
    assert caught.value.code is ErrorCode.INVALID_INPUT
    with pytest.raises(DomainError) as caught:
        await store.scan(1)
    assert caught.value.code is ErrorCode.INVALID_STATE

    target = tmp_path / "target"
    target.mkdir()
    (tmp_path / "2").symlink_to(target, target_is_directory=True)
    with pytest.raises(DomainError) as caught:
        await store.scan(2)
    assert caught.value.code is ErrorCode.INVALID_INPUT


async def test_directory_scan_and_install_include_nested_and_empty_entries(tmp_path: Path) -> None:
    source = tmp_path / "1"
    (source / "nested").mkdir(parents=True)
    (source / "empty").mkdir()
    (source / "root.bin").write_bytes(b"root")
    (source / "nested" / "child.bin").write_bytes(b"child")
    store = DownloadContentStore(tmp_path)

    preparation = await store.scan(1)
    assert preparation.identity.kind is ContentKind.DIRECTORY
    assert preparation.size == 9
    assert [entry.path for entry in preparation.entries] == [
        "empty", "nested", "nested/child.bin", "root.bin"
    ]
    await store.install(1, preparation)
    destination = tmp_path / preparation.storage_path
    assert (destination / "nested" / "child.bin").read_bytes() == b"child"
    assert (destination / "empty").is_dir()
    assert not source.exists()
    await store.install(1, preparation)
    await store.cleanup_attempt(1)


async def test_scan_rejects_symlinks_non_files_and_unfinished_control_files(tmp_path: Path) -> None:
    store = DownloadContentStore(tmp_path)
    source = tmp_path / "1"
    source.mkdir()
    (source / "file.aria2").write_bytes(b"pending")
    with pytest.raises(DomainError) as caught:
        await store.scan(1)
    assert caught.value.code is ErrorCode.DOWNLOAD_UNAVAILABLE
    (source / "file.aria2").unlink()

    external = tmp_path / "external"
    external.write_bytes(b"secret")
    (source / "link").symlink_to(external)
    with pytest.raises(DomainError) as caught:
        await store.scan(1)
    assert caught.value.code is ErrorCode.INVALID_INPUT
    (source / "link").unlink()

    directory_target = tmp_path / "directory-target"
    directory_target.mkdir()
    (source / "directory-link").symlink_to(directory_target, target_is_directory=True)
    with pytest.raises(DomainError) as caught:
        await store.scan(1)
    assert caught.value.code is ErrorCode.INVALID_INPUT


async def test_install_detects_source_and_destination_identity_conflicts(tmp_path: Path) -> None:
    source = tmp_path / "1"
    source.mkdir()
    (source / "file.bin").write_bytes(b"abc")
    store = DownloadContentStore(tmp_path)
    preparation = await store.scan(1)
    destination = tmp_path / preparation.storage_path
    destination.parent.mkdir()
    destination.write_bytes(b"different")
    with pytest.raises(DomainError) as caught:
        await store.install(1, preparation)
    assert caught.value.code is ErrorCode.INVALID_STATE
    assert (source / "file.bin").read_bytes() == b"abc"

    destination.unlink()
    (source / "file.bin").write_bytes(b"changed")
    with pytest.raises(DomainError) as caught:
        await store.install(1, preparation)
    assert caught.value.code is ErrorCode.INVALID_STATE


async def test_install_recovers_when_only_canonical_file_survives(tmp_path: Path) -> None:
    source = tmp_path / "1"
    source.mkdir()
    (source / "file.bin").write_bytes(b"abc")
    store = DownloadContentStore(tmp_path)
    preparation = await store.scan(1)
    destination = tmp_path / preparation.storage_path
    destination.parent.mkdir()
    (source / "file.bin").replace(destination)
    await store.install(1, preparation)
    assert destination.read_bytes() == b"abc"


async def test_install_rejects_symlink_storage_parent_and_layout_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "1"
    source.mkdir()
    (source / "file.bin").write_bytes(b"abc")
    store = DownloadContentStore(tmp_path)
    preparation = await store.scan(1)
    actual_objects = tmp_path / "actual-objects"
    actual_objects.mkdir()
    (tmp_path / "objects").symlink_to(actual_objects, target_is_directory=True)
    with pytest.raises(DomainError) as caught:
        await store.install(1, preparation)
    assert caught.value.code is ErrorCode.INVALID_INPUT

    (tmp_path / "objects").unlink()
    original_iterdir = Path.iterdir

    def empty_source(self: Path):
        if self == source:
            return iter([])
        return original_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", empty_source)
    with pytest.raises(DomainError) as caught:
        await store.install(1, preparation)
    assert caught.value.code is ErrorCode.INVALID_STATE


async def test_cleanup_unlinks_root_symlink_and_rejects_non_directory(tmp_path: Path) -> None:
    store = DownloadContentStore(tmp_path)
    target = tmp_path / "target"
    target.mkdir()
    (tmp_path / "1").symlink_to(target, target_is_directory=True)
    await store.cleanup_attempt(1)
    assert not (tmp_path / "1").exists()
    assert target.exists()

    (tmp_path / "2").write_bytes(b"not-directory")
    with pytest.raises(DomainError) as caught:
        await store.cleanup_attempt(2)
    assert caught.value.code is ErrorCode.INVALID_STATE


async def test_physical_delete_is_idempotent_for_files_trees_and_symlinks(tmp_path: Path) -> None:
    store = DownloadContentStore(tmp_path)
    single = tmp_path / "objects" / "file-abc"
    single.parent.mkdir()
    single.write_bytes(b"abc")
    await store.physical_delete("objects/file-abc")
    assert not single.exists()
    await store.physical_delete("objects/file-abc")

    tree = tmp_path / "objects" / "directory-def"
    (tree / "nested").mkdir(parents=True)
    (tree / "nested" / "child.bin").write_bytes(b"child")
    (tree / "empty").mkdir()
    await store.physical_delete("objects/directory-def")
    assert not tree.exists()
    await store.physical_delete("objects/directory-def")

    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "objects" / "file-link"
    link.symlink_to(outside)
    await store.physical_delete("objects/file-link")
    assert not link.exists()
    assert outside.exists()

    with pytest.raises(DomainError) as caught:
        await store.physical_delete("/absolute")
    assert caught.value.code is ErrorCode.INVALID_INPUT


async def test_open_read_handle_resolves_entry_paths_and_rejects_escape(tmp_path: Path) -> None:
    source = tmp_path / "1"
    (source / "nested").mkdir(parents=True)
    (source / "nested" / "child.bin").write_bytes(b"child")
    (source / "root.bin").write_bytes(b"root")
    store = DownloadContentStore(tmp_path)
    preparation = await store.scan(1)
    await store.install(1, preparation)

    root = await store.open_read_handle(preparation.storage_path, "root.bin")
    assert root.read() == b"root"
    root.close()
    nested = await store.open_read_handle(preparation.storage_path, "nested/child.bin")
    assert nested.read() == b"child"
    nested.close()

    for escape in ("../escape", "a\\b", "/absolute"):
        with pytest.raises(DomainError) as caught:
            await store.open_read_handle(preparation.storage_path, escape)
        assert caught.value.code is ErrorCode.INVALID_INPUT


async def test_scan_entries_matches_the_completion_layout(tmp_path: Path) -> None:
    source = tmp_path / "1"
    (source / "nested").mkdir(parents=True)
    (source / "empty").mkdir()
    (source / "root.bin").write_bytes(b"root")
    (source / "nested" / "child.bin").write_bytes(b"child")
    store = DownloadContentStore(tmp_path)
    preparation = await store.scan(1)
    await store.install(1, preparation)
    assert await store.scan_entries(preparation.storage_path) == list(preparation.entries)

    single = tmp_path / "2"
    single.mkdir()
    (single / "file.bin").write_bytes(b"abc")
    file_preparation = await store.scan(2)
    await store.install(2, file_preparation)
    assert file_preparation.identity.kind is ContentKind.FILE
    assert await store.scan_entries(file_preparation.storage_path) == []
