"""Business lifecycle handling for aria2 listener and polling inputs."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import aiohttp

from app.aria2.protocol import Aria2Gateway
from app.core.security import sanitize_string
from app.domain.status import ACTIVE_USER_TASK_STATUSES
from app.repositories.downloads import (
    get_global_download_by_gid,
    get_global_download_status_snapshot,
    get_representative_active_owner_id,
    guarded_update_global_download,
    list_inconsistent_completed_download_ids,
    list_stale_queued_download_ids,
    list_tracked_global_downloads,
    mark_global_download_failed,
    now_ms,
    update_active_user_tasks,
    update_global_download,
)
from app.services import download_ops
from app.services.download_service import complete_global_download
from app.services.failed_task_cleanup import cleanup_failed_task_artifacts
from app.services.aria2_error_messages import prefer_aria2_error_message
from app.services.storage import get_downloading_dir
from app.services.task_broadcast import broadcast_task_update_to_subscribers
from app.services.task_projection import (
    METADATA_NAME_PREFIX,
    has_live_bt_evidence,
    has_real_file_path,
    is_bt_resource_kind,
    is_metadata_phase_status,
)

logger = logging.getLogger(__name__)
_completion_locks: dict[tuple[int, int], asyncio.Lock] = {}
_completion_locks_guard = threading.Lock()


def _loop_id() -> int:
    return id(asyncio.get_running_loop())


async def get_task_complete_lock(download_id: int) -> asyncio.Lock:
    key = (_loop_id(), download_id)
    with _completion_locks_guard:
        lock = _completion_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _completion_locks[key] = lock
        return lock

COMPLETE_SOURCE_RETRY_COUNT = 4
COMPLETE_SOURCE_RETRY_INTERVAL = 0.5
COMPLETE_REPAIR_GRACE_SECONDS = 30.0
STALE_QUEUED_GRACE_SECONDS = 300.0
DOWNLOAD_DIR_NOT_FOUND_MESSAGE = "下载完成但下载目录不存在"
DOWNLOAD_FILE_NOT_FOUND_MESSAGE = "下载完成但下载文件未找到"
COMPLETED_SIZE_MISMATCH_MESSAGE = "下载完成但文件大小不匹配"
MISSING_GID_KEYWORDS = ("gid", "not found")
MISSING_GID_PATTERNS = (
    "gid#",
    "no such download",
    "unknown gid",
    "invalid gid",
)
TRANSIENT_RPC_ERROR_KEYWORDS = (
    "cannot connect to host",
    "connection refused",
    "temporarily unavailable",
    "timed out",
)
V0_SYNC_TRACKED_STATUSES = ACTIVE_USER_TASK_STATUSES


def _sanitize_path(file_path: str | None, task_id: int) -> str | None:
    if not file_path:
        return None
    try:
        abs_path = Path(file_path)
        return abs_path.name if abs_path.name else file_path
    except (ValueError, OSError) as exc:
        logger.debug("Failed to sanitize path for download %s: %s", task_id, exc)
        return file_path


def _status_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return False


def _has_bittorrent_evidence(
    status: dict[str, Any],
    download: dict[str, Any],
) -> bool:
    return is_bt_resource_kind(download) or has_live_bt_evidence(status)


def _should_upgrade_to_torrent(
    status: dict[str, Any],
    download: dict[str, Any],
) -> bool:
    return not is_bt_resource_kind(download) and has_live_bt_evidence(status)


def _is_effectively_complete_active_bt_status(
    status: dict[str, Any],
    download: dict[str, Any],
) -> bool:
    if str(status.get("status") or "") != "active":
        return False
    if status.get("followedBy"):
        return False
    if _status_bool(status.get("verifyIntegrityPending")):
        return False

    total_bytes = download_ops.safe_int(status.get("totalLength"))
    completed_bytes = download_ops.safe_int(status.get("completedLength"))
    if total_bytes <= 0 or completed_bytes < total_bytes:
        return False

    return _has_bittorrent_evidence(status, download) and has_real_file_path(status)


def _map_v0_status(
    status: dict[str, Any],
    download_id: int,
    *,
    prefer_bittorrent_name: bool = False,
) -> dict[str, Any]:
    fallback_name = (status.get("files") or [{}])[0].get("path")
    if fallback_name:
        fallback_name = _sanitize_path(fallback_name, download_id)

    if prefer_bittorrent_name:
        extracted = download_ops.extract_display_name(status, fallback_name)
    else:
        extracted = sanitize_string(fallback_name) if fallback_name else None

    raw_status = str(status.get("status") or "unknown")
    raw_error = status.get("errorMessage")
    return {
        "status": download_ops.map_aria2_status(status),
        "raw_status": raw_status,
        "display_name": extracted,
        "total_bytes": download_ops.safe_int(status.get("totalLength")),
        "completed_bytes": download_ops.safe_int(status.get("completedLength")),
        "error_message": sanitize_string(
            prefer_aria2_error_message(status.get("errorCode"), raw_error, "后端错误")
            if raw_error or raw_status == "error"
            else None
        ),
    }


def _exception_message(exc: Exception) -> str:
    return str(exc).lower()


def is_missing_gid_error(exc: Exception) -> bool:
    message = _exception_message(exc)
    if all(keyword in message for keyword in MISSING_GID_KEYWORDS):
        return True
    return any(pattern in message for pattern in MISSING_GID_PATTERNS)


def is_transient_rpc_error(exc: Exception) -> bool:
    if isinstance(exc, (aiohttp.ClientError, TimeoutError, OSError, ConnectionError)):
        return True
    message = _exception_message(exc)
    return any(keyword in message for keyword in TRANSIENT_RPC_ERROR_KEYWORDS)


async def list_v0_tracked_downloads() -> list[dict[str, Any]]:
    return await list_tracked_global_downloads(V0_SYNC_TRACKED_STATUSES)


async def _broadcast_download_update(download_id: int) -> None:
    await broadcast_task_update_to_subscribers(download_id)


async def get_representative_owner_id(download_id: int) -> int | None:
    return await get_representative_active_owner_id(download_id)


async def cleanup_failed_download_artifacts(
    *,
    client: Aria2Gateway,
    task_id: int,
    gid: str | None,
    owner_id: int | None,
    log_prefix: str,
    validate_status: bool = True,
) -> bool:
    return await cleanup_failed_task_artifacts(
        client=client,
        task_id=task_id,
        gid=gid,
        owner_id=owner_id,
        log_prefix=log_prefix,
        skip_status_check=not validate_status,
    )


async def fail_v0_download_and_cleanup(
    *,
    client: Aria2Gateway,
    download_id: int,
    gid: str | None,
    message: str,
    error_code: str | None,
    log_prefix: str,
) -> None:
    completion_lock = await get_task_complete_lock(download_id)
    async with completion_lock:
        owner_id = await get_representative_owner_id(download_id)
        failed_download = await mark_global_download_failed(
            download_id,
            message=message,
            error_code=error_code,
            clear_gid=True,
        )
        if failed_download is None or failed_download["status"] != "failed":
            return

        await cleanup_failed_download_artifacts(
            client=client,
            task_id=download_id,
            gid=gid,
            owner_id=owner_id,
            log_prefix=log_prefix,
            validate_status=False,
        )
        await _broadcast_download_update(download_id)


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


def payload_size_bytes(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    if not path.is_dir():
        return 0
    return sum(
        child.stat().st_size
        for child in path.rglob("*")
        if child.is_file() and not child.name.endswith(".aria2")
    )


def expected_completed_size(
    aria2_status: dict[str, Any],
    source_path: Path,
) -> int | None:
    files = aria2_status.get("files", [])
    if isinstance(files, list) and files:
        expected = 0
        has_length = False
        for item in files:
            if not isinstance(item, dict):
                continue
            raw_length = item.get("length")
            length = download_ops.safe_int(raw_length, default=-1)
            if length < 0:
                continue
            expected += length
            has_length = True
        if has_length:
            return expected

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
    client: Aria2Gateway | None,
) -> Path | None:
    latest_files = files

    for attempt in range(COMPLETE_SOURCE_RETRY_COUNT):
        source = resolve_complete_source_path(task_dir, latest_files, task_name)
        if source:
            return source

        if not completion_gid or client is None:
            if attempt < COMPLETE_SOURCE_RETRY_COUNT - 1:
                await asyncio.sleep(COMPLETE_SOURCE_RETRY_INTERVAL)
            continue

        try:
            refreshed_status = await client.tell_status(completion_gid)
            refreshed_files = refreshed_status.get("files", [])
            if isinstance(refreshed_files, list):
                latest_files = refreshed_files
        except Exception as exc:
            logger.debug(
                "[WS] Failed to refresh complete status gid=%s error=%s",
                completion_gid,
                exc,
            )

        if attempt < COMPLETE_SOURCE_RETRY_COUNT - 1:
            await asyncio.sleep(COMPLETE_SOURCE_RETRY_INTERVAL)

    return None


async def _remove_download_result_best_effort(
    client: Aria2Gateway,
    gid: str,
    log_prefix: str,
) -> None:
    try:
        await client.remove_download_result(gid)
    except Exception as exc:
        logger.debug(
            "%s Failed to remove aria2 result gid=%s error=%s", log_prefix, gid, exc
        )


async def _guarded_update_global_download(
    download_id: int,
    values: Mapping[str, Any],
) -> dict[str, Any] | None:
    return await guarded_update_global_download(
        download_id, dict(values), return_row=True
    )


async def update_download_and_active_user_tasks(
    *,
    download_id: int,
    global_values: dict[str, Any],
    user_status: str | None = None,
    force_display_name: bool = False,
) -> bool:
    updated = await _guarded_update_global_download(download_id, global_values)
    if updated is None:
        return False

    new_display_name = global_values.get("display_name")
    if user_status is not None or new_display_name:
        await update_active_user_tasks(
            download_id,
            status=user_status,
            display_name=new_display_name,
            force_display_name=force_display_name,
        )
    return True


async def switch_to_followed_download(
    *,
    client: Aria2Gateway,
    download: dict[str, Any],
    metadata_gid: str | None,
    followed_gid: str,
    display_name_fallback: str | None,
    log_prefix: str,
    complete_if_followed_complete: bool = False,
) -> bool:
    download_id = int(download["id"])
    logger.info(
        "%s Metadata download complete, updating GID: %s -> %s",
        log_prefix,
        metadata_gid,
        followed_gid,
    )

    real_status: dict[str, Any] | None = None
    try:
        real_status = await client.tell_status(followed_gid)
    except Exception as exc:
        logger.debug(
            "%s Failed to refresh followed download gid=%s error=%s",
            log_prefix,
            followed_gid,
            exc,
        )

    global_values: dict[str, Any] = {
        "aria2_gid": followed_gid,
        "status": "active",
    }
    display_name: str | None = None
    followed_is_complete = False

    if real_status:
        followed_is_complete = str(real_status.get("status") or "") == "complete"
        if (
            download_ops.first_followed_gid(real_status) is None
            and not followed_is_complete
        ):
            global_values["status"] = download_ops.map_aria2_status(real_status)
        progress = download_ops.map_progress_values(real_status, display_name_fallback)
        global_values.update(progress)
        display_name = progress.get("display_name")

        bt_hash = download_ops.bt_info_hash_from_status(real_status)
        if bt_hash:
            global_values["bt_info_hash"] = bt_hash

    if str(download.get("resource_kind") or "").lower() != "torrent":
        global_values["resource_kind"] = "torrent"

    updated_download = await _guarded_update_global_download(download_id, global_values)
    if updated_download is None:
        return False

    await update_active_user_tasks(
        download_id,
        status=str(global_values["status"]),
        display_name=display_name,
        force_display_name=True,
    )

    if metadata_gid and metadata_gid != followed_gid:
        await _remove_download_result_best_effort(client, metadata_gid, log_prefix)

    if followed_is_complete and complete_if_followed_complete and real_status:
        await handle_v0_download_complete(
            client=client,
            download=updated_download,
            aria2_status=real_status,
            completion_gid=followed_gid,
            log_prefix=log_prefix,
            allow_metadata_handoff_defer=False,
        )

    return True


async def resolve_download_for_gid(
    gid: str,
    aria2_status: dict[str, Any],
) -> dict[str, Any] | None:
    download = await get_global_download_by_gid(gid)
    if download is not None:
        return download

    following_gid = download_ops.following_gid(aria2_status) if aria2_status else None
    if not following_gid:
        return None

    download = await get_global_download_by_gid(str(following_gid))
    if download is None:
        return None

    logger.info("[WS] Updating followed download GID: %s -> %s", following_gid, gid)
    values: dict[str, Any] = {"aria2_gid": gid}
    if str(download.get("resource_kind") or "").lower() != "torrent":
        values["resource_kind"] = "torrent"
    updated = await _guarded_update_global_download(int(download["id"]), values)
    return updated or download


def _followed_gid_from_rows(
    rows: list[dict[str, Any]],
    metadata_gid: str,
) -> str | None:
    for row in rows:
        if not isinstance(row, dict):
            continue
        if download_ops.following_gid(row) != metadata_gid:
            continue

        gid = row.get("gid")
        if isinstance(gid, (str, int)) and str(gid):
            return str(gid)
    return None


async def _find_followed_gid_by_following(
    client: Aria2Gateway,
    metadata_gid: str,
    log_prefix: str,
) -> str | None:
    list_calls = (
        ("active", client.tell_active, ()),
        ("waiting", client.tell_waiting, (0, 1000)),
        ("stopped", client.tell_stopped, (0, 1000)),
    )
    for label, call, args in list_calls:
        try:
            rows = await call(*args)
        except Exception as exc:
            logger.debug(
                "%s Failed to scan aria2 %s tasks for following=%s error=%s",
                log_prefix,
                label,
                metadata_gid,
                exc,
            )
            continue
        if not isinstance(rows, list):
            continue

        followed_gid = _followed_gid_from_rows(rows, metadata_gid)
        if followed_gid is not None:
            return followed_gid
    return None


async def _refresh_followed_gid(
    client: Aria2Gateway,
    gid: str | None,
    log_prefix: str,
) -> str | None:
    if not gid:
        return None

    for attempt in range(COMPLETE_SOURCE_RETRY_COUNT):
        try:
            status = await client.tell_status(gid)
        except Exception as exc:
            logger.debug(
                "%s Failed to refresh metadata status gid=%s error=%s",
                log_prefix,
                gid,
                exc,
            )
        else:
            followed_gid = download_ops.first_followed_gid(status)
            if followed_gid is not None:
                return followed_gid

        followed_gid = await _find_followed_gid_by_following(client, gid, log_prefix)
        if followed_gid is not None:
            return followed_gid

        if attempt < COMPLETE_SOURCE_RETRY_COUNT - 1:
            await asyncio.sleep(COMPLETE_SOURCE_RETRY_INTERVAL)

    return None


async def switch_to_late_followed_download_if_supported(
    *,
    client: Aria2Gateway,
    download: dict[str, Any],
    metadata_gid: str | None,
    display_name_fallback: str | None,
    log_prefix: str,
    complete_if_followed_complete: bool = False,
) -> bool:
    followed_gid = await _refresh_followed_gid(client, metadata_gid, log_prefix)
    if followed_gid is None:
        return False

    return await switch_to_followed_download(
        client=client,
        download=download,
        metadata_gid=metadata_gid,
        followed_gid=followed_gid,
        display_name_fallback=display_name_fallback,
        log_prefix=log_prefix,
        complete_if_followed_complete=complete_if_followed_complete,
    )


async def defer_metadata_completion_if_handoff_pending(
    *,
    client: Aria2Gateway,
    download: dict[str, Any],
    aria2_status: dict[str, Any],
    metadata_gid: str | None,
    display_name_fallback: str | None,
    log_prefix: str,
) -> bool:
    if not download_ops.is_metadata_handoff_pending(download, aria2_status):
        return False

    if await switch_to_late_followed_download_if_supported(
        client=client,
        download=download,
        metadata_gid=metadata_gid,
        display_name_fallback=display_name_fallback,
        log_prefix=log_prefix,
    ):
        return True

    logger.info(
        "%s Metadata download complete without followedBy, waiting for handoff id=%s gid=%s",
        log_prefix,
        download["id"],
        metadata_gid,
    )
    await update_download_and_active_user_tasks(
        download_id=int(download["id"]),
        global_values={"status": "active"},
        user_status="active",
    )
    return True


def should_defer_stopped_result_cleanup(
    download: dict[str, Any],
    aria2_status: dict[str, Any],
) -> bool:
    return download_ops.is_metadata_handoff_pending(download, aria2_status)


async def handle_v0_download_complete(
    *,
    client: Aria2Gateway,
    download: dict[str, Any],
    aria2_status: dict[str, Any],
    completion_gid: str | None,
    log_prefix: str = "[WS]",
    allow_metadata_handoff_defer: bool = True,
) -> bool:
    download_id = int(download["id"])
    lock = await get_task_complete_lock(download_id)
    async with lock:
        current = await update_global_download(download_id, {})
        if current is None:
            logger.debug(
                "%s Download disappeared before completion id=%s",
                log_prefix,
                download_id,
            )
            return False
        if current.get("completed_file_id") is not None:
            logger.debug("%s Download already completed id=%s", log_prefix, download_id)
            return True
        if current.get("status") not in ACTIVE_USER_TASK_STATUSES:
            logger.debug(
                "%s Skip completion for terminal download id=%s status=%s",
                log_prefix,
                download_id,
                current.get("status"),
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
        if allow_metadata_handoff_defer and (
            await defer_metadata_completion_if_handoff_pending(
                client=client,
                download=current,
                aria2_status=aria2_status,
                metadata_gid=completion_gid,
                display_name_fallback=display_name_fallback,
                log_prefix=log_prefix,
            )
        ):
            return False

        task_dir = get_downloading_dir() / str(download_id)
        source_path = await resolve_complete_source_with_retry(
            completion_gid=completion_gid,
            task_dir=task_dir,
            files=files,
            task_name=task_name,
            client=client,
        )
        if source_path is None:
            if await switch_to_late_followed_download_if_supported(
                client=client,
                download=current,
                metadata_gid=completion_gid,
                display_name_fallback=display_name_fallback,
                log_prefix=log_prefix,
            ):
                return False

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
            owner_id = await get_representative_owner_id(download_id)
            await mark_global_download_failed(
                download_id,
                message=error_message,
                error_code=error_code,
                clear_gid=True,
            )
            await cleanup_failed_download_artifacts(
                client=client,
                task_id=download_id,
                gid=completion_gid,
                owner_id=owner_id,
                log_prefix=log_prefix,
                validate_status=False,
            )
            return False

        original_name = task_name or source_path.name
        if original_name.startswith(METADATA_NAME_PREFIX):
            original_name = source_path.name
        expected_size = expected_completed_size(aria2_status, source_path)
        if expected_size is not None:
            actual_size = payload_size_bytes(source_path)
            if actual_size < expected_size:
                if await switch_to_late_followed_download_if_supported(
                    client=client,
                    download=current,
                    metadata_gid=completion_gid,
                    display_name_fallback=display_name_fallback,
                    log_prefix=log_prefix,
                ):
                    return False

                error_code, error_message = completed_size_mismatch_error(
                    source_path=source_path,
                    expected_bytes=expected_size,
                    actual_bytes=actual_size,
                )
                owner_id = await get_representative_owner_id(download_id)
                await mark_global_download_failed(
                    download_id,
                    message=error_message,
                    error_code=error_code,
                    clear_gid=True,
                )
                await cleanup_failed_download_artifacts(
                    client=client,
                    task_id=download_id,
                    gid=completion_gid,
                    owner_id=owner_id,
                    log_prefix=log_prefix,
                    validate_status=False,
                )
                return False

        result = await complete_global_download(
            global_download_id=download_id,
            source_path=source_path,
            original_name=original_name,
        )
        logger.info(
            "%s Completed v0 download id=%s user_files_created=%s",
            log_prefix,
            download_id,
            result["user_files_created"],
        )

    if completion_gid:
        await _remove_download_result_best_effort(client, completion_gid, log_prefix)
    return True


async def handle_aria2_event(
    *,
    client: Aria2Gateway,
    gid: str,
    event: str,
    aria2_status: dict[str, Any],
) -> None:
    download = await resolve_download_for_gid(gid, aria2_status)
    if download is None:
        logger.debug("[WS] No v0 download found for gid=%s event=%s", gid, event)
        return

    download_id = int(download["id"])
    display_name_fallback = download.get("display_name") or download.get("source_uri")
    progress_values = download_ops.map_progress_values(
        aria2_status, display_name_fallback
    )

    if event == "start":
        await update_download_and_active_user_tasks(
            download_id=download_id,
            global_values={"status": "active", **progress_values},
            user_status="active",
        )
    elif event == "pause":
        await update_download_and_active_user_tasks(
            download_id=download_id,
            global_values={"status": "paused", **progress_values},
            user_status="paused",
        )
    elif event in {"complete", "bt_complete"}:
        followed_gid = download_ops.first_followed_gid(aria2_status)
        if followed_gid:
            await switch_to_followed_download(
                client=client,
                download=download,
                metadata_gid=gid,
                followed_gid=followed_gid,
                display_name_fallback=display_name_fallback,
                log_prefix="[WS]",
                complete_if_followed_complete=True,
            )
        else:
            completion_gid = str(download.get("aria2_gid") or gid)
            await handle_v0_download_complete(
                client=client,
                download=download,
                aria2_status=aria2_status,
                completion_gid=completion_gid,
                log_prefix="[WS]",
            )
    elif event in {"stop", "error"}:
        if event == "stop":
            message = "外部取消（管理员/外部客户端）"
            error_code = "removed"
        else:
            raw_error = aria2_status.get("errorMessage")
            message = prefer_aria2_error_message(
                aria2_status.get("errorCode"),
                str(raw_error) if raw_error is not None else None,
                "后端错误",
            )
            error_code = str(aria2_status.get("errorCode") or "error")

        if progress_values:
            await _guarded_update_global_download(download_id, progress_values)

        await fail_v0_download_and_cleanup(
            client=client,
            download_id=download_id,
            gid=gid,
            message=message,
            error_code=error_code,
            log_prefix="[WS]",
        )
    else:
        logger.debug("[WS] Ignoring unsupported aria2 event=%s gid=%s", event, gid)
        return

    await _broadcast_download_update(download_id)
    logger.debug(
        "[WS] Event handled gid=%s event=%s download_id=%s", gid, event, download_id
    )


async def complete_v0_download_from_sync(
    *,
    client: Aria2Gateway,
    download: dict[str, Any],
    aria2_status: dict[str, Any],
    completion_gid: str,
    allow_metadata_handoff_defer: bool = True,
) -> None:
    completed = await handle_v0_download_complete(
        client=client,
        download=download,
        aria2_status=aria2_status,
        completion_gid=completion_gid,
        log_prefix="[Sync]",
        allow_metadata_handoff_defer=allow_metadata_handoff_defer,
    )
    if completed:
        await _broadcast_download_update(int(download["id"]))


async def update_v0_download_from_aria2(
    *,
    client: Aria2Gateway,
    download: dict[str, Any],
    status: dict[str, Any],
) -> None:
    download_id = int(download["id"])
    bt_evidence = _has_bittorrent_evidence(status, download)
    upgrade_to_torrent = _should_upgrade_to_torrent(status, download)
    mapped = _map_v0_status(
        status,
        download_id,
        prefer_bittorrent_name=bt_evidence,
    )
    gid = str(download.get("aria2_gid") or "")
    followed_by = status.get("followedBy") or []

    if mapped["raw_status"] == "complete" and followed_by:
        switched = await switch_to_followed_download(
            client=client,
            download=download,
            metadata_gid=gid,
            followed_gid=str(followed_by[0]),
            display_name_fallback=str(download.get("display_name") or ""),
            log_prefix="[Sync]",
            complete_if_followed_complete=True,
        )
        if switched:
            await _broadcast_download_update(int(download["id"]))
        return

    if download_ops.is_metadata_handoff_pending(download, status):
        switched = await switch_to_late_followed_download_if_supported(
            client=client,
            download=download,
            metadata_gid=gid,
            display_name_fallback=str(download.get("display_name") or ""),
            log_prefix="[Sync]",
            complete_if_followed_complete=True,
        )
        if switched:
            await _broadcast_download_update(int(download["id"]))
            return

        logger.info(
            "[Sync] Metadata download complete without followedBy, waiting for handoff id=%s gid=%s",
            download_id,
            gid,
        )
        await guarded_update_global_download(
            download_id,
            {"status": "active"},
        )
        await update_active_user_tasks(download_id, status="active")
        return

    if mapped["raw_status"] == "complete":
        await complete_v0_download_from_sync(
            client=client,
            download=download,
            aria2_status=status,
            completion_gid=gid,
        )
        return

    if _is_effectively_complete_active_bt_status(status, download):
        await complete_v0_download_from_sync(
            client=client,
            download=download,
            aria2_status=status,
            completion_gid=gid,
        )
        return

    if mapped["status"] == "failed":
        message = mapped["error_message"] or (
            "外部取消（管理员/外部客户端）"
            if mapped["raw_status"] == "removed"
            else "后端错误"
        )
        logger.warning(
            "[Sync] v0 download failed download_id=%s gid=%s error=%s",
            download_id,
            gid,
            message,
        )
        await fail_v0_download_and_cleanup(
            client=client,
            download_id=download_id,
            gid=gid,
            message=message,
            error_code=str(status.get("errorCode") or mapped["raw_status"]),
            log_prefix="[Sync]",
        )
        return

    is_metadata = bt_evidence and is_metadata_phase_status(status)
    timestamp = now_ms()
    global_values: dict[str, Any] = {
        "status": mapped["status"],
        "completed_bytes": mapped["completed_bytes"],
        "updated_at_ms": timestamp,
    }
    if upgrade_to_torrent:
        global_values["resource_kind"] = "torrent"
    bt_info_hash = download_ops.bt_info_hash_from_status(status)
    if bt_evidence and bt_info_hash:
        global_values["bt_info_hash"] = bt_info_hash
    if not is_metadata:
        global_values["total_bytes"] = mapped["total_bytes"]
        if mapped["display_name"]:
            global_values["display_name"] = mapped["display_name"]

    changed = await guarded_update_global_download(download_id, global_values)
    if not changed:
        return

    await update_active_user_tasks(
        download_id,
        status=mapped["status"],
        display_name=mapped["display_name"] if not is_metadata else None,
    )
    await _broadcast_download_update(download_id)


async def repair_inconsistent_completed_downloads_v0() -> None:
    threshold_ms = now_ms() - int(COMPLETE_REPAIR_GRACE_SECONDS * 1000)
    for download_id in await list_inconsistent_completed_download_ids(threshold_ms):
        logger.warning(
            "[Sync] Completed v0 download was not indexed, failing id=%s", download_id
        )
        await mark_global_download_failed(
            download_id,
            message="下载完成但文件未入库",
            error_code="completion_not_indexed",
        )


async def cleanup_stale_queued_downloads_v0(
    grace_seconds: float = STALE_QUEUED_GRACE_SECONDS,
) -> None:
    threshold_ms = now_ms() - int(grace_seconds * 1000)
    for download_id in await list_stale_queued_download_ids(threshold_ms):
        logger.warning("[Sync] Cleaning stale v0 queued download_id=%s", download_id)
        failed_download = await mark_global_download_failed(
            download_id,
            message="任务提交超时，已自动清理",
            error_code="submit_timeout",
        )
        if failed_download is not None and failed_download["status"] == "failed":
            await _broadcast_download_update(download_id)


async def handle_missing_gid(
    *,
    client: Aria2Gateway,
    download: dict[str, Any],
    gid: str,
) -> None:
    download_id = int(download["id"])
    snapshot = await get_global_download_status_snapshot(download_id)
    if snapshot is None:
        logger.debug("[Sync] GID %s missing but download not found, skipping", gid)
        return

    status_val = str(snapshot["status"])
    completed_file_id = snapshot["completed_file_id"]
    completed_bytes = snapshot["completed_bytes"]
    total_bytes = snapshot["total_bytes"]

    if status_val == "completed" and completed_file_id is not None:
        logger.info("[Sync] GID %s missing but download already completed, skipping", gid)
        return

    if status_val not in V0_SYNC_TRACKED_STATUSES:
        logger.debug(
            "[Sync] GID %s missing but download not active (status=%s), skipping",
            gid,
            status_val,
        )
        return

    logger.warning(
        "[Sync] GID %s missing, attempting recovery from disk download_id=%s",
        gid,
        download_id,
    )
    fake_aria2_status: dict[str, Any] = {
        "status": "complete",
        "files": [],
        "totalLength": total_bytes or 0,
        "completedLength": completed_bytes or 0,
    }
    await complete_v0_download_from_sync(
        client=client,
        download=download,
        aria2_status=fake_aria2_status,
        completion_gid=gid,
        allow_metadata_handoff_defer=False,
    )
