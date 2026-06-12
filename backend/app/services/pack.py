from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import shutil
import tarfile
import threading
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable

import zstandard as zstd

from app.core.config import settings
from app.core.time_utils import ms_to_iso
from app.repositories.files import (
    delete_user_file_reference,
    ensure_stored_file_with_user_ref,
)
from app.repositories.pack import (
    active_pack_reserved_bytes,
    cancel_active_pack_task,
    clear_terminal_pack_tasks,
    completed_pack_output_name,
    complete_packing_task,
    convert_reserved_to_used,
    create_pending_pack_task,
    delete_user_pack_task,
    fail_active_pack_task,
    get_pack_task_detail_row,
    get_pack_task_quota_snapshot,
    get_pack_task_row,
    get_pack_task_status,
    get_user_pack_task_row,
    get_user_quota_bytes,
    list_pack_task_rows,
    mark_pack_task_packing_if_pending,
    update_pack_task_progress,
)
from app.domain.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from app.domain.pack import is_pack_active_status, is_pack_terminal_status
from app.services.settings_service import (
    get_pack_compression_level as get_configured_pack_compression_level,
    get_pack_format as get_configured_pack_format,
)
from app.services.file_service import get_user_space_info, resolve_file_ids
from app.services.task_broadcast import broadcast_task_update_to_subscribers
from app.services.usage_service import get_usage, release_reserved, reserve_bytes

logger = logging.getLogger(__name__)

_pack_queue_lock = asyncio.Lock()
_running_tasks_lock = asyncio.Lock()
_pack_create_lock: asyncio.Lock | None = None
_pack_create_lock_loop: asyncio.AbstractEventLoop | None = None

SUPPORTED_FORMATS = ("zip", "tar.zst")
_ZSTD_LEVEL_MAP = [1, 2, 3, 5, 7, 9, 12, 15, 18, 22]
_CHUNK_SIZE = 1024 * 1024


def _get_pack_create_lock() -> asyncio.Lock:
    global _pack_create_lock, _pack_create_lock_loop
    loop = asyncio.get_running_loop()
    if _pack_create_lock is None or _pack_create_lock_loop is not loop:
        _pack_create_lock = asyncio.Lock()
        _pack_create_lock_loop = loop
    return _pack_create_lock


def cleanup_pack_output(output_path: Path) -> bool:
    """Safely delete pack output under download whitelist root."""
    from app.services.storage import safe_delete_path

    try:
        return safe_delete_path(
            base_dir=Path(settings.download_dir).resolve(),
            target=output_path,
            recursive=False,
            allow_missing=True,
        )
    except Exception as exc:
        logger.warning("Failed to clean up pack output %s: %s", output_path, exc)
        return False


@dataclass(slots=True)
class _ArchiveItem:
    path: Path
    arcname: str
    is_dir: bool
    size: int


@dataclass(slots=True)
class _RunningPackJob:
    task: asyncio.Task[None]
    cancel_event: threading.Event


@dataclass(frozen=True)
class DeleteUserFileReferenceResult:
    deleted: bool
    affected_download_ids: list[int]


async def _release_task_reservation(task: dict[str, Any]) -> None:
    reserved = int(task["reserved_bytes"] or 0)
    if reserved <= 0:
        return
    await release_reserved(int(task["user_id"]), reserved)


async def _convert_reserved_to_used(
    user_id: int,
    *,
    reserved_bytes: int,
    used_bytes: int,
) -> None:
    await convert_reserved_to_used(
        user_id,
        reserved_bytes=reserved_bytes,
        used_bytes=used_bytes,
    )


async def _delete_user_file_reference_v0_result(
    user_id: int,
    user_file_id: int,
    *,
    adjust_usage: bool = True,
) -> DeleteUserFileReferenceResult:
    from app.services.storage import get_store_dir, safe_delete_path

    deleted, affected_download_ids, real_path = await delete_user_file_reference(
        user_id,
        user_file_id,
        adjust_usage=adjust_usage,
    )
    if not deleted:
        return DeleteUserFileReferenceResult(False, [])
    if real_path is None:
        return DeleteUserFileReferenceResult(True, affected_download_ids)

    path = Path(real_path)
    safe_delete_path(
        base_dir=get_store_dir(),
        target=path,
        recursive=path.is_dir(),
        allow_missing=True,
    )
    return DeleteUserFileReferenceResult(True, affected_download_ids)


