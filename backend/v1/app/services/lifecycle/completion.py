"""Completion paths extracted from ``aria2_lifecycle_service.py`` and
``download_service.py`` (M4 T08).

Hosts the v0 completion entry point (``handle_v0_download_complete``),
the source resolution helpers, and the content-store admission pipeline
(``complete_global_download`` / ``complete_global_download_locked``).
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.time_utils import now_ms
from app.domain.locks import get_download_lifecycle_lock
from app.domain.quota import get_disk_available_bytes
from app.modules.backend.port import BackendPort
from app.repositories.errors import RepositoryConflictError
from app.repositories.files import (
    create_stored_file_with_entries,
    delete_stored_file,
    get_stored_file_by_identity,
)
from app.repositories.task.downloads import (
    get_global_download_for_generation,
    get_global_download_status_snapshot,
    reconcile_download_size,
)
from app.repositories.task.user_tasks import complete_active_user_tasks_for_stored_file
from app.services import download_ops
from app.services.lifecycle.cleanup import (
    _reclaim_terminal_with_claim,
    _remove_download_result_best_effort,
    fail_download_and_reclaim,
)
from app.services.lifecycle.handoff import (
    COMPLETE_SOURCE_RETRY_COUNT,
    COMPLETE_SOURCE_RETRY_INTERVAL,
    defer_metadata_completion_if_handoff_pending,
    switch_to_late_followed_download_if_supported,
)
from app.services.settings_service import (
    get_max_task_size,
    get_min_free_disk,
)
from app.services.storage import (
    cleanup_task_download_dir,
    get_downloading_dir,
    get_store_path_for_hash,
    safe_delete_path,
)
from app.services.storage_index import (
    StorageScan,
    StorageScanError,
    content_identity_from_content_hash,
    scan_storage_path,
    scan_storage_path_async,
)
from app.services.storage_locks import get_content_hash_lock
from app.services.task_projection import METADATA_NAME_PREFIX

logger = logging.getLogger(__name__)

DOWNLOAD_DIR_NOT_FOUND_MESSAGE = "下载完成但下载目录不存在"
DOWNLOAD_FILE_NOT_FOUND_MESSAGE = "下载完成但下载文件未找到"
COMPLETED_SIZE_MISMATCH_MESSAGE = "下载完成但文件大小不匹配"


def _status_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return False


def _list_task_dir_entries(task_dir: Path) -> list[Path]:
    if not task_dir.exists() or not task_dir.is_dir():
        return []
    try:
        return [p for p in task_dir.iterdir() if not p.name.endswith(".aria2")]
    except OSError as exc:
        logger.error("Failed to list task directory %s: %s", task_dir, exc)
        return []


def source_not_found_error(task_dir: Path) -> tuple[str, str]:
    if not task_dir.exists() or not task_dir.is_dir():
        return "download_dir_not_found", DOWNLOAD_DIR_NOT_FOUND_MESSAGE
    return "download_file_not_found", DOWNLOAD_FILE_NOT_FOUND_MESSAGE


def expected_completed_size(
    aria2_status: dict[str, Any],
    source_path: Path,
) -> int | None:
    files = aria2_status.get("files", [])
    if isinstance(files, list) and files:
        expected = 0
        has_length = False
        has_file_item = False
        has_selected_file = False
        for item in files:
            if not isinstance(item, dict):
                continue
            has_file_item = True
            if "selected" in item and not _status_bool(item.get("selected")):
                continue
            has_selected_file = True
            raw_length = item.get("length")
            length = download_ops.safe_int(raw_length, default=-1)
            if length < 0:
                continue
            expected += length
            has_length = True
        if has_length:
            return expected
        if has_file_item and not has_selected_file:
            return 0

    total_length = download_ops.safe_int(aria2_status.get("totalLength"), default=-1)
    if total_length < 0:
        return None
    if source_path.is_dir() or total_length > 0:
        return total_length
    return None


def completed_size_mismatch_error(
    *,
    source_path: Path,
    expected_bytes: int,
    actual_bytes: int,
) -> tuple[str, str]:
    logger.error(
        "[WS] Completed download payload size mismatch path=%s expected=%s actual=%s",
        source_path,
        expected_bytes,
        actual_bytes,
    )
    return "completed_size_mismatch", COMPLETED_SIZE_MISMATCH_MESSAGE


def resolve_complete_source_path(
    task_dir: Path,
    files: list[dict[str, Any]],
    task_name: str | None,
) -> Path | None:
    task_candidates: list[Path] = []
    external_candidates: list[Path] = []

    for file_item in files:
        raw_path = file_item.get("path")
        if not raw_path or not isinstance(raw_path, str):
            continue

        file_path = Path(raw_path)
        try:
            rel_path = file_path.relative_to(task_dir)
            if rel_path.parts:
                task_candidates.append(task_dir / rel_path.parts[0])
            else:
                task_candidates.append(task_dir)
            continue
        except (OSError, ValueError) as exc:
            logger.debug(
                "Failed to resolve path %s relative to %s: %s",
                file_path,
                task_dir,
                exc,
            )

        external_candidates.append(file_path)

    existing_task_candidates: list[Path] = []
    seen: set[Path] = set()
    for candidate in task_candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if candidate.exists():
            existing_task_candidates.append(candidate)

    if len(existing_task_candidates) > 1 and task_dir.exists():
        return task_dir
    if len(existing_task_candidates) == 1:
        return existing_task_candidates[0]

    task_entries = _list_task_dir_entries(task_dir)
    if len(task_entries) > 1:
        return task_dir
    if len(task_entries) == 1:
        return task_entries[0]

    for candidate in external_candidates:
        if candidate.exists():
            return candidate

    if task_name and task_dir.exists():
        named_candidate = task_dir / task_name
        if named_candidate.exists():
            return named_candidate

    return None


async def resolve_complete_source_with_retry(
    *,
    completion_gid: str | None,
    task_dir: Path,
    files: list[dict[str, Any]],
    task_name: str | None,
    backend: BackendPort | None,
) -> Path | None:
    latest_files = files

    for attempt in range(COMPLETE_SOURCE_RETRY_COUNT):
        source = resolve_complete_source_path(task_dir, latest_files, task_name)
        if source:
            return source

        if not completion_gid or backend is None:
            if attempt < COMPLETE_SOURCE_RETRY_COUNT - 1:
                await asyncio.sleep(COMPLETE_SOURCE_RETRY_INTERVAL)
            continue

        try:
            refreshed_status = await backend.tell_status(completion_gid)
            refreshed_files = refreshed_status.get("files", [])
            if isinstance(refreshed_files, list):
                latest_files = refreshed_files
        except Exception as exc:  # noqa: BLE001  # external boundary preserves failure isolation
            logger.debug(
                "[WS] Failed to refresh complete status gid=%s error=%s",
                completion_gid,
                exc,
            )

        if attempt < COMPLETE_SOURCE_RETRY_COUNT - 1:
            await asyncio.sleep(COMPLETE_SOURCE_RETRY_INTERVAL)

    return None


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
        disk_available_bytes=lambda: get_disk_available_bytes(
            settings.download_dir, min_free_disk=get_min_free_disk()
        ),
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


async def handle_v0_download_complete(
    *,
    backend: BackendPort,
    download: dict[str, Any],
    aria2_status: dict[str, Any],
    completion_gid: str,
    log_prefix: str = "[WS]",
    allow_metadata_handoff_defer: bool = True,
) -> bool:
    download_id = int(download["id"])

    # Pre-lock phase: metadata handoff deferral and late-followed switch
    # acquire the lifecycle lock internally, so they must run outside it.
    current = await get_global_download_for_generation(
        download_id, completion_gid
    )
    if current is None:
        logger.debug(
            "%s Completion generation is stale id=%s gid=%s",
            log_prefix,
            download_id,
            completion_gid,
        )
        return False

    display_name_fallback = str(
        current.get("display_name") or current.get("source_uri") or ""
    )
    files = aria2_status.get("files", [])
    if not isinstance(files, list):
        files = []

    task_name = download_ops.extract_display_name(
        aria2_status,
        display_name_fallback,
    )
    if allow_metadata_handoff_defer:
        deferred, changed = await defer_metadata_completion_if_handoff_pending(
            backend=backend,
            download=current,
            aria2_status=aria2_status,
            metadata_gid=completion_gid,
            display_name_fallback=display_name_fallback,
            log_prefix=log_prefix,
        )
        if deferred:
            return changed

    task_dir = get_downloading_dir() / str(download_id)
    source_path = await resolve_complete_source_with_retry(
        completion_gid=completion_gid,
        task_dir=task_dir,
        files=files,
        task_name=task_name,
        backend=backend,
    )
    if source_path is None and await switch_to_late_followed_download_if_supported(
        backend=backend,
        download=current,
        metadata_gid=completion_gid,
        display_name_fallback=display_name_fallback,
        log_prefix=log_prefix,
    ):
            return True

    original_name = task_name or (source_path.name if source_path else "")
    if str(current.get("resource_kind") or "") == "http" and current.get(
        "display_name"
    ):
        original_name = str(current["display_name"])
    elif source_path and original_name.startswith(METADATA_NAME_PREFIX):
        original_name = source_path.name
    expected_size = (
        expected_completed_size(aria2_status, source_path)
        if source_path is not None
        else None
    )

    # Lock phase: complete or fail under the lifecycle lock.
    lifecycle_lock = await get_download_lifecycle_lock(download_id)
    async with lifecycle_lock:
        fenced = await get_global_download_for_generation(
            download_id, completion_gid
        )
        if fenced is None:
            logger.debug(
                "%s Completion generation is stale id=%s gid=%s",
                log_prefix,
                download_id,
                completion_gid,
            )
            return False

        if source_path is None:
            error_code, error_message = source_not_found_error(task_dir)
            logger.error(
                "%s Download completed but source path was not found id=%s gid=%s dir=%s files=%s error_code=%s",
                log_prefix,
                download_id,
                completion_gid,
                task_dir,
                len(files),
                error_code,
            )
            return await fail_download_and_reclaim(
                backend=backend,
                download_id=download_id,
                message=error_message,
                error_code=error_code,
                expected_gid=completion_gid,
                writer_gid=completion_gid,
                acquire_lifecycle_lock=False,
                log_prefix=log_prefix,
            )

        result = await complete_global_download_locked(
            global_download_id=download_id,
            expected_gid=completion_gid,
            source_path=source_path,
            original_name=original_name,
            expected_size=expected_size,
        )
        if result is None:
            return False
        if result["status"] == "rejected":
            await _reclaim_terminal_with_claim(
                backend=backend,
                download_id=download_id,
                gid=completion_gid,
                log_prefix=log_prefix,
            )
            return True
        if result["status"] == "invalid_source":
            return await fail_download_and_reclaim(
                backend=backend,
                download_id=download_id,
                message="下载完成但文件布局无效",
                error_code="invalid_completed_layout",
                expected_gid=completion_gid,
                writer_gid=completion_gid,
                acquire_lifecycle_lock=False,
                log_prefix=log_prefix,
            )
        logger.info(
            "%s Completed v0 download id=%s user_files_created=%s",
            log_prefix,
            download_id,
            result["user_files_created"],
        )

    if result["status"] != "incomplete":
        await _remove_download_result_best_effort(backend, completion_gid, log_prefix)
        try:
            await cleanup_task_download_dir(download_id)
        except Exception as exc:  # noqa: BLE001  # external boundary preserves failure isolation
            logger.warning(
                "%s Failed to reclaim completed download dir id=%s error=%s",
                log_prefix,
                download_id,
                exc,
            )
        return True

    # Incomplete: try late-followed switch outside the lock, then fail if needed.
    if await switch_to_late_followed_download_if_supported(
        backend=backend,
        download=current,
        metadata_gid=completion_gid,
        display_name_fallback=display_name_fallback,
        log_prefix=log_prefix,
    ):
        return True
    error_code, error_message = completed_size_mismatch_error(
        source_path=source_path,
        expected_bytes=int(expected_size or 0),
        actual_bytes=int(result["size_bytes"]),
    )
    lifecycle_lock = await get_download_lifecycle_lock(download_id)
    async with lifecycle_lock:
        return await fail_download_and_reclaim(
            backend=backend,
            download_id=download_id,
            message=error_message,
            error_code=error_code,
            expected_gid=completion_gid,
            writer_gid=completion_gid,
            acquire_lifecycle_lock=False,
            log_prefix=log_prefix,
        )
