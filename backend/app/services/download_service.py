from __future__ import annotations

import asyncio
import logging
import shutil
import threading
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from app.aria2.protocol import Aria2Gateway
from app.domain.task_policy import (
    CANCELABLE_TASK_STATUSES,
    RETRYABLE_DOWNLOAD_STATUSES,
    RETRYABLE_TASK_STATUSES,
)
from app.repositories.downloads import (
    attach_completed_file_to_user,
    cancel_active_user_task,
    complete_active_user_tasks_for_stored_file,
    count_active_user_tasks,
    create_user_task,
    get_user_task_by_id,
    get_or_create_global_download,
    get_user_task,
    now_ms,
    update_global_download,
    update_user_task,
)
from app.repositories.errors import RepositoryConflictError
from app.repositories.files import (
    create_stored_file_with_entries,
    delete_stored_file,
    get_stored_file_by_content_hash,
)
from app.services.hash import calculate_content_hash_async
from app.services.storage import (
    get_downloading_dir,
    get_store_path_for_hash,
    get_task_download_dir,
    safe_delete_path,
)
from app.services.settings_service import get_aria2_bt_stop_timeout_seconds
from app.services.storage_index import build_entry_templates
from app.services.usage_service import (
    release_reserved,
    reserve_bytes,
)

logger = logging.getLogger(__name__)
DUPLICATE_TASK_MESSAGE = "任务已存在"
_ALLOWED_USER_OPTIONS = frozenset(
    (
        "out",
        "header",
        "max-connection-per-server",
        "http-user",
        "http-passwd",
        "bt-tracker",
    )
)


class DuplicateTaskError(Exception):
    """Raised when a user already owns a non-retryable task for a resource."""


def _is_non_retryable_user_task(task: dict[str, Any] | None) -> bool:
    if task is None:
        return False
    return str(task.get("status") or "") not in RETRYABLE_TASK_STATUSES


def _raise_if_duplicate_user_task(task: dict[str, Any] | None) -> None:
    if _is_non_retryable_user_task(task):
        raise DuplicateTaskError(DUPLICATE_TASK_MESSAGE)


_download_locks: dict[tuple[int, int], asyncio.Lock] = {}
_download_locks_guard = threading.Lock()
_lifecycle_locks: dict[tuple[int, int], asyncio.Lock] = {}
_lifecycle_locks_guard = threading.Lock()
_user_task_locks: dict[tuple[int, int, int], asyncio.Lock] = {}
_user_task_locks_guard = threading.Lock()
_content_locks: dict[tuple[int, str], asyncio.Lock] = {}
_content_locks_guard = threading.Lock()


def _loop_id() -> int:
    return id(asyncio.get_running_loop())


async def _get_download_lock(download_id: int) -> asyncio.Lock:
    key = (_loop_id(), download_id)
    with _download_locks_guard:
        lock = _download_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _download_locks[key] = lock
        return lock


async def _get_lifecycle_lock(download_id: int) -> asyncio.Lock:
    key = (_loop_id(), download_id)
    with _lifecycle_locks_guard:
        lock = _lifecycle_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _lifecycle_locks[key] = lock
        return lock


async def _get_user_task_lock(user_id: int, global_download_id: int) -> asyncio.Lock:
    key = (_loop_id(), user_id, global_download_id)
    with _user_task_locks_guard:
        lock = _user_task_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _user_task_locks[key] = lock
        return lock


async def _get_content_lock(content_hash: str) -> asyncio.Lock:
    key = (_loop_id(), content_hash)
    with _content_locks_guard:
        lock = _content_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _content_locks[key] = lock
        return lock


async def _release_task_reservation(
    *, task: dict[str, Any], user_id: int, quota_bytes: int
) -> None:
    reserved_bytes = int(task.get("reserved_bytes") or 0)
    if reserved_bytes <= 0:
        return
    await release_reserved(user_id, reserved_bytes, quota_bytes=quota_bytes)
    await update_user_task(task["id"], {"reserved_bytes": 0})


async def _remove_submitted_gid(aria2_client: Aria2Gateway, gid: str) -> None:
    try:
        await aria2_client.force_remove(gid)
    except Exception:
        logger.exception("Failed to remove orphan aria2 download gid=%s", gid)