async def _delete_user_file_reference_v0(
    user_id: int,
    user_file_id: int,
    *,
    adjust_usage: bool = True,
) -> bool:
    return (
        await _delete_user_file_reference_v0_result(
            user_id,
            user_file_id,
            adjust_usage=adjust_usage,
        )
    ).deleted


async def _register_pack_output_v0(
    *,
    output_path: Path,
    original_name: str,
    user_id: int,
) -> tuple[int, int | None]:
    from app.services.hash import calculate_content_hash_async
    from app.services.storage import get_store_path_for_hash, safe_delete_path
    from app.services.storage_index import build_entry_templates

    content_hash = await calculate_content_hash_async(output_path)
    size_bytes = output_path.stat().st_size
    target_path = get_store_path_for_hash(content_hash)
    target_path.parent.mkdir(parents=True, exist_ok=True)

    if not target_path.exists():
        shutil.move(str(output_path), str(target_path))
    else:
        safe_delete_path(
            base_dir=output_path.parent,
            target=output_path,
            recursive=False,
            allow_missing=True,
        )

    return await ensure_stored_file_with_user_ref(
        user_id=user_id,
        content_hash=content_hash,
        real_path=str(target_path),
        size_bytes=size_bytes,
        is_directory=False,
        original_name=original_name,
        entry_templates=build_entry_templates(target_path),
    )


class _ProgressTracker:
    def __init__(self, total_bytes: int) -> None:
        self.total_bytes = max(0, total_bytes)
        self.processed_bytes = 0
        self._lock = threading.Lock()

    def add(self, size: int) -> None:
        if size <= 0:
            return
        with self._lock:
            self.processed_bytes += size

    def snapshot(self) -> tuple[int, int, int]:
        with self._lock:
            processed = self.processed_bytes
        if self.total_bytes <= 0:
            progress = 100
        else:
            progress = int((processed * 100) / self.total_bytes)
        progress = max(0, min(100, progress))
        return processed, self.total_bytes, progress


class _CancelAwareReader(io.RawIOBase):
    def __init__(
        self,
        source: io.BufferedReader,
        cancel_event: threading.Event,
        tracker: _ProgressTracker,
    ) -> None:
        self._source = source
        self._cancel_event = cancel_event
        self._tracker = tracker

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        if self._cancel_event.is_set():
            raise InterruptedError("pack cancelled")
        data = self._source.read(size)
        if data:
            self._tracker.add(len(data))
        return data


