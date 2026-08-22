from __future__ import annotations

import asyncio
import errno
import io
import json
import logging
import os
import shutil
import tarfile
import threading
import time
import zipfile
from collections.abc import Buffer
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Callable, TypeVar, cast

import zstandard as zstd

from app.core.config import settings
from app.core.time_utils import ms_to_iso
from app.repositories.errors import RepositoryConflictError
from app.repositories.files import (
    cleanup_pack_source_reference,
    finish_pack_source_physical_cleanup,
    get_stored_file_by_identity,
    get_stored_file_by_real_path,
    set_pack_source_cleanup_real_path,
)
from app.repositories.pack import (
    PackAdmissionError,
    active_pack_reserved_bytes,
    cancel_active_pack_task,
    clear_pack_install_reservation,
    clear_terminal_pack_tasks,
    create_pending_pack_with_reservation,
    delete_user_pack_task,
    fail_active_pack_task,
    finalize_prepared_pack_task,
    get_pack_task_detail_row,
    get_pack_task_row,
    get_pack_task_status,
    get_user_pack_task_row,
    list_pack_dispatch_task_ids,
    list_pack_recovery_rows,
    list_pack_task_rows,
    list_pack_task_source_rows,
    list_user_pack_cleanup_rows,
    mark_pack_source_cleanup_error,
    mark_pack_task_packing_if_pending,
    mark_source_cleanup_complete,
    persist_pack_prepared,
    physical_budget_remaining_bytes,
    requeue_interrupted_pack_task,
    reserve_pack_install_bytes,
    resolve_pack_task_source_rows,
    schedule_pack_retry,
    set_pack_materialized_bytes,
    settle_user_pack_markers,
    update_pack_task_progress,
)
from app.domain.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from app.domain.error_text import fmt_gb
from app.domain.pack import is_pack_active_status, is_pack_terminal_status
from app.services.settings_service import (
    get_min_free_disk,
    get_pack_compression_level as get_configured_pack_compression_level,
    get_pack_format as get_configured_pack_format,
)
from app.services.file_service import resolve_file_ids
from app.services.storage_locks import (
    get_content_hash_lock,
    wait_for_content_readers_locked,
)
from app.services.task_broadcast import broadcast_task_update_to_subscribers
from app.services.storage_index import (
    CONTENT_HASH_V1,
    calculate_legacy_content_hash,
    content_identity_from_content_hash,
    scan_storage_path,
)
from app.services.usage_service import get_usage
from app.repositories.usage import rebuild_usage_from_authoritative_state

logger = logging.getLogger(__name__)

_pack_queue_lock = asyncio.Lock()
_running_tasks_lock = asyncio.Lock()

_ZSTD_LEVEL_MAP = [1, 2, 3, 5, 7, 9, 12, 15, 18, 22]
_CHUNK_SIZE = 1024 * 1024
_MAX_ARCHIVE_ENTRIES = 100_000
_MAX_ARCHIVE_PATH_BYTES = 4096
_MAX_TOTAL_ARCHIVE_PATH_BYTES = 16 * 1024 * 1024
_METADATA_FIXED_SLACK = 64 * 1024
_MATERIALIZED_UPDATE_BYTES = 8 * 1024 * 1024
_MATERIALIZED_UPDATE_SECONDS = 2.0
_PACK_RETRY_DELAYS = (0.25, 1.0, 5.0, 15.0)
_MAX_ACTIVE_PACK_JOBS = 1
_DISPATCH_SWEEP_SECONDS = 0.25
_T = TypeVar("_T")


class PackBoundaryError(ValueError):
    pass


class _BoundedSink(io.BufferedIOBase):
    def __init__(
        self,
        path: Path,
        *,
        max_bytes: int,
        min_free_bytes: int,
        cancel_event: threading.Event,
    ) -> None:
        self._file = path.open("w+b")
        self._max_bytes = max_bytes
        self._min_free_bytes = min_free_bytes
        self._cancel_event = cancel_event
        self._disk_path = path.parent

    def writable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._file.tell()

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        return self._file.seek(offset, whence)

    def fileno(self) -> int:
        return self._file.fileno()

    def write(self, data: Buffer, /) -> int:
        if self._cancel_event.is_set():
            raise InterruptedError("pack cancelled")
        current_size = os.fstat(self._file.fileno()).st_size
        target_extent = max(current_size, self._file.tell() + memoryview(data).nbytes)
        if target_extent > self._max_bytes:
            raise PackBoundaryError(
                f"打包输出 {fmt_gb(target_extent)} "
                f"超过预留空间 {fmt_gb(self._max_bytes)}"
            )
        growth = target_extent - current_size
        if growth:
            free = shutil.disk_usage(self._disk_path).free
            if free - growth < self._min_free_bytes:
                raise PackBoundaryError(
                    f"磁盘可用 {fmt_gb(free)}，"
                    f"低于最小预留 {fmt_gb(self._min_free_bytes)}，无法继续打包"
                )
        return self._file.write(data)

    def flush(self) -> None:
        if not self._file.closed:
            self._file.flush()

    def close(self) -> None:
        if not self._file.closed:
            self._file.close()
        super().close()


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


