from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from aria2deck.application.downloads.models import ContentPreparation, ManifestEntry
from aria2deck.domain.content import (
    ContentEntryIdentity,
    ContentKind,
    content_identity,
    directory_content_digest,
)
from aria2deck.domain.errors import DomainError, ErrorCode
from aria2deck.infrastructure.content_store import DownloadContentStore


def manifest(*entries: tuple[str, int]) -> tuple[ManifestEntry, ...]:
    """清单是入库的唯一事实来源：只有这里列出的文件才会被核验、搬运、入库。"""
    return tuple(ManifestEntry(path, size) for path, size in entries)


async def test_collect_rejects_invalid_ids_missing_and_symlink_roots(tmp_path: Path) -> None:
    store = DownloadContentStore(tmp_path)
    with pytest.raises(DomainError) as caught:
        await store.collect(0, manifest(("file.bin", 3)))
    assert caught.value.code is ErrorCode.INVALID_INPUT
    with pytest.raises(DomainError) as caught:
        await store.collect(1, manifest(("file.bin", 3)))
    assert caught.value.code is ErrorCode.INVALID_STATE

    target = tmp_path / "target"
    target.mkdir()
    (target / "file.bin").write_bytes(b"abc")
    (tmp_path / "2").symlink_to(target, target_is_directory=True)
    with pytest.raises(DomainError) as caught:
        await store.collect(2, manifest(("file.bin", 3)))
    assert caught.value.code is ErrorCode.INVALID_INPUT


async def test_collect_and_install_follow_the_manifest_only(tmp_path: Path) -> None:
    source = tmp_path / "1"
    (source / "nested").mkdir(parents=True)
    (source / "empty").mkdir()
    (source / "root.bin").write_bytes(b"root")
    (source / "nested" / "child.bin").write_bytes(b"child")
    store = DownloadContentStore(tmp_path)
    entries = manifest(("root.bin", 4), ("nested/child.bin", 5))

    preparation = await store.collect(1, entries)

    assert preparation.identity.kind is ContentKind.DIRECTORY
    assert preparation.size == 9
    # 目录条目由清单路径推导：磁盘上那个空目录不属于内容。
    assert [entry.path for entry in preparation.entries] == [
        "nested", "nested/child.bin", "root.bin"
    ]
    await store.install(1, preparation, entries)
    destination = tmp_path / preparation.storage_path
    assert (destination / "nested" / "child.bin").read_bytes() == b"child"
    assert not (destination / "empty").exists()
    assert not (source / "root.bin").exists(), "清单文件已搬进暂存目录"
    assert (source / "empty").is_dir(), "清单外的目录留在原地等终态清理"

    await store.install(1, preparation, entries)  # 幂等：规范位已就位，只核验
    await store.cleanup_attempt(1)
    assert not source.exists()
    assert not (tmp_path / ".ingest" / "1").exists()


async def test_collect_ignores_control_files_and_residue_outside_the_manifest(
    tmp_path: Path,
) -> None:
    """生产事故形态：磁力真名落盘 + `.aria2` 残留，清单只含 .bin → 残留根本不进内容。"""
    source = tmp_path / "1"
    source.mkdir()
    (source / "Real Torrent Name.bin").write_bytes(b"abc")
    (source / "Real Torrent Name.aria2").write_bytes(b"control")
    (source / "residue.iso").write_bytes(b"x" * 4096)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"secret")
    (source / "link.bin").symlink_to(outside)
    store = DownloadContentStore(tmp_path)
    entries = manifest(("Real Torrent Name.bin", 3))

    preparation = await store.collect(1, entries)

    assert preparation.identity.kind is ContentKind.FILE
    assert preparation.identity.digest == hashlib.sha256(b"abc").hexdigest()
    assert preparation.entries == ()
    await store.install(1, preparation, entries)
    assert (tmp_path / preparation.storage_path).read_bytes() == b"abc"
    assert (source / "Real Torrent Name.aria2").exists(), "清单外的残留入库全程没被碰过"
    assert (source / "residue.iso").exists()
    assert (source / "link.bin").is_symlink(), "清单外符号链接不参与、不跟随、不清理"
    assert outside.read_bytes() == b"secret"


@pytest.mark.parametrize(
    "path",
    [
        "/absolute.bin",
        "../escape.bin",
        "a/../b.bin",
        "a//b.bin",
        "a/./b.bin",
        "./a.bin",
        ".",
        "",
        "a\\b.bin",
        "a\x00b.bin",
    ],
)
async def test_collect_rejects_manifest_paths_that_are_not_normalized(
    tmp_path: Path, path: str
) -> None:
    source = tmp_path / "1"
    source.mkdir()
    (source / "b.bin").write_bytes(b"abc")
    store = DownloadContentStore(tmp_path)

    with pytest.raises(DomainError) as caught:
        await store.collect(1, (ManifestEntry(path, 3),))

    assert caught.value.code is ErrorCode.INVALID_INPUT


