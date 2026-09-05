from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import stat
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


from app.domain.content_identity import (
    CONTENT_HASH_V1,
    CONTENT_HASH_V2,
    LEGACY_OBJECT_KIND,
    ContentIdentity,
    content_identity_from_content_hash,
    content_identity_from_row,
    is_v2_digest,
    v2_content_identity,
)

__all__ = [
    "CONTENT_HASH_V2",
    "content_identity_from_content_hash",
    "content_identity_from_raw_file_digest",
]

MAX_STORAGE_ENTRIES = 100_000
MAX_STORAGE_PATH_DEPTH = 32
MAX_STORAGE_COMPONENT_BYTES = 255
MAX_STORAGE_RELATIVE_PATH_BYTES = 4096
_HASH_CHUNK_SIZE = 1024 * 1024
_FILE_DOMAIN = b"aria2deck-content-v2\x00file\x00"
_DIRECTORY_DOMAIN = b"aria2deck-content-v2\x00directory\x00"


class StorageScanError(ValueError):
    pass


@dataclass(frozen=True)
class StorageScan:
    content_hash: str
    size_bytes: int
    is_directory: bool
    entry_templates: list[dict[str, Any]]
    content_hash_version: str = CONTENT_HASH_V1
    content_object_kind: str = LEGACY_OBJECT_KIND
    content_digest: str | None = None

    @property
    def content_identity(self) -> ContentIdentity:
        return content_identity_from_row(
            {
                "content_hash": self.content_hash,
                "content_hash_version": self.content_hash_version,
                "content_object_kind": self.content_object_kind,
                "content_digest": self.content_digest or self.content_hash,
            }
        )


def _cancelled(event: threading.Event | None) -> None:
    if event is not None and event.is_set():
        raise InterruptedError("storage scan cancelled")


def _invalid(message: str) -> StorageScanError:
    return StorageScanError(f"下载内容布局无效：{message}")


def _kind(item_stat: os.stat_result) -> bool:
    if stat.S_ISLNK(item_stat.st_mode):
        raise _invalid("不支持符号链接")
    if stat.S_ISDIR(item_stat.st_mode):
        return True
    if stat.S_ISREG(item_stat.st_mode):
        return False
    raise _invalid("包含不支持的特殊文件")


def _template(relative: str, item_stat: os.stat_result, is_dir: bool) -> dict[str, Any]:
    parent, _, name = relative.rpartition("/")
    if relative == ".":
        parent, name = "", "."
    return {"relative_path": relative, "parent_path": parent, "name": name,
            "size_bytes": 0 if is_dir else item_stat.st_size, "is_dir": int(is_dir),
            "mtime_ms": int(item_stat.st_mtime * 1000),
            "sort_key": f"{parent}\0{'0' if is_dir else '1'}\0{name.lower()}"}


def _validate_relative(relative: str) -> None:
    if relative == ".":
        return
    parts = relative.split("/")
    try:
        path_bytes = len(relative.encode())
        component_bytes = [len(part.encode()) for part in parts]
    except UnicodeEncodeError as exc:
        raise _invalid("路径编码无效") from exc
    if len(parts) > MAX_STORAGE_PATH_DEPTH:
        raise _invalid("目录层级过深")
    if path_bytes > MAX_STORAGE_RELATIVE_PATH_BYTES:
        raise _invalid("相对路径过长")
    if any(size > MAX_STORAGE_COMPONENT_BYTES for size in component_bytes):
        raise _invalid("路径组件过长")


def _file_hash(
    path: Path,
    event: threading.Event | None,
    on_bytes_read: Callable[[int], None] | None = None,
) -> str:
    digest = hashlib.sha256()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise _invalid("无法安全读取文件") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise _invalid("包含不支持的特殊文件")
    except BaseException:
        os.close(fd)
        raise
    with os.fdopen(fd, "rb") as file:
        while chunk := file.read(_HASH_CHUNK_SIZE):
            _cancelled(event)
            digest.update(chunk)
            if on_bytes_read is not None:
                on_bytes_read(len(chunk))
    _cancelled(event)
    return digest.hexdigest()


def content_identity_from_raw_file_digest(raw_digest: str) -> ContentIdentity:
    # bytes.fromhex 接受 "" 与任意偶数长度十六进制，未校验会产出格式合法
    # 但错误的 v2:file 身份（CodeRabbit PR#8 二审）
    if not is_v2_digest(raw_digest):
        raise ValueError("invalid raw file digest")
    digest = hashlib.sha256(_FILE_DOMAIN)
    digest.update(bytes.fromhex(raw_digest))
    return v2_content_identity("file", digest.hexdigest())


def _v2_file_digest(raw_digest: str) -> str:
    return content_identity_from_raw_file_digest(raw_digest).digest


def _directory_digest(records: list[tuple[bytes, str, int, str | None]]) -> str:
    digest = hashlib.sha256(_DIRECTORY_DOMAIN)
    for record_type, relative, size, file_digest in records:
        relative_bytes = relative.encode("utf-8")
        body = record_type + len(relative_bytes).to_bytes(4, "big") + relative_bytes
        body += size.to_bytes(8, "big")
        if file_digest is not None:
            body += bytes.fromhex(file_digest)
        digest.update(len(body).to_bytes(8, "big"))
        digest.update(body)
    return digest.hexdigest()


def _v2_scan(
    *,
    digest: str,
    size_bytes: int,
    is_directory: bool,
    entries: list[dict[str, Any]],
) -> StorageScan:
    identity = v2_content_identity("directory" if is_directory else "file", digest)
    return StorageScan(
        content_hash=identity.content_hash,
        size_bytes=size_bytes,
        is_directory=is_directory,
        entry_templates=entries,
        content_hash_version=identity.version,
        content_object_kind=identity.object_kind,
        content_digest=identity.digest,
    )


