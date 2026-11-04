from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
from pathlib import Path
from typing import IO

from ..application.downloads.models import ContentPreparation
from ..domain.content import (
    ContentEntryIdentity,
    ContentKind,
    content_identity,
    directory_content_digest,
    normalize_relative_path,
)
from ..domain.errors import DomainError, ErrorCode


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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

    def _verify_prepared(self, storage_path: str) -> tuple[int, str]:
        target = self._destination(storage_path)
        if not target.is_file():
            raise DomainError(ErrorCode.INVALID_STATE, "prepared 输出文件不存在")
        return target.stat().st_size, _file_digest(target)

    async def verify_prepared(self, storage_path: str) -> tuple[int, str]:
        return await asyncio.to_thread(self._verify_prepared, storage_path)

    def _remove_prepared(self, storage_path: str) -> None:
        target = self._destination(storage_path)
        if target.exists() or target.is_symlink():
            target.unlink()

    async def remove_prepared(self, storage_path: str) -> None:
        await asyncio.to_thread(self._remove_prepared, storage_path)

    def _install_prepared(self, storage_path: str, digest: str) -> str:
        canonical = f"objects/file-{digest}"
        source = self._destination(storage_path)
        destination = self._destination(canonical)
        if destination.exists():
            # 规范对象已在位（此前安装过）：丢弃 part 文件即可。
            if source.exists() or source.is_symlink():
                source.unlink()
            return canonical
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.parent.is_symlink():
            raise DomainError(ErrorCode.INVALID_INPUT, "内容存储目录不能是符号链接")
        source.replace(destination)
        return canonical

    async def install_prepared(self, storage_path: str, digest: str) -> str:
        return await asyncio.to_thread(self._install_prepared, storage_path, digest)

    def _scan_entries(self, storage_path: str) -> list[ContentEntryIdentity]:
        return list(self._scan(self._destination(storage_path)).entries)

    async def scan_entries(self, storage_path: str) -> list[ContentEntryIdentity]:
        return await asyncio.to_thread(self._scan_entries, storage_path)