async def test_collect_rejects_duplicate_manifest_paths(tmp_path: Path) -> None:
    source = tmp_path / "1"
    source.mkdir()
    (source / "file.bin").write_bytes(b"abc")
    store = DownloadContentStore(tmp_path)

    with pytest.raises(DomainError) as caught:
        await store.collect(1, manifest(("file.bin", 3), ("file.bin", 3)))

    assert caught.value.code is ErrorCode.INVALID_STATE


async def test_collect_rejects_symlinked_files_components_and_roots(tmp_path: Path) -> None:
    source = tmp_path / "1"
    source.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (external / "victim.bin").write_bytes(b"abc")
    store = DownloadContentStore(tmp_path)

    (source / "file-link.bin").symlink_to(external / "victim.bin")
    with pytest.raises(DomainError) as caught:
        await store.collect(1, manifest(("file-link.bin", 3)))
    assert caught.value.code is ErrorCode.INVALID_INPUT
    (source / "file-link.bin").unlink()

    (source / "dir-link").symlink_to(external, target_is_directory=True)
    with pytest.raises(DomainError) as caught:
        await store.collect(1, manifest(("dir-link/victim.bin", 3)))
    assert caught.value.code is ErrorCode.INVALID_INPUT
    (source / "dir-link").unlink()

    (tmp_path / "2").symlink_to(external, target_is_directory=True)
    with pytest.raises(DomainError) as caught:
        await store.collect(2, manifest(("victim.bin", 3)))
    assert caught.value.code is ErrorCode.INVALID_INPUT
    assert (external / "victim.bin").read_bytes() == b"abc", "拒绝时不得读取链接指向的内容"


@pytest.mark.parametrize(
    ("case", "expected_code"),
    [
        ("missing", ErrorCode.INVALID_STATE),
        ("size", ErrorCode.INVALID_STATE),
        ("missing-directory", ErrorCode.INVALID_STATE),
        ("file-as-directory", ErrorCode.INVALID_STATE),
        ("fifo", ErrorCode.INVALID_INPUT),
        ("directory", ErrorCode.INVALID_INPUT),
    ],
)
async def test_collect_rejects_manifest_entries_that_disagree_with_disk(
    tmp_path: Path, case: str, expected_code: ErrorCode
) -> None:
    """缺失/大小不符按可重试处理（INVALID_STATE），符号链接与非普通文件直接拒绝。"""
    source = tmp_path / "1"
    source.mkdir()
    (source / "file.bin").write_bytes(b"abc")
    (source / "sub").mkdir()
    paths = {"missing": ("absent.bin", 3), "size": ("file.bin", 4), "fifo": ("pipe", 0)}
    case_paths = {
        "missing-directory": ("absent/dir.bin", 3),
        "file-as-directory": ("file.bin/child.bin", 3),
        "directory": ("sub", 0),
    }
    if case == "fifo":
        os.mkfifo(source / "pipe")
    entry = paths.get(case) or case_paths[case]
    store = DownloadContentStore(tmp_path)

    with pytest.raises(DomainError) as caught:
        await store.collect(1, manifest(entry))

    assert caught.value.code is expected_code


async def test_collect_empty_manifest_is_an_explicitly_empty_directory(tmp_path: Path) -> None:
    """空清单（[]）表示“明确知道没有文件”，不是“不知道”：产出一个空目录内容。"""
    source = tmp_path / "1"
    source.mkdir()
    store = DownloadContentStore(tmp_path)

    preparation = await store.collect(1, ())

    assert preparation.identity.kind is ContentKind.DIRECTORY
    assert preparation.identity.digest == directory_content_digest([])
    assert (preparation.size, preparation.entries) == (0, ())
    await store.install(1, preparation, ())
    assert (tmp_path / preparation.storage_path).is_dir()


async def test_collect_makes_a_single_root_file_a_file_object(tmp_path: Path) -> None:
    source = tmp_path / "1"
    source.mkdir()
    (source / "file.bin").write_bytes(b"abc")
    store = DownloadContentStore(tmp_path)

    preparation = await store.collect(1, manifest(("file.bin", 3)))

    assert preparation.identity.kind is ContentKind.FILE
    assert preparation.storage_path == f"objects/file-{hashlib.sha256(b'abc').hexdigest()}"


async def test_collect_keeps_a_nested_single_file_a_directory_object(tmp_path: Path) -> None:
    source = tmp_path / "1"
    (source / "bundle").mkdir(parents=True)
    (source / "bundle" / "file.bin").write_bytes(b"abc")
    store = DownloadContentStore(tmp_path)
    entries = manifest(("bundle/file.bin", 3))

    preparation = await store.collect(1, entries)

    assert preparation.identity.kind is ContentKind.DIRECTORY
    assert [entry.path for entry in preparation.entries] == ["bundle", "bundle/file.bin"]


async def test_install_rejects_source_content_changed_after_collection(tmp_path: Path) -> None:
    """collect 之后源文件被改写：宁可失败重试，也不能把没核验过的字节搬进规范位。"""
    source = tmp_path / "1"
    source.mkdir()
    (source / "file.bin").write_bytes(b"abc")
    store = DownloadContentStore(tmp_path)
    entries = manifest(("file.bin", 3))
    preparation = await store.collect(1, entries)
    (source / "file.bin").write_bytes(b"abd")  # 同大小、不同内容

    with pytest.raises(DomainError) as caught:
        await store.install(1, preparation, entries)

    assert caught.value.code is ErrorCode.INVALID_STATE
    assert not (tmp_path / preparation.storage_path).exists(), "规范位不出现未核验内容"


