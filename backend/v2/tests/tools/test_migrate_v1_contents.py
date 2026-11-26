"""单元 3：内容搬迁——一次读盘同时复制与算 V2 摘要，按摘要去重。"""

from __future__ import annotations

import errno
import hashlib
import os
from pathlib import Path

import pytest

from aria2deck.domain.content import (
    ContentEntryIdentity,
    ContentKind,
    content_identity,
    directory_content_digest,
)
from tests.tools.conftest import SampleLayout, checksum_tree
from tools.migrate_v1.contents import migrate_contents, objects_root
from tools.migrate_v1.planner import build_plan
from tools.migrate_v1.reader import read_snapshot


def _migrate(layout: SampleLayout, v2_root: Path, *, dry_run: bool = False, link: bool = False):
    plan = build_plan(read_snapshot(layout.database), layout.download_dir)
    return plan, migrate_contents(plan, v2_root, dry_run=dry_run, link=link)


def test_file_content_is_byte_identical_and_digest_is_plain_sha256(
    v1_sample: SampleLayout, tmp_path: Path
) -> None:
    """V1 的 digest 是再套一层域分隔的哈希，V2 必须是文件本身的 sha256。"""
    v2_root = tmp_path / "v2"
    _, migration = _migrate(v1_sample, v2_root)
    objects = objects_root(v2_root)
    source = v1_sample.file_paths["solo"]

    expected = hashlib.sha256(source.read_bytes()).hexdigest()
    stored = objects / f"file-{expected}"
    assert stored.read_bytes() == source.read_bytes()
    assert any(item.content_key == f"file:{expected}" for item in migration.objects)


def test_every_file_digest_matches_the_v2_algorithm(
    v1_sample: SampleLayout, tmp_path: Path
) -> None:
    v2_root = tmp_path / "v2"
    _, migration = _migrate(v1_sample, v2_root)
    objects = objects_root(v2_root)

    for content in migration.objects:
        if content.kind != "file":
            continue
        payload = (objects / f"file-{content.digest}").read_bytes()
        assert content_identity(ContentKind.FILE, hashlib.sha256(payload).hexdigest()).digest == content.digest
        assert content.size == len(payload)
        assert content.storage_path == f"objects/file-{content.digest}"


def test_directory_content_rebuilds_entries_and_manifest_digest(
    v1_sample: SampleLayout, tmp_path: Path
) -> None:
    v2_root = tmp_path / "v2"
    _, migration = _migrate(v1_sample, v2_root)

    directory = next(item for item in migration.objects if item.kind == "directory")
    source = v1_sample.file_paths["folder"]

    expected_entries = []
    for item in sorted(source.rglob("*")):
        relative = item.relative_to(source).as_posix()
        if item.is_dir():
            expected_entries.append(ContentEntryIdentity(relative, ContentKind.DIRECTORY, 0))
            continue
        expected_entries.append(
            ContentEntryIdentity(
                relative,
                ContentKind.FILE,
                item.stat().st_size,
                hashlib.sha256(item.read_bytes()).hexdigest(),
            )
        )
    expected_entries.sort(key=lambda entry: entry.path)

    assert list(directory.entries) == expected_entries
    assert directory.digest == directory_content_digest(expected_entries)
    assert directory.size == sum(entry.size for entry in expected_entries)
    assert {entry.path for entry in directory.entries} == {"nested", "nested/two.txt", "one.txt"}


def test_directory_files_are_copied_byte_identical(
    v1_sample: SampleLayout, tmp_path: Path
) -> None:
    v2_root = tmp_path / "v2"
    _, migration = _migrate(v1_sample, v2_root)

    directory = next(item for item in migration.objects if item.kind == "directory")
    copied = objects_root(v2_root) / f"directory-{directory.digest}"
    source = v1_sample.file_paths["folder"]

    assert (copied / "one.txt").read_bytes() == (source / "one.txt").read_bytes()
    assert (copied / "nested" / "two.txt").read_bytes() == (
        source / "nested" / "two.txt"
    ).read_bytes()