class PackTaskManager:
    _running_tasks: dict[int, _RunningPackJob] = {}

    @classmethod
    def get_pack_format(cls) -> str:
        return get_configured_pack_format()

    @classmethod
    def get_compression_level(cls) -> int:
        return get_configured_pack_compression_level()

    @classmethod
    def is_any_task_running(cls) -> bool:
        return len(cls._running_tasks) > 0

    @classmethod
    async def start_pack(
        cls,
        task_id: int,
        user_id: int,
        abs_paths: list[str],
        file_ids: list[int],
        output_name: str | None = None,
        delete_source: bool = False,
        source_names: list[str] | None = None,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> None:
        async with _pack_queue_lock:
            await cls._do_pack(
                task_id,
                user_id,
                abs_paths,
                file_ids,
                output_name,
                delete_source,
                source_names,
                on_progress,
            )

    @classmethod
    async def _do_pack(
        cls,
        task_id: int,
        user_id: int,
        abs_paths: list[str],
        file_ids: list[int],
        output_name: str | None = None,
        delete_source: bool = False,
        source_names: list[str] | None = None,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> None:
        from app.core.config import settings
        from app.services.storage import get_downloading_dir

        pack_dir = get_downloading_dir() / f"pack_{task_id}"
        pack_dir.mkdir(parents=True, exist_ok=True)
        sources = [Path(p) for p in abs_paths]
        if not sources:
            await cls._update_task_error(task_id, "No valid source files")
            return

        # 白名单校验：只允许 download_dir 及其子目录下的文件
        base_dir = Path(settings.download_dir).resolve()
        for source in sources:
            resolved = source.resolve()
            if not (resolved == base_dir or base_dir in resolved.parents):
                await cls._update_task_error(
                    task_id, f"路径不在允许范围内: {source.name}"
                )
                return
            if not source.exists():
                await cls._update_task_error(
                    task_id, f"Path does not exist: {source.name}"
                )
                return

        pack_format = cls.get_pack_format()
        compression = cls.get_compression_level()
        base_name = output_name or (sources[0].name if len(sources) == 1 else "archive")

        output_filename = f"{base_name}.{pack_format}"
        output_path = pack_dir / output_filename
        counter = 1
        while output_path.exists():
            output_filename = f"{base_name}_{counter}.{pack_format}"
            output_path = pack_dir / output_filename
            counter += 1

        task = await get_pack_task_row(task_id)
        if not task or task["status"] != "pending":
            logger.info("Pack task %s status changed, skipping", task_id)
            return
        if not await mark_pack_task_packing_if_pending(task_id):
            logger.info("Pack task %s status changed, skipping", task_id)
            return

        items = cls._build_archive_items(sources, source_names)
        total_bytes = sum(item.size for item in items if not item.is_dir)
        tracker = _ProgressTracker(total_bytes)
        cancel_event = threading.Event()
        current_task = asyncio.current_task()
        if current_task is None:
            await cls._update_task_error(task_id, "Cannot start pack task")
            return

        async with _running_tasks_lock:
            cls._running_tasks[task_id] = _RunningPackJob(
                task=current_task, cancel_event=cancel_event
            )

        if await get_pack_task_status(task_id) != "packing":
            cancel_event.set()

        writer_task: asyncio.Task[None] | None = None
        if not cancel_event.is_set():
            writer_task = asyncio.create_task(
                asyncio.to_thread(
                    cls._write_archive_sync,
                    output_path,
                    pack_format,
                    compression,
                    items,
                    tracker,
                    cancel_event,
                )
            )

        last_progress = -1
        extra_reserved = 0
        try:
            while writer_task is not None and not writer_task.done():
                _processed, _total, progress = tracker.snapshot()
                if progress != last_progress:
                    await cls._update_task_progress(task_id, progress)
                    last_progress = progress
                    if on_progress:
                        on_progress(task_id, progress)
                await asyncio.sleep(0.2)

            if writer_task is not None:
                await writer_task

            if cancel_event.is_set():
                cleanup_pack_output(output_path)
                return

            output_size = output_path.stat().st_size if output_path.exists() else 0

            stored_file_id = None
            output_user_file_id: int | None = None
            if output_path.exists():
                if not await cls._is_task_status(task_id, "packing"):
                    cleanup_pack_output(output_path)
                    return
                try:
                    (
                        stored_file_id,
                        output_user_file_id,
                    ) = await _register_pack_output_v0(
                        output_path=output_path,
                        original_name=output_filename,
                        user_id=user_id,
                    )
                except Exception:
                    logger.exception(
                        "Failed to register pack output: task_id=%s", task_id
                    )
                    cleanup_pack_output(output_path)
                    await cls._update_task_error(
                        task_id, "打包成功但注册文件失败，请重试"
                    )
                    return

            if output_user_file_id is not None and not await cls._is_task_status(
                task_id, "packing"
            ):
                try:
                    await _delete_user_file_reference_v0(
                        user_id,
                        output_user_file_id,
                        adjust_usage=False,
                    )
                except Exception:
                    logger.warning(
                        "Failed to rollback output ref: user_file_id=%s",
                        output_user_file_id,
                    )
                return

            if output_user_file_id is not None and output_size > 0:
                task_for_quota = await get_pack_task_quota_snapshot(task_id)
                if not task_for_quota or task_for_quota["status"] != "packing":
                    try:
                        await _delete_user_file_reference_v0(
                            user_id,
                            output_user_file_id,
                            adjust_usage=False,
                        )
                    except Exception:
                        logger.warning(
                            "Failed to rollback output ref: user_file_id=%s",
                            output_user_file_id,
                        )
                    return

                extra_required = max(
                    0, output_size - int(task_for_quota["reserved_bytes"] or 0)
                )
                if extra_required > 0:
                    try:
                        await reserve_bytes(user_id, extra_required)
                    except ValueError:
                        try:
                            await _delete_user_file_reference_v0(
                                user_id,
                                output_user_file_id,
                                adjust_usage=False,
                            )
                        except Exception:
                            logger.warning(
                                "Failed to rollback output ref: user_file_id=%s",
                                output_user_file_id,
                            )
                        await cls._update_task_error(
                            task_id, "空间不足。打包结果超过预留空间"
                        )
                        return
                    extra_reserved = extra_required

            completed_task = await complete_packing_task(
                task_id,
                output_stored_file_id=stored_file_id,
            )
            if completed_task is None:
                logger.warning("Pack task %s was cancelled during packing", task_id)
            if completed_task:
                await _convert_reserved_to_used(
                    user_id,
                    reserved_bytes=int(completed_task["reserved_bytes"] or 0)
                    + extra_reserved,
                    used_bytes=output_size if output_user_file_id is not None else 0,
                )
                extra_reserved = 0
                if stored_file_id and delete_source and file_ids:
                    for uf_id in file_ids:
                        try:
                            delete_result = (
                                await _delete_user_file_reference_v0_result(
                                    user_id,
                                    uf_id,
                                )
                            )
                            for download_id in delete_result.affected_download_ids:
                                await broadcast_task_update_to_subscribers(download_id)
                        except Exception:
                            logger.warning(
                                "Failed to delete source ref: user_file_id=%s", uf_id
                            )
            elif output_user_file_id is not None:
                if extra_reserved > 0:
                    await release_reserved(user_id, extra_reserved)
                    extra_reserved = 0
                try:
                    await _delete_user_file_reference_v0(
                        user_id,
                        output_user_file_id,
                        adjust_usage=False,
                    )
                except Exception:
                    logger.warning(
                        "Failed to rollback output ref: user_file_id=%s",
                        output_user_file_id,
                    )
        except InterruptedError:
            if extra_reserved > 0:
                await release_reserved(user_id, extra_reserved)
            cleanup_pack_output(output_path)
        except asyncio.CancelledError:
            cancel_event.set()
            if extra_reserved > 0:
                await release_reserved(user_id, extra_reserved)
            if writer_task is not None:
                try:
                    await writer_task
                except Exception as exc:
                    logger.debug(
                        "Pack writer task raised during cancellation task_id=%s",
                        task_id,
                        exc_info=exc,
                    )
            cleanup_pack_output(output_path)
            raise
        except Exception as exc:
            if extra_reserved > 0:
                await release_reserved(user_id, extra_reserved)
            cleanup_pack_output(output_path)
            await cls._update_task_error(task_id, str(exc))
        finally:
            async with _running_tasks_lock:
                cls._running_tasks.pop(task_id, None)
            # Clean up the temporary pack directory
            from app.services.storage import safe_delete_path, get_downloading_dir

            safe_delete_path(
                base_dir=get_downloading_dir(),
                target=pack_dir,
                recursive=True,
                allow_missing=True,
            )

    @classmethod
    def _write_archive_sync(
        cls,
        output_path: Path,
        pack_format: str,
        compression_level: int,
        items: list[_ArchiveItem],
        tracker: _ProgressTracker,
        cancel_event: threading.Event,
    ) -> None:
        if pack_format == "zip":
            cls._write_zip_sync(
                output_path, compression_level, items, tracker, cancel_event
            )
            return
        cls._write_tar_zst_sync(
            output_path, compression_level, items, tracker, cancel_event
        )

    @classmethod
    def _write_zip_sync(
        cls,
        output_path: Path,
        compression_level: int,
        items: list[_ArchiveItem],
        tracker: _ProgressTracker,
        cancel_event: threading.Event,
    ) -> None:
        with zipfile.ZipFile(
            output_path,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=max(0, min(9, compression_level)),
            allowZip64=True,
        ) as archive:
            for item in items:
                if cancel_event.is_set():
                    raise InterruptedError("pack cancelled")

                if item.is_dir:
                    archive.writestr(item.arcname.rstrip("/") + "/", b"")
                    continue

                with (
                    item.path.open("rb") as source,
                    archive.open(item.arcname, "w") as target,
                ):
                    while True:
                        if cancel_event.is_set():
                            raise InterruptedError("pack cancelled")
                        chunk = source.read(_CHUNK_SIZE)
                        if not chunk:
                            break
                        target.write(chunk)
                        tracker.add(len(chunk))

    @classmethod
    def _write_tar_zst_sync(
        cls,
        output_path: Path,
        compression_level: int,
        items: list[_ArchiveItem],
        tracker: _ProgressTracker,
        cancel_event: threading.Event,
    ) -> None:
        mapped_level = _ZSTD_LEVEL_MAP[max(0, min(9, compression_level))]
        compressor = zstd.ZstdCompressor(level=mapped_level, threads=-1)

        with output_path.open("wb") as sink:
            with compressor.stream_writer(sink, closefd=False) as zst_stream:
                with tarfile.open(fileobj=zst_stream, mode="w|") as archive:
                    for item in items:
                        if cancel_event.is_set():
                            raise InterruptedError("pack cancelled")

                        if item.is_dir:
                            stat_result = item.path.stat()
                            info = tarfile.TarInfo(item.arcname.rstrip("/") + "/")
                            info.type = tarfile.DIRTYPE
                            info.mode = stat_result.st_mode & 0o777
                            info.mtime = int(stat_result.st_mtime)
                            archive.addfile(info)
                            continue

                        stat_result = item.path.stat()
                        info = tarfile.TarInfo(item.arcname)
                        info.size = stat_result.st_size
                        info.mode = stat_result.st_mode & 0o777
                        info.mtime = int(stat_result.st_mtime)
                        with item.path.open("rb") as source:
                            reader = _CancelAwareReader(source, cancel_event, tracker)
                            archive.addfile(info, fileobj=reader)

    @classmethod
    def _build_archive_items(
        cls,
        sources: list[Path],
        source_names: list[str] | None,
    ) -> list[_ArchiveItem]:
        if not source_names or len(source_names) != len(sources):
            source_names = [src.name for src in sources]

        if len(sources) == 1 and sources[0].is_dir():
            source = cls._unwrap_hash_directory(sources[0], source_names[0])
            return cls._collect_directory_items(source, prefix="", include_root=False)

        items: list[_ArchiveItem] = []
        used_roots: set[str] = set()
        for source, raw_name in zip(sources, source_names):
            source_for_pack = (
                cls._unwrap_hash_directory(source, raw_name)
                if source.is_dir()
                else source
            )
            root = cls._safe_archive_name(raw_name, source_for_pack.name)
            root = cls._deduplicate_root_name(root, used_roots)

            if source_for_pack.is_dir():
                items.extend(
                    cls._collect_directory_items(
                        source_for_pack, prefix=root, include_root=True
                    )
                )
                continue

            size = source_for_pack.stat().st_size
            items.append(
                _ArchiveItem(
                    path=source_for_pack, arcname=root, is_dir=False, size=size
                )
            )

        return items

    @classmethod
    def _collect_directory_items(
        cls,
        directory: Path,
        prefix: str,
        include_root: bool,
    ) -> list[_ArchiveItem]:
        items: list[_ArchiveItem] = []
        if include_root and prefix:
            items.append(
                _ArchiveItem(path=directory, arcname=prefix + "/", is_dir=True, size=0)
            )

        for root, dir_names, file_names in os.walk(directory):
            root_path = Path(root)
            safe_dir_names: list[str] = []
            for dirname in sorted(dir_names):
                if (root_path / dirname).is_symlink():
                    continue
                safe_dir_names.append(dirname)
            dir_names[:] = safe_dir_names
            file_names.sort()
            rel_root = root_path.relative_to(directory)

            for dirname in dir_names:
                dir_path = root_path / dirname
                arcname = cls._join_arcname(prefix, rel_root, dirname, is_dir=True)
                items.append(
                    _ArchiveItem(path=dir_path, arcname=arcname, is_dir=True, size=0)
                )

            for filename in file_names:
                file_path = root_path / filename
                if file_path.is_symlink():
                    continue
                size = file_path.stat().st_size
                arcname = cls._join_arcname(prefix, rel_root, filename, is_dir=False)
                items.append(
                    _ArchiveItem(
                        path=file_path, arcname=arcname, is_dir=False, size=size
                    )
                )

        return items

    @staticmethod
    def _join_arcname(prefix: str, rel_root: Path, name: str, is_dir: bool) -> str:
        parts: list[str] = []
        if prefix:
            parts.append(prefix)
        if rel_root != Path("."):
            parts.extend(rel_root.parts)
        parts.append(name)
        arc = PurePosixPath(*parts).as_posix()
        return arc + "/" if is_dir else arc

    @staticmethod
    def _safe_archive_name(raw_name: str, fallback: str) -> str:
        name = (raw_name or "").strip()
        candidate = Path(name).name.strip() if name else ""
        if not candidate or candidate in {".", ".."}:
            candidate = Path(fallback).name.strip() if fallback else ""
        if not candidate or candidate in {".", ".."}:
            candidate = "archive"

        sanitized: list[str] = []
        for ch in candidate:
            code = ord(ch)
            if ch in {"/", "\\"} or code < 32 or code == 127:
                sanitized.append("_")
                continue
            sanitized.append(ch)

        normalized = "".join(sanitized).strip().strip(".")
        if not normalized or normalized in {".", ".."}:
            return "archive"
        return normalized

    @staticmethod
    def _deduplicate_root_name(name: str, used_names: set[str]) -> str:
        candidate = name
        suffix = 1
        while candidate in used_names:
            candidate = f"{name}_{suffix}"
            suffix += 1
        used_names.add(candidate)
        return candidate

    @classmethod
    def _unwrap_hash_directory(cls, source_dir: Path, _expected_name: str) -> Path:
        if not source_dir.is_dir():
            return source_dir
        if len(source_dir.name) != 64:
            return source_dir
        if not all(ch in "0123456789abcdefABCDEF" for ch in source_dir.name):
            return source_dir

        children = [child for child in source_dir.iterdir()]
        if len(children) != 1 or not children[0].is_dir():
            return source_dir

        return children[0]

    @classmethod
    async def _update_task_progress(cls, task_id: int, progress: int) -> None:
        await update_pack_task_progress(task_id, progress)

    @classmethod
    async def cancel_pack(cls, task_id: int) -> bool:
        async with _running_tasks_lock:
            job = cls._running_tasks.get(task_id)
        if not job:
            return False
        job.cancel_event.set()
        return True

    @classmethod
    async def _update_task_error(cls, task_id: int, error: str) -> None:
        task = await fail_active_pack_task(task_id, error)
        if task:
            await _release_task_reservation(dict(task))

    @classmethod
    async def _is_task_status(cls, task_id: int, expected_status: str) -> bool:
        return await get_pack_task_status(task_id) == expected_status


def calculate_folder_size(path: Path) -> int:
    total = 0
    try:
        for entry in path.rglob("*"):
            if entry.is_file():
                total += entry.stat().st_size
    except OSError as exc:
        logger.error("Failed to calculate folder size for %s: %s", path, exc)
    return total


async def get_reserved_space() -> int:
    return await active_pack_reserved_bytes()


async def get_server_available_space() -> int:
    download_path = Path(settings.download_dir)
    download_path.mkdir(parents=True, exist_ok=True)
    disk = shutil.disk_usage(download_path)
    reserved = await get_reserved_space()
    return max(0, disk.free - reserved)


async def get_user_available_space_for_pack(user_id: int) -> int:
    server_available = await get_server_available_space()
    user_quota = await get_user_quota_bytes(user_id)
    user_space = await get_usage(user_id, user_quota)
    user_quota_remaining = max(
        0,
        user_space["quota_bytes"]
        - user_space["used_bytes"]
        - user_space["reserved_bytes"],
    )
    return min(server_available, user_quota_remaining)


def pack_task_to_dict(task: dict[str, Any]) -> dict:
    response_status = "done" if task["status"] == "completed" else task["status"]
    return {
        "id": task["id"],
        "user_id": task["user_id"],
        "owner_id": task["user_id"],
        "source_user_file_ids": json.loads(task["source_user_file_ids_json"] or "[]"),
        "source_user_file_ids_json": task["source_user_file_ids_json"],
        "source_size_bytes": task["source_size_bytes"],
        "folder_path": task["source_user_file_ids_json"],
        "folder_size": task["source_size_bytes"],
        "reserved_bytes": task["reserved_bytes"],
        "reserved_space": task["reserved_bytes"],
        "output_name": task["output_name"],
        "output_stored_file_id": task["output_stored_file_id"],
        "stored_file_id": task["output_stored_file_id"],
        "delete_source": bool(task["delete_source"]),
        "output_size": task.get("output_size"),
        "output_path": None,
        "status": response_status,
        "progress": task["progress"],
        "error_message": task["error_message"],
        "created_at": ms_to_iso(task["created_at_ms"]),
        "updated_at": ms_to_iso(task["updated_at_ms"]),
        "finished_at": ms_to_iso(task["finished_at_ms"]),
    }


async def calculate_user_files_size(user_id: int, file_ids: list[int]) -> int:
    resolved = await resolve_file_ids(user_id, file_ids)
    return sum(size for _, size, _ in resolved)


async def clear_finished_pack_tasks(user_id: int) -> dict:
    return {"ok": True, "count": await clear_terminal_pack_tasks(user_id)}


async def cancel_or_delete_pack_task(
    user_id: int,
    quota_bytes: int,
    task_id: int,
) -> dict:
    task = await get_user_pack_task_row(user_id, task_id)

    if not task:
        raise NotFoundError("任务不存在")

    task_status = task["status"]
    if is_pack_active_status(str(task_status)):
        await PackTaskManager.cancel_pack(task_id)
        reserved_to_release = int(task["reserved_bytes"] or 0)
        refreshed_task = await cancel_active_pack_task(user_id, task_id)
        cancelled = bool(refreshed_task and refreshed_task["status"] == "cancelled")
        if cancelled and reserved_to_release > 0:
            await release_reserved(user_id, reserved_to_release, quota_bytes=quota_bytes)
        if cancelled:
            return {"ok": True, "message": "任务已取消"}

        task = await get_user_pack_task_row(user_id, task_id)
        if not task:
            raise NotFoundError("任务不存在")
        task_status = task["status"]

    if is_pack_terminal_status(str(task_status)):
        await delete_user_pack_task(user_id, task_id)
        return {"ok": True, "message": "任务已删除"}

    raise BadRequestError("无法处理该任务状态")


def _validate_output_name(output_name: str | None) -> None:
    if not output_name:
        return
    if len(output_name) > 200:
        raise BadRequestError("输出文件名不能超过 200 个字符")
    invalid_chars = set('/\\:*?"<>|\0')
    if invalid_chars & set(output_name):
        raise BadRequestError("输出文件名包含非法字符")


async def create_pack_task_from_user_files(
    *,
    user_id: int,
    quota_bytes: int,
    file_ids: list[int],
    output_name: str | None,
    delete_source: bool,
) -> dict:
    resolved = await resolve_file_ids(user_id, file_ids)
    abs_paths = [path for path, _, _ in resolved]
    source_names = [name for _, _, name in resolved]
    total_size = sum(size for _, size, _ in resolved)
    if total_size == 0:
        raise BadRequestError("选中的文件为空")

    source_user_file_ids_json = json.dumps(sorted(file_ids))
    reserved_bytes = total_size

    if not output_name and len(resolved) == 1:
        display_name = resolved[0][2]
        output_name = Path(display_name).stem or display_name
    _validate_output_name(output_name)

    async with _get_pack_create_lock():
        completed_name = await completed_pack_output_name(
            user_id=user_id,
            source_user_file_ids_json=source_user_file_ids_json,
        )
        if completed_name is not None:
            raise ConflictError(f"已存在打包完成的文件「{completed_name}」")

        try:
            await reserve_bytes(user_id, reserved_bytes, quota_bytes=quota_bytes)
        except ValueError:
            info = await get_user_space_info(user_id, quota_bytes)
            raise ForbiddenError(
                f"空间不足。需要: {reserved_bytes / 1024**3:.2f} GB, 可用: {info['available'] / 1024**3:.2f} GB"
            ) from None

        try:
            task_row = await create_pending_pack_task(
                user_id=user_id,
                source_user_file_ids_json=source_user_file_ids_json,
                source_size_bytes=total_size,
                reserved_bytes=reserved_bytes,
                output_name=output_name,
                delete_source=delete_source,
            )
            task_id = int(task_row["id"])
        except ValueError as exc:
            await release_reserved(user_id, reserved_bytes, quota_bytes=quota_bytes)
            if str(exc) == "active_duplicate":
                raise ConflictError("相同文件已有进行中的打包任务") from exc
            raise
        except Exception:
            await release_reserved(user_id, reserved_bytes, quota_bytes=quota_bytes)
            raise

    asyncio.create_task(
        PackTaskManager.start_pack(
            task_id,
            user_id,
            abs_paths,
            file_ids,
            output_name,
            delete_source,
            source_names=source_names,
        )
    )
    return pack_task_to_dict(dict(task_row))


async def list_pack_tasks(user_id: int) -> list[dict]:
    tasks = await list_pack_task_rows(user_id)
    return [pack_task_to_dict(dict(task)) for task in tasks]


async def get_pack_task(user_id: int, task_id: int) -> dict:
    task = await get_pack_task_detail_row(user_id, task_id)
    if not task:
        raise NotFoundError("任务不存在")
    return pack_task_to_dict(dict(task))
