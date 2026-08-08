from __future__ import annotations

import asyncio
import logging
import shutil
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.repositories.downloads import (
    claim_submitted_gid_for_failure,
    claim_terminal_reclaim,
    complete_active_user_tasks_for_stored_file,
    get_global_download_for_generation,
    get_global_download_status_snapshot,
    now_ms,
    reconcile_download_size,
)
from app.repositories.errors import RepositoryConflictError
from app.repositories.files import (
    create_stored_file_with_entries,
    delete_stored_file,
    get_stored_file_by_identity,
)
from app.services.storage import (
    get_downloading_dir,
    get_store_path_for_hash,
    safe_delete_path,
)
from app.services.settings_service import (
    get_max_task_size,
    get_min_free_disk,
)
from app.services.storage_locks import get_content_hash_lock
from app.services.storage_index import (
    StorageScan,
    content_identity_from_content_hash,
    StorageScanError,
    scan_storage_path,
    scan_storage_path_async,
)

logger = logging.getLogger(__name__)

_lifecycle_locks: dict[tuple[int, int], asyncio.Lock] = {}
_lifecycle_locks_guard = threading.Lock()


def _loop_id() -> int:
    return id(asyncio.get_running_loop())


async def get_download_lifecycle_lock(download_id: int) -> asyncio.Lock:
    key = (_loop_id(), download_id)
    with _lifecycle_locks_guard:
        lock = _lifecycle_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _lifecycle_locks[key] = lock
        return lock


def get_disk_available_bytes() -> int:
    download_path = Path(settings.download_dir)
    download_path.mkdir(parents=True, exist_ok=True)
    return max(0, shutil.disk_usage(download_path).free - get_min_free_disk())


def _status_int(value: Any, default: int = -1) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def candidate_size_from_status(
    status: Mapping[str, Any], *, require_trusted_total: bool = False
) -> tuple[int, int] | None:
    completed = max(0, _status_int(status.get("completedLength"), 0))
    selected_total = 0
    selected_count = 0
    selected_complete = True
    files = status.get("files")
    if isinstance(files, list):
        for item in files:
            if not isinstance(item, Mapping):
                selected_complete = False
                continue
            selected = item.get("selected")
            if selected is False or (
                isinstance(selected, str) and selected.lower() == "false"
            ):
                continue
            selected_count += 1
            length = _status_int(item.get("length"))
            if length < 0:
                selected_complete = False
                continue
            selected_total += length
    total = _status_int(status.get("totalLength"))
    trusted_total = total > 0 or (
        selected_count > 0 and selected_complete and selected_total > 0
    )
    if require_trusted_total and not trusted_total:
        return None
    candidate = max(total, selected_total, completed)
    if candidate < 0 or (candidate == 0 and not trusted_total):
        return None
    return candidate, completed


async def _scan_completed_source(source_path: Path) -> StorageScan:
    return await scan_storage_path_async(source_path, scanner=scan_storage_path)


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


async def _compensate_incomplete_completion(
    *,
    global_download_id: int,
    content_hash: str,
    store_path: Path,
    source_path: Path,
    moved_source: bool,
    created_stored_file_id: int | None,
    registration_started: bool,
) -> None:
    snapshot = await get_global_download_status_snapshot(global_download_id)
    if snapshot is not None and snapshot.get("completed_file_id") is not None:
        return

    stored_file_id = created_stored_file_id
    if stored_file_id is None and registration_started:
        candidate = await get_stored_file_by_identity(
            content_identity_from_content_hash(content_hash)
        )
        if candidate is not None and Path(str(candidate["real_path"])) == store_path:
            stored_file_id = int(candidate["id"])
    if stored_file_id is not None:
        await delete_stored_file(stored_file_id)
    if moved_source:
        _restore_moved_source(store_path, source_path)


async def _compensate_completion_safely(**kwargs: Any) -> None:
    compensation = asyncio.create_task(_compensate_incomplete_completion(**kwargs))
    try:
        await asyncio.shield(compensation)
    except asyncio.CancelledError:
        await asyncio.shield(compensation)


async def complete_global_download(
    *,
    global_download_id: int,
    expected_gid: str,
    source_path: Path,
    original_name: str,
    expected_size: int | None = None,
) -> dict[str, Any] | None:
    lifecycle_lock = await get_download_lifecycle_lock(global_download_id)
    async with lifecycle_lock:
        return await complete_global_download_locked(
            global_download_id=global_download_id,
            expected_gid=expected_gid,
            source_path=source_path,
            original_name=original_name,
            expected_size=expected_size,
        )


