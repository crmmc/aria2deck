from __future__ import annotations

import asyncio
import logging
import shutil
import threading
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from app.aria2.protocol import Aria2Gateway
from app.core.config import get_internal_base_url, settings
from app.http.safe_client import UnsafeTargetError, normalize_public_http_url
from app.domain.task_policy import (
    CANCELABLE_TASK_STATUSES,
    RETRYABLE_DOWNLOAD_STATUSES,
    RETRYABLE_TASK_STATUSES,
)
from app.repositories.downloads import (
    attach_completed_file_to_user,
    admit_user_task,
    assign_submitted_gid,
    cancel_active_user_task,
    complete_active_user_tasks_for_stored_file,
    count_active_user_tasks,
    DiskAvailable,
    DownloadAdmissionError,
    claim_submitted_gid_for_failure,
    fail_user_task_submission,
    get_user_task_by_id,
    get_global_download_for_generation,
    get_global_download_status_snapshot,
    get_or_create_global_download,
    get_user_task,
    guarded_update_download_and_active_user_tasks,
    mark_global_download_failed,
    now_ms,
    prepare_download_retry,
    reconcile_download_size,
)
from app.repositories.errors import RepositoryConflictError
from app.repositories.files import (
    create_stored_file_with_entries,
    delete_stored_file,
    get_stored_file_by_identity,
)
from app.services.failed_task_cleanup import (
    cleanup_failed_task_artifacts,
    cleanup_terminal_download_generation,
)
from app.services.hash import extract_info_hash_from_magnet
from app.services.internal_fetch import (
    build_gateway_submission,
    http_resource_identity,
    source_request_options,
)
from app.services.storage import (
    get_downloading_dir,
    get_store_path_for_hash,
    get_task_download_dir,
    safe_delete_path,
)
from app.services.settings_service import (
    get_aria2_bt_stop_timeout_seconds,
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
DUPLICATE_TASK_MESSAGE = "任务已存在"
DOWNLOAD_SUBMISSION_FAILED_MESSAGE = "内部下载任务提交失败"
_ALLOWED_USER_OPTIONS = frozenset(
    (
        "out",
        "header",
        "max-connection-per-server",
        "http-user",
        "http-passwd",
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


async def get_download_lifecycle_lock(download_id: int) -> asyncio.Lock:
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


async def _cleanup_submitted_failure(
    *,
    aria2_client: Aria2Gateway,
    download_id: int,
    gid: str,
    owner_id: int | None,
    log_prefix: str,
    message: str = "提交下载失败",
) -> bool:
    await mark_global_download_failed(
        download_id,
        expected_gid=gid,
        message=message,
        error_code="submit_failed",
    )
    snapshot = await get_global_download_status_snapshot(download_id)
    if snapshot is not None and snapshot.get("aria2_gid") is None:
        await claim_submitted_gid_for_failure(
            download_id=download_id, gid=gid, message=message
        )
        snapshot = await get_global_download_status_snapshot(download_id)
    if (
        snapshot is not None
        and str(snapshot.get("aria2_gid") or "") == gid
        and str(snapshot.get("status") or "") in {"failed", "cancelled"}
    ):
        cleanup = await cleanup_terminal_download_generation(
            client=aria2_client,
            task_id=download_id,
            gid=gid,
            owner_id=owner_id,
            log_prefix=log_prefix,
            skip_status_check=True,
        )
        return cleanup.safe_to_reuse

    try:
        await aria2_client.force_remove(gid)
        await aria2_client.remove_download_result(gid)
    except Exception:
        logger.exception("Failed to stop unowned aria2 download gid=%s", gid)
    return False


async def _cleanup_submitted_failure_safely(**kwargs: Any) -> bool:
    operation = asyncio.create_task(_cleanup_submitted_failure(**kwargs))
    try:
        return await asyncio.shield(operation)
    except asyncio.CancelledError:
        await asyncio.shield(operation)
        raise


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


def _normalize_out_option(value: Any) -> str:
    out = str(value)
    if not out or out in {".", ".."} or "/" in out or "\\" in out:
        raise ValueError(
            "invalid out option: must be a filename without path separators"
        )
    return out


async def _validate_submit_options(options: Mapping[str, Any] | None) -> None:
    if not options:
        return
    if "bt-tracker" in options:
        raise ValueError("bt-tracker option is not allowed")
    if "out" in options:
        _normalize_out_option(options["out"])


async def complete_global_download(
    *,
    global_download_id: int,
    expected_gid: str,
    source_path: Path,
    original_name: str,
    expected_size: int | None = None,
) -> dict[str, Any] | None:
    source_path = Path(source_path)
    lifecycle_lock = await get_download_lifecycle_lock(global_download_id)
    async with lifecycle_lock:
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
    size_known: bool | None = None,
    size_limit_bytes: int | None = None,
    disk_available_bytes: int | None = None,
    options: Mapping[str, Any] | None = None,
    submit_uris: list[str] | None = None,
) -> dict[str, Any]:
    if resource_kind not in {"http", "magnet"}:
        raise ValueError("unsupported download resource kind")
    if resource_kind == "http":
        get_internal_base_url()
        try:
            uri = normalize_public_http_url(uri)
        except UnsafeTargetError as exc:
            raise ValueError(str(exc)) from exc
        source_options = source_request_options(
            options, mirrors=(submit_uris or [])[1:]
        )
        resource_key = http_resource_identity(resource_key, source_options)
    else:
        info_hash = extract_info_hash_from_magnet(uri)
        if not info_hash:
            raise ValueError("invalid magnet URI")
        uri = f"magnet:?xt=urn:btih:{info_hash}"
        resource_key = info_hash
        submit_uris = None

    async def submit_download(
        override_uris: list[str] | None,
        submit_options: Mapping[str, Any] | None,
    ) -> str:
        if resource_kind == "http":
            if not override_uris:
                raise RuntimeError("内部下载网关地址不可用")
            submission_uris = override_uris
        else:
            submission_uris = [uri]
        return await aria2_client.add_uri(submission_uris, submit_options or {})

    return await _create_user_download_with_submit(
        user_id=user_id,
        quota_bytes=quota_bytes,
        source_uri=uri,
        resource_key=resource_key,
        resource_kind=resource_kind,
        display_name=display_name,
        total_bytes=total_bytes,
        size_known=size_known,
        size_limit_bytes=size_limit_bytes,
        disk_available_bytes=disk_available_bytes,
        aria2_client=aria2_client,
        options=options,
        server_options=None,
        gateway_source_uris=submit_uris if resource_kind == "http" else None,
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
    size_known: bool | None = None,
    size_limit_bytes: int | None = None,
    disk_available_bytes: int | None = None,
    options: Mapping[str, Any] | None = None,
    server_options: Mapping[str, Any] | None = None,
    uris: list[str] | None = None,
) -> dict[str, Any]:
    if uris:
        raise ValueError("torrent webseed URIs are not allowed")

    async def submit_download(
        _override_uris: list[str] | None,
        submit_options: Mapping[str, Any] | None,
    ) -> str:
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
        size_known=size_known,
        size_limit_bytes=size_limit_bytes,
        disk_available_bytes=disk_available_bytes,
        aria2_client=aria2_client,
        options=options,
        server_options=server_options,
        gateway_source_uris=None,
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
    size_known: bool | None,
    size_limit_bytes: int | None,
    disk_available_bytes: int | None,
    aria2_client: Aria2Gateway,
    options: Mapping[str, Any] | None,
    submit_download: Callable[
        [list[str] | None, Mapping[str, Any] | None], Awaitable[str]
    ],
    server_options: Mapping[str, Any] | None = None,
    gateway_source_uris: list[str] | None = None,
) -> dict[str, Any]:
    await _validate_submit_options(options)
    requested_total_bytes = max(0, int(total_bytes))
    requested_size_known = (
        requested_total_bytes > 0 if size_known is None else bool(size_known)
    )
    size_limit = int(size_limit_bytes or get_max_task_size())
    disk_available = (
        get_disk_available_bytes
        if disk_available_bytes is None
        else max(0, int(disk_available_bytes))
    )
    global_values = {
        "resource_key": resource_key,
        "resource_kind": resource_kind,
        "source_uri": source_uri,
        "display_name": display_name,
        "total_bytes": requested_total_bytes,
        "size_known": 0,
        "size_limit_bytes": size_limit,
        "disk_reserved_bytes": 0,
    }
    global_download = await get_or_create_global_download(global_values)
    lifecycle_lock = await get_download_lifecycle_lock(global_download["id"])
    async with lifecycle_lock:
        global_download = await get_or_create_global_download(global_values)
        if global_download["status"] in RETRYABLE_DOWNLOAD_STATUSES:
            residual_gid = str(global_download.get("aria2_gid") or "")
            if residual_gid:
                cleanup = await cleanup_terminal_download_generation(
                    client=aria2_client,
                    task_id=int(global_download["id"]),
                    gid=residual_gid,
                    owner_id=user_id,
                    log_prefix="[Retry]",
                    skip_status_check=True,
                )
                if not cleanup.safe_to_reuse:
                    raise DownloadAdmissionError("previous_cleanup_pending")
            updated_global = await prepare_download_retry(
                int(global_download["id"])
            )
            if updated_global is None:
                raise DownloadAdmissionError("stale")
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

        try:
            task = await admit_user_task(
                user_id=user_id,
                global_download_id=int(global_download["id"]),
                expected_gid=(
                    str(global_download["aria2_gid"])
                    if global_download.get("aria2_gid")
                    else None
                ),
                display_name=display_name,
                total_bytes=effective_total_bytes,
                size_known=bool(global_download.get("size_known"))
                or requested_size_known,
                size_limit_bytes=size_limit,
                disk_available_bytes=disk_available,
            )
        except RepositoryConflictError:
            existing_task = await get_user_task(user_id, global_download["id"])
            _raise_if_duplicate_user_task(existing_task)
            raise

        global_download = await _ensure_download_submitted(
            global_download=global_download,
            task_id=int(task["id"]),
            size_limit_bytes=size_limit,
            disk_available_bytes=disk_available,
            options=options,
            server_options=server_options,
            aria2_client=aria2_client,
            gateway_source_uris=gateway_source_uris,
            submit_download=submit_download,
        )

        refreshed = await get_user_task(user_id, int(global_download["id"]))
        if refreshed is None:
            raise LookupError("user task not found")
        return refreshed


async def _admit_paused_unknown_download(
    *,
    download: dict[str, Any],
    gid: str,
    aria2_client: Aria2Gateway,
    size_limit_bytes: int,
    disk_available_bytes: DiskAvailable,
) -> dict[str, Any]:
    status = await aria2_client.tell_status(gid)
    candidate = candidate_size_from_status(status, require_trusted_total=True)
    if candidate is None:
        await mark_global_download_failed(
            int(download["id"]),
            expected_gid=gid,
            message="无法在暂停状态获取可信文件大小",
            error_code="unknown_size",
        )
        raise DownloadAdmissionError("unknown_size")

    result = await reconcile_download_size(
        download_id=int(download["id"]),
        expected_gid=gid,
        candidate_bytes=candidate[0],
        completed_bytes=candidate[1],
        size_limit_bytes=size_limit_bytes,
        disk_available_bytes=disk_available_bytes,
    )
    if not result.admitted:
        raise DownloadAdmissionError(str(result["outcome"]))

    await aria2_client.unpause(gid)
    updated = await guarded_update_download_and_active_user_tasks(
        int(download["id"]),
        {"status": "active"},
        expected_gid=gid,
        user_status="active",
    )
    if updated is None:
        raise DownloadAdmissionError("stale")
    return updated


async def _ensure_download_submitted(
    *,
    global_download: dict[str, Any],
    task_id: int,
    size_limit_bytes: int,
    disk_available_bytes: DiskAvailable,
    options: Mapping[str, Any] | None,
    server_options: Mapping[str, Any] | None,
    aria2_client: Aria2Gateway,
    gateway_source_uris: list[str] | None,
    submit_download: Callable[
        [list[str] | None, Mapping[str, Any] | None], Awaitable[str]
    ],
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
        submission_uris: list[str] | None = None
        resource_kind = str(current.get("resource_kind") or "")
        if resource_kind == "http":
            gateway_uris, gateway_options = build_gateway_submission(
                download_id=int(current["id"]),
                source_uri=str(current["source_uri"]),
                options=options,
                source_uris=gateway_source_uris,
            )
            submission_uris = gateway_uris
            submit_options.update(gateway_options)
        else:
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
        unknown_size = not bool(current.get("size_known"))
        if unknown_size and resource_kind == "magnet":
            submit_options["pause-metadata"] = "true"
        elif unknown_size:
            submit_options["pause"] = "true"

        try:
            gid = await submit_download(submission_uris, submit_options)
        except Exception as exc:
            await fail_user_task_submission(
                task_id=task_id,
                global_download_id=int(current["id"]),
                message=DOWNLOAD_SUBMISSION_FAILED_MESSAGE,
            )
            raise RuntimeError(DOWNLOAD_SUBMISSION_FAILED_MESSAGE) from exc

        submitted_status = (
            "waiting" if unknown_size and resource_kind != "magnet" else "active"
        )
        try:
            updated = await assign_submitted_gid(
                download_id=int(current["id"]),
                gid=gid,
                status=submitted_status,
            )
            if updated is None:
                raise RuntimeError("failed to persist submitted download")

            if unknown_size and resource_kind != "magnet":
                return await _admit_paused_unknown_download(
                    download=updated,
                    gid=gid,
                    aria2_client=aria2_client,
                    size_limit_bytes=size_limit_bytes,
                    disk_available_bytes=disk_available_bytes,
                )
            return updated
        except BaseException as exc:
            await _cleanup_submitted_failure_safely(
                aria2_client=aria2_client,
                download_id=int(current["id"]),
                gid=gid,
                owner_id=None,
                log_prefix="[Submit]",
                message=str(exc) or type(exc).__name__,
            )
            raise


async def cancel_user_task(
    *,
    user_id: int,
    user_task_id: int,
    quota_bytes: int,
    aria2_client: Aria2Gateway,
    cleanup_pending_user: bool = False,
) -> dict[str, Any]:
    async def load_task() -> dict[str, Any] | None:
        if cleanup_pending_user:
            return await get_user_task_by_id(
                user_id, user_task_id, include_pending_user=True
            )
        return await get_user_task_by_id(user_id, user_task_id)

    task = await load_task()
    if task is None:
        raise LookupError("task not found")

    if task["status"] not in CANCELABLE_TASK_STATUSES:
        return task

    lifecycle_lock = await get_download_lifecycle_lock(task["global_download_id"])
    async with lifecycle_lock:
        task = await load_task()
        if task is None:
            raise LookupError("task not found")
        if task["status"] not in CANCELABLE_TASK_STATUSES:
            return task

        active_count = await count_active_user_tasks(task["global_download_id"])
        should_cancel_global = active_count <= 1
        gid = task.get("aria2_gid")
        if should_cancel_global and gid:
            cleanup = await cleanup_failed_task_artifacts(
                client=aria2_client,
                task_id=int(task["global_download_id"]),
                gid=str(gid),
                owner_id=user_id,
                log_prefix="[Cancel]",
                skip_status_check=True,
            )
            if not cleanup.safe_to_reuse:
                raise RuntimeError("旧下载任务尚未安全停止，无法取消")

        cancelled = await cancel_active_user_task(
            user_id,
            user_task_id,
            error_message="用户取消",
            finished_at_ms=now_ms(),
        )
        if cancelled is None:
            latest = await load_task()
            if latest is None:
                raise LookupError("task not found")
            return latest

        return cancelled