async def test_install_reuses_verified_existing_content_and_keeps_conflicts(
    tmp_path: Path,
) -> None:
    source = tmp_path / "1"
    source.mkdir()
    (source / "file.bin").write_bytes(b"abc")
    store = DownloadContentStore(tmp_path)
    entries = manifest(("file.bin", 3))
    preparation = await store.collect(1, entries)
    destination = tmp_path / preparation.storage_path
    destination.parent.mkdir()
    destination.write_bytes(b"different")

    with pytest.raises(DomainError) as caught:
        await store.install(1, preparation, entries)
    assert caught.value.code is ErrorCode.INVALID_STATE
    assert (source / "file.bin").read_bytes() == b"abc", "冲突时不搬、不覆盖"
    assert destination.read_bytes() == b"different"

    destination.write_bytes(b"abc")  # 同内容已在位（去重）：核验通过直接复用
    await store.install(1, preparation, entries)
    assert (source / "file.bin").read_bytes() == b"abc", "源目录留给终态清理"


async def test_install_resumes_after_a_crash_between_file_moves(tmp_path: Path) -> None:
    source = tmp_path / "1"
    (source / "nested").mkdir(parents=True)
    (source / "a.bin").write_bytes(b"abc")
    (source / "nested" / "b.bin").write_bytes(b"de")
    entries = manifest(("a.bin", 3), ("nested/b.bin", 2))
    store = DownloadContentStore(tmp_path)
    preparation = await store.collect(1, entries)

    staging = tmp_path / ".ingest" / "1"
    (staging / "nested").mkdir(parents=True)
    (source / "a.bin").replace(staging / "a.bin")  # 上一轮搬完第一个文件后崩溃

    await store.install(1, preparation, entries)

    destination = tmp_path / preparation.storage_path
    assert (destination / "a.bin").read_bytes() == b"abc"
    assert (destination / "nested" / "b.bin").read_bytes() == b"de"


async def test_install_rejects_symlinked_storage_parent_and_destination(tmp_path: Path) -> None:
    source = tmp_path / "1"
    source.mkdir()
    (source / "file.bin").write_bytes(b"abc")
    store = DownloadContentStore(tmp_path)
    entries = manifest(("file.bin", 3))
    preparation = await store.collect(1, entries)

    actual_objects = tmp_path / "actual-objects"
    actual_objects.mkdir()
    (tmp_path / "objects").symlink_to(actual_objects, target_is_directory=True)
    with pytest.raises(DomainError) as caught:
        await store.install(1, preparation, entries)
    assert caught.value.code is ErrorCode.INVALID_INPUT

    (tmp_path / "objects").unlink()
    destination = tmp_path / preparation.storage_path
    destination.parent.mkdir()
    external = tmp_path / "outside.bin"
    external.write_bytes(b"abc")
    destination.symlink_to(external)
    with pytest.raises(DomainError) as caught:
        await store.install(1, preparation, entries)
    assert caught.value.code is ErrorCode.INVALID_INPUT
    assert destination.is_symlink(), "符号链接规范位不得被覆盖或复用"
    assert external.read_bytes() == b"abc"


@pytest.mark.parametrize("case", ["file-at-directory-path", "different-content"])
async def test_install_rejects_an_existing_destination_that_is_not_the_manifest_content(
    tmp_path: Path, case: str
) -> None:
    source = tmp_path / "1"
    (source / "bundle").mkdir(parents=True)
    (source / "bundle" / "a.bin").write_bytes(b"abc")
    store = DownloadContentStore(tmp_path)
    entries = manifest(("bundle/a.bin", 3))
    preparation = await store.collect(1, entries)
    destination = tmp_path / preparation.storage_path
    destination.parent.mkdir()
    if case == "file-at-directory-path":
        destination.write_bytes(b"abc")
    else:
        (destination / "bundle").mkdir(parents=True)
        (destination / "bundle" / "a.bin").write_bytes(b"abd")  # 同大小、不同内容

    with pytest.raises(DomainError) as caught:
        await store.install(1, preparation, entries)

    assert caught.value.code is ErrorCode.INVALID_STATE
    assert (source / "bundle" / "a.bin").read_bytes() == b"abc"


async def test_install_rejects_a_symlinked_staging_parent(tmp_path: Path) -> None:
    source = tmp_path / "1"
    source.mkdir()
    (source / "file.bin").write_bytes(b"abc")
    store = DownloadContentStore(tmp_path)
    entries = manifest(("file.bin", 3))
    preparation = await store.collect(1, entries)
    actual = tmp_path / "actual-ingest"
    actual.mkdir()
    (tmp_path / ".ingest").symlink_to(actual, target_is_directory=True)

    with pytest.raises(DomainError) as caught:
        await store.install(1, preparation, entries)

    assert caught.value.code is ErrorCode.INVALID_INPUT
    assert not (actual / "1").exists(), "拒绝时不得在链接目标下创建暂存目录"


