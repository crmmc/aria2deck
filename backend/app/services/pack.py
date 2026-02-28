from __future__ import annotations

import asyncio
import io
import logging
import os
import shutil
import tarfile
import threading
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable

import zstandard as zstd
from sqlmodel import select

from app.core.config import settings
from app.database import get_session
from app.models import PackTask, User

logger = logging.getLogger(__name__)

_pack_queue_lock = asyncio.Lock()
_running_tasks_lock = asyncio.Lock()

SUPPORTED_FORMATS = ("zip", "tar.zst")
_ZSTD_LEVEL_MAP = [1, 2, 3, 5, 7, 9, 12, 15, 18, 22]
_CHUNK_SIZE = 1024 * 1024


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
        from app.routers.config import get_config_value

        val = get_config_value("pack_format")
        if val == "7z":
            return "tar.zst"
        return val if val in SUPPORTED_FORMATS else "zip"

    @classmethod
    def get_compression_level(cls) -> int:
        from app.routers.config import get_config_value

        val = get_config_value("pack_compression_level")
        try:
            level = int(val) if val else 5
            return max(0, min(9, level))
        except ValueError:
            return 5

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
        from app.services.storage import get_downloading_dir

        pack_dir = get_downloading_dir() / f"pack_{task_id}"
        pack_dir.mkdir(parents=True, exist_ok=True)
        sources = [Path(p) for p in abs_paths]
        if not sources:
            await cls._update_task_error(task_id, "No valid source files")
            return

        for source in sources:
            if not source.exists():
                await cls._update_task_error(task_id, f"Path does not exist: {source.name}")
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

        async with get_session() as db:
            task = await db.get(PackTask, task_id)
            if not task or task.status != "pending":
                logger.info("Pack task %s status changed, skipping", task_id)
                return
            task.status = "packing"
            task.output_path = str(output_path)
            task.updated_at = utc_now()
            db.add(task)

        items = cls._build_archive_items(sources, source_names)
        total_bytes = sum(item.size for item in items if not item.is_dir)
        tracker = _ProgressTracker(total_bytes)
        cancel_event = threading.Event()
        current_task = asyncio.current_task()
        if current_task is None:
            await cls._update_task_error(task_id, "Cannot start pack task")
            return

        async with _running_tasks_lock:
            cls._running_tasks[task_id] = _RunningPackJob(task=current_task, cancel_event=cancel_event)

        async with get_session() as db:
            task_state = await db.get(PackTask, task_id)
            if not task_state or task_state.status != "packing":
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
                    from app.services.storage import register_pack_output

                    stored_file, user_file = await register_pack_output(
                        output_path=output_path,
                        original_name=output_filename,
                        user_id=user_id,
                    )
                    stored_file_id = stored_file.id
                    if user_file is not None and user_file.id is not None:
                        output_user_file_id = user_file.id
                except Exception:
                    logger.exception("Failed to register pack output: task_id=%s", task_id)
                    cleanup_pack_output(output_path)
                    await cls._update_task_error(task_id, "打包成功但注册文件失败，请重试")
                    return

            if output_user_file_id is not None and not await cls._is_task_status(task_id, "packing"):
                from app.services.storage import delete_user_file_reference

                try:
                    await delete_user_file_reference(output_user_file_id)
                except Exception:
                    logger.warning("Failed to rollback output ref: user_file_id=%s", output_user_file_id)
                return

            if stored_file_id and delete_source and file_ids:
                from app.services.storage import delete_user_file_reference

                if not await cls._is_task_status(task_id, "packing"):
                    return

                for uf_id in file_ids:
                    try:
                        await delete_user_file_reference(uf_id)
                    except Exception:
                        logger.warning("Failed to delete source ref: user_file_id=%s", uf_id)

            async with get_session() as db:
                task = await db.get(PackTask, task_id)
                if task and task.status == "packing":
                    task.status = "done"
                    task.progress = 100
                    task.output_size = output_size
                    task.stored_file_id = stored_file_id
                    task.reserved_space = 0
                    task.updated_at = utc_now()
                    db.add(task)
                else:
                    logger.warning("Pack task %s was cancelled during packing", task_id)
        except InterruptedError:
            cleanup_pack_output(output_path)
        except asyncio.CancelledError:
            cancel_event.set()
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
            cls._write_zip_sync(output_path, compression_level, items, tracker, cancel_event)
            return
        cls._write_tar_zst_sync(output_path, compression_level, items, tracker, cancel_event)

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

                with item.path.open("rb") as source, archive.open(item.arcname, "w") as target:
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
            source_for_pack = cls._unwrap_hash_directory(source, raw_name) if source.is_dir() else source
            root = cls._safe_archive_name(raw_name, source_for_pack.name)
            root = cls._deduplicate_root_name(root, used_roots)

            if source_for_pack.is_dir():
                items.extend(cls._collect_directory_items(source_for_pack, prefix=root, include_root=True))
                continue

            size = source_for_pack.stat().st_size
            items.append(_ArchiveItem(path=source_for_pack, arcname=root, is_dir=False, size=size))

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
            items.append(_ArchiveItem(path=directory, arcname=prefix + "/", is_dir=True, size=0))

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
                items.append(_ArchiveItem(path=dir_path, arcname=arcname, is_dir=True, size=0))

            for filename in file_names:
                file_path = root_path / filename
                if file_path.is_symlink():
                    continue
                size = file_path.stat().st_size
                arcname = cls._join_arcname(prefix, rel_root, filename, is_dir=False)
                items.append(_ArchiveItem(path=file_path, arcname=arcname, is_dir=False, size=size))

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
        async with get_session() as db:
            task = await db.get(PackTask, task_id)
            if task and task.status == "packing":
                task.progress = progress
                task.updated_at = utc_now()
                db.add(task)

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
        async with get_session() as db:
            task = await db.get(PackTask, task_id)
            if task and task.status in ("pending", "packing"):
                task.status = "failed"
                task.error_message = error
                task.reserved_space = 0
                task.updated_at = utc_now()
                db.add(task)

    @classmethod
    async def _is_task_status(cls, task_id: int, expected_status: str) -> bool:
        async with get_session() as db:
            task = await db.get(PackTask, task_id)
            return bool(task and task.status == expected_status)


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
    async with get_session() as db:
        result = await db.exec(select(PackTask.reserved_space, PackTask.status))
        rows = result.all()
        total = 0
        for reserved_space, status in rows:
            if status in ("pending", "packing"):
                total += int(reserved_space or 0)
        return total


async def get_server_available_space() -> int:
    download_path = Path(settings.download_dir)
    download_path.mkdir(parents=True, exist_ok=True)
    disk = shutil.disk_usage(download_path)
    reserved = await get_reserved_space()
    return max(0, disk.free - reserved)


async def get_user_available_space_for_pack(user_id: int) -> int:
    from app.services.storage import get_user_space_info

    server_available = await get_server_available_space()
    async with get_session() as db:
        result = await db.exec(select(User).where(User.id == user_id))
        user = result.first()
    user_quota = user.quota if user else 0
    user_space = await get_user_space_info(user_id, user_quota)
    user_quota_remaining = max(0, user_space["quota"] - user_space["used"] - user_space["frozen"])
    return min(server_available, user_quota_remaining)
