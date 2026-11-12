from __future__ import annotations

import asyncio
import hashlib
import io
import os
import random
import tarfile
import threading
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest
import zstandard

from aria2deck.application.packs.models import ArchiveSource
from aria2deck.domain.content import ContentKind
from aria2deck.domain.errors import DomainError, ErrorCode
from aria2deck.infrastructure import archive_writer as archive_writer_module
from aria2deck.infrastructure.archive_writer import ArchiveWriter, loop_bridge
from aria2deck.infrastructure.pack_writer import PackWriterControl

LARGE_BUDGET = 1 << 30


def _digest_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _make_tree(root: Path) -> dict[str, bytes]:
    files = {
        "one.bin": b"a" * 10,
        "dir/nested/two.bin": b"b" * 3000,
        "dir/empty/deep.txt": b"",
    }
    for relative, payload in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    (root / "dir" / "empty-dir").mkdir(parents=True)
    return files


def _expected_archive(prefix: str, root: Path, kind: ContentKind) -> dict[str, bytes | None]:
    """Independent expectation built from the source tree, not from the writer."""
    if kind is ContentKind.FILE:
        return {prefix: root.read_bytes()}
    expected: dict[str, bytes | None] = {f"{prefix}/": None}
    for parent, directories, names in os.walk(root):
        parent_path = Path(parent)
        for name in directories:
            relative = (parent_path / name).relative_to(root).as_posix()
            expected[f"{prefix}/{relative}/"] = None
        for name in names:
            relative = (parent_path / name).relative_to(root).as_posix()
            expected[f"{prefix}/{relative}"] = (parent_path / name).read_bytes()
    return expected


def _roundtrip_source(tmp_path: Path, case: str) -> tuple[ArchiveSource, dict[str, bytes | None]]:
    root = tmp_path / case
    root.mkdir()
    if case == "compressible":
        target = root / "log.txt"
        target.write_bytes(b"a" * (256 * 1024))
        return ArchiveSource("log.txt", target, ContentKind.FILE), _expected_archive(
            "log.txt", target, ContentKind.FILE
        )
    if case == "incompressible":
        target = root / "noise.bin"
        # 测试数据只需不可压缩，不需要密码学随机性。
        target.write_bytes(random.Random(7).randbytes(256 * 1024))  # noqa: S311
        return ArchiveSource("noise.bin", target, ContentKind.FILE), _expected_archive(
            "noise.bin", target, ContentKind.FILE
        )
    if case == "metadata":
        for index in range(60):
            target = root / f"d{index % 4}" / f"e{index % 3}" / f"f{index}.txt"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(bytes([index % 251]) * (index % 5))
        return ArchiveSource("许多文件", root, ContentKind.DIRECTORY), _expected_archive(
            "许多文件", root, ContentKind.DIRECTORY
        )
    if case == "empty":
        return ArchiveSource("空目录", root, ContentKind.DIRECTORY), _expected_archive(
            "空目录", root, ContentKind.DIRECTORY
        )
    raise AssertionError(f"unknown case {case}")


def _read_archive(path: Path, pack_format: str) -> dict[str, bytes | None]:
    if pack_format == "zip":
        with zipfile.ZipFile(path) as archive:
            return {
                info.filename: None if info.is_dir() else archive.read(info)
                for info in archive.infolist()
            }
    decompressed = zstandard.ZstdDecompressor().stream_reader(path.open("rb")).read()
    with tarfile.open(fileobj=io.BytesIO(decompressed), mode="r:") as archive:
        contents: dict[str, bytes | None] = {}
        for member in archive.getmembers():
            if member.isdir():
                contents[f"{member.name}/"] = None
                continue
            extracted = archive.extractfile(member)
            assert extracted is not None
            contents[member.name] = extracted.read()
        return contents


class _ScandirSpy:
    """Counts yielded entries and iterator closures while keeping scandir semantics."""

    def __init__(self) -> None:
        self._original = os.scandir
        self.yielded = 0
        self.opened = 0
        self.closed = 0

    def __call__(self, path: str | os.PathLike[str]) -> _ScandirSpyIterator:
        self.opened += 1
        return _ScandirSpyIterator(self, self._original(path))