def _fsync_parent(path: Path) -> None:
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_fd = os.open(path, directory_flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _fsync_file_and_parent(
    path: Path, cancel_event: threading.Event | None = None
) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError("pack cancelled")
    with path.open("rb") as file_obj:
        os.fsync(file_obj.fileno())
    _fsync_parent(path.parent)


def _unlink_file_and_fsync_parent(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    _fsync_parent(path.parent)


def _durable_link_file(
    source: Path,
    target: Path,
    temporary: Path,
    cancel_event: threading.Event,
) -> bool:
    cleanup_pack_output(temporary)
    try:
        if cancel_event.is_set():
            raise InterruptedError("pack cancelled")
        try:
            os.link(source, temporary)
        except OSError as exc:
            fallback_errors = {
                errno.EXDEV,
                errno.EPERM,
                errno.EACCES,
                errno.EMLINK,
                getattr(errno, "ENOTSUP", errno.EOPNOTSUPP),
                errno.EOPNOTSUPP,
            }
            if exc.errno in fallback_errors:
                return False
            raise
        os.replace(temporary, target)
        try:
            _fsync_file_and_parent(target, cancel_event)
        except BaseException:
            _unlink_file_and_fsync_parent(target)
            raise
        return True
    finally:
        cleanup_pack_output(temporary)


def _source_cleanup_tombstone(path: Path, task_id: int, ordinal: int) -> Path:
    prefix = f".aria2deck-pack-delete-{task_id}-{ordinal}"
    if path.name == prefix:
        return path
    return path.with_name(prefix)


def _prepare_source_delete(
    original: Path,
    tombstone: Path,
    cancel_event: threading.Event,
) -> Path | None:
    if cancel_event.is_set():
        raise InterruptedError("pack cancelled")
    original_exists = original.exists() or original.is_symlink()
    tombstone_exists = tombstone.exists() or tombstone.is_symlink()
    if original == tombstone:
        if original_exists:
            return tombstone
        _fsync_parent(original.parent)
        return None
    if not original_exists:
        if tombstone_exists:
            return tombstone
        _fsync_parent(original.parent)
        return None
    if original.is_dir() and not original.is_symlink():
        if tombstone_exists:
            raise FileExistsError(tombstone)
        os.replace(original, tombstone)
        _fsync_parent(original.parent)
        return tombstone
    original.unlink()
    _fsync_parent(original.parent)
    return None


def _remove_tree_cancellable(
    root: Path,
    cancel_event: threading.Event,
) -> None:
    stack: list[tuple[Path, bool]] = [(root, False)]
    while stack:
        if cancel_event.is_set():
            raise InterruptedError("pack cancelled")
        path, visited = stack.pop()
        if not path.exists() and not path.is_symlink():
            continue
        if path.is_symlink() or not path.is_dir():
            path.unlink()
            continue
        if visited:
            path.rmdir()
            continue
        stack.append((path, True))
        with os.scandir(path) as entries:
            children = [Path(entry.path) for entry in entries]
        stack.extend((child, False) for child in children)
    _fsync_parent(root.parent)


def _durable_copy_file(
    source: Path,
    target: Path,
    temporary: Path,
    cancel_event: threading.Event,
    max_bytes: int,
    min_free_bytes: int,
) -> None:
    cleanup_pack_output(temporary)
    try:
        with source.open("rb") as source_obj, temporary.open("xb") as target_obj:
            while True:
                if cancel_event.is_set():
                    raise InterruptedError("pack cancelled")
                chunk = source_obj.read(_CHUNK_SIZE)
                if not chunk:
                    break
                copied = target_obj.tell() + len(chunk)
                if copied > max_bytes:
                    raise PackBoundaryError(
                        f"打包安装副本 {fmt_gb(copied)} "
                        f"超过预留空间 {fmt_gb(max_bytes)}"
                    )
                free = shutil.disk_usage(temporary.parent).free
                if free - len(chunk) < min_free_bytes:
                    raise PackBoundaryError(
                        f"磁盘可用 {fmt_gb(free)}，"
                        f"低于最小预留 {fmt_gb(min_free_bytes)}，无法继续打包"
                    )
                target_obj.write(chunk)
            target_obj.flush()
            os.fsync(target_obj.fileno())
        if cancel_event.is_set():
            raise InterruptedError("pack cancelled")
        os.replace(temporary, target)
        try:
            _fsync_file_and_parent(target, cancel_event)
        except BaseException:
            _unlink_file_and_fsync_parent(target)
            raise
    finally:
        cleanup_pack_output(temporary)


@dataclass(frozen=True, slots=True)
class _InstalledPrepared:
    path: Path
    created_by_this_attempt: bool


class _SourceNames(list[str]):
    def __init__(self, values: list[str], content_hashes: list[str]) -> None:
        super().__init__(values)
        self.content_hashes = content_hashes


@dataclass(slots=True)
class _ArchiveItem:
    path: Path
    arcname: str
    is_dir: bool
    size: int


@dataclass(slots=True)
class _ArchiveBudget:
    entries: int = 0
    path_bytes: int = 0

    def add(self, arcname: str) -> None:
        try:
            encoded_bytes = len(arcname.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise PackBoundaryError("打包路径编码无效") from exc
        if encoded_bytes > _MAX_ARCHIVE_PATH_BYTES:
            raise PackBoundaryError("打包路径过长")
        if self.entries >= _MAX_ARCHIVE_ENTRIES:
            raise PackBoundaryError("打包文件条目过多")
        if self.path_bytes + encoded_bytes > _MAX_TOTAL_ARCHIVE_PATH_BYTES:
            raise PackBoundaryError("打包路径元数据过大")
        self.entries += 1
        self.path_bytes += encoded_bytes


@dataclass(slots=True)
class _RunningPackJob:
    task: asyncio.Task[None]
    cancel_event: threading.Event
    user_id: int | None = None
    writer_task: asyncio.Task[None] | None = None
    thread_tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    phase: str = "queued"


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
    _blocked_user_ids: set[int] = set()
    _dispatcher_task: asyncio.Task[None] | None = None

    @classmethod
    def get_pack_format(cls) -> str:
        return get_configured_pack_format()

    @classmethod
    def get_compression_level(cls) -> int:
        return get_configured_pack_compression_level()

    @classmethod
    def is_any_task_running(cls) -> bool:
        return len(cls._running_tasks) > 0

    @staticmethod
    def _start_thread(
        job: _RunningPackJob,
        func: Callable[..., _T],
        *args: Any,
    ) -> asyncio.Task[_T]:
        task = asyncio.create_task(asyncio.to_thread(func, *args))
        job.thread_tasks.add(task)
        task.add_done_callback(job.thread_tasks.discard)
        return task

    @classmethod
    async def _run_thread(
        cls,
        job: _RunningPackJob,
        func: Callable[..., _T],
        *args: Any,
    ) -> _T:
        task = cls._start_thread(job, func, *args)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            job.cancel_event.set()
            await asyncio.gather(task, return_exceptions=True)
            raise

    @classmethod
    async def _run_optional_thread(
        cls,
        job: _RunningPackJob | None,
        cancel_event: threading.Event,
        func: Callable[..., _T],
        *args: Any,
    ) -> _T:
        if job is not None:
            return await cls._run_thread(job, func, *args)
        task = asyncio.create_task(asyncio.to_thread(func, *args))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            cancel_event.set()
            await asyncio.gather(task, return_exceptions=True)
            raise

    @staticmethod
    async def _wait_thread_tasks(job: _RunningPackJob) -> None:
        if job.thread_tasks:
            await asyncio.gather(*tuple(job.thread_tasks), return_exceptions=True)

    @classmethod
    async def submit(cls, task_id: int) -> bool:
        task_row = await get_pack_task_row(task_id)
        user_id = int(task_row["user_id"]) if task_row is not None else None
        async with _running_tasks_lock:
            if task_id in cls._running_tasks:
                return False
            if len(cls._running_tasks) >= _MAX_ACTIVE_PACK_JOBS:
                return False
            if user_id is not None and user_id in cls._blocked_user_ids:
                return False
            cancel_event = threading.Event()
            task = asyncio.create_task(cls.start_pack(task_id, user_id or 0, [], []))
            cls._running_tasks[task_id] = _RunningPackJob(
                task=task,
                cancel_event=cancel_event,
                user_id=user_id,
            )
            task.add_done_callback(partial(cls._consume_done, task_id))
        return True

    @classmethod
    def _consume_done(cls, task_id: int, task: asyncio.Task[None]) -> None:
        job = cls._running_tasks.get(task_id)
        if job is not None and job.task is task:
            cls._running_tasks.pop(task_id, None)
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            logger.exception("打包后台任务异常退出: task_id=%s", task_id)

    @classmethod
    async def start_dispatcher(cls) -> None:
        task = cls._dispatcher_task
        if task is not None and not task.done():
            return
        task = asyncio.create_task(
            cls._dispatcher_loop(),
            name="pack-dispatcher",
        )
        cls._dispatcher_task = task
        task.add_done_callback(cls._consume_dispatcher_done)

    @classmethod
    def _consume_dispatcher_done(cls, task: asyncio.Task[None]) -> None:
        if cls._dispatcher_task is task:
            cls._dispatcher_task = None
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            logger.exception("打包 dispatcher 异常退出")

    @classmethod
    async def _dispatcher_loop(cls) -> None:
        while True:
            try:
                async with _running_tasks_lock:
                    capacity = _MAX_ACTIVE_PACK_JOBS - len(cls._running_tasks)
                if capacity > 0:
                    task_ids = await list_pack_dispatch_task_ids(limit=capacity)
                    for task_id in task_ids:
                        await cls.submit(task_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("打包 dispatcher 扫描失败，将继续重试")
            await asyncio.sleep(_DISPATCH_SWEEP_SECONDS)

    @classmethod
    async def shutdown(cls) -> None:
        dispatcher = cls._dispatcher_task
        if dispatcher is not None:
            dispatcher.cancel()
            await asyncio.gather(dispatcher, return_exceptions=True)
        async with _running_tasks_lock:
            jobs = list(cls._running_tasks.values())
        for job in jobs:
            job.cancel_event.set()
            job.task.cancel()
        if jobs:
            await asyncio.gather(
                *(job.task for job in jobs),
                return_exceptions=True,
            )
            await asyncio.gather(
                *(cls._wait_thread_tasks(job) for job in jobs),
                return_exceptions=True,
            )

    @classmethod
    async def cancel_user_jobs(cls, user_id: int) -> None:
        async with _running_tasks_lock:
            cls._blocked_user_ids.add(user_id)
            jobs = [
                job for job in cls._running_tasks.values() if job.user_id == user_id
            ]
        for job in jobs:
            job.cancel_event.set()
            job.task.cancel()
        if jobs:
            await asyncio.gather(
                *(job.task for job in jobs),
                return_exceptions=True,
            )
            await asyncio.gather(
                *(cls._wait_thread_tasks(job) for job in jobs),
                return_exceptions=True,
            )

    @classmethod
    async def prepare_user_deletion(cls, user_id: int) -> bool:
        await cls.cancel_user_jobs(user_id)
        rows = await list_user_pack_cleanup_rows(user_id)
        for row in rows:
            if is_pack_active_status(str(row["status"])):
                await cancel_active_pack_task(user_id, int(row["id"]))

        current = asyncio.current_task()
        if current is None:
            raise RuntimeError("无法建立用户打包清理任务")
        cleanup_job = _RunningPackJob(
            current,
            threading.Event(),
            user_id=user_id,
            phase="user-delete",
        )
        try:
            rows = await list_user_pack_cleanup_rows(user_id)
            for row in rows:
                task_id = int(row["id"])
                sources = await list_pack_task_source_rows(task_id)
                if row["source_cleanup_pending"] and any(
                    source.get("cleanup_real_path") for source in sources
                ):
                    await cls._replay_source_cleanup(row, job=cleanup_job)
                if not await settle_user_pack_markers(task_id, user_id):
                    return False
                from app.services.storage import get_downloading_dir

                pack_dir = get_downloading_dir() / f"pack_{task_id}"
                await cls._run_thread(
                    cleanup_job,
                    _remove_tree_cancellable,
                    pack_dir,
                    cleanup_job.cancel_event,
                )
            await clear_terminal_pack_tasks(user_id)
            return not await list_user_pack_cleanup_rows(user_id)
        except asyncio.CancelledError:
            cleanup_job.cancel_event.set()
            await cls._wait_thread_tasks(cleanup_job)
            raise

    @classmethod
    async def unblock_user(cls, user_id: int) -> None:
        async with _running_tasks_lock:
            cls._blocked_user_ids.discard(user_id)

    @classmethod
    async def recover_startup(cls) -> None:
        current = asyncio.current_task()
        if current is None:
            raise RuntimeError("无法初始化打包恢复任务")
        startup_job = _RunningPackJob(current, threading.Event(), phase="startup")
        try:
            await rebuild_usage_from_authoritative_state()
            rows = await list_pack_recovery_rows()
            active_ids = {
                int(row["id"])
                for row in rows
                if row["status"] in {"pending", "packing"}
            }
            for row in rows:
                task_id = int(row["id"])
                try:
                    await cls._recover_one_startup(row, startup_job)
                except asyncio.CancelledError:
                    raise
                except (PackBoundaryError, RepositoryConflictError) as exc:
                    try:
                        await cls._update_task_error(task_id, str(exc))
                        cls._cleanup_pack_dir(task_id)
                    except Exception:
                        logger.exception(
                            "打包任务失败状态持久化异常，已隔离: task_id=%s",
                            task_id,
                        )
                except Exception:
                    logger.exception(
                        "单个打包任务启动恢复失败，已隔离: task_id=%s", task_id
                    )
            await cls._run_thread(
                startup_job, cls._cleanup_stale_pack_dirs, active_ids
            )
            await rebuild_usage_from_authoritative_state()
        except asyncio.CancelledError:
            startup_job.cancel_event.set()
            await cls._wait_thread_tasks(startup_job)
            raise

    @classmethod
    async def _recover_one_startup(
        cls, row: dict[str, Any], startup_job: _RunningPackJob
    ) -> None:
        task_id = int(row["id"])
        if row["status"] in {"pending", "packing"}:
            await clear_pack_install_reservation(task_id)
        if row["status"] == "packing" and row["prepared_content_hash"]:
            measured = await cls._run_thread(
                startup_job,
                cls._measure_pack_materialized_bytes,
                row,
                startup_job.cancel_event,
            )
            await set_pack_materialized_bytes(task_id, measured)
            await cls._finalize_prepared(
                row, startup_job.cancel_event, job=startup_job
            )
            return
        if row["status"] == "packing":
            cls._cleanup_pack_dir(task_id)
            await set_pack_materialized_bytes(task_id, 0)
            await requeue_interrupted_pack_task(task_id)
        elif row["status"] == "pending":
            cls._cleanup_pack_dir(task_id)
            await set_pack_materialized_bytes(task_id, 0)
        if row["source_cleanup_pending"]:
            await cls._replay_source_cleanup(row, job=startup_job)

    @classmethod
    async def submit_pending(cls) -> None:
        async with _running_tasks_lock:
            capacity = _MAX_ACTIVE_PACK_JOBS - len(cls._running_tasks)
        if capacity <= 0:
            return
        for task_id in await list_pack_dispatch_task_ids(limit=capacity):
            await cls.submit(task_id)

    @staticmethod
    def _measure_pack_materialized_bytes(
        task: dict[str, Any], cancel_event: threading.Event
    ) -> int:
        from app.services.storage import get_downloading_dir, get_store_path_for_hash

        total = 0
        pack_dir = get_downloading_dir() / f"pack_{int(task['id'])}"
        if pack_dir.is_dir() and not pack_dir.is_symlink():
            with os.scandir(pack_dir) as entries:
                for entry in entries:
                    if cancel_event.is_set():
                        raise InterruptedError("pack cancelled")
                    if entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
        content_hash = str(task.get("prepared_content_hash") or "")
        try:
            identity = content_identity_from_content_hash(content_hash)
            valid_legacy = identity.version != CONTENT_HASH_V1 or (
                len(content_hash) == 64 and all(char in "0123456789abcdef" for char in content_hash)
            )
            canonical = get_store_path_for_hash(content_hash) if valid_legacy else None
        except ValueError:
            canonical = None
        if canonical is not None and canonical.is_file() and not canonical.is_symlink():
            total += canonical.stat().st_size
        return min(max(0, int(task.get("reserved_bytes") or 0)), total)

    @staticmethod
    def _cleanup_stale_pack_dirs(active_ids: set[int]) -> None:
        from app.services.storage import get_downloading_dir, safe_delete_path

        downloading_dir = get_downloading_dir()
        for candidate in downloading_dir.iterdir():
            name = candidate.name
            if not candidate.is_dir() or not name.startswith("pack_"):
                continue
            raw_id = name.removeprefix("pack_")
            if raw_id.isdigit() and int(raw_id) in active_ids:
                continue
            safe_delete_path(
                base_dir=downloading_dir,
                target=candidate,
                recursive=True,
                allow_missing=True,
            )

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
        del abs_paths, file_ids, output_name, delete_source, source_names
        current = asyncio.current_task()
        if current is None:
            return
        owns_job = False
        async with _running_tasks_lock:
            job = cls._running_tasks.get(task_id)
            if job is None:
                job = _RunningPackJob(
                    current,
                    threading.Event(),
                    user_id=user_id if user_id > 0 else None,
                )
                cls._running_tasks[task_id] = job
                owns_job = True
        try:
            await cls._dispatch_persistent_pack(task_id, job, on_progress)
        finally:
            if owns_job:
                async with _running_tasks_lock:
                    if cls._running_tasks.get(task_id) is job:
                        cls._running_tasks.pop(task_id, None)

    @classmethod
    async def _dispatch_persistent_pack(
        cls,
        task_id: int,
        job: _RunningPackJob,
        on_progress: Callable[[int, int], None] | None,
    ) -> None:
        try:
            async with _pack_queue_lock:
                await cls._run_persistent_pack(task_id, job, on_progress)
        except asyncio.CancelledError:
            job.cancel_event.set()
            await cls._wait_thread_tasks(job)
            raise
        except InterruptedError:
            if await get_pack_task_status(task_id) in {"failed", "cancelled"}:
                cls._cleanup_pack_dir(task_id)
        except (PackBoundaryError, RepositoryConflictError) as exc:
            try:
                await cls._update_task_error(task_id, str(exc))
                cls._cleanup_pack_dir(task_id)
            except Exception:
                logger.exception(
                    "打包 deterministic 失败状态暂时无法持久化: task_id=%s",
                    task_id,
                )
                await cls._schedule_retry(task_id)
        except Exception:
            logger.exception("打包任务暂时失败，已持久化重试: task_id=%s", task_id)
            await cls._schedule_retry(task_id)

    @classmethod
    async def _schedule_retry(cls, task_id: int) -> None:
        task = await get_pack_task_row(task_id)
        if task is None or task["status"] not in {"pending", "packing", "completed"}:
            return
        if task["status"] == "packing" and not task["prepared_content_hash"]:
            cls._cleanup_pack_dir(task_id)
            await set_pack_materialized_bytes(task_id, 0)
            await clear_pack_install_reservation(task_id)
            await requeue_interrupted_pack_task(task_id)
            task = await get_pack_task_row(task_id)
            if task is None:
                return
        retry_count = min(1_000_000, int(task.get("retry_count") or 0) + 1)
        delay = _PACK_RETRY_DELAYS[
            min(retry_count - 1, len(_PACK_RETRY_DELAYS) - 1)
        ]
        await schedule_pack_retry(
            task_id,
            retry_count=retry_count,
            next_retry_at_ms=int(time.time() * 1000 + delay * 1000),
        )

    @classmethod
    async def _run_persistent_pack(
        cls,
        task_id: int,
        job: _RunningPackJob,
        on_progress: Callable[[int, int], None] | None,
    ) -> None:
        task = await get_pack_task_row(task_id)
        if task is None or task["status"] not in {"pending", "packing", "completed"}:
            return
        if task["status"] == "completed" and task["source_cleanup_pending"]:
            if not await cls._replay_source_cleanup(task, job=job):
                raise OSError("打包源清理尚未完成")
            return
        if task["prepared_content_hash"]:
            job.phase = "prepared"
            await cls._finalize_prepared(task, job.cancel_event, job=job)
            return
        if task["status"] != "pending":
            return
        if not await mark_pack_task_packing_if_pending(task_id):
            return
        task = await get_pack_task_row(task_id)
        if task is None:
            return
        try:
            sources, source_names, _file_ids, source_hashes = (
                await cls._resolve_task_sources(task)
            )
            job.phase = "hashing"
            await cls._run_thread(
                job,
                cls._validate_source_hashes,
                sources,
                source_hashes,
                job.cancel_event,
            )
            job.phase = "scanning"
            items = await cls._run_thread(
                job,
                cls._build_archive_items,
                sources,
                _SourceNames(source_names, source_hashes),
                job.cancel_event,
            )
            pack_format = cls.get_pack_format()
            base_name = cls._safe_archive_name(
                str(task["output_name"] or "archive"),
                "archive",
            )
            output_filename = f"{base_name}.{pack_format}"
            from app.services.storage import get_downloading_dir

            pack_dir = get_downloading_dir() / f"pack_{task_id}"
            pack_dir.mkdir(parents=True, exist_ok=True)
            partial_path = pack_dir / f"{output_filename}.partial"
            prepared_path = pack_dir / output_filename
            cleanup_pack_output(partial_path)
            cleanup_pack_output(prepared_path)
            await cls._write_and_prepare(
                task,
                job,
                items,
                partial_path,
                prepared_path,
                output_filename,
                on_progress,
            )
            refreshed = await get_pack_task_row(task_id)
            if refreshed and refreshed["prepared_content_hash"]:
                job.phase = "prepared"
                await cls._finalize_prepared(refreshed, job.cancel_event, job=job)
        except InterruptedError:
            raise
        except asyncio.CancelledError:
            job.cancel_event.set()
            await cls._wait_thread_tasks(job)
            raise
        except PackBoundaryError:
            raise
        except Exception:
            raise

    @classmethod
    async def _resolve_task_sources(
        cls,
        task: dict[str, Any],
    ) -> tuple[list[Path], list[str], list[int], list[str]]:
        source_rows = await list_pack_task_source_rows(int(task["id"]))
        resolved = await resolve_pack_task_source_rows(int(task["id"]))
        if not source_rows or len(resolved) != len(source_rows):
            raise PackBoundaryError("部分打包源身份已变化")
        expected_ordinals = list(range(len(source_rows)))
        if [int(row["ordinal"]) for row in source_rows] != expected_ordinals:
            raise PackBoundaryError("打包源顺序记录无效")
        base_dir = Path(settings.download_dir).resolve()
        sources: list[Path] = []
        names: list[str] = []
        file_ids: list[int] = []
        hashes: list[str] = []
        for row in resolved:
            source = Path(str(row["real_path"])).resolve()
            if base_dir not in source.parents or not source.exists():
                raise PackBoundaryError("部分打包源不可用")
            sources.append(source)
            names.append(str(row["display_name"] or "未命名"))
            file_ids.append(int(row["original_user_file_id"]))
            hashes.append(str(row["content_hash"] or ""))
        return sources, names, file_ids, hashes

    @staticmethod
    def _validate_source_hashes(
        sources: list[Path],
        content_hashes: list[str],
        cancel_event: threading.Event,
    ) -> None:
        for source, expected_hash in zip(sources, content_hashes):
            try:
                identity = content_identity_from_content_hash(expected_hash)
            except ValueError as exc:
                raise PackBoundaryError("打包源内容身份无效") from exc
            if identity.version == CONTENT_HASH_V1:
                if len(expected_hash) != 64 or any(
                    char not in "0123456789abcdef" for char in expected_hash
                ):
                    continue
                actual_hash = calculate_legacy_content_hash(source, cancel_event)
            else:
                actual_hash = scan_storage_path(source, cancel_event).content_hash
            if actual_hash != expected_hash:
                raise PackBoundaryError("打包源内容校验失败")


    @classmethod
    async def _write_and_prepare(
        cls,
        task: dict[str, Any],
        job: _RunningPackJob,
        items: list[_ArchiveItem],
        partial_path: Path,
        prepared_path: Path,
        output_filename: str,
        on_progress: Callable[[int, int], None] | None,
    ) -> None:
        tracker = _ProgressTracker(sum(item.size for item in items if not item.is_dir))
        job.phase = "writing"
        job.writer_task = cls._start_thread(
            job,
            cls._write_archive_sync,
            partial_path,
            cls.get_pack_format(),
            cls.get_compression_level(),
            items,
            tracker,
            job.cancel_event,
            int(task["reserved_bytes"]),
            get_min_free_disk(),
        )
        last_progress = -1
        last_materialized = int(task.get("materialized_bytes") or 0)
        last_materialized_at = time.monotonic()
        while not job.writer_task.done():
            _processed, _total, progress = tracker.snapshot()
            if progress != last_progress:
                await cls._update_task_progress(int(task["id"]), progress)
                last_progress = progress
                if on_progress:
                    on_progress(int(task["id"]), progress)
            try:
                current_extent = partial_path.stat().st_size
            except FileNotFoundError:
                current_extent = 0
            now = time.monotonic()
            if current_extent > last_materialized and (
                current_extent - last_materialized >= _MATERIALIZED_UPDATE_BYTES
                or now - last_materialized_at >= _MATERIALIZED_UPDATE_SECONDS
            ):
                await set_pack_materialized_bytes(int(task["id"]), current_extent)
                last_materialized = current_extent
                last_materialized_at = now
            await asyncio.sleep(0.2)
        await job.writer_task
        if job.cancel_event.is_set():
            raise InterruptedError("pack cancelled")
        size_bytes = partial_path.stat().st_size
        await set_pack_materialized_bytes(int(task["id"]), size_bytes)
        content_hash = (
            await cls._run_thread(job, scan_storage_path, partial_path, job.cancel_event)
        ).content_hash
        os.replace(partial_path, prepared_path)
        await cls._run_thread(
            job, _fsync_file_and_parent, prepared_path, job.cancel_event
        )
        persisted = await persist_pack_prepared(
            int(task["id"]),
            content_hash=content_hash,
            size_bytes=size_bytes,
            filename=output_filename,
        )
        if not persisted:
            cleanup_pack_output(prepared_path)
            raise InterruptedError("pack state changed")

    @classmethod
    async def _finalize_prepared(
        cls,
        task: dict[str, Any],
        cancel_event: threading.Event,
        *,
        job: _RunningPackJob | None = None,
    ) -> dict[str, Any] | None:
        if cancel_event.is_set():
            raise InterruptedError("pack cancelled")
        content_hash = str(task["prepared_content_hash"] or "")
        size_bytes = int(task["prepared_size_bytes"] or 0)
        filename = str(task["prepared_filename"] or "")
        try:
            identity = content_identity_from_content_hash(content_hash)
        except ValueError as exc:
            raise PackBoundaryError("打包恢复记录无效") from exc
        valid_hash = (
            identity.version != CONTENT_HASH_V1
            or (len(content_hash) == 64 and all(char in "0123456789abcdef" for char in content_hash))
        )
        if (
            not valid_hash
            or identity.object_kind not in {"legacy", "file"}
            or size_bytes < 0
            or Path(filename).name != filename
            or len(filename.encode("utf-8")) > 255
        ):
            raise PackBoundaryError("打包恢复记录无效")
        task_id = int(task["id"])
        terminal = False
        content_lock = await get_content_hash_lock(content_hash)
        async with content_lock:
            installed = await cls._install_prepared_file(
                task_id,
                content_hash=content_hash,
                size_bytes=size_bytes,
                filename=filename,
                cancel_event=cancel_event,
                job=job,
            )
            try:
                completed = await finalize_prepared_pack_task(
                    task_id,
                    content_hash=content_hash,
                    size_bytes=size_bytes,
                    filename=filename,
                    real_path=str(installed.path),
                )
            except asyncio.CancelledError:
                await cls._cleanup_unowned_install(
                    installed, content_hash, cancel_event, job
                )
                raise
            except RepositoryConflictError as exc:
                try:
                    status = await cls._terminalize_finalize_cas(task_id, str(exc))
                except Exception as transition_exc:
                    raise OSError("打包失败状态暂时无法持久化") from transition_exc
                terminal = status is None or is_pack_terminal_status(str(status))
                if terminal:
                    await cls._cleanup_unowned_install(
                        installed, content_hash, cancel_event, job
                    )
                    completed = None
                else:
                    raise
            except Exception:
                status = await get_pack_task_status(task_id)
                terminal = status is None or is_pack_terminal_status(str(status))
                if terminal:
                    await cls._cleanup_unowned_install(
                        installed, content_hash, cancel_event, job
                    )
                raise
            if completed is None and not terminal:
                try:
                    status = await cls._terminalize_finalize_cas(
                        task_id, "打包完成状态已变化"
                    )
                except Exception as transition_exc:
                    raise OSError("打包失败状态暂时无法持久化") from transition_exc
                terminal = status is None or is_pack_terminal_status(str(status))
                if terminal:
                    await cls._cleanup_unowned_install(
                        installed, content_hash, cancel_event, job
                    )
        if completed is None:
            if terminal:
                cls._cleanup_pack_dir(task_id)
            return None
        cls._cleanup_pack_dir(task_id)
        if completed["source_cleanup_pending"]:
            if not await cls._replay_source_cleanup(
                completed, job=job, cancel_event=cancel_event
            ):
                raise OSError("打包源清理尚未完成")
        return completed

    @classmethod
    async def _terminalize_finalize_cas(
        cls, task_id: int, message: str
    ) -> str | None:
        status = await get_pack_task_status(task_id)
        if status == "packing":
            await cls._update_task_error(task_id, message)
            status = await get_pack_task_status(task_id)
        return status

    @classmethod
    async def _cleanup_unowned_install(
        cls,
        installed: _InstalledPrepared,
        content_hash: str,
        cancel_event: threading.Event,
        job: _RunningPackJob | None,
    ) -> None:
        if not installed.created_by_this_attempt:
            return
        owner = await get_stored_file_by_real_path(str(installed.path))
        if owner is not None:
            return
        await cls._run_optional_thread(
            job,
            cancel_event,
            _unlink_file_and_fsync_parent,
            installed.path,
        )
        logger.info(
            "已清理未注册打包输出: content_hash=%s path=%s",
            content_hash,
            installed.path,
        )

    @classmethod
    async def _install_prepared_file(
        cls,
        task_id: int,
        *,
        content_hash: str,
        size_bytes: int,
        filename: str,
        cancel_event: threading.Event,
        job: _RunningPackJob | None,
    ) -> _InstalledPrepared:
        identity = content_identity_from_content_hash(content_hash)
        from app.services.storage import (
            get_downloading_dir,
            get_store_dir,
            get_store_path_for_hash,
        )

        async def matches(path: Path) -> bool:
            try:
                if path.is_symlink() or not path.is_file():
                    return False
                if path.stat().st_size != size_bytes:
                    return False
                if identity.version == CONTENT_HASH_V1:
                    actual_hash = await cls._run_optional_thread(
                        job, cancel_event, calculate_legacy_content_hash, path, cancel_event
                    )
                else:
                    actual_hash = (
                        await cls._run_optional_thread(
                            job, cancel_event, scan_storage_path, path, cancel_event
                        )
                    ).content_hash
                return actual_hash == content_hash
            except (OSError, ValueError):
                return False

        pack_dir = get_downloading_dir() / f"pack_{task_id}"
        prepared = pack_dir / filename
        store_dir = get_store_dir().resolve()
        stored = await get_stored_file_by_identity(identity)
        if stored is not None:
            existing = Path(str(stored["real_path"])).resolve(strict=False)
            if store_dir in existing.parents and await matches(existing):
                await cls._run_optional_thread(
                    job, cancel_event, _fsync_file_and_parent, existing, cancel_event
                )
                return _InstalledPrepared(existing, False)

        target = get_store_path_for_hash(content_hash)
        target.parent.mkdir(parents=True, exist_ok=True)
        target_owner = await get_stored_file_by_real_path(str(target))
        if (
            target_owner is not None
            and str(target_owner["content_hash"]) != content_hash
        ):
            raise PackBoundaryError("打包恢复目标已属于其他内容")
        prepared_valid = await matches(prepared)
        target_valid = await matches(target)
        if target_valid:
            await cls._run_optional_thread(
                job, cancel_event, _fsync_file_and_parent, target, cancel_event
            )
            return _InstalledPrepared(target, False)
        if not prepared_valid:
            if prepared.exists() or prepared.is_symlink() or target.exists():
                raise PackBoundaryError("打包恢复文件校验失败")
            raise PackBoundaryError("打包恢复文件缺失")
        if target.is_dir():
            raise PackBoundaryError("打包恢复目标冲突")
        temporary = target.with_name(f".{target.name}.pack-{task_id}.tmp")
        linked = await cls._run_optional_thread(
            job,
            cancel_event,
            _durable_link_file,
            prepared,
            target,
            temporary,
            cancel_event,
        )
        if linked:
            return _InstalledPrepared(target, True)

        min_free_bytes = get_min_free_disk()
        disk_available = max(
            0,
            shutil.disk_usage(target.parent).free - min_free_bytes,
        )
        if not await reserve_pack_install_bytes(
            task_id, size_bytes, disk_available
        ):
            raise PackBoundaryError(
                f"磁盘可用 {fmt_gb(disk_available)} 不足，"
                f"无法安装打包输出（需 {fmt_gb(size_bytes)}）"
            )
        copied = False
        try:
            await cls._run_optional_thread(
                job,
                cancel_event,
                _durable_copy_file,
                prepared,
                target,
                temporary,
                cancel_event,
                size_bytes,
                min_free_bytes,
            )
            copied = True
        finally:
            try:
                await clear_pack_install_reservation(task_id)
            except asyncio.CancelledError:
                if copied:
                    await cls._run_optional_thread(
                        job,
                        cancel_event,
                        _unlink_file_and_fsync_parent,
                        target,
                    )
                raise
            except Exception:
                logger.exception(
                    "打包安装临时预算释放失败，将由 finalize/recovery 修复: task_id=%s",
                    task_id,
                )
        return _InstalledPrepared(target, True)

    @staticmethod
    def _cleanup_pack_dir(task_id: int) -> None:
        from app.services.storage import get_downloading_dir, safe_delete_path

        pack_dir = get_downloading_dir() / f"pack_{task_id}"
        safe_delete_path(
            base_dir=get_downloading_dir(),
            target=pack_dir,
            recursive=True,
            allow_missing=True,
        )

    @classmethod
    async def _durable_delete_source_path(
        cls,
        task_id: int,
        ordinal: int,
        real_path: str,
        cancel_event: threading.Event,
        job: _RunningPackJob | None,
    ) -> None:
        from app.services.storage import get_store_dir, is_path_within_base

        base_dir = get_store_dir().resolve()
        path = Path(real_path)
        if not is_path_within_base(base_dir, path) or path.resolve(
            strict=False
        ) == base_dir:
            raise ValueError("打包源物理路径超出存储目录")
        tombstone = _source_cleanup_tombstone(path, task_id, ordinal)
        durable_path = await cls._run_optional_thread(
            job,
            cancel_event,
            _prepare_source_delete,
            path,
            tombstone,
            cancel_event,
        )
        if durable_path is None:
            return
        if str(durable_path) != real_path:
            persisted = await set_pack_source_cleanup_real_path(
                task_id, ordinal, str(durable_path)
            )
            if not persisted:
                raise RepositoryConflictError("源文件 tombstone 状态已变化")
        await cls._run_optional_thread(
            job,
            cancel_event,
            _remove_tree_cancellable,
            durable_path,
            cancel_event,
        )

    @classmethod
    async def _replay_source_cleanup(
        cls,
        task: dict[str, Any],
        *,
        job: _RunningPackJob | None = None,
        cancel_event: threading.Event | None = None,
    ) -> bool:
        if not task.get("source_cleanup_pending"):
            return True
        event = job.cancel_event if job is not None else cancel_event or threading.Event()
        task_id = int(task["id"])
        all_clean = True
        for source in await list_pack_task_source_rows(task_id):
            if event.is_set():
                raise InterruptedError("pack cancelled")
            state = str(source["cleanup_state"])
            real_path = source.get("cleanup_real_path")
            if state != "pending" and not real_path:
                continue
            ordinal = int(source["ordinal"])
            content_hash = str(source.get("content_hash") or "")
            if not content_hash:
                all_clean = False
                await mark_pack_source_cleanup_error(
                    task_id, ordinal, "源文件内容身份缺失"
                )
                continue
            content_lock = await get_content_hash_lock(content_hash)
            try:
                async with content_lock:
                    if event.is_set():
                        raise InterruptedError("pack cancelled")
                    affected_download_ids: list[int] = []
                    if state == "pending":
                        _outcome, affected_download_ids, real_path = (
                            await cleanup_pack_source_reference(task_id, ordinal)
                        )
                    if real_path:
                        await wait_for_content_readers_locked(content_hash)
                        await cls._durable_delete_source_path(
                            task_id,
                            ordinal,
                            str(real_path),
                            event,
                            job,
                        )
                        await finish_pack_source_physical_cleanup(
                            task_id, ordinal, None
                        )
                    for download_id in affected_download_ids:
                        await broadcast_task_update_to_subscribers(download_id)
            except (asyncio.CancelledError, InterruptedError):
                raise
            except Exception as exc:
                all_clean = False
                message = f"{type(exc).__name__}: {exc}"
                if real_path:
                    await finish_pack_source_physical_cleanup(
                        task_id, ordinal, message
                    )
                else:
                    await mark_pack_source_cleanup_error(task_id, ordinal, message)
                logger.exception(
                    "打包源清理将在后续重试: task_id=%s ordinal=%s",
                    task_id,
                    ordinal,
                )
        if not all_clean:
            return False
        return await mark_source_cleanup_complete(task_id)

    @classmethod
    def _write_archive_sync(
        cls,
        output_path: Path,
        pack_format: str,
        compression_level: int,
        items: list[_ArchiveItem],
        tracker: _ProgressTracker,
        cancel_event: threading.Event,
        max_bytes: int,
        min_free_bytes: int,
    ) -> None:
        if pack_format == "zip":
            cls._write_zip_sync(
                output_path,
                compression_level,
                items,
                tracker,
                cancel_event,
                max_bytes,
                min_free_bytes,
            )
            return
        cls._write_tar_zst_sync(
            output_path,
            compression_level,
            items,
            tracker,
            cancel_event,
            max_bytes,
            min_free_bytes,
        )

    @classmethod
    def _write_zip_sync(
        cls,
        output_path: Path,
        compression_level: int,
        items: list[_ArchiveItem],
        tracker: _ProgressTracker,
        cancel_event: threading.Event,
        max_bytes: int,
        min_free_bytes: int,
    ) -> None:
        with (
            _BoundedSink(
                output_path,
                max_bytes=max_bytes,
                min_free_bytes=min_free_bytes,
                cancel_event=cancel_event,
            ) as sink,
            zipfile.ZipFile(
                sink,
                mode="w",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=max(0, min(9, compression_level)),
                allowZip64=True,
            ) as archive,
        ):
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
        max_bytes: int,
        min_free_bytes: int,
    ) -> None:
        mapped_level = _ZSTD_LEVEL_MAP[max(0, min(9, compression_level))]
        compressor = zstd.ZstdCompressor(level=mapped_level, threads=-1)

        with _BoundedSink(
            output_path,
            max_bytes=max_bytes,
            min_free_bytes=min_free_bytes,
            cancel_event=cancel_event,
        ) as sink:
            with compressor.stream_writer(
                cast(BinaryIO, sink), closefd=False
            ) as zst_stream:
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
        cancel_event: threading.Event | None = None,
    ) -> list[_ArchiveItem]:
        cls._check_cancel(cancel_event)
        content_hashes = getattr(source_names, "content_hashes", None)
        if not source_names or len(source_names) != len(sources):
            source_names = [src.name for src in sources]
            content_hashes = None
        budget = _ArchiveBudget()
        if len(sources) == 1 and sources[0].is_dir():
            source = cls._unwrap_stored_directory(
                sources[0], (content_hashes or [""])[0], cancel_event
            )
            return cls._collect_directory_items(
                source,
                prefix="",
                include_root=False,
                budget=budget,
                cancel_event=cancel_event,
            )

        items: list[_ArchiveItem] = []
        used_roots: set[str] = set()
        for index, (source, raw_name) in enumerate(zip(sources, source_names)):
            cls._check_cancel(cancel_event)
            content_hash = content_hashes[index] if content_hashes else ""
            source_for_pack = (
                cls._unwrap_stored_directory(source, content_hash, cancel_event)
                if source.is_dir()
                else source
            )
            root = cls._deduplicate_root_name(
                cls._safe_archive_name(raw_name, source_for_pack.name),
                used_roots,
            )
            if source_for_pack.is_dir():
                items.extend(
                    cls._collect_directory_items(
                        source_for_pack,
                        prefix=root,
                        include_root=True,
                        budget=budget,
                        cancel_event=cancel_event,
                    )
                )
                continue
            item = _ArchiveItem(
                path=source_for_pack,
                arcname=root,
                is_dir=False,
                size=source_for_pack.stat().st_size,
            )
            budget.add(item.arcname)
            items.append(item)
        return items

    @classmethod
    def _collect_directory_items(
        cls,
        directory: Path,
        prefix: str,
        include_root: bool,
        budget: _ArchiveBudget,
        cancel_event: threading.Event | None = None,
    ) -> list[_ArchiveItem]:
        cls._check_cancel(cancel_event)
        items: list[_ArchiveItem] = []
        if include_root and prefix:
            arcname = prefix + "/"
            budget.add(arcname)
            items.append(
                _ArchiveItem(path=directory, arcname=arcname, is_dir=True, size=0)
            )

        for root, dir_names, file_names in os.walk(directory):
            cls._check_cancel(cancel_event)
            root_path = Path(root)
            safe_dir_names: list[str] = []
            for dirname in sorted(dir_names):
                cls._check_cancel(cancel_event)
                if (root_path / dirname).is_symlink():
                    continue
                safe_dir_names.append(dirname)
            dir_names[:] = safe_dir_names
            file_names.sort()
            rel_root = root_path.relative_to(directory)

            for dirname in dir_names:
                dir_path = root_path / dirname
                arcname = cls._join_arcname(prefix, rel_root, dirname, is_dir=True)
                budget.add(arcname)
                items.append(
                    _ArchiveItem(path=dir_path, arcname=arcname, is_dir=True, size=0)
                )

            for filename in file_names:
                cls._check_cancel(cancel_event)
                file_path = root_path / filename
                if file_path.is_symlink():
                    continue
                size = file_path.stat().st_size
                arcname = cls._join_arcname(prefix, rel_root, filename, is_dir=False)
                budget.add(arcname)
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

    @staticmethod
    def _check_cancel(cancel_event: threading.Event | None) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError("pack cancelled")

    @classmethod
    def _unwrap_stored_directory(
        cls,
        source_dir: Path,
        content_hash: str,
        cancel_event: threading.Event | None = None,
    ) -> Path:
        from app.services.storage import is_canonical_store_path

        cls._check_cancel(cancel_event)
        try:
            identity = content_identity_from_content_hash(content_hash)
        except ValueError:
            return source_dir
        if (
            identity.version != CONTENT_HASH_V1
            and identity.object_kind != "directory"
        ) or not is_canonical_store_path(source_dir, content_hash):
            return source_dir
        children = []
        with os.scandir(source_dir) as entries:
            for entry in entries:
                cls._check_cancel(cancel_event)
                children.append(Path(entry.path))
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
        await fail_active_pack_task(task_id, error)

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


def _estimate_pack_reservation(items: list[_ArchiveItem]) -> tuple[int, int]:
    source_size = sum(item.size for item in items if not item.is_dir)
    metadata_bytes = sum(
        512 + (2 * len(item.arcname.encode("utf-8"))) for item in items
    )
    slack = max(_METADATA_FIXED_SLACK, source_size // 100)
    return source_size, source_size + metadata_bytes + slack


async def _scan_archive_items_for_admission(
    sources: list[Path], source_names: list[str]
) -> list[_ArchiveItem]:
    cancel_event = threading.Event()
    scan_task = asyncio.create_task(
        asyncio.to_thread(
            PackTaskManager._build_archive_items,
            sources,
            source_names,
            cancel_event,
        )
    )
    try:
        return await asyncio.shield(scan_task)
    except asyncio.CancelledError:
        cancel_event.set()
        await asyncio.gather(scan_task, return_exceptions=True)
        raise


async def get_reserved_space() -> int:
    return await active_pack_reserved_bytes()


async def get_server_available_space() -> int:
    download_path = Path(settings.download_dir)
    download_path.mkdir(parents=True, exist_ok=True)
    disk_available = max(
        0,
        shutil.disk_usage(download_path).free - get_min_free_disk(),
    )
    return await physical_budget_remaining_bytes(disk_available)


async def get_pack_available_space_info(user_id: int) -> dict[str, int]:
    from app.repositories.auth import get_user_by_id

    user = await get_user_by_id(user_id)
    quota = int(user["quota_bytes"]) if user else 0
    usage = await get_usage(user_id, quota)
    return {
        "available": min(
            int(usage["available_bytes"]),
            await get_server_available_space(),
        ),
        "quota": quota,
        "used": int(usage["used_bytes"]),
    }


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
    task_id: int,
) -> dict:
    task = await get_user_pack_task_row(user_id, task_id)

    if not task:
        raise NotFoundError("任务不存在")

    task_status = task["status"]
    if is_pack_active_status(str(task_status)):
        await PackTaskManager.cancel_pack(task_id)
        refreshed_task = await cancel_active_pack_task(user_id, task_id)
        cancelled = bool(refreshed_task and refreshed_task["status"] == "cancelled")
        if cancelled:
            return {"ok": True, "message": "任务已取消"}

        task = await get_user_pack_task_row(user_id, task_id)
        if not task:
            raise NotFoundError("任务不存在")
        task_status = task["status"]
        if is_pack_terminal_status(str(task_status)):
            return {"ok": True, "message": "任务已结束"}

    if is_pack_terminal_status(str(task_status)):
        if not await delete_user_pack_task(user_id, task_id):
            raise BadRequestError("源文件清理记录尚未完成，暂不能删除任务")
        return {"ok": True, "message": "任务已删除"}

    raise BadRequestError("无法处理该任务状态")


def _validate_output_name(output_name: str | None) -> None:
    if not output_name:
        return
    if len(output_name) > 200:
        raise BadRequestError("输出文件名不能超过 200 个字符")
    if len(output_name.encode("utf-8")) > 200:
        raise BadRequestError("输出文件名不能超过 200 个字节")
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
    del quota_bytes
    if len(file_ids) != len(set(file_ids)):
        raise BadRequestError("文件列表包含重复项")
    resolved = await resolve_file_ids(user_id, file_ids)
    sources = [Path(path).resolve() for path, _size, _name in resolved]
    source_names = [str(name) for _path, _size, name in resolved]
    base_dir = Path(settings.download_dir).resolve()
    if any(base_dir not in source.parents or not source.exists() for source in sources):
        raise BadRequestError("部分打包源不可用")
    try:
        items = await _scan_archive_items_for_admission(sources, source_names)
    except PackBoundaryError as exc:
        raise BadRequestError(str(exc)) from exc
    except OSError as exc:
        raise BadRequestError("无法读取打包源") from exc
    source_size, reserved_bytes = _estimate_pack_reservation(items)
    if source_size == 0:
        raise BadRequestError("选中的文件为空")

    source_ids_json = json.dumps(sorted(file_ids), separators=(",", ":"))
    if not output_name and len(resolved) == 1:
        display_name = resolved[0][2]
        output_name = Path(display_name).stem or display_name
    _validate_output_name(output_name)
    download_path = Path(settings.download_dir)
    disk_available = max(
        0,
        shutil.disk_usage(download_path).free - get_min_free_disk(),
    )
    try:
        task_row = await create_pending_pack_with_reservation(
            user_id=user_id,
            source_user_file_ids_json=source_ids_json,
            source_size_bytes=source_size,
            reserved_bytes=reserved_bytes,
            output_name=output_name,
            delete_source=delete_source,
            disk_available_bytes=disk_available,
        )
    except PackAdmissionError as exc:
        if exc.reason == "quota":
            raise ForbiddenError(
                exc.message or "空间不足，无法冻结打包输出空间"
            ) from exc
        if exc.reason == "disk":
            raise ForbiddenError(exc.message or "磁盘可用空间不足") from exc
        if exc.reason == "duplicate":
            raise ConflictError("相同文件已有进行中的打包任务") from exc
        if exc.reason == "completed":
            raise ConflictError("相同文件已有打包完成的文件") from exc
        if exc.reason == "source":
            raise BadRequestError("部分打包源已不存在") from exc
        raise NotFoundError("用户不存在") from exc
    await PackTaskManager.submit(int(task_row["id"]))
    return pack_task_to_dict(dict(task_row))


async def list_pack_tasks(user_id: int) -> list[dict]:
    tasks = await list_pack_task_rows(user_id)
    return [pack_task_to_dict(dict(task)) for task in tasks]


async def get_pack_task(user_id: int, task_id: int) -> dict:
    task = await get_pack_task_detail_row(user_id, task_id)
    if not task:
        raise NotFoundError("任务不存在")
    return pack_task_to_dict(dict(task))
