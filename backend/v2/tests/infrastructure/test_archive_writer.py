from __future__ import annotations

import hashlib
import tarfile
import zipfile
from pathlib import Path

import pytest
import zstandard

from aria2deck.application.packs.models import ArchiveSource
from aria2deck.domain.content import ContentKind
from aria2deck.domain.errors import DomainError, ErrorCode
from aria2deck.infrastructure.archive_writer import ArchiveWriter


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
        on_progress=progress.append,
    )

    assert result.digest == _digest_of(destination)
    decompressed = zstandard.ZstdDecompressor().stream_reader(
        destination.open("rb")
    ).read()
    import io

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
        )
    assert caught.value.code is ErrorCode.INVALID_INPUT

    with pytest.raises(DomainError) as caught:
        await writer.write_archive(
            tmp_path / "out3.zip",
            sources=[ArchiveSource("one", real, ContentKind.FILE)],
            pack_format="rar",
            compression_level=3,
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
        )
    assert caught.value.code is ErrorCode.INVALID_STATE


async def test_loop_bridge_invokes_async_callback_from_thread(tmp_path: Path) -> None:
    import asyncio

    from aria2deck.infrastructure.archive_writer import loop_bridge

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
        )
    assert caught.value.code is ErrorCode.INVALID_INPUT
    with pytest.raises(DomainError) as caught:
        await writer.write_archive(
            tmp_path / "out2.zip",
            sources=[ArchiveSource("gone", tmp_path / "gone-dir", ContentKind.DIRECTORY)],
            pack_format="zip",
            compression_level=3,
        )
    assert caught.value.code is ErrorCode.INVALID_STATE


async def test_tar_zst_includes_directory_entries(tmp_path: Path) -> None:
    import io
    import tarfile

    import zstandard

    source_root = tmp_path / "source"
    files = _make_tree(source_root)
    writer = ArchiveWriter()
    destination = tmp_path / "dir.tar.zst"

    await writer.write_archive(
        destination,
        sources=[ArchiveSource("资料", source_root / "dir", ContentKind.DIRECTORY)],
        pack_format="tar.zst",
        compression_level=1,
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