async def complete_global_download_locked(
    *,
    global_download_id: int,
    expected_gid: str,
    source_path: Path,
    original_name: str,
    expected_size: int | None = None,
) -> dict[str, Any] | None:
    source_path = Path(source_path)
    current = await get_global_download_for_generation(
        global_download_id, expected_gid
    )
    if current is None:
        return None
    if not source_path.exists():
        raise FileNotFoundError(f"Source path does not exist: {source_path}")

    try:
        scan = await _scan_completed_source(source_path)
    except StorageScanError:
        logger.warning("Rejected invalid completed source id=%s", global_download_id)
        return {"status": "invalid_source", "entries_created": 0, "user_files_created": 0}
    size_bytes = scan.size_bytes
    if expected_size is not None and size_bytes < expected_size:
        return {"status": "incomplete", "size_bytes": size_bytes, "entries_created": 0, "user_files_created": 0}
    admission = await reconcile_download_size(
        download_id=global_download_id,
        expected_gid=expected_gid,
        candidate_bytes=size_bytes,
        completed_bytes=size_bytes,
        size_limit_bytes=int(
            current.get("size_limit_bytes") or get_max_task_size()
        ),
        disk_available_bytes=get_disk_available_bytes,
    )
    if not admission.admitted:
        if admission.get("outcome") == "stale":
            return None
        return {
            "status": "rejected",
            "reason": admission.get("outcome"),
            "entries_created": 0,
            "user_files_created": 0,
        }

    identity = scan.content_identity
    content_hash = identity.content_hash
    is_directory = scan.is_directory
    content_lock = await get_content_hash_lock(content_hash)
    async with content_lock:
        stored_file = await get_stored_file_by_identity(identity)
        entries_created = 0
        moved_source = False
        created_stored_file_id: int | None = None
        registration_started = False
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
                raise RuntimeError("content store path already exists")
            store_path, moved_source = _move_to_content_store(source_path, content_hash)
            entry_templates = scan.entry_templates
            try:
                registration_started = True
                (
                    stored_file,
                    entries_created,
                ) = await create_stored_file_with_entries(
                    {
                        "content_hash": content_hash,
                        "content_hash_version": identity.version,
                        "content_object_kind": identity.object_kind,
                        "content_digest": identity.digest,
                        "real_path": str(store_path),
                        "size_bytes": size_bytes,
                        "is_directory": 1 if is_directory else 0,
                        "original_name": original_name,
                    },
                    entry_templates,
                )
                created_stored_file_id = int(stored_file["id"])
            except asyncio.CancelledError:
                await _compensate_completion_safely(
                    global_download_id=global_download_id,
                    content_hash=content_hash,
                    store_path=store_path,
                    source_path=source_path,
                    moved_source=moved_source,
                    created_stored_file_id=created_stored_file_id,
                    registration_started=registration_started,
                )
                raise
            except RepositoryConflictError:
                try:
                    existing = await get_stored_file_by_identity(identity)
                except asyncio.CancelledError:
                    await _compensate_completion_safely(
                        global_download_id=global_download_id,
                        content_hash=content_hash,
                        store_path=store_path,
                        source_path=source_path,
                        moved_source=moved_source,
                        created_stored_file_id=created_stored_file_id,
                        registration_started=registration_started,
                    )
                    raise
                if existing is None:
                    await _compensate_completion_safely(
                        global_download_id=global_download_id,
                        content_hash=content_hash,
                        store_path=store_path,
                        source_path=source_path,
                        moved_source=moved_source,
                        created_stored_file_id=None,
                        registration_started=registration_started,
                    )
                    raise
                registration_started = False
                stored_file = existing
                size_bytes = int(stored_file["size_bytes"])
            except Exception:
                await _compensate_completion_safely(
                    global_download_id=global_download_id,
                    content_hash=content_hash,
                    store_path=store_path,
                    source_path=source_path,
                    moved_source=moved_source,
                    created_stored_file_id=created_stored_file_id,
                    registration_started=registration_started,
                )
                raise

    try:
        completed_at_ms = now_ms()
        user_files_created = await complete_active_user_tasks_for_stored_file(
            global_download_id=global_download_id,
            expected_gid=expected_gid,
            stored_file_id=int(stored_file["id"]),
            size_bytes=size_bytes,
            original_name=original_name,
            completed_at_ms=completed_at_ms,
        )
    except asyncio.CancelledError:
        await _compensate_completion_safely(
            global_download_id=global_download_id,
            content_hash=content_hash,
            store_path=store_path,
            source_path=source_path,
            moved_source=moved_source,
            created_stored_file_id=created_stored_file_id,
            registration_started=registration_started,
        )
        raise
    except Exception:
        await _compensate_completion_safely(
            global_download_id=global_download_id,
            content_hash=content_hash,
            store_path=store_path,
            source_path=source_path,
            moved_source=moved_source,
            created_stored_file_id=created_stored_file_id,
            registration_started=registration_started,
        )
        raise

    if user_files_created is None:
        await _compensate_completion_safely(
            global_download_id=global_download_id,
            content_hash=content_hash,
            store_path=store_path,
            source_path=source_path,
            moved_source=moved_source,
            created_stored_file_id=created_stored_file_id,
            registration_started=registration_started,
        )
        return None

    if not moved_source:
        _delete_download_source(source_path, recursive=is_directory)

    return {
        "status": "completed",
        "entries_created": entries_created,
        "user_files_created": user_files_created,
    }