async def test_install_rejects_a_symlinked_attempt_root_before_moving_anything(
    tmp_path: Path,
) -> None:
    store = DownloadContentStore(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    (external / "file.bin").write_bytes(b"abc")
    (tmp_path / "1").symlink_to(external, target_is_directory=True)
    identity = content_identity(ContentKind.FILE, hashlib.sha256(b"abc").hexdigest())
    expected = ContentPreparation(identity, 3, f"objects/file-{identity.digest}", ())

    with pytest.raises(DomainError) as caught:
        await store.install(1, expected, manifest(("file.bin", 3)))

    assert caught.value.code is ErrorCode.INVALID_INPUT
    assert (external / "file.bin").read_bytes() == b"abc", "拒绝时不得跨出 attempt 目录"


async def test_install_reports_a_destination_that_vanished_between_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "1"
    source.mkdir()
    (source / "file.bin").write_bytes(b"abc")
    store = DownloadContentStore(tmp_path)
    entries = manifest(("file.bin", 3))
    preparation = await store.collect(1, entries)
    destination = tmp_path / preparation.storage_path
    real_exists = Path.exists

    def racing_exists(self: Path) -> bool:
        if self == destination:
            return True  # 竞态：exists 说在，lstat 已说没了
        return real_exists(self)

    monkeypatch.setattr(Path, "exists", racing_exists)
    with pytest.raises(DomainError) as caught:
        await store.install(1, preparation, entries)

    assert caught.value.code is ErrorCode.INVALID_STATE


async def test_cleanup_unlinks_root_symlink_and_rejects_non_directory(tmp_path: Path) -> None:
    store = DownloadContentStore(tmp_path)
    target = tmp_path / "target"
    target.mkdir()
    (tmp_path / "1").symlink_to(target, target_is_directory=True)
    (tmp_path / ".ingest").mkdir()
    (tmp_path / ".ingest" / "1").symlink_to(target, target_is_directory=True)
    await store.cleanup_attempt(1)
    assert not (tmp_path / "1").exists()
    assert not (tmp_path / ".ingest" / "1").exists()
    assert target.exists()

    (tmp_path / "2").write_bytes(b"not-directory")
    with pytest.raises(DomainError) as caught:
        await store.cleanup_attempt(2)
    assert caught.value.code is ErrorCode.INVALID_STATE

    (tmp_path / "3").mkdir()
    (tmp_path / ".ingest" / "3").mkdir()
    (tmp_path / ".ingest" / "3" / "a.bin").write_bytes(b"abc")
    await store.cleanup_attempt(3)
    assert not (tmp_path / "3").exists()
    assert not (tmp_path / ".ingest" / "3").exists()

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
    entries = manifest(("root.bin", 4), ("nested/child.bin", 5))
    preparation = await store.collect(1, entries)
    await store.install(1, preparation, entries)

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
    (source / "root.bin").write_bytes(b"root")
    (source / "nested" / "child.bin").write_bytes(b"child")
    store = DownloadContentStore(tmp_path)
    entries = manifest(("root.bin", 4), ("nested/child.bin", 5))
    preparation = await store.collect(1, entries)
    await store.install(1, preparation, entries)
    assert await store.scan_entries(preparation.storage_path) == list(preparation.entries)

    single = tmp_path / "2"
    single.mkdir()
    (single / "file.bin").write_bytes(b"abc")
    file_entries = manifest(("file.bin", 3))
    file_preparation = await store.collect(2, file_entries)
    await store.install(2, file_preparation, file_entries)
    assert file_preparation.identity.kind is ContentKind.FILE
    assert await store.scan_entries(file_preparation.storage_path) == []

    with pytest.raises(DomainError) as caught:
        await store.scan_entries("objects/file-" + "f" * 64)
    assert caught.value.code is ErrorCode.INVALID_STATE

    (tmp_path / "outside").mkdir()
    linked = tmp_path / "objects" / ("directory-" + "e" * 64)
    linked.symlink_to(tmp_path / "outside", target_is_directory=True)
    with pytest.raises(DomainError) as caught:
        await store.scan_entries(f"objects/directory-{'e' * 64}")
    assert caught.value.code is ErrorCode.INVALID_INPUT


async def test_install_prepared_reports_created_reuse_and_guards_symlink_parent(
    tmp_path: Path,
) -> None:
    store = DownloadContentStore(tmp_path)
    part = tmp_path / "packs" / "prepared-1.part"
    part.parent.mkdir(parents=True)
    payload = b"payload"
    part.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    storage_path = f"objects/file-{digest}"

    created = await store.install_prepared("packs/prepared-1.part", digest)
    assert (created.storage_path, created.created_by_this_attempt) == (storage_path, True)
    assert (tmp_path / storage_path).read_bytes() == payload
    assert not part.exists()

    part.write_bytes(payload)  # 同摘要 part 回到现场：规范位已存在，只能复用。
    reused = await store.install_prepared("packs/prepared-1.part", digest)
    assert (reused.storage_path, reused.created_by_this_attempt) == (storage_path, False)
    assert not part.exists()
    assert (tmp_path / storage_path).read_bytes() == payload

    import shutil

    other = tmp_path / "objects2"
    other.mkdir()
    shutil.rmtree(tmp_path / "objects")
    (tmp_path / "objects").symlink_to(other, target_is_directory=True)
    with pytest.raises(DomainError) as caught:
        await store.install_prepared("packs/prepared-1.part", "cd" * 32)
    assert caught.value.code is ErrorCode.INVALID_INPUT


async def test_install_prepared_rejects_a_conflicting_canonical_and_keeps_both(
    tmp_path: Path,
) -> None:
    store = DownloadContentStore(tmp_path)
    payload = b"expected prepared"
    digest = hashlib.sha256(payload).hexdigest()
    part = tmp_path / "packs" / "prepared-1.part"
    part.parent.mkdir(parents=True)
    part.write_bytes(payload)
    canonical = tmp_path / "objects" / f"file-{digest}"
    canonical.parent.mkdir(parents=True)
    canonical.write_bytes(b"stale canonical")

    with pytest.raises(DomainError) as caught:
        await store.install_prepared("packs/prepared-1.part", digest)

    assert caught.value.code is ErrorCode.INVALID_STATE
    assert canonical.read_bytes() == b"stale canonical", "冲突 canonical 不在存储层回收"
    assert part.read_bytes() == payload, "冲突 part 交给 dispatcher 的确定性失败清理"


async def test_install_prepared_rejects_a_canonical_directory(tmp_path: Path) -> None:
    store = DownloadContentStore(tmp_path)
    part = tmp_path / "packs" / "prepared-1.part"
    part.parent.mkdir(parents=True)
    part.write_bytes(b"payload")
    digest = hashlib.sha256(b"payload").hexdigest()
    (tmp_path / "objects" / f"file-{digest}").mkdir(parents=True)

    with pytest.raises(DomainError) as caught:
        await store.install_prepared("packs/prepared-1.part", digest)

    assert caught.value.code is ErrorCode.INVALID_STATE
    assert part.exists()


@pytest.mark.parametrize("link_target_exists", [True, False], ids=["matching", "broken"])
async def test_install_prepared_rejects_canonical_symlinks(
    tmp_path: Path, link_target_exists: bool
) -> None:
    store = DownloadContentStore(tmp_path)
    payload = b"expected prepared"
    digest = hashlib.sha256(payload).hexdigest()
    part = tmp_path / "packs" / "prepared-1.part"
    part.parent.mkdir(parents=True)
    part.write_bytes(payload)
    canonical = tmp_path / "objects" / f"file-{digest}"
    canonical.parent.mkdir(parents=True)
    target = tmp_path / "outside" / f"file-{digest}"
    target.parent.mkdir()
    if link_target_exists:
        target.write_bytes(payload)  # 同摘要：旧实现会跟随链接并复用，再删掉 part。
    canonical.symlink_to(target)

    with pytest.raises(DomainError) as caught:
        await store.install_prepared("packs/prepared-1.part", digest)

    assert caught.value.code is ErrorCode.INVALID_STATE
    assert part.read_bytes() == payload, "拒绝规范位符号链接时不得删除 part"
    assert canonical.is_symlink(), "失效或匹配的规范位符号链接都不得被覆盖或复用"
    assert canonical.readlink() == target
    if link_target_exists:
        assert target.read_bytes() == payload


async def test_remove_prepared_is_idempotent_for_missing_and_symlinks(tmp_path: Path) -> None:
    store = DownloadContentStore(tmp_path)
    await store.remove_prepared("packs/prepared-9.part")  # 不存在：幂等成功。

    target = tmp_path / "outside"
    target.write_bytes(b"x")
    part = tmp_path / "packs" / "prepared-9.part"
    part.parent.mkdir(parents=True)
    part.symlink_to(target)
    await store.remove_prepared("packs/prepared-9.part")
    assert not part.exists()
    assert target.exists()


async def test_verify_prepared_distinguishes_missing_from_non_regular_entries(
    tmp_path: Path,
) -> None:
    store = DownloadContentStore(tmp_path)
    part = tmp_path / "packs" / "prepared-1.part"
    part.parent.mkdir(parents=True)
    payload = b"prepared payload"
    part.write_bytes(payload)

    assert await store.verify_prepared("packs/prepared-1.part") == (
        len(payload),
        hashlib.sha256(payload).hexdigest(),
    )

    with pytest.raises(DomainError) as caught:
        await store.verify_prepared("packs/prepared-2.part")
    assert caught.value.code is ErrorCode.NOT_FOUND, "真实不存在才允许按缺失处理"

    part.unlink()
    part.mkdir()
    with pytest.raises(DomainError) as caught:
        await store.verify_prepared("packs/prepared-1.part")
    assert caught.value.code is ErrorCode.INVALID_STATE, "目录不得伪装成缺失"

    part.rmdir()
    external = tmp_path / "outside.bin"
    external.write_bytes(b"outside")
    part.symlink_to(external)
    with pytest.raises(DomainError) as caught:
        await store.verify_prepared("packs/prepared-1.part")
    assert caught.value.code is ErrorCode.INVALID_STATE, "符号链接不得伪装成缺失"
    assert external.read_bytes() == b"outside"

    part.unlink()
    (tmp_path / "packs" / "prepared-3.part").mkdir()
    with pytest.raises(DomainError) as caught:
        await store.verify_prepared("packs/prepared-3.part")
    assert caught.value.code is ErrorCode.INVALID_STATE

MIB = 1024 * 1024


def _write_canonical_file(store_root: Path, payload: bytes) -> str:
    digest = hashlib.sha256(payload).hexdigest()
    target = store_root / "objects" / f"file-{digest}"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return f"objects/file-{digest}"


def _write_canonical_directory(
    store_root: Path, files: dict[str, bytes]
) -> tuple[str, list[ContentEntryIdentity]]:
    entries = [
        ContentEntryIdentity(
            name, ContentKind.FILE, len(payload), hashlib.sha256(payload).hexdigest()
        )
        for name, payload in files.items()
    ]
    digest = directory_content_digest(entries)
    target = store_root / "objects" / f"directory-{digest}"
    target.mkdir(parents=True, exist_ok=True)
    for name, payload in files.items():
        (target / name).write_bytes(payload)
    return f"objects/directory-{digest}", entries


@pytest.mark.parametrize(
    ("storage_path", "expected_kind"),
    [
        ("objects/file-" + "a" * 64, ContentKind.FILE),
        ("objects/directory-" + "0123456789abcdef" * 4, ContentKind.DIRECTORY),
    ],
)
def test_parse_canonical_path_accepts_canonical_names_without_touching_disk(
    tmp_path: Path, storage_path: str, expected_kind: ContentKind
) -> None:
    store = DownloadContentStore(tmp_path / "absent")
    kind, digest = store.parse_canonical_path(storage_path)
    assert kind is expected_kind
    assert digest == storage_path.rpartition("-")[2]


@pytest.mark.parametrize(
    "storage_path",
    [
        "objects/file-" + "A" * 64,
        "objects/file-" + "a" * 63,
        "objects/file-" + "a" * 65,
        "objects/file-" + "g" * 64,
        "objects/link-" + "a" * 64,
        "objects/file-" + "a" * 64 + "/nested",
        "objects/file-" + "a" * 64 + "/",
        "objects//file-" + "a" * 64,
        "objects/../objects/file-" + "a" * 64,
        "/objects/file-" + "a" * 64,
        "objects\\file-" + "a" * 64,
        "objects/file-" + "a" * 64 + "\x00",
        "file-" + "a" * 64,
        "",
    ],
)
def test_parse_canonical_path_rejects_anything_else(tmp_path: Path, storage_path: str) -> None:
    store = DownloadContentStore(tmp_path / "absent")
    with pytest.raises(DomainError) as caught:
        store.parse_canonical_path(storage_path)
    assert caught.value.code is ErrorCode.INVALID_INPUT


async def test_list_canonical_entries_counts_unrecognized_and_measures_sizes(
    tmp_path: Path,
) -> None:
    payload = b"hello"
    storage_path = _write_canonical_file(tmp_path, payload)
    directory_path, _entries = _write_canonical_directory(tmp_path, {"child.bin": b"child"})
    objects = tmp_path / "objects"
    (objects / "file-not-canonical").write_bytes(b"x")
    (objects / f"file-{'d' * 64}").symlink_to(tmp_path / "outside")
    store = DownloadContentStore(tmp_path)

    entries, unrecognized = await store.list_canonical_entries()

    assert unrecognized == 2, "命名不符与符号链接都不计入清单"
    assert sorted(entry.storage_path for entry in entries) == sorted(
        [storage_path, directory_path]
    )
    by_path = {entry.storage_path: entry for entry in entries}
    file_entry = by_path[storage_path]
    assert (file_entry.kind, file_entry.size) == (ContentKind.FILE, len(payload))
    assert file_entry.digest == storage_path.rpartition("-")[2]
    assert file_entry.modified_at_ms > 0
    directory_entry = by_path[directory_path]
    assert (directory_entry.kind, directory_entry.size) == (ContentKind.DIRECTORY, 5)


async def test_list_canonical_entries_returns_empty_when_objects_is_unusable(
    tmp_path: Path,
) -> None:
    store = DownloadContentStore(tmp_path)
    assert await store.list_canonical_entries() == ([], 0)

    (tmp_path / "actual").mkdir()
    (tmp_path / "objects").symlink_to(tmp_path / "actual", target_is_directory=True)
    assert await store.list_canonical_entries() == ([], 0), "objects 是符号链接时不跟随"


async def test_list_canonical_entries_skips_entries_vanished_mid_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    objects = tmp_path / "objects"
    objects.mkdir()
    kept = objects / f"file-{'a' * 64}"
    kept.write_bytes(b"keep")
    vanished = objects / f"file-{'b' * 64}"
    vanished.write_bytes(b"gone")
    real_lstat = os.lstat

    def racing_lstat(path: object) -> os.stat_result:
        if Path(str(path)).name == vanished.name:
            raise FileNotFoundError(str(path))
        return real_lstat(path)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "lstat", racing_lstat)
    store = DownloadContentStore(tmp_path)

    entries, unrecognized = await store.list_canonical_entries()

    assert [entry.storage_path for entry in entries] == [f"objects/file-{'a' * 64}"]
    assert unrecognized == 0, "竞态消失的条目既不进清单也不算不规范"


