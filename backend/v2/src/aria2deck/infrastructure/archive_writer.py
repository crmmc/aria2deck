from __future__ import annotations

import asyncio
import hashlib
import os
import tarfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Literal

import zstandard

from ..application.packs.models import ArchiveResult, ArchiveSource, ProgressCallback
from ..domain.content import ContentKind, validate_display_name
from ..domain.errors import DomainError, ErrorCode

CHUNK_SIZE = 1024 * 1024
PackFormat = Literal["zip", "tar.zst"]

# zstandard 无 0 级：0 表示不压缩，仅对 zip 有 STORED 语义，tar.zst 最低用 1。
_MIN_ZSTD_LEVEL = 1


@dataclass(frozen=True, slots=True)
class _ArchiveItem:
    arcname: str
    path: Path
    is_dir: bool
    size: int


def percent_of(written: int, total: int) -> int:
    return 100 if total == 0 else min(written * 100 // total, 100)


class _ProgressReporter:
    """Tracks bytes written and reports monotone 0..100 percentages to the loop."""

    def __init__(self, total: int, report: Callable[[int], None] | None) -> None:
        self.total = total
        self._report = report
        self._written = 0
        self._last_percent = -1

    def _emit(self, percent: int) -> None:
        if self._report is None:
            return
        if percent != self._last_percent:
            self._last_percent = percent
            self._report(percent)

    def begin(self) -> None:
        self._emit(0)

    def finish(self) -> None:
        self._emit(100)

    def advance(self, count: int) -> None:
        self._written += count
        self._emit(percent_of(self._written, self.total))


class _ReportingReader:
    """tarfile.addfile source wrapper counting bytes as they are copied."""

    def __init__(self, stream: IO[bytes], reporter: _ProgressReporter) -> None:
        self._stream = stream
        self._reporter = reporter
        self.counted = 0

    def read(self, size: int = -1) -> bytes:
        chunk = self._stream.read(size)
        self.counted += len(chunk)
        self._reporter.advance(len(chunk))
        return chunk


def _digest_file(path: Path, chunk_size: int) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def loop_bridge(on_progress: ProgressCallback, loop: asyncio.AbstractEventLoop) -> Callable[[int], None]:
    """Adapt an async progress callback into a sync one safe to call from the writer thread."""

    def bridge(percent: int) -> None:
        asyncio.run_coroutine_threadsafe(on_progress(percent), loop).result()

    return bridge


class ArchiveWriter:
    """Streams sources into zip / tar.zst archives with progress and final-byte digest.

    on_progress 在写入线程内同步调用（0..100 单调）；异步消费者用 loop_bridge 适配。
    """

    def __init__(self, *, chunk_size: int = CHUNK_SIZE) -> None:
        self.chunk_size = chunk_size

    async def write_archive(
        self,
        destination: Path,
        *,
        sources: list[ArchiveSource],
        pack_format: str,
        compression_level: int,
        on_progress: Callable[[int], None] | None = None,
    ) -> ArchiveResult:
        return await asyncio.to_thread(
            self._write,
            destination,
            sources,
            pack_format,
            compression_level,
            on_progress,
        )

    def _write(
        self,
        destination: Path,
        sources: list[ArchiveSource],
        pack_format: str,
        compression_level: int,
        report: Callable[[int], None] | None,
    ) -> ArchiveResult:
        if pack_format not in ("zip", "tar.zst"):
            raise DomainError(ErrorCode.INVALID_INPUT, "打包格式无效")
        if compression_level < 0 or compression_level > 22:
            raise DomainError(ErrorCode.INVALID_INPUT, "压缩级别必须在 0 到 22 之间")
        items, total = self._plan(sources)
        reporter = _ProgressReporter(total, report)
        destination.parent.mkdir(parents=True, exist_ok=True)
        reporter.begin()
        if pack_format == "zip":
            self._write_zip(destination, items, compression_level, reporter)
        else:
            self._write_tar_zst(destination, items, compression_level, reporter)
        reporter.finish()
        size = destination.stat().st_size
        return ArchiveResult(size, _digest_file(destination, self.chunk_size))

    def _plan(self, sources: list[ArchiveSource]) -> tuple[list[_ArchiveItem], int]:
        items: list[_ArchiveItem] = []
        for source in sources:
            prefix = validate_display_name(source.display_name)
            root = source.root
            if root.is_symlink():
                raise DomainError(ErrorCode.INVALID_INPUT, "打包内容不能包含符号链接")
            if source.kind is ContentKind.FILE:
                if not root.is_file():
                    raise DomainError(ErrorCode.INVALID_STATE, "打包来源文件缺失")
                items.append(_ArchiveItem(prefix, root, False, root.stat().st_size))
                continue
            if not root.is_dir():
                raise DomainError(ErrorCode.INVALID_STATE, "打包来源目录缺失")
            items.append(_ArchiveItem(prefix, root, True, 0))
            for parent, directories, files in os.walk(root, followlinks=False):
                parent_path = Path(parent)
                for name in directories:
                    item = parent_path / name
                    if item.is_symlink():
                        raise DomainError(ErrorCode.INVALID_INPUT, "打包内容不能包含符号链接")
                    relative = item.relative_to(root).as_posix()
                    items.append(_ArchiveItem(f"{prefix}/{relative}", item, True, 0))
                for name in files:
                    item = parent_path / name
                    if item.is_symlink() or not item.is_file():
                        raise DomainError(ErrorCode.INVALID_INPUT, "打包内容只能包含普通文件")
                    relative = item.relative_to(root).as_posix()
                    items.append(_ArchiveItem(f"{prefix}/{relative}", item, False, item.stat().st_size))
        items.sort(key=lambda item: item.arcname)
        return items, sum(item.size for item in items)

    def _write_zip(
        self,
        destination: Path,
        items: list[_ArchiveItem],
        compression_level: int,
        reporter: _ProgressReporter,
    ) -> None:
        compression = zipfile.ZIP_STORED if compression_level == 0 else zipfile.ZIP_DEFLATED
        level = None if compression_level == 0 else compression_level
        with zipfile.ZipFile(
            destination, "w", compression=compression, compresslevel=level
        ) as archive:
            for item in items:
                if item.is_dir:
                    archive.mkdir(item.arcname)
                    continue
                with open(item.path, "rb") as source, archive.open(item.arcname, "w") as member:
                    copied = 0
                    for chunk in iter(lambda: source.read(self.chunk_size), b""):
                        member.write(chunk)
                        copied += len(chunk)
                        reporter.advance(len(chunk))
                    if copied != item.size:
                        raise OSError("打包来源大小在写入过程中变化")

    def _write_tar_zst(
        self,
        destination: Path,
        items: list[_ArchiveItem],
        compression_level: int,
        reporter: _ProgressReporter,
    ) -> None:
        compressor = zstandard.ZstdCompressor(level=max(compression_level, _MIN_ZSTD_LEVEL))
        with open(destination, "wb") as raw:
            with compressor.stream_writer(raw) as compressed:
                with tarfile.open(fileobj=compressed, mode="w|", format=tarfile.PAX_FORMAT) as archive:
                    for item in items:
                        info = tarfile.TarInfo(item.arcname)
                        info.uid = 0
                        info.gid = 0
                        info.uname = ""
                        info.gname = ""
                        info.mtime = 0
                        if item.is_dir:
                            info.type = tarfile.DIRTYPE
                            info.mode = 0o755
                            archive.addfile(info)
                            continue
                        info.size = item.size
                        info.mode = 0o644
                        with open(item.path, "rb") as source:
                            reader = _ReportingReader(source, reporter)
                            archive.addfile(info, reader)
                            if reader.counted != item.size:
                                raise OSError("打包来源大小在写入过程中变化")