class _ScandirSpyIterator:
    def __init__(self, spy: _ScandirSpy, iterator: Iterator[os.DirEntry[str]]) -> None:
        self._spy = spy
        self._iterator = iterator

    def __enter__(self) -> _ScandirSpyIterator:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._iterator.close()
        self._spy.closed += 1

    def __iter__(self) -> Iterator[os.DirEntry[str]]:
        for entry in self._iterator:
            self._spy.yielded += 1
            yield entry


class _SinkSpy:
    """Records every attempted sink write as (offset, length)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.attempts: list[tuple[int, int]] = []
        original = archive_writer_module._BudgetedSink.write

        def recording(sink, data):  # type: ignore[no-untyped-def]
            self.attempts.append((sink.tell(), len(data)))
            return original(sink, data)

        monkeypatch.setattr(archive_writer_module._BudgetedSink, "write", recording)


async def test_zip_archive_roundtrip_progress_and_digest(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    files = _make_tree(source_root)
    writer = ArchiveWriter(chunk_size=1024)
    destination = tmp_path / "out" / "pack.zip"
    progress: list[int] = []

    result = await writer.write_archive(
        destination,
        sources=[
            ArchiveSource("lone file", source_root / "one.bin", ContentKind.FILE),
            ArchiveSource("资料", source_root / "dir", ContentKind.DIRECTORY),
        ],
        pack_format="zip",
        compression_level=6,
        max_output_bytes=LARGE_BUDGET,
        min_free_bytes=0,
        cancel_event=threading.Event(),
        on_progress=progress.append,
    )

    assert result.size == destination.stat().st_size
    assert result.digest == _digest_of(destination)
    with zipfile.ZipFile(destination) as archive:
        assert set(archive.namelist()) == {
            "lone file",
            "资料/",
            "资料/nested/",
            "资料/nested/two.bin",
            "资料/empty/",
            "资料/empty/deep.txt",
            "资料/empty-dir/",
        }
        assert archive.read("lone file") == files["one.bin"]
        assert archive.read("资料/nested/two.bin") == files["dir/nested/two.bin"]
        assert archive.read("资料/empty/deep.txt") == b""
    assert progress[0] == 0
    assert progress[-1] == 100
    assert progress == sorted(progress), "进度必须单调不减"


async def test_zip_level_zero_uses_stored_entries(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    _make_tree(source_root)
    writer = ArchiveWriter()
    destination = tmp_path / "stored.zip"

    await writer.write_archive(
        destination,
        sources=[ArchiveSource("one.bin", source_root / "one.bin", ContentKind.FILE)],
        pack_format="zip",
        compression_level=0,
        max_output_bytes=LARGE_BUDGET,
        min_free_bytes=0,
        cancel_event=threading.Event(),
    )
    with zipfile.ZipFile(destination) as archive:
        info = archive.getinfo("one.bin")
        assert info.compress_type == zipfile.ZIP_STORED
        assert info.file_size == info.compress_size


async def test_tar_zst_roundtrip_chunked_progress_and_digest(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    _make_tree(source_root)
    payload = bytes(range(256)) * (1024 * 1024 // 256)  # 1 MiB
    big = source_root / "big.bin"
    big.write_bytes(payload)
    writer = ArchiveWriter(chunk_size=256 * 1024)
    destination = tmp_path / "pack.tar.zst"
    progress: list[int] = []

    result = await writer.write_archive(
        destination,
        sources=[ArchiveSource("big.bin", big, ContentKind.FILE)],
        pack_format="tar.zst",
        compression_level=3,
        max_output_bytes=LARGE_BUDGET,
        min_free_bytes=0,
        cancel_event=threading.Event(),
        on_progress=progress.append,
    )

    assert result.digest == _digest_of(destination)
    decompressed = zstandard.ZstdDecompressor().stream_reader(
        destination.open("rb")
    ).read()

    with tarfile.open(fileobj=io.BytesIO(decompressed), mode="r:") as archive:
        member = archive.extractfile("big.bin")
        assert member is not None
        assert member.read() == payload
    # 1 MiB 源以 256 KiB 块写入：中间进度必须有非 0/100 的档位。
    assert any(0 < value < 100 for value in progress)
    assert progress == sorted(progress)


async def test_writer_rejects_symlinks_and_invalid_formats(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    _make_tree(source_root)
    outside = tmp_path / "outside"
    outside.write_bytes(b"secret")
    linked = source_root / "one.bin"
    real = source_root / "one.real"
    linked.replace(real)
    linked.symlink_to(outside)
    writer = ArchiveWriter()

    with pytest.raises(DomainError) as caught:
        await writer.write_archive(
            tmp_path / "out.zip",
            sources=[ArchiveSource("link", linked, ContentKind.FILE)],
            pack_format="zip",
            compression_level=3,
            max_output_bytes=LARGE_BUDGET,
            min_free_bytes=0,
            cancel_event=threading.Event(),
        )
    assert caught.value.code is ErrorCode.INVALID_INPUT

    nested_link = source_root / "dir" / "nested-link"
    nested_link.symlink_to(outside)
    with pytest.raises(DomainError) as caught:
        await writer.write_archive(
            tmp_path / "out2.zip",
            sources=[ArchiveSource("dir", source_root / "dir", ContentKind.DIRECTORY)],
            pack_format="zip",
            compression_level=3,
            max_output_bytes=LARGE_BUDGET,
            min_free_bytes=0,
            cancel_event=threading.Event(),
        )
    assert caught.value.code is ErrorCode.INVALID_INPUT

    with pytest.raises(DomainError) as caught:
        await writer.write_archive(
            tmp_path / "out3.zip",
            sources=[ArchiveSource("one", real, ContentKind.FILE)],
            pack_format="rar",
            compression_level=3,
            max_output_bytes=LARGE_BUDGET,
            min_free_bytes=0,
            cancel_event=threading.Event(),
        )
    assert caught.value.code is ErrorCode.INVALID_INPUT


async def test_writer_reports_zero_and_hundred_for_empty_sources(tmp_path: Path) -> None:
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    writer = ArchiveWriter()
    progress: list[int] = []

    result = await writer.write_archive(
        tmp_path / "empty.zip",
        sources=[ArchiveSource("empty", empty_dir, ContentKind.DIRECTORY)],
        pack_format="zip",
        compression_level=3,
        max_output_bytes=LARGE_BUDGET,
        min_free_bytes=0,
        cancel_event=threading.Event(),
        on_progress=progress.append,
    )
    assert result.size > 0  # 空目录 zip 仍有结构字节
    assert progress == [0, 100]
    with zipfile.ZipFile(tmp_path / "empty.zip") as archive:
        assert archive.namelist() == ["empty/"]


async def test_writer_missing_source_root_is_rejected(tmp_path: Path) -> None:
    writer = ArchiveWriter()
    with pytest.raises(DomainError) as caught:
        await writer.write_archive(
            tmp_path / "out.zip",
            sources=[ArchiveSource("gone", tmp_path / "gone.bin", ContentKind.FILE)],
            pack_format="zip",
            compression_level=3,
            max_output_bytes=LARGE_BUDGET,
            min_free_bytes=0,
            cancel_event=threading.Event(),
        )
    assert caught.value.code is ErrorCode.INVALID_STATE


async def test_loop_bridge_invokes_async_callback_from_thread(tmp_path: Path) -> None:
    seen: list[int] = []

    async def on_progress(percent: int) -> None:
        seen.append(percent)

    async def main() -> None:
        loop = asyncio.get_running_loop()
        bridge = loop_bridge(on_progress, loop)
        await asyncio.to_thread(bridge, 37)

    await main()
    assert seen == [37]


async def test_writer_rejects_out_of_range_level_and_missing_directory(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    _make_tree(source_root)
    writer = ArchiveWriter()
    with pytest.raises(DomainError) as caught:
        await writer.write_archive(
            tmp_path / "out.zip",
            sources=[ArchiveSource("one", source_root / "one.bin", ContentKind.FILE)],
            pack_format="zip",
            compression_level=23,
            max_output_bytes=LARGE_BUDGET,
            min_free_bytes=0,
            cancel_event=threading.Event(),
        )
    assert caught.value.code is ErrorCode.INVALID_INPUT
    with pytest.raises(DomainError) as caught:
        await writer.write_archive(
            tmp_path / "out2.zip",
            sources=[ArchiveSource("gone", tmp_path / "gone-dir", ContentKind.DIRECTORY)],
            pack_format="zip",
            compression_level=3,
            max_output_bytes=LARGE_BUDGET,
            min_free_bytes=0,
            cancel_event=threading.Event(),
        )
    assert caught.value.code is ErrorCode.INVALID_STATE


async def test_tar_zst_includes_directory_entries(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    files = _make_tree(source_root)
    writer = ArchiveWriter()
    destination = tmp_path / "dir.tar.zst"

    await writer.write_archive(
        destination,
        sources=[ArchiveSource("资料", source_root / "dir", ContentKind.DIRECTORY)],
        pack_format="tar.zst",
        compression_level=1,
        max_output_bytes=LARGE_BUDGET,
        min_free_bytes=0,
        cancel_event=threading.Event(),
    )
    decompressed = zstandard.ZstdDecompressor().stream_reader(destination.open("rb")).read()
    with tarfile.open(fileobj=io.BytesIO(decompressed), mode="r:") as archive:
        names = archive.getnames()
        nested = archive.extractfile("资料/nested/two.bin")
        members = archive.getmembers()
        assert nested is not None
        assert nested.read() == files["dir/nested/two.bin"]
    assert "资料" in names and "资料/empty-dir" in names
    assert any(member.isdir() for member in members)


@pytest.mark.parametrize("pack_format", ["zip", "tar.zst"])
@pytest.mark.parametrize("case", ["compressible", "incompressible", "metadata", "empty"])
async def test_archive_roundtrip_for_every_input_shape(
    tmp_path: Path, case: str, pack_format: str
) -> None:
    source, expected = _roundtrip_source(tmp_path, case)
    destination = tmp_path / f"out-{pack_format.replace('.', '-')}.bin"

    result = await ArchiveWriter(chunk_size=32 * 1024).write_archive(
        destination,
        sources=[source],
        pack_format=pack_format,
        compression_level=3,
        max_output_bytes=LARGE_BUDGET,
        min_free_bytes=0,
        cancel_event=threading.Event(),
    )

    assert result.size == destination.stat().st_size
    assert result.digest == _digest_of(destination)
    assert _read_archive(destination, pack_format) == expected


@pytest.mark.parametrize("pack_format", ["zip", "tar.zst"])
async def test_output_budget_allows_exact_size_and_rejects_one_byte_less(
    tmp_path: Path, pack_format: str
) -> None:
    target = tmp_path / "source" / "data.bin"
    target.parent.mkdir()
    target.write_bytes(b"exact-budget" * 1024)
    source = ArchiveSource("data.bin", target, ContentKind.FILE)
    writer = ArchiveWriter(chunk_size=4096)
    level = 0 if pack_format == "zip" else 3

    measured = await writer.write_archive(
        tmp_path / "measured.bin",
        sources=[source],
        pack_format=pack_format,
        compression_level=level,
        max_output_bytes=LARGE_BUDGET,
        min_free_bytes=0,
        cancel_event=threading.Event(),
    )
    exact = await writer.write_archive(
        tmp_path / "exact.bin",
        sources=[source],
        pack_format=pack_format,
        compression_level=level,
        max_output_bytes=measured.size,
        min_free_bytes=0,
        cancel_event=threading.Event(),
    )
    assert exact.size == measured.size

    with pytest.raises(DomainError) as caught:
        await writer.write_archive(
            tmp_path / "short.bin",
            sources=[source],
            pack_format=pack_format,
            compression_level=level,
            max_output_bytes=measured.size - 1,
            min_free_bytes=0,
            cancel_event=threading.Event(),
        )
    assert caught.value.code is ErrorCode.TASK_TOO_LARGE


async def test_zip_header_rewrites_and_central_directory_share_the_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "src" / "a.bin"
    target.parent.mkdir()
    payload = b"12345"
    target.write_bytes(payload)
    source = ArchiveSource("a.bin", target, ContentKind.FILE)
    writer = ArchiveWriter()
    # STORED 布局：local header 30+5、数据 5、central directory 46+5、EOCD 22。
    local_end = 30 + len("a.bin") + len(payload)
    central_end = local_end + 46 + len("a.bin")
    final_size = central_end + 22

    result = await writer.write_archive(
        tmp_path / "exact.zip",
        sources=[source],
        pack_format="zip",
        compression_level=0,
        max_output_bytes=final_size,
        min_free_bytes=0,
        cancel_event=threading.Event(),
    )
    assert result.size == final_size

    spy = _SinkSpy(monkeypatch)
    with pytest.raises(DomainError) as caught:
        await writer.write_archive(
            tmp_path / "central.zip",
            sources=[source],
            pack_format="zip",
            compression_level=0,
            max_output_bytes=central_end - 1,
            min_free_bytes=0,
            cancel_event=threading.Event(),
        )
    assert caught.value.code is ErrorCode.TASK_TOO_LARGE
    # 数据与本地头回写已完成，central directory 的最后一段（文件名）被拒。
    assert spy.attempts[-1] == (central_end - len("a.bin"), len("a.bin"))
    assert (tmp_path / "central.zip").stat().st_size == central_end - len("a.bin")

    spy.attempts.clear()
    with pytest.raises(DomainError) as caught:
        await writer.write_archive(
            tmp_path / "eocd.zip",
            sources=[source],
            pack_format="zip",
            compression_level=0,
            max_output_bytes=final_size - 1,
            min_free_bytes=0,
            cancel_event=threading.Event(),
        )
    assert caught.value.code is ErrorCode.TASK_TOO_LARGE
    # 只差最后 22 字节 EOCD：它也必须经过同一个有界 sink。
    assert spy.attempts[-1] == (central_end, 22)
    assert (tmp_path / "eocd.zip").stat().st_size == central_end


async def test_tar_zst_close_tail_cannot_bypass_the_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "src" / "data.bin"
    target.parent.mkdir()
    target.write_bytes(b"tail" * 4096)
    source = ArchiveSource("data.bin", target, ContentKind.FILE)
    writer = ArchiveWriter(chunk_size=64 * 1024)
    measured = await writer.write_archive(
        tmp_path / "measured.tar.zst",
        sources=[source],
        pack_format="tar.zst",
        compression_level=3,
        max_output_bytes=LARGE_BUDGET,
        min_free_bytes=0,
        cancel_event=threading.Event(),
    )

    spy = _SinkSpy(monkeypatch)
    with pytest.raises(DomainError) as caught:
        await writer.write_archive(
            tmp_path / "short.tar.zst",
            sources=[source],
            pack_format="tar.zst",
            compression_level=3,
            max_output_bytes=measured.size - 1,
            min_free_bytes=0,
            cancel_event=threading.Event(),
        )

    assert caught.value.code is ErrorCode.TASK_TOO_LARGE
    assert spy.attempts[-1][0] + spy.attempts[-1][1] == measured.size
    assert (tmp_path / "short.tar.zst").stat().st_size < measured.size


@pytest.mark.parametrize("pack_format", ["zip", "tar.zst"])
async def test_close_failure_does_not_replace_the_primary_domain_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pack_format: str
) -> None:
    target = tmp_path / "src" / "data.bin"
    target.parent.mkdir()
    target.write_bytes(b"primary budget error")

    def fail_flush(_self: object) -> None:
        raise OSError("controlled close failure")

    monkeypatch.setattr(archive_writer_module._BudgetedSink, "flush", fail_flush)
    with pytest.raises(DomainError) as caught:
        await ArchiveWriter().write_archive(
            tmp_path / f"close-error-{pack_format.replace('.', '-')}.bin",
            sources=[ArchiveSource("data.bin", target, ContentKind.FILE)],
            pack_format=pack_format,
            compression_level=3,
            max_output_bytes=1,
            min_free_bytes=0,
            cancel_event=threading.Event(),
        )
    assert caught.value.code is ErrorCode.TASK_TOO_LARGE


def test_budgeted_sink_enforces_the_disk_floor_boundary_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(archive_writer_module, "_free_bytes", lambda path: 1000)
    destination = tmp_path / "sink.bin"

    with destination.open("wb") as raw:
        sink = archive_writer_module._BudgetedSink(
            raw,
            destination,
            max_output_bytes=10_000,
            min_free_bytes=900,
            cancel_event=threading.Event(),
        )
        assert sink.seekable() is True
        assert sink.fileno() == raw.fileno()
        sink.write(b"x" * 100)  # 1000 - 100 == 900：恰好等于下限必须通过。
        assert sink.tell() == 100
        assert sink.seek(0) == 0
        sink.flush()
        sink.close()
        sink.close()  # 重复关闭必须幂等。
    assert destination.stat().st_size == 100

    with destination.open("wb") as raw:
        sink = archive_writer_module._BudgetedSink(
            raw,
            destination,
            max_output_bytes=10_000,
            min_free_bytes=901,
            cancel_event=threading.Event(),
        )
        with pytest.raises(DomainError) as caught:
            sink.write(b"x" * 100)  # 低 1 字节必须失败。
        assert caught.value.code is ErrorCode.PHYSICAL_SPACE_EXCEEDED
        sink.close()


@pytest.mark.parametrize("pack_format", ["zip", "tar.zst"])
async def test_disk_floor_is_enforced_for_both_formats(
    tmp_path: Path, pack_format: str
) -> None:
    target = tmp_path / "src" / "data.bin"
    target.parent.mkdir()
    target.write_bytes(b"floor" * 1024)
    source = ArchiveSource("data.bin", target, ContentKind.FILE)

    with pytest.raises(DomainError) as caught:
        await ArchiveWriter().write_archive(
            tmp_path / "out.bin",
            sources=[source],
            pack_format=pack_format,
            compression_level=3,
            max_output_bytes=LARGE_BUDGET,
            min_free_bytes=1 << 60,
            cancel_event=threading.Event(),
        )
    assert caught.value.code is ErrorCode.PHYSICAL_SPACE_EXCEEDED


async def test_plan_stops_at_the_first_entry_over_limit_and_closes_scandir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "many"
    root.mkdir()
    for index in range(20):
        (root / f"f{index:02d}.bin").write_bytes(b"x")
    monkeypatch.setattr("aria2deck.domain.packs.MAX_ARCHIVE_ENTRIES", 3)
    spy = _ScandirSpy()
    monkeypatch.setattr(archive_writer_module.os, "scandir", spy)

    with pytest.raises(DomainError) as caught:
        await ArchiveWriter().write_archive(
            tmp_path / "out.zip",
            sources=[ArchiveSource("many", root, ContentKind.DIRECTORY)],
            pack_format="zip",
            compression_level=3,
            max_output_bytes=LARGE_BUDGET,
            min_free_bytes=0,
            cancel_event=threading.Event(),
        )

    assert caught.value.code is ErrorCode.INVALID_INPUT
    assert spy.opened == spy.closed, "越界后必须关闭目录迭代器"
    # 来源根计 1 条，第 3 个文件把条目推到 4 > 3，后续条目不得再读取。
    assert spy.yielded == 3
    assert not (tmp_path / "out.zip").exists()


async def test_plan_checks_the_complete_utf8_path_before_adding_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("aria2deck.domain.packs.MAX_ARCHIVE_PATH_BYTES", 800)
    prefix = "目" * 200  # 200 字符 = 600 UTF-8 字节（显示名长度上限）。
    components = ["内" * 80]  # 80 字符 = 240 UTF-8 字节。
    root = tmp_path / "tree"
    current = root / components[0]
    current.mkdir(parents=True)
    (current / "leaf.txt").write_bytes(b"x")
    full_bytes = (
        len(prefix.encode("utf-8"))
        + 1
        + sum(len(component.encode("utf-8")) + 1 for component in components)
        + len("leaf.txt")
    )
    assert full_bytes > 800
    assert len(prefix) + 1 + sum(len(component) + 1 for component in components) + 9 < 800

    with pytest.raises(DomainError) as caught:
        await ArchiveWriter().write_archive(
            tmp_path / "too-deep.zip",
            sources=[ArchiveSource(prefix, root, ContentKind.DIRECTORY)],
            pack_format="zip",
            compression_level=3,
            max_output_bytes=LARGE_BUDGET,
            min_free_bytes=0,
            cancel_event=threading.Event(),
        )
    assert caught.value.code is ErrorCode.INVALID_INPUT
    assert not (tmp_path / "too-deep.zip").exists()

    # 对照：同样结构的 UTF-8 路径在总量以内必须可以真实写出。
    short_prefix = "目" * 60  # 180 字节前缀，完整路径仍在 800 字节内。
    destination = tmp_path / "utf8.zip"
    await ArchiveWriter().write_archive(
        destination,
        sources=[ArchiveSource(short_prefix, root, ContentKind.DIRECTORY)],
        pack_format="zip",
        compression_level=3,
        max_output_bytes=LARGE_BUDGET,
        min_free_bytes=0,
        cancel_event=threading.Event(),
    )
    with zipfile.ZipFile(destination) as archive:
        assert any(name.endswith("/leaf.txt") for name in archive.namelist())


async def test_plan_rejects_special_files_inside_a_directory(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    (root / "regular.bin").write_bytes(b"x")
    os.mkfifo(root / "pipe")

    with pytest.raises(DomainError) as caught:
        await ArchiveWriter().write_archive(
            tmp_path / "special.zip",
            sources=[ArchiveSource("tree", root, ContentKind.DIRECTORY)],
            pack_format="zip",
            compression_level=3,
            max_output_bytes=LARGE_BUDGET,
            min_free_bytes=0,
            cancel_event=threading.Event(),
        )
    assert caught.value.code is ErrorCode.INVALID_INPUT
    assert not (tmp_path / "special.zip").exists()


async def test_cancel_event_stops_the_sync_writer_with_interrupted_error(
    tmp_path: Path,
) -> None:
    target = tmp_path / "src" / "data.bin"
    target.parent.mkdir()
    target.write_bytes(b"cancel" * 1024 * 64)
    cancel_event = threading.Event()

    def on_progress(percent: int) -> None:
        if percent > 0:
            cancel_event.set()

    with pytest.raises(InterruptedError):
        await ArchiveWriter(chunk_size=16 * 1024).write_archive(
            tmp_path / "cancelled.zip",
            sources=[ArchiveSource("data.bin", target, ContentKind.FILE)],
            pack_format="zip",
            compression_level=3,
            max_output_bytes=LARGE_BUDGET,
            min_free_bytes=0,
            cancel_event=cancel_event,
            on_progress=on_progress,
        )
    assert cancel_event.is_set()


async def test_preset_cancel_event_stops_before_creating_output(tmp_path: Path) -> None:
    target = tmp_path / "src" / "data.bin"
    target.parent.mkdir()
    target.write_bytes(b"x" * 32)
    cancel_event = threading.Event()
    cancel_event.set()
    destination = tmp_path / "out.zip"

    with pytest.raises(InterruptedError):
        await ArchiveWriter().write_archive(
            destination,
            sources=[ArchiveSource("data.bin", target, ContentKind.FILE)],
            pack_format="zip",
            compression_level=3,
            max_output_bytes=LARGE_BUDGET,
            min_free_bytes=0,
            cancel_event=cancel_event,
        )
    assert not destination.exists()


@pytest.mark.parametrize("pack_format", ["zip", "tar.zst"])
async def test_late_cancel_after_begin_creates_no_output_file(
    tmp_path: Path, pack_format: str
) -> None:
    """停止请求落在 reporter.begin() 之后时，zip 与 tar.zst 都不得迟到创建输出。"""
    target = tmp_path / "src" / "data.bin"
    target.parent.mkdir()
    target.write_bytes(b"late cancel" * 64)
    destination = tmp_path / f"out.{pack_format}"
    cancel_event = threading.Event()
    entered = threading.Event()
    release = threading.Event()
    callback_returned = threading.Event()

    def on_progress(percent: int) -> None:
        if percent == 0 and not entered.is_set():
            entered.set()
            assert release.wait(10), "受控取消边界未释放"
            callback_returned.set()

    control = PackWriterControl(stop_timeout_seconds=10.0)
    writer_task = asyncio.create_task(
        ArchiveWriter(chunk_size=16).write_archive(
            destination,
            sources=[ArchiveSource("data.bin", target, ContentKind.FILE)],
            pack_format=pack_format,
            compression_level=3,
            max_output_bytes=LARGE_BUDGET,
            min_free_bytes=0,
            cancel_event=cancel_event,
            on_progress=on_progress,
        )
    )
    control.register(1, writer_task, cancel_event=cancel_event)
    assert await asyncio.to_thread(entered.wait, 10), "写线程必须进入 on_progress(0)"

    stop_task = asyncio.create_task(control.stop_and_wait(1))
    try:
        done, _ = await asyncio.wait({stop_task}, timeout=0.3)
        assert not done, "真实写线程退出前 stop_and_wait 不得返回"
        assert cancel_event.is_set(), "stop_and_wait 必须先设置协作取消事件"
        assert not destination.exists(), "取消边界阻塞期间不得提前创建输出"
    finally:
        release.set()

    await asyncio.wait_for(stop_task, 10)
    assert callback_returned.is_set(), "受控同步回调必须已返回"
    outcome = await asyncio.gather(writer_task, return_exceptions=True)
    assert isinstance(outcome[0], InterruptedError), "真实同步写入必须以中断退出"
    assert not destination.exists(), "取消先于创建输出：不得留下 0 字节输出或 part"
    await asyncio.sleep(0.1)
    assert not destination.exists(), "停止返回后不得迟到创建输出"


async def test_outer_cancel_returns_only_after_the_writer_thread_exits(
    tmp_path: Path,
) -> None:
    target = tmp_path / "src" / "big.bin"
    target.parent.mkdir()
    target.write_bytes(b"c" * (4 * 1024 * 1024))
    entered = threading.Event()
    release = threading.Event()
    observed: list[int] = []
    cancel_event = threading.Event()

    def on_progress(percent: int) -> None:
        observed.append(percent)
        if percent >= 25:
            entered.set()
            release.wait(10)

    destination = tmp_path / "out.zip"
    task = asyncio.create_task(
        ArchiveWriter(chunk_size=32 * 1024).write_archive(
            destination,
            sources=[ArchiveSource("big.bin", target, ContentKind.FILE)],
            pack_format="zip",
            compression_level=1,
            max_output_bytes=LARGE_BUDGET,
            min_free_bytes=0,
            cancel_event=cancel_event,
            on_progress=on_progress,
        )
    )
    assert await asyncio.to_thread(entered.wait, 5), "写线程必须进入真实写入"

    task.cancel()
    await asyncio.sleep(0.05)
    task.cancel()  # 第二次取消不得打断等待真实线程。
    await asyncio.sleep(0)
    assert not task.done(), "真实写线程未退出前不得返回"

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 10)

    assert cancel_event.is_set()
    size = destination.stat().st_size
    digest = _digest_of(destination)
    seen = len(observed)
    await asyncio.sleep(0.1)
    assert destination.stat().st_size == size, "取消返回后不得再有迟到写入"
    assert _digest_of(destination) == digest, "取消返回后摘要必须稳定"
    assert len(observed) == seen
    assert size < 4 * 1024 * 1024


async def test_source_change_mid_write_stays_an_io_error_not_cancellation(
    tmp_path: Path,
) -> None:
    target = tmp_path / "src" / "data.bin"
    target.parent.mkdir()
    target.write_bytes(b"d" * (512 * 1024))
    cancel_event = threading.Event()
    truncated = threading.Event()

    def on_progress(percent: int) -> None:
        if percent > 0 and not truncated.is_set():
            truncated.set()
            target.write_bytes(b"")

    with pytest.raises(OSError) as caught:
        await ArchiveWriter(chunk_size=64 * 1024).write_archive(
            tmp_path / "out.zip",
            sources=[ArchiveSource("data.bin", target, ContentKind.FILE)],
            pack_format="zip",
            compression_level=1,
            max_output_bytes=LARGE_BUDGET,
            min_free_bytes=0,
            cancel_event=cancel_event,
            on_progress=on_progress,
        )
    assert not isinstance(caught.value, DomainError | InterruptedError)
    assert not cancel_event.is_set()