async def test_stat_canonical_reads_metadata_and_rejects_links_and_escapes(
    tmp_path: Path,
) -> None:
    payload = b"hello"
    storage_path = _write_canonical_file(tmp_path, payload)
    directory_path, _entries = _write_canonical_directory(
        tmp_path, {"a.bin": b"12345", "b.bin": b"678"}
    )
    store = DownloadContentStore(tmp_path)

    assert (await store.stat_canonical(storage_path)).size == len(payload)
    assert (await store.stat_canonical(directory_path)).size == 8

    with pytest.raises(DomainError) as caught:
        await store.stat_canonical(f"objects/file-{'c' * 64}")
    assert caught.value.code is ErrorCode.NOT_FOUND

    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    link = tmp_path / "objects" / f"file-{'e' * 64}"
    link.symlink_to(outside)
    with pytest.raises(DomainError) as caught:
        await store.stat_canonical(f"objects/file-{'e' * 64}")
    assert caught.value.code is ErrorCode.INVALID_INPUT
    assert outside.read_bytes() == b"outside", "拒绝时不得读取链接指向的内容"

    actual = tmp_path / "actual-objects"
    actual.mkdir()
    outside_dir = tmp_path / "escaped"
    outside_dir.mkdir()
    (tmp_path / "objects").rename(tmp_path / "objects-moved")
    (tmp_path / "objects").symlink_to(actual, target_is_directory=True)
    with pytest.raises(DomainError) as caught:
        await store.stat_canonical(f"objects/file-{'f' * 64}")
    assert caught.value.code is ErrorCode.INVALID_INPUT