def calculate_legacy_content_hash(
    root: Path,
    event: threading.Event | None = None,
    on_bytes_read: Callable[[int], None] | None = None,
) -> str:
    root = Path(root)
    try:
        root_stat = root.lstat()
    except FileNotFoundError as exc:
        raise ValueError(f"Path does not exist or is not a file/directory: {root}") from exc
    if not _kind(root_stat):
        return (
            _file_hash(root, event)
            if on_bytes_read is None
            else _file_hash(root, event, on_bytes_read)
        )
    digest, discovered = hashlib.sha256(), 1
    pending: list[tuple[Path, str, os.stat_result, bool]] = [(root, ".", root_stat, True)]
    while pending:
        _cancelled(event)
        path, relative, _item_stat, is_dir = pending.pop()
        if not is_dir:
            digest.update(relative.encode())
            file_hash = (
                _file_hash(path, event)
                if on_bytes_read is None
                else _file_hash(path, event, on_bytes_read)
            )
            digest.update(file_hash.encode())
            continue
        try:
            with os.scandir(path) as iterator:
                children = []
                for item in iterator:
                    if discovered >= MAX_STORAGE_ENTRIES:
                        raise _invalid("文件条目过多")
                    children.append((item.name, Path(item.path), item.stat(follow_symlinks=False)))
                    discovered += 1
        except OSError as exc:
            raise _invalid("无法扫描目录") from exc
        for name, child_path, child_stat in sorted(
            children, key=lambda item: item[0], reverse=True
        ):
            child_relative = name if relative == "." else f"{relative}/{name}"
            _validate_relative(child_relative)
            pending.append((child_path, child_relative, child_stat, _kind(child_stat)))
    return digest.hexdigest()


def scan_storage_path(
    root: Path,
    event: threading.Event | None = None,
    on_bytes_read: Callable[[int], None] | None = None,
) -> StorageScan:
    root = Path(root)
    _cancelled(event)
    try:
        root_stat = root.lstat()
    except FileNotFoundError as exc:
        raise ValueError(f"Path does not exist or is not a file/directory: {root}") from exc
    root_is_dir = _kind(root_stat)
    entries = [_template(".", root_stat, root_is_dir)]
    if not root_is_dir:
        raw_digest = (
            _file_hash(root, event)
            if on_bytes_read is None
            else _file_hash(root, event, on_bytes_read)
        )
        return _v2_scan(
            digest=content_identity_from_raw_file_digest(raw_digest).digest,
            size_bytes=root_stat.st_size,
            is_directory=False,
            entries=entries,
        )
    records: list[tuple[bytes, str, int, str | None]] = []
    total_size, discovered = 0, 1
    pending: list[tuple[Path, str, os.stat_result, bool]] = [(root, ".", root_stat, True)]
    while pending:
        _cancelled(event)
        path, relative, item_stat, is_dir = pending.pop()
        if not is_dir:
            raw_digest = (
                _file_hash(path, event)
                if on_bytes_read is None
                else _file_hash(path, event, on_bytes_read)
            )
            records.append((b"F", relative, item_stat.st_size, _v2_file_digest(raw_digest)))
            total_size += item_stat.st_size
            continue
        records.append((b"D", "" if relative == "." else relative, 0, None))
        try:
            with os.scandir(path) as iterator:
                children = []
                for item in iterator:
                    _cancelled(event)
                    if discovered >= MAX_STORAGE_ENTRIES:
                        raise _invalid("文件条目过多")
                    children.append((item.name, Path(item.path), item.stat(follow_symlinks=False)))
                    discovered += 1
        except OSError as exc:
            raise _invalid("无法扫描目录") from exc
        for name, child_path, child_stat in sorted(
            children, key=lambda item: item[0].encode(), reverse=True
        ):
            child_relative = name if relative == "." else f"{relative}/{name}"
            _validate_relative(child_relative)
            child_is_dir = _kind(child_stat)
            entries.append(_template(child_relative, child_stat, child_is_dir))
            pending.append((child_path, child_relative, child_stat, child_is_dir))
    entries.sort(key=lambda item: (item["relative_path"] != ".", item["parent_path"], item["sort_key"], item["relative_path"]))
    records.sort(key=lambda item: item[1].encode("utf-8"))
    return _v2_scan(
        digest=_directory_digest(records),
        size_bytes=total_size,
        is_directory=True,
        entries=entries,
    )


async def scan_storage_path_async(
    root: Path,
    cancel_event: threading.Event | None = None,
    *,
    scanner: Callable[[Path, threading.Event | None], StorageScan] = scan_storage_path,
) -> StorageScan:
    event = cancel_event or threading.Event()
    worker = asyncio.create_task(asyncio.to_thread(scanner, root, event))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        event.set()
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except InterruptedError:
                break
            except Exception as exc:  # noqa: BLE001  # cleanup must not mask cancellation
                # worker 在取消清理期间抛出业务异常：记录后退出循环走最终清理，
                # 不让业务异常覆盖调用方的取消语义（CodeRabbit #8）
                logger.debug("取消扫描清理期间 worker 异常 error_type=%s", type(exc).__name__)
                break
        try:
            worker.result()
        except Exception as exc:  # noqa: BLE001  # cancelled worker cleanup must not mask cancellation
            logger.debug("取消扫描时后台 worker 失败 error_type=%s", type(exc).__name__)
        raise


def build_entry_templates(root: Path) -> list[dict[str, Any]]:
    return scan_storage_path(root).entry_templates


def build_entries(stored_file_id: int, root: Path) -> list[dict[str, Any]]:
    return [{"stored_file_id": stored_file_id, **entry} for entry in build_entry_templates(root)]