def _path_size_bytes(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    if path.is_dir():
        return sum(child.stat().st_size for child in path.rglob("*") if child.is_file())
    return 0


def _move_to_content_store(source_path: Path, content_hash: str) -> tuple[Path, bool]:
    store_path = get_store_path_for_hash(content_hash)
    store_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        source_path.rename(store_path)
    except OSError:
        if store_path.exists():
            return store_path, False
        raise
    return store_path, True


def _delete_download_source(source_path: Path, *, recursive: bool) -> None:
    if not source_path.exists() and not source_path.is_symlink():
        return
    safe_delete_path(
        base_dir=get_downloading_dir(),
        target=source_path,
        recursive=recursive,
    )


def _restore_moved_source(store_path: Path, source_path: Path) -> None:
    if not store_path.exists() or source_path.exists():
        return
    source_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(store_path), str(source_path))


def _normalize_out_option(value: Any) -> str:
    out = str(value)
    if not out or out in {".", ".."} or "/" in out or "\\" in out:
        raise ValueError(
            "invalid out option: must be a filename without path separators"
        )
    return out


def _validate_submit_options(options: Mapping[str, Any] | None) -> None:
    if not options or "out" not in options:
        return
    _normalize_out_option(options["out"])


async def complete_global_download(
    *,
    global_download_id: int,
    source_path: Path,
    original_name: str,
) -> dict[str, Any]:
    source_path = Path(source_path)
    if not source_path.exists():
        raise FileNotFoundError(f"Source path does not exist: {source_path}")

    content_hash = await calculate_content_hash_async(source_path)
    size_bytes = _path_size_bytes(source_path)
    is_directory = source_path.is_dir()

    lifecycle_lock = await _get_lifecycle_lock(global_download_id)
    async with lifecycle_lock:
        content_lock = await _get_content_lock(content_hash)
        async with content_lock:
            stored_file = await get_stored_file_by_content_hash(content_hash)
            entries_created = 0
            moved_source = False
            created_stored_file = False
            store_path = get_store_path_for_hash(content_hash)
            if stored_file:
                size_bytes = int(stored_file["size_bytes"])
                store_path = Path(str(stored_file["real_path"]))
                if not store_path.exists():
                    store_path.parent.mkdir(parents=True, exist_ok=True)
                    source_path.rename(store_path)
                    moved_source = True
            else:
                if store_path.exists():
                    entry_root = store_path
                else:
                    store_path, moved_source = _move_to_content_store(
                        source_path, content_hash
                    )
                    entry_root = store_path

                entry_templates = build_entry_templates(entry_root)
                try:
                    (
                        stored_file,
                        entries_created,
                    ) = await create_stored_file_with_entries(
                        {
                            "content_hash": content_hash,
                            "real_path": str(store_path),
                            "size_bytes": size_bytes,
                            "is_directory": 1 if is_directory else 0,
                            "original_name": original_name,
                        },
                        entry_templates,
                    )
                    created_stored_file = True
                except RepositoryConflictError:
                    existing = await get_stored_file_by_content_hash(content_hash)
                    if existing is None:
                        if moved_source:
                            _restore_moved_source(store_path, source_path)
                        raise
                    stored_file = existing
                    size_bytes = int(stored_file["size_bytes"])
                except Exception:
                    if moved_source:
                        _restore_moved_source(store_path, source_path)
                    raise

        try:
            completed_at_ms = now_ms()
            user_files_created = await complete_active_user_tasks_for_stored_file(
                global_download_id=global_download_id,
                stored_file_id=int(stored_file["id"]),
                size_bytes=size_bytes,
                original_name=original_name,
                completed_at_ms=completed_at_ms,
            )
        except Exception:
            if created_stored_file:
                await delete_stored_file(int(stored_file["id"]))
            if moved_source:
                _restore_moved_source(store_path, source_path)
            raise

        if not moved_source:
            _delete_download_source(source_path, recursive=is_directory)

        return {
            "status": "completed",
            "entries_created": entries_created,
            "user_files_created": user_files_created,
        }


async def create_user_download(
    *,
    user_id: int,
    quota_bytes: int,
    uri: str,
    resource_key: str,
    resource_kind: str,
    display_name: str | None,
    total_bytes: int,
    aria2_client: Aria2Gateway,
    options: Mapping[str, Any] | None = None,
    submit_uris: list[str] | None = None,
) -> dict[str, Any]:
    async def submit_download(submit_options: Mapping[str, Any] | None) -> str:
        return await aria2_client.add_uri(submit_uris or [uri], submit_options or {})

    return await _create_user_download_with_submit(
        user_id=user_id,
        quota_bytes=quota_bytes,
        source_uri=uri,
        resource_key=resource_key,
        resource_kind=resource_kind,
        display_name=display_name,
        total_bytes=total_bytes,
        aria2_client=aria2_client,
        options=options,
        server_options=None,
        submit_download=submit_download,
    )