async def test_stat_canonical_ignores_non_regular_files_when_measuring(tmp_path: Path) -> None:
    directory_path, _entries = _write_canonical_directory(tmp_path, {"a.bin": b"123"})
    (tmp_path / directory_path / "link").symlink_to(tmp_path / "outside")
    os.mkfifo(tmp_path / directory_path / "pipe")

    assert (await DownloadContentStore(tmp_path).stat_canonical(directory_path)).size == 3


async def test_stat_canonical_ignores_files_vanished_while_measuring_a_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory_path, _entries = _write_canonical_directory(
        tmp_path, {"kept.bin": b"12345", "gone.bin": b"xx"}
    )
    real_lstat = os.lstat

    def racing_lstat(path: object) -> os.stat_result:
        if Path(str(path)).name == "gone.bin":
            raise FileNotFoundError(str(path))
        return real_lstat(path)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "lstat", racing_lstat)

    assert (await DownloadContentStore(tmp_path).stat_canonical(directory_path)).size == 5


async def test_verify_canonical_hashes_a_file_with_cumulative_progress(tmp_path: Path) -> None:
    payload = b"a" * (20 * MIB)
    storage_path = _write_canonical_file(tmp_path, payload)
    store = DownloadContentStore(tmp_path)
    reported: list[int] = []

    measured, entries = await store.verify_canonical(storage_path, on_progress=reported.append)

    assert entries == []
    assert (measured.kind, measured.size) == (ContentKind.FILE, len(payload))
    # 每读一块（1 MiB）就回调一次累计字节：停止指令靠它尽早生效，小文件也必须能中断
    assert reported == [MIB * step for step in range(1, 21)], "进度按块报告"
    assert reported[-2] < len(payload), "读到最后一块之前就已能收到停止指令"
    without_progress, _ = await store.verify_canonical(storage_path)
    assert without_progress.size == measured.size


