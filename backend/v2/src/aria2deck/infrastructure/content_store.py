from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
import stat
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import IO

from ..application.downloads.models import ContentPreparation, ManifestEntry
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


def _lstat_or_mismatch(target: Path) -> os.stat_result:
    """缺失按可重试处理：aria2 的报告与磁盘状态短暂不一致时不判死。"""
    try:
        return os.lstat(target)
    except FileNotFoundError:
        raise DomainError(ErrorCode.INVALID_STATE, "下载内容与完成清单不一致") from None


def _implied_directories(paths: Iterable[str]) -> list[str]:
    """清单文件路径上隐含的目录：目录摘要与 content_entries 都需要这些层级。"""
    directories: set[str] = set()
    for path in paths:
        parts = path.split("/")[:-1]
        for depth in range(1, len(parts) + 1):
            directories.add("/".join(parts[:depth]))
    return sorted(directories)


def _tree_size(root: Path) -> int:
    """目录型对象大小 = 内部普通文件 st_size 之和，与清单口径一致。"""
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
    source: Path,
    on_bytes: Callable[[int], None] | None,
    *,
    reject_control_files: bool = True,
) -> list[ContentEntryIdentity]:
    """遍历目录并逐项核验（符号链接、非普通文件一律拒绝）。

    reject_control_files 只服务“不知道内容该有哪些文件”的场景（未登记文件收编、
    读模型对账）。成交付入库不走这里：清单说了算，`.aria2` 结尾也可能就是用户内容。
    """
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
            if reject_control_files and name.endswith(".aria2"):
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
    """Verifies manifest files on disk and installs them in the canonical store."""

    def __init__(self, download_directory: Path) -> None:
        self.download_directory = download_directory

    def _attempt_path(self, attempt_id: int) -> Path:
        if attempt_id <= 0:
            raise DomainError(ErrorCode.INVALID_INPUT, "下载尝试 ID 无效")
        return self.download_directory / str(attempt_id)

    def _staging_path(self, attempt_id: int) -> Path:
        """入库暂存目录：与内容存储同盘，装好一次 rename 就位。"""
        return self.download_directory / ".ingest" / str(attempt_id)

    @staticmethod
    def _same(preparation: ContentPreparation, expected: ContentPreparation) -> bool:
        return (
            preparation.identity == expected.identity
            and preparation.size == expected.size
            and preparation.storage_path == expected.storage_path
        )

    @staticmethod
    def _manifest_file(source: Path, path: str, size: int) -> Path:
        """逐组件核验清单路径：任何一级是符号链接都拒绝，末级必须是大小一致的文件。"""
        target = source
        parts = path.split("/")
        for part in parts[:-1]:
            target = target / part
            info = _lstat_or_mismatch(target)
            if stat.S_ISLNK(info.st_mode):
                raise DomainError(ErrorCode.INVALID_INPUT, "下载内容不能包含符号链接")
            if not stat.S_ISDIR(info.st_mode):
                raise DomainError(ErrorCode.INVALID_STATE, "下载内容与完成清单不一致")
        target = target / parts[-1]
        info = _lstat_or_mismatch(target)
        if stat.S_ISLNK(info.st_mode):
            raise DomainError(ErrorCode.INVALID_INPUT, "下载内容不能包含符号链接")
        if not stat.S_ISREG(info.st_mode):
            raise DomainError(ErrorCode.INVALID_INPUT, "下载内容只能包含普通文件")
        if info.st_size != size:
            raise DomainError(
                ErrorCode.INVALID_STATE,
                f"下载内容与完成清单大小不一致（清单 {size} 字节，磁盘 {info.st_size} 字节）",
            )
        return target

    def _collect(
        self, source: Path, manifest: tuple[ManifestEntry, ...]
    ) -> ContentPreparation:
        """按清单核验磁盘：只认清单上的文件，清单外的东西不看不碰。"""
        if source.is_symlink():
            raise DomainError(ErrorCode.INVALID_INPUT, "下载内容不能包含符号链接")
        if not source.is_dir():
            raise DomainError(ErrorCode.INVALID_STATE, "下载内容目录不存在或布局无效")
        files: list[ContentEntryIdentity] = []
        seen: set[str] = set()
        for item in manifest:
            path = normalize_relative_path(item.path)
            if path in seen:
                raise DomainError(ErrorCode.INVALID_STATE, "完成清单数据无效")
            seen.add(path)
            target = self._manifest_file(source, path, item.size)
            files.append(
                ContentEntryIdentity(path, ContentKind.FILE, item.size, _file_digest(target))
            )
        if len(files) == 1 and "/" not in files[0].path:
            only = files[0]
            identity = content_identity(ContentKind.FILE, only.digest or "")
            return ContentPreparation(
                identity, only.size, f"objects/file-{identity.digest}", ()
            )
        entries = files + [
            ContentEntryIdentity(directory, ContentKind.DIRECTORY, 0)
            for directory in _implied_directories(seen)
        ]
        entries.sort(key=lambda entry: entry.path)
        digest = directory_content_digest(entries)
        identity = content_identity(ContentKind.DIRECTORY, digest)
        return ContentPreparation(
            identity,
            sum(entry.size for entry in files),
            f"objects/directory-{identity.digest}",
            tuple(entries),
        )

    async def collect(
        self, attempt_id: int, manifest: tuple[ManifestEntry, ...]
    ) -> ContentPreparation:
        """入库唯一的内容入口：清单是唯一事实来源，磁盘只做安全与大小校验。"""
        return await asyncio.to_thread(self._collect, self._attempt_path(attempt_id), manifest)

    def _destination(self, storage_path: str) -> Path:
        normalized = normalize_relative_path(storage_path)
        return self.download_directory.joinpath(*normalized.split("/"))

    def _stage(
        self, source: Path, staging: Path, manifest: tuple[ManifestEntry, ...]
    ) -> None:
        """把清单文件搬进暂存目录；清单外的东西一律不碰。

        暂存目录与内容存储同盘，装好后一次 rename 就位，规范位不会出现半成品。
        崩溃/重试时已搬入的文件留在暂存目录，按清单续搬，下轮整体核验。
        """
        if source.is_symlink() or staging.is_symlink() or staging.parent.is_symlink():
            raise DomainError(ErrorCode.INVALID_INPUT, "下载内容不能包含符号链接")
        staging.mkdir(parents=True, exist_ok=True)
        for item in manifest:
            path = normalize_relative_path(item.path)
            target = staging.joinpath(*path.split("/"))
            if target.exists() or target.is_symlink():
                continue
            origin = self._manifest_file(source, path, item.size)
            target.parent.mkdir(parents=True, exist_ok=True)
            origin.replace(target)

    def _verify_installed(self, target: Path, expected: ContentPreparation) -> None:
        """规范位必须与清单算出的身份完全一致：多一个条目、少一字节都不算装好。"""
        try:
            info = os.lstat(target)
        except FileNotFoundError:
            raise DomainError(ErrorCode.INVALID_STATE, "规范内容安装或校验失败") from None
        if stat.S_ISLNK(info.st_mode):
            raise DomainError(ErrorCode.INVALID_INPUT, "内容存储目录不能是符号链接")
        if expected.identity.kind is ContentKind.FILE:
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_size != expected.size
                or _file_digest(target) != expected.identity.digest
            ):
                raise DomainError(ErrorCode.INVALID_STATE, "规范内容安装或校验失败")
            return
        if not stat.S_ISDIR(info.st_mode):
            raise DomainError(ErrorCode.INVALID_STATE, "规范内容安装或校验失败")
        entries = _directory_entries(target, None, reject_control_files=False)
        if (
            directory_content_digest(entries) != expected.identity.digest
            or sum(entry.size for entry in entries) != expected.size
        ):
            raise DomainError(ErrorCode.INVALID_STATE, "规范内容安装或校验失败")

    def _install(
        self,
        attempt_id: int,
        expected: ContentPreparation,
        manifest: tuple[ManifestEntry, ...],
    ) -> None:
        source = self._attempt_path(attempt_id)
        destination = self._destination(expected.storage_path)
        if destination.exists() or destination.is_symlink():
            # 内容已在位（同内容去重或上一轮已装好）：只核验，不动源目录。
            self._verify_installed(destination, expected)
            return
        staging = self._staging_path(attempt_id)
        self._stage(source, staging, manifest)
        if not self._same(self._collect(staging, manifest), expected):
            raise DomainError(ErrorCode.INVALID_STATE, "暂存内容与完成清单不一致")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.parent.is_symlink():
            raise DomainError(ErrorCode.INVALID_INPUT, "内容存储目录不能是符号链接")
        if expected.identity.kind is ContentKind.FILE:
            staged = staging.joinpath(*normalize_relative_path(manifest[0].path).split("/"))
            staged.replace(destination)
        else:
            staging.replace(destination)
        self._verify_installed(destination, expected)

    async def install(
        self,
        attempt_id: int,
        expected: ContentPreparation,
        manifest: tuple[ManifestEntry, ...],
    ) -> None:
        await asyncio.to_thread(self._install, attempt_id, expected, manifest)

    def _cleanup_attempt(self, attempt_id: int) -> None:
        staging = self._staging_path(attempt_id)
        if staging.is_symlink():
            staging.unlink()
        elif staging.exists():
            shutil.rmtree(staging)
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
        """物理只读扫描（读模型对账/未登记文件收编用），不是入库入口。"""
        target = self._destination(storage_path)
        if target.is_symlink():
            raise DomainError(ErrorCode.INVALID_INPUT, "内容不能包含符号链接")
        if target.is_file():
            return []
        if not target.is_dir():
            raise DomainError(ErrorCode.INVALID_STATE, "下载内容目录不存在或布局无效")
        return sorted(_directory_entries(target, None), key=lambda entry: entry.path)

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
        # 不复用 scan_entries：它的物理遍历适合对账，目录型对象的身份已由清单确定。
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