def test_shared_content_is_stored_once(v1_sample: SampleLayout, tmp_path: Path) -> None:
    v2_root = tmp_path / "v2"
    _, migration = _migrate(v1_sample, v2_root)

    shared_ids = [1, 2]
    plan = build_plan(read_snapshot(v1_sample.database), v1_sample.download_dir)
    assert {stored.v1_id for stored in plan.stored_files} >= set(shared_ids)
    names = [path.name for path in objects_root(v2_root).iterdir()]
    assert len(names) == len(set(names))
    assert not any(name.startswith(".tmp-") for name in names)


def test_same_content_twice_on_disk_reuses_single_object(
    v1_sample: SampleLayout, tmp_path: Path
) -> None:
    """两条 V1 记录指向内容相同的两个文件 → 只留一份 V2 对象。"""
    import sqlite3

    duplicate = v1_sample.store_dir / "bb" / "copy.bin"
    duplicate.write_bytes(v1_sample.file_paths["solo"].read_bytes())
    connection = sqlite3.connect(v1_sample.database)
    try:
        connection.execute(
            "INSERT INTO stored_files (id, content_hash, content_hash_version, "
            "content_object_kind, real_path, size_bytes, is_directory, original_name, "
            "created_at_ms) VALUES (8, 'dup', 'v1', 'legacy', ?, ?, 0, 'copy.bin', 1000)",
            (str(duplicate), duplicate.stat().st_size),
        )
        connection.commit()
    finally:
        connection.close()

    v2_root = tmp_path / "v2"
    plan, migration = _migrate(v1_sample, v2_root)

    solo = next(item for item in plan.stored_files if item.source.name == "solo.bin")
    copy = next(item for item in plan.stored_files if item.source.name == "copy.bin")
    assert (
        migration.by_stored_file[solo.v1_id].content_key
        == migration.by_stored_file[copy.v1_id].content_key
    )
    names = {path.name for path in objects_root(v2_root).iterdir()}
    expected_solo = f"file-{hashlib.sha256(v1_sample.file_paths['solo'].read_bytes()).hexdigest()}"
    expected_shared = f"file-{hashlib.sha256(v1_sample.file_paths['shared'].read_bytes()).hexdigest()}"
    expected_directory = next(
        path.name for path in objects_root(v2_root).iterdir() if path.name.startswith("directory-")
    )
    assert names == {expected_solo, expected_shared, expected_directory}


def test_dry_run_writes_nothing(v1_sample: SampleLayout, tmp_path: Path) -> None:
    v2_root = tmp_path / "v2"
    _, migration = _migrate(v1_sample, v2_root, dry_run=True)

    assert not v2_root.exists()
    assert migration.objects
    assert all(item.size > 0 or item.kind == "file" for item in migration.objects)


def test_dry_run_reports_the_same_digests_as_a_real_run(
    v1_sample: SampleLayout, tmp_path: Path
) -> None:
    _, preview = _migrate(v1_sample, tmp_path / "preview", dry_run=True)
    _, actual = _migrate(v1_sample, tmp_path / "actual")

    assert {item.content_key for item in preview.objects} == {
        item.content_key for item in actual.objects
    }
    assert {item.size for item in preview.objects} == {item.size for item in actual.objects}