async def create_user_torrent_download(
    *,
    user_id: int,
    quota_bytes: int,
    torrent_data: str,
    resource_key: str,
    source_uri: str,
    display_name: str | None,
    total_bytes: int,
    aria2_client: Aria2Gateway,
    options: Mapping[str, Any] | None = None,
    server_options: Mapping[str, Any] | None = None,
    uris: list[str] | None = None,
) -> dict[str, Any]:
    async def submit_download(submit_options: Mapping[str, Any] | None) -> str:
        return await aria2_client.add_torrent(
            torrent_data,
            uris or [],
            submit_options or {},
        )

    return await _create_user_download_with_submit(
        user_id=user_id,
        quota_bytes=quota_bytes,
        source_uri=source_uri,
        resource_key=resource_key,
        resource_kind="torrent",
        display_name=display_name,
        total_bytes=total_bytes,
        aria2_client=aria2_client,
        options=options,
        server_options=server_options,
        submit_download=submit_download,
    )


async def _create_user_download_with_submit(
    *,
    user_id: int,
    quota_bytes: int,
    source_uri: str,
    resource_key: str,
    resource_kind: str,
    display_name: str | None,
    total_bytes: int,
    aria2_client: Aria2Gateway,
    options: Mapping[str, Any] | None,
    server_options: Mapping[str, Any] | None = None,
    submit_download: Callable[[Mapping[str, Any] | None], Awaitable[str]],
) -> dict[str, Any]:
    _validate_submit_options(options)
    requested_total_bytes = max(0, int(total_bytes))
    global_values = {
        "resource_key": resource_key,
        "resource_kind": resource_kind,
        "source_uri": source_uri,
        "display_name": display_name,
        "total_bytes": requested_total_bytes,
    }
    global_download = await get_or_create_global_download(global_values)
    lifecycle_lock = await _get_lifecycle_lock(global_download["id"])
    async with lifecycle_lock:
        global_download = await get_or_create_global_download(global_values)
        if global_download["status"] in RETRYABLE_DOWNLOAD_STATUSES:
            updated_global = await update_global_download(
                global_download["id"],
                {
                    "aria2_gid": None,
                    "status": "queued",
                    "error_code": None,
                    "error_message": None,
                    "completed_at_ms": None,
                },
            )
            if updated_global is None:
                raise LookupError("global download not found")
            global_download = updated_global

        existing_task = await get_user_task(user_id, global_download["id"])
        _raise_if_duplicate_user_task(existing_task)

        effective_total_bytes = max(
            requested_total_bytes,
            max(0, int(global_download.get("total_bytes") or 0)),
        )
        completed_file_id = global_download.get("completed_file_id")
        if global_download["status"] == "completed" and completed_file_id is not None:
            return await attach_completed_file_to_user(
                user_id=user_id,
                quota_bytes=quota_bytes,
                global_download_id=int(global_download["id"]),
                stored_file_id=int(completed_file_id),
                size_bytes=int(
                    global_download["completed_bytes"] or effective_total_bytes
                ),
                display_name=str(
                    display_name or global_download.get("display_name") or source_uri
                ),
                finished_at_ms=int(global_download["completed_at_ms"] or now_ms()),
            )

        lock = await _get_user_task_lock(user_id, global_download["id"])
        async with lock:
            existing_task = await get_user_task(user_id, global_download["id"])
            _raise_if_duplicate_user_task(existing_task)

            reserved_bytes = effective_total_bytes
            reservation_made = False
            task: dict[str, Any] | None = existing_task
            if reserved_bytes > 0:
                await reserve_bytes(user_id, reserved_bytes, quota_bytes=quota_bytes)
                reservation_made = True

            try:
                task_values = {
                    "status": "queued",
                    "reserved_bytes": reserved_bytes,
                    "display_name": display_name,
                    "error_message": None,
                    "finished_at_ms": None,
                }
                if task:
                    updated_task = await update_user_task(task["id"], task_values)
                    if updated_task is None:
                        raise LookupError("user task not found")
                    task = updated_task
                else:
                    task = await create_user_task(
                        {
                            "user_id": user_id,
                            "global_download_id": global_download["id"],
                            **task_values,
                        }
                    )
            except RepositoryConflictError:
                if reservation_made:
                    await release_reserved(
                        user_id, reserved_bytes, quota_bytes=quota_bytes
                    )
                existing_task = await get_user_task(user_id, global_download["id"])
                if existing_task:
                    _raise_if_duplicate_user_task(existing_task)
                    return existing_task
                raise
            except Exception:
                if reservation_made:
                    await release_reserved(
                        user_id, reserved_bytes, quota_bytes=quota_bytes
                    )
                raise

        try:
            global_download = await _ensure_download_submitted(
                global_download=global_download,
                options=options,
                server_options=server_options,
                aria2_client=aria2_client,
                submit_download=submit_download,
            )
        except Exception as exc:
            await _release_task_reservation(
                task=task, user_id=user_id, quota_bytes=quota_bytes
            )
            await update_user_task(
                task["id"],
                {
                    "status": "failed",
                    "error_message": str(exc),
                },
            )
            raise

        if global_download["status"] == "active":
            updated_task = await update_user_task(task["id"], {"status": "active"})
            if updated_task:
                task = updated_task

        return task


