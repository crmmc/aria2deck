from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
import stat
from collections.abc import Callable
from pathlib import Path
from typing import IO

from ..application.downloads.models import ContentPreparation
from ..application.library.models import CanonicalEntry
from ..application.packs.models import InstallOutcome
from ..domain.content import (
    ContentEntryIdentity,
    ContentKind,
    content_identity,
    directory_content_digest,
    normalize_relative_path,
)
from ..domain.errors import DomainError, ErrorCode

_CANONICAL_NAME = re.compile(r"(file|directory)-([0-9a-f]{64})")
_CANONICAL_PATH = re.compile(rf"objects/{_CANONICAL_NAME.pattern}")
ProgressCallback = Callable[[int], None]


class _ProgressReporter:
    """每读一块回调一次累计字节。

    不能攒批回调：调用方靠它在停止时尽早中止校验，攒到 8 MiB 才发会让小于
    8 MiB 的条目完全无法中断，点「停止」后照常入库。
    """

    def __init__(self, on_progress: ProgressCallback) -> None:
        self.on_progress = on_progress
        self.total = 0

    def add(self, count: int) -> None:
        self.total += count
        self.on_progress(self.total)


def _progress_sink(on_progress: ProgressCallback | None) -> Callable[[int], None] | None:
    return None if on_progress is None else _ProgressReporter(on_progress).add


