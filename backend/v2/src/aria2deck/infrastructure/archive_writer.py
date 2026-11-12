from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import tarfile
import threading
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Literal, cast

import zstandard

from ..application.packs.models import ArchiveResult, ArchiveSource, ProgressCallback
from ..domain.content import ContentKind, validate_display_name
from ..domain.errors import DomainError, ErrorCode
from ..domain.packs import ArchiveBudget, ArchiveItem, require_pack_output_within_budget

CHUNK_SIZE = 1024 * 1024
PackFormat = Literal["zip", "tar.zst"]

logger = logging.getLogger(__name__)

# zstandard 无 0 级：0 表示不压缩，仅对 zip 有 STORED 语义，tar.zst 最低用 1。
_MIN_ZSTD_LEVEL = 1


@dataclass(frozen=True, slots=True)
class _ArchiveItem:
    arcname: str
    path: Path
    is_dir: bool
    size: int


def _charge(budget: ArchiveBudget, item: _ArchiveItem) -> ArchiveBudget:
    """Charge one planned entry against the domain entry/path budget atomically."""
    return budget.with_items([ArchiveItem(item.arcname, item.is_dir, item.size)])


def percent_of(written: int, total: int) -> int:
    return 100 if total == 0 else min(written * 100 // total, 100)


def _raise_if_cancelled(cancel_event: threading.Event) -> None:
    if cancel_event.is_set():
        raise InterruptedError("打包写入已取消")


def _free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


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

    def __init__(
        self,
        stream: IO[bytes],
        reporter: _ProgressReporter,
        cancel_event: threading.Event,
    ) -> None:
        self._stream = stream
        self._reporter = reporter
        self._cancel_event = cancel_event
        self.counted = 0

    def read(self, size: int = -1) -> bytes:
        _raise_if_cancelled(self._cancel_event)
        chunk = self._stream.read(size)
        self.counted += len(chunk)
        self._reporter.advance(len(chunk))
        return chunk


class _BudgetedSink:
    """zip 与 tar.zst 共用的有界输出层：真实 extent、磁盘下限与协作取消。

    ZIP 回写本地头不重复计费：extent 只取 ``max(当前文件长度, 位置+本次长度)``，
    已写过区域的覆盖写不会增加 extent。
    """

    def __init__(
        self,
        stream: IO[bytes],
        destination: Path,
        *,
        max_output_bytes: int,
        min_free_bytes: int,
        cancel_event: threading.Event,
    ) -> None:
        self._stream = stream
        self._destination = destination
        self._max_output_bytes = max_output_bytes
        self._min_free_bytes = min_free_bytes
        self._cancel_event = cancel_event
        self._closed = False

    def _admit(self, length: int) -> None:
        _raise_if_cancelled(self._cancel_event)
        current = os.fstat(self._stream.fileno()).st_size
        extent = max(current, self._stream.tell() + length)
        if extent > self._max_output_bytes:
            raise DomainError(ErrorCode.TASK_TOO_LARGE, "打包输出超过已批准的空间预算")
        growth = extent - current
        if growth > 0 and _free_bytes(self._destination) - growth < self._min_free_bytes:
            raise DomainError(ErrorCode.PHYSICAL_SPACE_EXCEEDED, "机器剩余空间不足")

    def write(self, data: bytes) -> int:
        self._admit(len(data))
        return self._stream.write(data)

    def tell(self) -> int:
        return self._stream.tell()

    def seek(self, offset: int, whence: int = 0) -> int:
        return self._stream.seek(offset, whence)

    def seekable(self) -> bool:
        return True

    def fileno(self) -> int:
        return self._stream.fileno()

    def flush(self) -> None:
        self._stream.flush()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.flush()
        finally:
            self._stream.close()


def _close_sink_preserving_error(sink: _BudgetedSink) -> None:
    try:
        sink.close()
    except Exception:
        # 已进入业务分类的原始异常优先；关闭故障只用于诊断，不能改变错误分类。
        logger.debug("打包输出关闭失败，保留原始异常", exc_info=True)


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
        max_output_bytes: int,
        min_free_bytes: int,
        cancel_event: threading.Event,
        on_progress: Callable[[int], None] | None = None,
    ) -> ArchiveResult:
        worker = asyncio.create_task(
            asyncio.to_thread(
                self._write,
                destination,
                sources,
                pack_format,
                compression_level,
                on_progress,
                max_output_bytes=max_output_bytes,
                min_free_bytes=min_free_bytes,
                cancel_event=cancel_event,
            )
        )
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            # 先通知真实线程并在事件循环上等它退出；禁止同步 join 或提前返回。
            cancel_event.set()
            await self._await_worker(worker)
            raise

    @staticmethod
    async def _await_worker(worker: asyncio.Task[ArchiveResult]) -> None:
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                if worker.done():
                    break
                # 第二次取消只重复停止请求，不能放弃等待真实线程。
                continue
            except Exception:
                # 写入线程已自行失败（含 InterruptedError）：等待结束即可，
                # 调用方原样抛 CancelledError，不把线程异常冒充为取消原因。
                break
        if not worker.cancelled():
            worker.exception()  # 收走线程异常，避免未取出异常告警。

    def _write(
        self,
        destination: Path,
        sources: list[ArchiveSource],
        pack_format: str,
        compression_level: int,
        report: Callable[[int], None] | None,
        *,
        max_output_bytes: int,
        min_free_bytes: int,
        cancel_event: threading.Event,
    ) -> ArchiveResult:
        if pack_format not in ("zip", "tar.zst"):
            raise DomainError(ErrorCode.INVALID_INPUT, "打包格式无效")
        if compression_level < 0 or compression_level > 22:
            raise DomainError(ErrorCode.INVALID_INPUT, "压缩级别必须在 0 到 22 之间")
        items, total = self._plan(sources, cancel_event)
        reporter = _ProgressReporter(total, report)
        destination.parent.mkdir(parents=True, exist_ok=True)
        reporter.begin()
        # begin() 可执行任意受控回调（可能阻塞到取消请求落地），
        # 打开 destination 前必须重查，否则取消后仍会迟到创建 0 字节输出/part。
        _raise_if_cancelled(cancel_event)
        if pack_format == "zip":
            self._write_zip(
                destination,
                items,
                compression_level,
                reporter,
                max_output_bytes=max_output_bytes,
                min_free_bytes=min_free_bytes,
                cancel_event=cancel_event,
            )
        else:
            self._write_tar_zst(
                destination,
                items,
                compression_level,
                reporter,
                max_output_bytes=max_output_bytes,
                min_free_bytes=min_free_bytes,
                cancel_event=cancel_event,
            )
        reporter.finish()
        size = destination.stat().st_size
        require_pack_output_within_budget(size, max_output_bytes)
        return ArchiveResult(size, _digest_file(destination, self.chunk_size))

    def _plan(
        self, sources: list[ArchiveSource], cancel_event: threading.Event
    ) -> tuple[list[_ArchiveItem], int]:
        items: list[_ArchiveItem] = []
        budget = ArchiveBudget()
        for source in sources:
            _raise_if_cancelled(cancel_event)
            prefix = validate_display_name(source.display_name)
            root = source.root
            if root.is_symlink():
                raise DomainError(ErrorCode.INVALID_INPUT, "打包内容不能包含符号链接")
            if source.kind is ContentKind.FILE:
                if not root.is_file():
                    raise DomainError(ErrorCode.INVALID_STATE, "打包来源文件缺失")
                item = _ArchiveItem(prefix, root, False, root.stat().st_size)
                budget = _charge(budget, item)
                items.append(item)
                continue
            if not root.is_dir():
                raise DomainError(ErrorCode.INVALID_STATE, "打包来源目录缺失")
            root_item = _ArchiveItem(f"{prefix}/", root, True, 0)
            budget = _charge(budget, root_item)
            items.append(root_item)
            scanned, budget = self._scan_directory(prefix, root, budget, cancel_event)
            items.extend(scanned)
        items.sort(key=lambda item: item.arcname)
        return items, sum(item.size for item in items)

    def _scan_directory(
        self,
        prefix: str,
        root: Path,
        budget: ArchiveBudget,
        cancel_event: threading.Event,
    ) -> tuple[list[_ArchiveItem], ArchiveBudget]:
        items: list[_ArchiveItem] = []
        pending: list[tuple[Path, str]] = [(root, "")]
        while pending:
            _raise_if_cancelled(cancel_event)
            directory, relative = pending.pop()
            with os.scandir(directory) as entries:
                for entry in entries:
                    _raise_if_cancelled(cancel_event)
                    if entry.is_symlink():
                        raise DomainError(ErrorCode.INVALID_INPUT, "打包内容不能包含符号链接")
                    child = f"{relative}/{entry.name}" if relative else entry.name
                    arcname = f"{prefix}/{child}"
                    if entry.is_dir(follow_symlinks=False):
                        item = _ArchiveItem(f"{arcname}/", Path(entry.path), True, 0)
                        budget = _charge(budget, item)
                        items.append(item)
                        pending.append((Path(entry.path), child))
                    elif entry.is_file(follow_symlinks=False):
                        size = entry.stat(follow_symlinks=False).st_size
                        item = _ArchiveItem(arcname, Path(entry.path), False, size)
                        budget = _charge(budget, item)
                        items.append(item)
                    else:
                        raise DomainError(ErrorCode.INVALID_INPUT, "打包内容只能包含普通文件")
        return items, budget

    def _write_zip(
        self,
        destination: Path,
        items: list[_ArchiveItem],
        compression_level: int,
        reporter: _ProgressReporter,
        *,
        max_output_bytes: int,
        min_free_bytes: int,
        cancel_event: threading.Event,
    ) -> None:
        compression = zipfile.ZIP_STORED if compression_level == 0 else zipfile.ZIP_DEFLATED
        level = None if compression_level == 0 else compression_level
        with destination.open("wb") as raw:
            sink = _BudgetedSink(
                raw,
                destination,
                max_output_bytes=max_output_bytes,
                min_free_bytes=min_free_bytes,
                cancel_event=cancel_event,
            )
            try:
                # cast：sink 实现 zipfile 需要的写/定位子集，不是完整 IO[bytes]。
                with zipfile.ZipFile(
                    cast(IO[bytes], sink),
                    "w",
                    compression=compression,
                    compresslevel=level,
                ) as archive:
                    for item in items:
                        _raise_if_cancelled(cancel_event)
                        if item.is_dir:
                            archive.mkdir(item.arcname)
                            continue
                        with open(item.path, "rb") as source, archive.open(item.arcname, "w") as member:
                            copied = 0
                            for chunk in iter(lambda: source.read(self.chunk_size), b""):
                                _raise_if_cancelled(cancel_event)
                                member.write(chunk)
                                copied += len(chunk)
                                reporter.advance(len(chunk))
                            if copied != item.size:
                                raise OSError("打包来源大小在写入过程中变化")
            except BaseException:
                _close_sink_preserving_error(sink)
                raise
            else:
                sink.close()

    def _write_tar_zst(
        self,
        destination: Path,
        items: list[_ArchiveItem],
        compression_level: int,
        reporter: _ProgressReporter,
        *,
        max_output_bytes: int,
        min_free_bytes: int,
        cancel_event: threading.Event,
    ) -> None:
        compressor = zstandard.ZstdCompressor(level=max(compression_level, _MIN_ZSTD_LEVEL))
        with destination.open("wb") as raw:
            sink = _BudgetedSink(
                raw,
                destination,
                max_output_bytes=max_output_bytes,
                min_free_bytes=min_free_bytes,
                cancel_event=cancel_event,
            )
            try:
                # closefd=False：帧尾部仍写入 sink，但由本函数统一负责关闭底层文件。
                with compressor.stream_writer(
                    cast(IO[bytes], sink), closefd=False
                ) as compressed:
                    with tarfile.open(
                        fileobj=compressed, mode="w|", format=tarfile.PAX_FORMAT
                    ) as archive:
                        for item in items:
                            _raise_if_cancelled(cancel_event)
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
                                reader = _ReportingReader(source, reporter, cancel_event)
                                archive.addfile(info, reader)
                                if reader.counted != item.size:
                                    raise OSError("打包来源大小在写入过程中变化")
            except BaseException:
                _close_sink_preserving_error(sink)
                raise
            else:
                sink.close()