async def test_verify_canonical_keeps_directory_identity_for_a_single_file(tmp_path: Path) -> None:
    payload = b"only"
    storage_path, expected_entries = _write_canonical_directory(tmp_path, {"only.bin": payload})
    store = DownloadContentStore(tmp_path)

    measured, entries = await store.verify_canonical(storage_path)

    assert measured.kind is ContentKind.DIRECTORY, "目录型对象即使只有一个文件也不得判成单文件"
    assert measured.size == len(payload)
    assert entries == expected_entries


async def test_verify_canonical_lists_subdirectories_in_the_manifest(tmp_path: Path) -> None:
    payload = b"abc"
    expected = [
        ContentEntryIdentity("sub", ContentKind.DIRECTORY, 0),
        ContentEntryIdentity(
            "sub/a.bin", ContentKind.FILE, len(payload), hashlib.sha256(payload).hexdigest()
        ),
    ]
    storage_path = f"objects/directory-{directory_content_digest(expected)}"
    (tmp_path / storage_path / "sub").mkdir(parents=True)
    (tmp_path / storage_path / "sub" / "a.bin").write_bytes(payload)

    measured, scanned = await DownloadContentStore(tmp_path).verify_canonical(storage_path)

    assert scanned == expected
    assert measured.size == len(payload)