def _file_digest(path: Path, on_bytes: Callable[[int], None] | None = None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            if on_bytes is not None:
                on_bytes(len(chunk))
    return digest.hexdigest()


def _tree_size(root: Path) -> int:
    """目录型对象大小 = 内部普通文件 st_size 之和，与 _scan 的口径一致。"""
    total = 0
    for parent, _directories, files in os.walk(root, followlinks=False):
        for name in files:
            try:
                info = os.lstat(Path(parent) / name)
            except OSError:  # 竞态删除的文件按 0 计
                continue
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
    return total


def _directory_entries(
    source: Path, on_bytes: Callable[[int], None] | None
) -> list[ContentEntryIdentity]:
    """按 _scan 的防线遍历目录（符号链接、控制文件、非普通文件一律拒绝）。"""
    entries: list[ContentEntryIdentity] = []
    for parent, directories, files in os.walk(source, followlinks=False):
        parent_path = Path(parent)
        for name in directories:
            item = parent_path / name
            if item.is_symlink():
                raise DomainError(ErrorCode.INVALID_INPUT, "内容不能包含符号链接")
            entries.append(
                ContentEntryIdentity(item.relative_to(source).as_posix(), ContentKind.DIRECTORY, 0)
            )
        for name in files:
            item = parent_path / name
            if item.is_symlink() or not item.is_file():
                raise DomainError(ErrorCode.INVALID_INPUT, "内容只能包含普通文件")
            if name.endswith(".aria2"):
                raise DomainError(ErrorCode.DOWNLOAD_UNAVAILABLE, "下载控制文件尚未清理")
            entries.append(
                ContentEntryIdentity(
                    item.relative_to(source).as_posix(),
                    ContentKind.FILE,
                    item.stat().st_size,
                    _file_digest(item, on_bytes),
                )
            )
    return entries


class DownloadContentStore:
    """Scans attempt directories and installs verified objects in the canonical store."""

    def __init__(self, download_directory: Path) -> None:
        self.download_directory = download_directory

    def _attempt_path(self, attempt_id: int) -> Path:
        if attempt_id <= 0:
            raise DomainError(ErrorCode.INVALID_INPUT, "下载尝试 ID 无效")
        return self.download_directory / str(attempt_id)

    @staticmethod
    def _same(preparation: ContentPreparation, expected: ContentPreparation) -> bool:
        return (
            preparation.identity == expected.identity
            and preparation.size == expected.size
            and preparation.storage_path == expected.storage_path
        )

    def _scan(self, source: Path) -> ContentPreparation:
        if source.is_symlink():
            raise DomainError(ErrorCode.INVALID_INPUT, "下载内容不能包含符号链接")
        if source.is_file():
            digest = _file_digest(source)
            identity = content_identity(ContentKind.FILE, digest)
            return ContentPreparation(
                identity,
                source.stat().st_size,
                f"objects/file-{identity.digest}",
                (),
            )
        if not source.is_dir():
            raise DomainError(ErrorCode.INVALID_STATE, "下载内容目录不存在或布局无效")
        entries: list[ContentEntryIdentity] = []
        root_files = 0
        root_directories = 0
        for parent, directories, files in os.walk(source, followlinks=False):
            parent_path = Path(parent)
            for name in directories:
                item = parent_path / name
                if item.is_symlink():
                    raise DomainError(ErrorCode.INVALID_INPUT, "下载内容不能包含符号链接")
                relative = item.relative_to(source).as_posix()
                entries.append(ContentEntryIdentity(relative, ContentKind.DIRECTORY, 0))
                if parent_path == source:
                    root_directories += 1
            for name in files:
                item = parent_path / name
                if item.is_symlink() or not item.is_file():
                    raise DomainError(ErrorCode.INVALID_INPUT, "下载内容只能包含普通文件")
                if name.endswith(".aria2"):
                    raise DomainError(ErrorCode.DOWNLOAD_UNAVAILABLE, "下载控制文件尚未清理")
                relative = item.relative_to(source).as_posix()
                size = item.stat().st_size
                entries.append(
                    ContentEntryIdentity(relative, ContentKind.FILE, size, _file_digest(item))
                )
                if parent_path == source:
                    root_files += 1
        if root_files == 1 and root_directories == 0 and len(entries) == 1:
            only = entries[0]
            identity = content_identity(ContentKind.FILE, only.digest or "")
            return ContentPreparation(
                identity, only.size, f"objects/file-{identity.digest}", ()
            )
        entries.sort(key=lambda entry: entry.path)
        digest = directory_content_digest(entries)
        identity = content_identity(ContentKind.DIRECTORY, digest)
        size = sum(entry.size for entry in entries)
        return ContentPreparation(
            identity, size, f"objects/directory-{identity.digest}", tuple(entries)
        )

    async def scan(self, attempt_id: int) -> ContentPreparation:
        return await asyncio.to_thread(self._scan, self._attempt_path(attempt_id))

    def _sweep_control_files(self, attempt_id: int) -> None:
        source = self._attempt_path(attempt_id)
        if source.is_symlink():
            raise DomainError(ErrorCode.INVALID_INPUT, "下载控制文件路径无效")
        if not source.is_dir():
            return
        for parent, _directories, files in os.walk(source, followlinks=False):
            parent_path = Path(parent)
            for name in files:
                if not name.endswith(".aria2"):
                    continue
                control = parent_path / name
                if control.is_symlink() or not control.is_file():
                    raise DomainError(ErrorCode.INVALID_INPUT, "下载控制文件路径无效")
                control.unlink()

    async def sweep_control_files(self, attempt_id: int) -> None:
        """按后缀清掉 attempt 目录下全部 aria2 控制文件（含子目录）。

        仅供 BT 源使用：控制文件按种子真名落盘，而磁力任务提交时的下载名在元数据
        到达前是 info 哈希，入库时刻两者可能对不上，按名字精确删除必然漏；BT
        attempt 目录里的 .aria2 定义上就是 aria2 的管道文件，不会误删用户内容。
        HTTP 源不得走这里——用户内容真名可以以 .aria2 结尾。attempt 目录本身或
        控制文件是符号链接时拒绝（与 _scan/_cleanup_attempt 同一防线，防越界删除）。
        """
        await asyncio.to_thread(self._sweep_control_files, attempt_id)

    def _destination(self, storage_path: str) -> Path:
        normalized = normalize_relative_path(storage_path)
        return self.download_directory.joinpath(*normalized.split("/"))

    def _install(self, attempt_id: int, expected: ContentPreparation) -> None:
        source = self._attempt_path(attempt_id)
        destination = self._destination(expected.storage_path)
        source_matches = False
        if source.exists():
            try:
                source_matches = self._same(self._scan(source), expected)
            except DomainError:
                if not destination.exists():
                    raise
        if source_matches and not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.parent.is_symlink():
                raise DomainError(ErrorCode.INVALID_INPUT, "内容存储目录不能是符号链接")
            if expected.identity.kind is ContentKind.FILE:
                files = [item for item in source.iterdir() if item.is_file()]
                if len(files) != 1:
                    raise DomainError(ErrorCode.INVALID_STATE, "单文件下载布局已变化")
                files[0].replace(destination)
            else:
                source.replace(destination)
        if not destination.exists() or not self._same(self._scan(destination), expected):
            raise DomainError(ErrorCode.INVALID_STATE, "规范内容安装或校验失败")

    async def install(self, attempt_id: int, expected: ContentPreparation) -> None:
        await asyncio.to_thread(self._install, attempt_id, expected)

    def _cleanup_attempt(self, attempt_id: int) -> None:
        source = self._attempt_path(attempt_id)
        if source.is_symlink():
            source.unlink()
        elif source.exists():
            if not source.is_dir():
                raise DomainError(ErrorCode.INVALID_STATE, "下载清理目标不是目录")
            shutil.rmtree(source)

    async def cleanup_attempt(self, attempt_id: int) -> None:
        await asyncio.to_thread(self._cleanup_attempt, attempt_id)

    def _physical_delete(self, storage_path: str) -> None:
        target = self._destination(storage_path)
        if target.is_symlink():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()

    async def physical_delete(self, storage_path: str) -> None:
        await asyncio.to_thread(self._physical_delete, storage_path)

    def _open_read_handle(self, storage_path: str, entry_relative_path: str | None) -> IO[bytes]:
        root = self._destination(storage_path)
        target = root if entry_relative_path is None else root / normalize_relative_path(entry_relative_path)
        return target.open("rb")

    async def open_read_handle(
        self, storage_path: str, entry_relative_path: str | None = None
    ) -> IO[bytes]:
        return await asyncio.to_thread(self._open_read_handle, storage_path, entry_relative_path)

    def physical_path(self, storage_path: str, entry_relative_path: str | None = None) -> Path:
        root = self._destination(storage_path)
        return root if entry_relative_path is None else root / normalize_relative_path(entry_relative_path)

    def on_disk(self, storage_path: str) -> bool:
        return self._destination(storage_path).exists()

    def machine_free_bytes(self) -> int:
        return shutil.disk_usage(self.download_directory).free

    def machine_disk_usage(self) -> tuple[int, int, int]:
        usage = shutil.disk_usage(self.download_directory)
        return usage.total, usage.used, usage.free

    def download_used_bytes(self) -> int:
        if not self.download_directory.is_dir():
            return 0
        total = 0
        for parent, _directories, names in os.walk(self.download_directory, followlinks=False):
            for name in names:
                try:
                    total += (Path(parent) / name).stat().st_size
                except OSError:  # pragma: no cover -- 竞态删除的文件按 0 计
                    continue
        return total

    def _verify_prepared(self, storage_path: str) -> tuple[int, str]:
        target = self._destination(storage_path)
        try:
            info = os.lstat(target)
        except FileNotFoundError:
            raise DomainError(ErrorCode.NOT_FOUND, "prepared 输出文件不存在") from None
        if not stat.S_ISREG(info.st_mode):
            # 目录、符号链接或其他非普通文件是持久冲突，不能当作缺失重排。
            raise DomainError(ErrorCode.INVALID_STATE, "prepared 输出不是普通文件")
        return info.st_size, _file_digest(target)

    async def verify_prepared(self, storage_path: str) -> tuple[int, str]:
        return await asyncio.to_thread(self._verify_prepared, storage_path)

    def _remove_prepared(self, storage_path: str) -> None:
        target = self._destination(storage_path)
        if target.exists() or target.is_symlink():
            target.unlink()

    async def remove_prepared(self, storage_path: str) -> None:
        await asyncio.to_thread(self._remove_prepared, storage_path)

    def _install_prepared(self, storage_path: str, digest: str) -> InstallOutcome:
        canonical = f"objects/file-{digest}"
        source = self._destination(storage_path)
        destination = self._destination(canonical)
        try:
            canonical_info = os.lstat(destination)
        except FileNotFoundError:
            canonical_info = None
        if canonical_info is not None:
            # 规范位必须是普通文件：符号链接（含失效链接）不能复用、覆盖或跟随。
            if not stat.S_ISREG(canonical_info.st_mode):
                raise DomainError(ErrorCode.INVALID_STATE, "规范位不是普通文件")
            if _file_digest(destination) != digest:
                raise DomainError(ErrorCode.INVALID_STATE, "规范位内容与待安装摘要不一致")
            if source.exists() or source.is_symlink():
                source.unlink()
            return InstallOutcome(canonical, created_by_this_attempt=False)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.parent.is_symlink():
            raise DomainError(ErrorCode.INVALID_INPUT, "内容存储目录不能是符号链接")
        source.replace(destination)
        return InstallOutcome(canonical, created_by_this_attempt=True)

    async def install_prepared(self, storage_path: str, digest: str) -> InstallOutcome:
        return await asyncio.to_thread(self._install_prepared, storage_path, digest)

    def _scan_entries(self, storage_path: str) -> list[ContentEntryIdentity]:
        return list(self._scan(self._destination(storage_path)).entries)

    async def scan_entries(self, storage_path: str) -> list[ContentEntryIdentity]:
        return await asyncio.to_thread(self._scan_entries, storage_path)

    def parse_canonical_path(self, storage_path: str) -> tuple[ContentKind, str]:
        """整串匹配规范命名；不匹配即拒绝，绝不触碰文件系统。"""
        match = _CANONICAL_PATH.fullmatch(storage_path)
        if match is None:
            raise DomainError(ErrorCode.INVALID_INPUT, "存储路径格式无效")
        return ContentKind(match.group(1)), match.group(2)

    def _canonical_root(self) -> Path:
        return self.download_directory / "objects"

    def _canonical_target(self, storage_path: str) -> tuple[Path, ContentKind, str]:
        kind, digest = self.parse_canonical_path(storage_path)
        if self._canonical_root().is_symlink():
            raise DomainError(ErrorCode.INVALID_INPUT, "内容存储目录不能是符号链接")
        return self.download_directory.joinpath(*storage_path.split("/")), kind, digest

    @staticmethod
    def _lstat_no_symlink(target: Path) -> os.stat_result:
        try:
            info = os.lstat(target)
        except FileNotFoundError:
            raise DomainError(ErrorCode.NOT_FOUND, "条目不存在") from None
        if stat.S_ISLNK(info.st_mode):
            raise DomainError(ErrorCode.INVALID_INPUT, "条目不能是符号链接")
        return info

    @staticmethod
    def _entry(
        storage_path: str,
        target: Path,
        kind: ContentKind,
        digest: str,
        info: os.stat_result,
    ) -> CanonicalEntry:
        size = _tree_size(target) if stat.S_ISDIR(info.st_mode) else info.st_size
        return CanonicalEntry(storage_path, kind, digest, size, info.st_mtime_ns)

    def _list_canonical_entries(self) -> tuple[list[CanonicalEntry], int]:
        root = self._canonical_root()
        if root.is_symlink() or not root.is_dir():
            return [], 0
        entries: list[CanonicalEntry] = []
        unrecognized = 0
        with os.scandir(root) as scanned:
            for item in scanned:
                match = _CANONICAL_NAME.fullmatch(item.name)
                if match is None or item.is_symlink():
                    unrecognized += 1
                    continue
                target = root / item.name
                try:
                    info = os.lstat(target)
                except OSError:  # 竞态删除的条目不计入清单
                    continue
                entries.append(
                    self._entry(
                        f"objects/{item.name}",
                        target,
                        ContentKind(match.group(1)),
                        match.group(2),
                        info,
                    )
                )
        return entries, unrecognized

    async def list_canonical_entries(self) -> tuple[list[CanonicalEntry], int]:
        """列出 objects/ 下的规范条目与不规范条目数量；只读目录项与元数据。"""
        return await asyncio.to_thread(self._list_canonical_entries)

    def _stat_canonical(self, storage_path: str) -> CanonicalEntry:
        target, kind, digest = self._canonical_target(storage_path)
        info = self._lstat_no_symlink(target)
        return self._entry(storage_path, target, kind, digest, info)

    async def stat_canonical(self, storage_path: str) -> CanonicalEntry:
        return await asyncio.to_thread(self._stat_canonical, storage_path)

    def _verify_canonical(
        self, storage_path: str, on_progress: ProgressCallback | None
    ) -> tuple[CanonicalEntry, list[ContentEntryIdentity]]:
        target, kind, digest = self._canonical_target(storage_path)
        info = self._lstat_no_symlink(target)
        on_bytes = _progress_sink(on_progress)
        if kind is ContentKind.FILE:
            if not stat.S_ISREG(info.st_mode):
                raise DomainError(ErrorCode.INVALID_INPUT, "条目类型与文件名不一致")
            if _file_digest(target, on_bytes) != digest:
                raise DomainError(ErrorCode.INVALID_STATE, "内容与文件名摘要不一致")
            return (
                CanonicalEntry(storage_path, kind, digest, info.st_size, info.st_mtime_ns),
                [],
            )
        if not stat.S_ISDIR(info.st_mode):
            raise DomainError(ErrorCode.INVALID_INPUT, "条目类型与文件名不一致")
        # 不复用 _scan：它的「根目录只有一个文件就当单文件」特例会把目录型对象判成文件型。
        entries = _directory_entries(target, on_bytes)
        if directory_content_digest(entries) != digest:
            raise DomainError(ErrorCode.INVALID_STATE, "内容与文件名摘要不一致")
        size = sum(entry.size for entry in entries)
        return CanonicalEntry(storage_path, kind, digest, size, info.st_mtime_ns), entries

    async def verify_canonical(
        self, storage_path: str, *, on_progress: ProgressCallback | None = None
    ) -> tuple[CanonicalEntry, list[ContentEntryIdentity]]:
        """读内容校验摘要与文件名一致；on_progress 每读满 8 MiB 收到一次累计字节。"""
        return await asyncio.to_thread(self._verify_canonical, storage_path, on_progress)