async def _ensure_download_submitted(
    *,
    global_download: dict[str, Any],
    options: Mapping[str, Any] | None,
    server_options: Mapping[str, Any] | None,
    aria2_client: Aria2Gateway,
    submit_download: Callable[[Mapping[str, Any] | None], Awaitable[str]],
) -> dict[str, Any]:
    if global_download.get("aria2_gid") or global_download["status"] != "queued":
        return global_download

    lock = await _get_download_lock(global_download["id"])
    async with lock:
        current = await get_or_create_global_download(
            {
                "resource_key": global_download["resource_key"],
                "resource_kind": global_download["resource_kind"],
                "source_uri": global_download["source_uri"],
                "display_name": global_download["display_name"],
                "total_bytes": global_download["total_bytes"],
            }
        )
        if current.get("aria2_gid") or current["status"] != "queued":
            return current

        task_dir = get_task_download_dir(current["id"])

        submit_options: dict[str, Any] = {
            "dir": str(task_dir),
            "seed-time": "0",
            "bt-stop-timeout": str(get_aria2_bt_stop_timeout_seconds()),
        }
        if options:
            for key in _ALLOWED_USER_OPTIONS:
                if key in options:
                    if key == "out":
                        submit_options[key] = _normalize_out_option(options[key])
                    else:
                        submit_options[key] = str(options[key])
        if server_options:
            for key, value in server_options.items():
                submit_options[str(key)] = str(value)

        gid = await submit_download(submit_options)
        try:
            updated = await update_global_download(
                current["id"],
                {
                    "aria2_gid": gid,
                    "status": "active",
                },
            )
            if updated is None:
                raise RuntimeError("failed to persist submitted download")
            return updated
        except Exception:
            await _remove_submitted_gid(aria2_client, gid)
            raise


async def cancel_user_task(
    *,
    user_id: int,
    user_task_id: int,
    quota_bytes: int,
    aria2_client: Aria2Gateway,
) -> dict[str, Any]:
    task = await get_user_task_by_id(user_id, user_task_id)
    if task is None:
        raise LookupError("task not found")

    if task["status"] not in CANCELABLE_TASK_STATUSES:
        return task

    lifecycle_lock = await _get_lifecycle_lock(task["global_download_id"])
    async with lifecycle_lock:
        task = await get_user_task_by_id(user_id, user_task_id)
        if task is None:
            raise LookupError("task not found")
        if task["status"] not in CANCELABLE_TASK_STATUSES:
            return task

        active_count = await count_active_user_tasks(task["global_download_id"])
        should_cancel_global = active_count <= 1
        gid = task.get("aria2_gid")
        if should_cancel_global and gid:
            await aria2_client.force_remove(str(gid))

        cancelled = await cancel_active_user_task(
            user_id,
            user_task_id,
            error_message="用户取消",
            finished_at_ms=now_ms(),
        )
        if cancelled is None:
            latest = await get_user_task_by_id(user_id, user_task_id)
            if latest is None:
                raise LookupError("task not found")
            return latest

        if should_cancel_global:
            await update_global_download(
                task["global_download_id"],
                {
                    "status": "cancelled",
                    "aria2_gid": None,
                    "error_message": "用户取消",
                },
            )

        return cancelled
