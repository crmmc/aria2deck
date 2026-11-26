"""内容搬迁：一次读盘同时完成「复制到 V2 对象目录」与「按 V2 算法算摘要」。

V1 的摘要把真实 sha256 再套一层域分隔哈希，与 V2 不能换算，必须重算。
`--dry-run` 走同一条读盘路径，只是不落任何字节。
"""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path
from typing import BinaryIO

from aria2deck.domain.content import (
    ContentEntryIdentity,
    ContentKind,
    content_identity,
    directory_content_digest,
    normalize_relative_path,
)
from aria2deck.domain.errors import DomainError, ErrorCode

from . import MigrationAbort
from .planner import (
    ContentMigration,
    MigratedContent,
    Plan,
    StoredFilePlan,
    content_key,
    storage_path,
)

CHUNK_SIZE = 1024 * 1024


def objects_root(v2_root: Path) -> Path:
    return v2_root / "downloads" / "objects"


def _layout_invalid(message: str) -> DomainError:
    return DomainError(ErrorCode.INVALID_INPUT, message)


def _stream(source: Path, sink: BinaryIO | None) -> tuple[str, int]:
    """按块读一遍源文件：同时算 sha256，并（可选）写进目标临时文件。"""
    digest = hashlib.sha256()
    size = 0
    with source.open("rb") as reader:
        while chunk := reader.read(CHUNK_SIZE):
            digest.update(chunk)
            if sink is not None:
                sink.write(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _discard(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    elif path.exists() or path.is_symlink():
        path.unlink(missing_ok=True)


def _place(temporary: Path, destination: Path) -> None:
    """同内容已存在则丢掉临时文件、复用已有规范对象。"""
    if destination.exists():
        _discard(temporary)
    else:
        temporary.rename(destination)


class _ObjectBuilder:
    """按 dry-run 决定是否真的写字节；两条路径共用同一套读盘与摘要逻辑。"""

    def __init__(self, objects: Path, *, dry_run: bool) -> None:
        self.objects = objects
        self.dry_run = dry_run

    def file(self, stored: StoredFilePlan) -> MigratedContent:
        temporary = self.objects / f".tmp-file-{stored.v1_id}"
        if not self.dry_run:
            self.objects.mkdir(parents=True, exist_ok=True)
            _discard(temporary)
        try:
            if self.dry_run:
                digest, size = _stream(stored.source, None)
            else:
                with temporary.open("xb") as sink:
                    digest, size = _stream(stored.source, sink)
        except BaseException:
            if not self.dry_run:
                _discard(temporary)
            raise
        if not self.dry_run:
            _place(temporary, self.objects / f"file-{digest}")
        return _content("file", digest, size, (), stored.created_at_ms)

    def directory(self, stored: StoredFilePlan) -> MigratedContent:
        temporary = self.objects / f".tmp-directory-{stored.v1_id}"
        if not self.dry_run:
            self.objects.mkdir(parents=True, exist_ok=True)
            _discard(temporary)
            temporary.mkdir()
        try:
            entries, size = self._walk(stored.source, temporary)
            digest = directory_content_digest(list(entries))
        except BaseException:
            if not self.dry_run:
                _discard(temporary)
            raise
        if not self.dry_run:
            _place(temporary, self.objects / f"directory-{digest}")
        return _content("directory", digest, size, entries, stored.created_at_ms)

    def _walk(
        self, source: Path, temporary: Path
    ) -> tuple[tuple[ContentEntryIdentity, ...], int]:
        entries: list[ContentEntryIdentity] = []
        total = 0
        for parent, directories, files in os.walk(source, followlinks=False):
            parent_path = Path(parent)
            for name in directories:
                item = parent_path / name
                if item.is_symlink():
                    raise _layout_invalid("目录内容含符号链接")
                relative = _relative(item, source)
                if not self.dry_run:
                    (temporary.joinpath(*relative.split("/"))).mkdir(parents=True, exist_ok=True)
                entries.append(ContentEntryIdentity(relative, ContentKind.DIRECTORY, 0))
            for name in files:
                item = parent_path / name
                if item.is_symlink() or not item.is_file():
                    raise _layout_invalid("目录内有非普通文件")
                if name.endswith(".aria2"):
                    raise _layout_invalid("目录内残留 .aria2 控制文件")
                relative = _relative(item, source)
                if self.dry_run:
                    digest, size = _stream(item, None)
                else:
                    target = temporary.joinpath(*relative.split("/"))
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with target.open("xb") as sink:
                        digest, size = _stream(item, sink)
                entries.append(
                    ContentEntryIdentity(
                        relative,
                        ContentKind.FILE,
                        size,
                        content_identity(ContentKind.FILE, digest).digest,
                    )
                )
                total += size
        entries.sort(key=lambda entry: entry.path)
        return tuple(entries), total


def _relative(item: Path, root: Path) -> str:
    return normalize_relative_path(item.relative_to(root).as_posix())


def _content(
    kind: str,
    digest: str,
    size: int,
    entries: tuple[ContentEntryIdentity, ...],
    created_at_ms: int,
) -> MigratedContent:
    return MigratedContent(
        content_key=content_key(kind, digest),
        kind=kind,
        digest=digest,
        size=size,
        storage_path=storage_path(kind, digest),
        entries=entries,
        created_at_ms=created_at_ms,
    )


def _failure_reason(error: Exception) -> str:
    if isinstance(error, DomainError):
        return f"内容布局不符合 V2 规范：{error}"
    if isinstance(error, FileNotFoundError):
        return "磁盘文件缺失"
    return f"读取失败：{type(error).__name__}"


def migrate_contents(plan: Plan, v2_root: Path, *, dry_run: bool) -> ContentMigration:
    """把计划里的每一条内容搬到 `downloads/objects/`，按 V2 摘要去重。"""
    builder = _ObjectBuilder(objects_root(Path(v2_root)), dry_run=dry_run)
    by_key: dict[str, MigratedContent] = {}
    by_stored_file: dict[int, MigratedContent] = {}
    failures: list[tuple[int, str]] = []
    for stored in plan.stored_files:
        try:
            migrated = (
                builder.directory(stored) if stored.is_directory else builder.file(stored)
            )
        except MigrationAbort:
            raise
        except (DomainError, OSError) as error:
            failures.append((stored.v1_id, _failure_reason(error)))
            continue
        known = by_key.get(migrated.content_key)
        if known is None:
            by_key[migrated.content_key] = migrated
        elif known.size != migrated.size:
            failures.append((stored.v1_id, "同摘要内容大小不一致"))
            continue
        by_stored_file[stored.v1_id] = by_key[migrated.content_key]
    return ContentMigration(
        objects=tuple(by_key[key] for key in sorted(by_key)),
        by_stored_file=by_stored_file,
        failures=tuple(failures),
    )