async def test_verify_canonical_accepts_an_empty_directory_and_accumulates_progress(
    tmp_path: Path,
) -> None:
    store = DownloadContentStore(tmp_path)
    empty_digest = directory_content_digest([])
    (tmp_path / "objects" / f"directory-{empty_digest}").mkdir(parents=True)
    measured, entries = await store.verify_canonical(f"objects/directory-{empty_digest}")
    assert (measured.size, entries) == (0, [])

    chunk = b"a" * (5 * MIB)
    storage_path, _expected = _write_canonical_directory(
        tmp_path, {"a.bin": chunk, "b.bin": chunk}
    )
    reported: list[int] = []
    await store.verify_canonical(storage_path, on_progress=reported.append)
    assert reported == [MIB * step for step in range(1, 11)], "多文件跨文件连续累计"

    # 小于一块的条目也要先回调一次，否则点「停止」无法中断它
    small = b"a" * (1024 * 1024)
    small_path, _entries = _write_canonical_directory(tmp_path, {"s.bin": small})
    small_reported: list[int] = []
    await store.verify_canonical(small_path, on_progress=small_reported.append)
    assert small_reported == [len(small)]


async def test_verify_canonical_rejects_a_directory_whose_manifest_differs(tmp_path: Path) -> None:
    store = DownloadContentStore(tmp_path)
    storage_path, _expected = _write_canonical_directory(tmp_path, {"a.bin": b"1234567"})
    (tmp_path / storage_path / "a.bin").write_bytes(b"7654321")  # 同大小、不同内容

    with pytest.raises(DomainError) as caught:
        await store.verify_canonical(storage_path)

    assert caught.value.code is ErrorCode.INVALID_STATE


async def test_verify_canonical_rejects_digest_and_kind_mismatches(tmp_path: Path) -> None:
    store = DownloadContentStore(tmp_path)
    wrong_digest = hashlib.sha256(b"other").hexdigest()
    rewritten = tmp_path / "objects" / f"file-{wrong_digest}"
    rewritten.parent.mkdir(parents=True)
    rewritten.write_bytes(b"real")
    with pytest.raises(DomainError) as caught:
        await store.verify_canonical(f"objects/file-{wrong_digest}")
    assert caught.value.code is ErrorCode.INVALID_STATE

    (tmp_path / "objects" / f"file-{'1' * 64}").mkdir()
    with pytest.raises(DomainError) as caught:
        await store.verify_canonical(f"objects/file-{'1' * 64}")
    assert caught.value.code is ErrorCode.INVALID_INPUT, "file- 前缀却是目录：类型不符"

    (tmp_path / "objects" / f"directory-{'2' * 64}").write_bytes(b"x")
    with pytest.raises(DomainError) as caught:
        await store.verify_canonical(f"objects/directory-{'2' * 64}")
    assert caught.value.code is ErrorCode.INVALID_INPUT, "directory- 前缀却是文件：类型不符"

    with pytest.raises(DomainError) as caught:
        await store.verify_canonical(f"objects/file-{'3' * 64}")
    assert caught.value.code is ErrorCode.NOT_FOUND


@pytest.mark.parametrize(
    ("case", "expected_code"),
    [
        ("control", ErrorCode.DOWNLOAD_UNAVAILABLE),
        ("symlink", ErrorCode.INVALID_INPUT),
        ("symlink-dir", ErrorCode.INVALID_INPUT),
        ("fifo", ErrorCode.INVALID_INPUT),
    ],
)
async def test_verify_canonical_rejects_unsafe_directory_contents(
    tmp_path: Path, case: str, expected_code: ErrorCode
) -> None:
    store = DownloadContentStore(tmp_path)
    storage_path = f"objects/directory-{'4' * 64}"
    target = tmp_path / storage_path
    target.mkdir(parents=True)
    (target / "payload.bin").write_bytes(b"data")
    if case == "control":
        (target / "payload.bin.aria2").write_bytes(b"control")
    elif case == "symlink":
        (target / "link").symlink_to(tmp_path / "outside")
    elif case == "symlink-dir":
        (tmp_path / "outside-dir").mkdir()
        (target / "link").symlink_to(tmp_path / "outside-dir", target_is_directory=True)
    else:
        os.mkfifo(target / "pipe")

    with pytest.raises(DomainError) as caught:
        await store.verify_canonical(storage_path)
    assert caught.value.code is expected_code


def test_ingestion_has_a_single_manifest_driven_entry_point() -> None:
    """单一路径锚定：入库只能“读清单 → collect/install”，没有扫磁盘发现内容的退路。"""
    from aria2deck.application.downloads.lifecycle import ContentStore

    for name in ("scan", "sweep_control_files", "_scan", "_sweep_control_files"):
        assert not hasattr(DownloadContentStore, name), f"入库入口不得保留 {name}"
        assert not hasattr(ContentStore, name), f"生命周期协议不得保留 {name}"
    assert {"collect", "install", "cleanup_attempt", "machine_free_bytes"} <= set(dir(ContentStore))