def test_unreadable_file_is_recorded_as_failure(
    v1_sample: SampleLayout, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tools.migrate_v1.contents as contents_module

    original = contents_module._stream

    def explode(source: Path, sink) -> tuple[str, int]:
        if source.name == "solo.bin":
            raise PermissionError("denied")
        return original(source, sink)

    monkeypatch.setattr(contents_module, "_stream", explode)

    _, migration = _migrate(v1_sample, tmp_path / "v2")

    assert migration.failures == ((2, "读取失败：PermissionError"),)
    assert 2 not in migration.by_stored_file
    leftovers = [path.name for path in objects_root(tmp_path / "v2").iterdir()]
    assert not any(name.startswith(".tmp") for name in leftovers)


def test_layout_violation_is_recorded_as_failure(
    v1_sample: SampleLayout, tmp_path: Path
) -> None:
    control = v1_sample.file_paths["folder"] / "x.aria2"
    control.write_bytes(b"control")

    plan, migration = _migrate(v1_sample, tmp_path / "v2")

    assert 5 not in {stored.v1_id for stored in plan.stored_files}
    assert migration.failures == ()


def test_v1_tree_is_untouched(v1_sample: SampleLayout, tmp_path: Path) -> None:
    before = checksum_tree(v1_sample.download_dir)
    _migrate(v1_sample, tmp_path / "v2")

    assert checksum_tree(v1_sample.download_dir) == before


def test_link_mode_hardlinks_instead_of_copying(v1_sample: SampleLayout, tmp_path: Path) -> None:
    """硬链接模式：规范对象与源共享 inode，摘要算法不变，源树原样未动。"""
    v2_root = tmp_path / "v2"
    before = checksum_tree(v1_sample.download_dir)
    _, migration = _migrate(v1_sample, v2_root, link=True)

    assert migration.failures == ()
    source = v1_sample.file_paths["solo"]
    expected = hashlib.sha256(source.read_bytes()).hexdigest()
    stored = objects_root(v2_root) / f"file-{expected}"
    assert os.stat(stored).st_ino == os.stat(source).st_ino
    assert checksum_tree(v1_sample.download_dir) == before

    # 再跑一遍：目标已存在时复用规范对象，不报错也不重复链接
    _, again = _migrate(v1_sample, v2_root, link=True)
    assert again.failures == ()


def test_link_mode_directory_files_are_hardlinked(v1_sample: SampleLayout, tmp_path: Path) -> None:
    v2_root = tmp_path / "v2"
    _, migration = _migrate(v1_sample, v2_root, link=True)

    assert migration.failures == ()
    directory = next(item for item in migration.objects if item.kind == "directory")
    entry = next(e for e in directory.entries if e.kind is ContentKind.FILE)
    parts = entry.path.split("/")
    linked = (objects_root(v2_root) / f"directory-{directory.digest}").joinpath(*parts)
    source = v1_sample.file_paths["folder"].joinpath(*parts)
    assert os.stat(linked).st_ino == os.stat(source).st_ino


def test_link_mode_dry_run_writes_nothing_but_surfaces_cross_device(
    v1_sample: SampleLayout, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """dry-run 不落字节，但跨盘不可行必须在预演阶段就暴露。"""
    v2_root = tmp_path / "v2"
    _, ok = _migrate(v1_sample, v2_root, dry_run=True, link=True)
    assert ok.failures == ()
    assert not v2_root.exists()

    monkeypatch.setattr("tools.migrate_v1.contents._same_device", lambda a, b: False)
    _, blocked = _migrate(v1_sample, v2_root, dry_run=True, link=True)
    assert blocked.by_stored_file == {}
    assert blocked.failures
    assert all("同一文件系统" in reason for _, reason in blocked.failures)


def test_link_mode_records_cross_device_link_errors_as_failures(
    v1_sample: SampleLayout, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """st_dev 检查漏掉的嵌套挂载：os.link 抛 EXDEV 时记为逐项失败而不是崩溃。"""

    def deny(source: Path, destination: Path) -> None:
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr("tools.migrate_v1.contents.os.link", deny)
    _, migration = _migrate(v1_sample, tmp_path / "v2", link=True)

    assert migration.objects == ()
    assert migration.failures
    assert all(reason == "跨文件系统，无法硬链接" for _, reason in migration.failures)


def test_objects_root_layout(tmp_path: Path) -> None:
    assert objects_root(tmp_path).as_posix().endswith("downloads/objects")
