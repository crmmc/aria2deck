"""aria2 WebSocket event listener for the v0 shared download model."""

from __future__ import annotations

import asyncio
import logging
import random
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse, urlunparse

import aiohttp

from app.aria2 import download_ops
from app.aria2.errors import prefer_aria2_error_message
from app.aria2.failed_task_cleanup import (
    cleanup_failed_task_artifacts,
    get_representative_owner_id,
)
from app.domain.downloads import ACTIVE_USER_TASK_STATUSES
from app.repositories.downloads import (
    get_global_download_by_gid,
    mark_global_download_failed,
    update_global_download,
)
from app.services.download_service import complete_global_download
from app.services.settings_service import (
    get_config_value_sync,
    get_ws_reconnect_factor,
    get_ws_reconnect_jitter,
    get_ws_reconnect_max_delay,
)
from app.services.task_broadcast import broadcast_task_update_to_subscribers
from app.services.task_projection import (
    METADATA_NAME_PREFIX,
)

if TYPE_CHECKING:
    from app.aria2.client import Aria2Client
    from app.core.state import AppState

logger = logging.getLogger(__name__)

_event_tasks: set[asyncio.Task[None]] = set()

EVENT_MAP = {
    "aria2.onDownloadStart": "start",
    "aria2.onDownloadPause": "pause",
    "aria2.onDownloadStop": "stop",
    "aria2.onDownloadComplete": "complete",
    "aria2.onDownloadError": "error",
    "aria2.onBtDownloadComplete": "bt_complete",
}

RECONNECT_BASE_DELAY = 1.0
COMPLETE_SOURCE_RETRY_COUNT = 4
COMPLETE_SOURCE_RETRY_INTERVAL = 0.5
DOWNLOAD_DIR_NOT_FOUND_MESSAGE = "下载完成但下载目录不存在"
DOWNLOAD_FILE_NOT_FOUND_MESSAGE = "下载完成但下载文件未找到"
COMPLETED_SIZE_MISMATCH_MESSAGE = "下载完成但文件大小不匹配"


def _http_to_ws_url(http_url: str) -> str:
    """Convert an HTTP RPC URL to a WebSocket URL."""
    parsed = urlparse(http_url)
    ws_scheme = "wss" if parsed.scheme == "https" else "ws"
    return urlunparse((ws_scheme, parsed.netloc, parsed.path, "", "", ""))


def _calculate_backoff(
    attempt: int,
    max_delay: float | None = None,
    jitter: float | None = None,
    factor: float | None = None,
) -> float:
    """Calculate exponential reconnect backoff with jitter."""
    if max_delay is None:
        max_delay = get_ws_reconnect_max_delay()
    if jitter is None:
        jitter = get_ws_reconnect_jitter()
    if factor is None:
        factor = get_ws_reconnect_factor()

    base_delay = min(RECONNECT_BASE_DELAY * (factor**attempt), max_delay)
    jitter_offset = base_delay * jitter * (2 * random.random() - 1)
    return base_delay + jitter_offset


def _list_task_dir_entries(task_dir: Path) -> list[Path]:
    """List payload entries in a task directory, ignoring aria2 control files."""
    if not task_dir.exists() or not task_dir.is_dir():
        return []
    try:
        return [p for p in task_dir.iterdir() if not p.name.endswith(".aria2")]
    except OSError as exc:
        logger.error("Failed to list task directory %s: %s", task_dir, exc)
        return []


def _source_not_found_error(task_dir: Path) -> tuple[str, str]:
    if not task_dir.exists() or not task_dir.is_dir():
        return "download_dir_not_found", DOWNLOAD_DIR_NOT_FOUND_MESSAGE
    return "download_file_not_found", DOWNLOAD_FILE_NOT_FOUND_MESSAGE


def _payload_size_bytes(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    if not path.is_dir():
        return 0
    return sum(
        child.stat().st_size
        for child in path.rglob("*")
        if child.is_file() and not child.name.endswith(".aria2")
    )


def _expected_completed_size(
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


def _completed_size_mismatch_error(
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


def _resolve_complete_source_path(
    task_dir: Path,
    files: list[dict[str, Any]],
    task_name: str | None,
) -> Path | None:
    """Infer the completed payload path from aria2 files plus task directory."""
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


async def _resolve_complete_source_with_retry(
    completion_gid: str | None,
    task_dir: Path,
    files: list[dict[str, Any]],
    task_name: str | None,
    state: AppState | None = None,
) -> Path | None:
    """Retry source resolution briefly while aria2 flushes final paths."""
    from app.core.state import get_aria2_client

    latest_files = files
    client = get_aria2_client(state=state)

    for attempt in range(COMPLETE_SOURCE_RETRY_COUNT):
        source = _resolve_complete_source_path(task_dir, latest_files, task_name)
        if source:
            return source

        if not completion_gid:
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


def _first_followed_gid(aria2_status: dict[str, Any]) -> str | None:
    followed_by = aria2_status.get("followedBy")
    if not isinstance(followed_by, list):
        return None

    for gid in followed_by:
        if isinstance(gid, (str, int)) and str(gid):
            return str(gid)
    return None


def _following_gid(aria2_status: dict[str, Any]) -> str | None:
    following = aria2_status.get("following") or aria2_status.get("followingGid")
    if isinstance(following, (str, int)) and str(following):
        return str(following)
    return None


async def _resolve_download_for_gid(
    gid: str,
    aria2_status: dict[str, Any],
) -> dict[str, Any] | None:
    download = await get_global_download_by_gid(gid)
    if download is not None:
        return download

    following_gid = _following_gid(aria2_status) if aria2_status else None
    if not following_gid:
        return None

    download = await get_global_download_by_gid(str(following_gid))
    if download is None:
        return None

    logger.info("[WS] Updating followed download GID: %s -> %s", following_gid, gid)
    updated = await _guarded_update_global_download(
        int(download["id"]), {"aria2_gid": gid}
    )
    return updated or download


async def _guarded_update_global_download(
    download_id: int,
    values: dict[str, Any],
) -> dict[str, Any] | None:
    return await download_ops.guarded_update_global_download(
        download_id, values, return_row=True
    )


async def _update_download_and_active_user_tasks(
    *,
    download_id: int,
    global_values: dict[str, Any],
    user_status: str | None = None,
) -> bool:
    updated = await _guarded_update_global_download(download_id, global_values)
    if updated is None:
        return False

    new_display_name = global_values.get("display_name")
    if user_status is not None or new_display_name:
        await download_ops.update_active_user_tasks(
            download_id,
            status=user_status,
            display_name=new_display_name,
            force_display_name=False,
        )
    return True


async def _switch_to_followed_download(
    *,
    client: Aria2Client,
    download_id: int,
    metadata_gid: str | None,
    followed_gid: str,
    display_name_fallback: str | None,
    metadata_values: dict[str, Any] | None = None,
    log_prefix: str,
) -> bool:
    download = await get_global_download_by_gid(metadata_gid or followed_gid)
    if download is None:
        return False
    return await download_ops.switch_to_followed_download(
        client=client,
        download=download,
        metadata_gid=metadata_gid,
        followed_gid=followed_gid,
        display_name_fallback=display_name_fallback,
        log_prefix=log_prefix,
    )


async def _refresh_followed_gid(
    client: Aria2Client,
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
            return None

        followed_gid = _first_followed_gid(status)
        if followed_gid is not None:
            return followed_gid

        if attempt < COMPLETE_SOURCE_RETRY_COUNT - 1:
            await asyncio.sleep(COMPLETE_SOURCE_RETRY_INTERVAL)

    return None


async def _switch_to_late_followed_download_if_supported(
    *,
    client: Aria2Client,
    download: dict[str, Any],
    metadata_gid: str | None,
    display_name_fallback: str | None,
    log_prefix: str,
) -> bool:
    followed_gid = await _refresh_followed_gid(client, metadata_gid, log_prefix)
    if followed_gid is None:
        return False

    return await _switch_to_followed_download(
        client=client,
        download_id=int(download["id"]),
        metadata_gid=metadata_gid,
        followed_gid=followed_gid,
        display_name_fallback=display_name_fallback,
        log_prefix=log_prefix,
    )


async def _defer_metadata_completion_if_handoff_pending(
    *,
    client: Aria2Client,
    download: dict[str, Any],
    aria2_status: dict[str, Any],
    metadata_gid: str | None,
    display_name_fallback: str | None,
    log_prefix: str,
) -> bool:
    if not download_ops.is_metadata_handoff_pending(download, aria2_status):
        return False

    if await _switch_to_late_followed_download_if_supported(
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
    await _update_download_and_active_user_tasks(
        download_id=int(download["id"]),
        global_values={"status": "active"},
        user_status="active",
    )
    return True


async def _remove_download_result_best_effort(
    client: Aria2Client,
    gid: str,
    log_prefix: str,
) -> None:
    try:
        await client.remove_download_result(gid)
    except Exception as exc:
        logger.debug(
            "%s Failed to remove aria2 result gid=%s error=%s", log_prefix, gid, exc
        )


async def handle_v0_download_complete(
    *,
    state: AppState,
    client: Aria2Client,
    download: dict[str, Any],
    aria2_status: dict[str, Any],
    completion_gid: str | None,
    log_prefix: str = "[WS]",
    allow_metadata_handoff_defer: bool = True,
) -> bool:
    """Index a completed v0 download and attach it to active user tasks."""
    from app.core.state import get_task_complete_lock
    from app.services.storage import get_downloading_dir

    download_id = int(download["id"])
    lock = await get_task_complete_lock(state, download_id)
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
            await _defer_metadata_completion_if_handoff_pending(
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
        source_path = await _resolve_complete_source_with_retry(
            completion_gid=completion_gid,
            task_dir=task_dir,
            files=files,
            task_name=task_name,
            state=state,
        )
        if source_path is None:
            if await _switch_to_late_followed_download_if_supported(
                client=client,
                download=current,
                metadata_gid=completion_gid,
                display_name_fallback=display_name_fallback,
                log_prefix=log_prefix,
            ):
                return False

            error_code, error_message = _source_not_found_error(task_dir)
            logger.error(
                "%s Download completed but source path was not found id=%s gid=%s dir=%s files=%s error_code=%s",
                log_prefix,
                download_id,
                completion_gid,
                task_dir,
                len(files),
                error_code,
            )
            # Mark as failed since aria2 reported completion but we can't find the file
            owner_id = await get_representative_owner_id(download_id)
            await mark_global_download_failed(
                download_id,
                message=error_message,
                error_code=error_code,
                clear_gid=True,
            )
            await cleanup_failed_task_artifacts(
                client=client,
                task_id=download_id,
                gid=completion_gid,
                owner_id=owner_id,
                log_prefix=log_prefix,
            )
            return False

        original_name = task_name or source_path.name
        if original_name.startswith(METADATA_NAME_PREFIX):
            original_name = source_path.name
        expected_size = _expected_completed_size(aria2_status, source_path)
        if expected_size is not None:
            actual_size = _payload_size_bytes(source_path)
            if actual_size < expected_size:
                if await _switch_to_late_followed_download_if_supported(
                    client=client,
                    download=current,
                    metadata_gid=completion_gid,
                    display_name_fallback=display_name_fallback,
                    log_prefix=log_prefix,
                ):
                    return False

                error_code, error_message = _completed_size_mismatch_error(
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
                await cleanup_failed_task_artifacts(
                    client=client,
                    task_id=download_id,
                    gid=completion_gid,
                    owner_id=owner_id,
                    log_prefix=log_prefix,
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
    state: AppState,
    gid: str,
    event: str,
) -> None:
    """Handle a single aria2 event against v0 global_downloads/user_tasks."""
    from app.core.state import get_aria2_client, get_task_complete_lock

    client = get_aria2_client(state=state)
    try:
        aria2_status = await client.tell_status(gid)
    except Exception as exc:
        logger.warning("[WS] Failed to fetch aria2 status gid=%s error=%s", gid, exc)
        aria2_status = {}

    download = await _resolve_download_for_gid(gid, aria2_status)
    if download is None:
        logger.debug("[WS] No v0 download found for gid=%s event=%s", gid, event)
        return

    download_id = int(download["id"])
    display_name_fallback = download.get("display_name") or download.get("source_uri")
    progress_values = download_ops.map_progress_values(aria2_status, display_name_fallback)

    if event == "start":
        await _update_download_and_active_user_tasks(
            download_id=download_id,
            global_values={"status": "active", **progress_values},
            user_status="active",
        )
    elif event == "pause":
        await _update_download_and_active_user_tasks(
            download_id=download_id,
            global_values={"status": "paused", **progress_values},
            user_status="paused",
        )
    elif event in {"complete", "bt_complete"}:
        followed_gid = _first_followed_gid(aria2_status)
        if followed_gid:
            await _switch_to_followed_download(
                client=client,
                download_id=download_id,
                metadata_gid=gid,
                followed_gid=followed_gid,
                display_name_fallback=display_name_fallback,
                metadata_values=progress_values,
                log_prefix="[WS]",
            )
        else:
            completion_gid = str(download.get("aria2_gid") or gid)
            await handle_v0_download_complete(
                state=state,
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

        completion_lock = await get_task_complete_lock(state, download_id)
        async with completion_lock:
            owner_id = await get_representative_owner_id(download_id)
            failed_download = await mark_global_download_failed(
                download_id,
                message=message,
                error_code=error_code,
                clear_gid=True,
            )
            if failed_download is not None and failed_download["status"] == "failed":
                await cleanup_failed_task_artifacts(
                    client=client,
                    task_id=download_id,
                    gid=gid,
                    owner_id=owner_id,
                    log_prefix="[WS]",
                )
    else:
        logger.debug("[WS] Ignoring unsupported aria2 event=%s gid=%s", event, gid)
        return

    await broadcast_task_update_to_subscribers(state, download_id)
    logger.debug(
        "[WS] Event handled gid=%s event=%s download_id=%s", gid, event, download_id
    )


async def listen_aria2_events(state: AppState) -> None:
    """aria2 WebSocket event listener main loop."""
    from app.core.config import settings

    reconnect_attempt = 0

    while True:
        rpc_url = get_config_value_sync("aria2_rpc_url") or settings.aria2_rpc_url
        ws_url = _http_to_ws_url(rpc_url)

        try:
            timeout = aiohttp.ClientTimeout(connect=10, sock_connect=10, sock_read=None)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                logger.info("[WS] Connecting to aria2 WebSocket: %s", ws_url)

                async with session.ws_connect(ws_url) as ws:
                    logger.info("[WS] Connected to aria2 WebSocket")
                    reconnect_attempt = 0

                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                data = msg.json()
                                method = data.get("method")

                                if method in EVENT_MAP:
                                    params = data.get("params", [])
                                    if params and isinstance(params[0], dict):
                                        gid = params[0].get("gid")
                                        if gid:
                                            event = EVENT_MAP[method]
                                            logger.debug(
                                                "[WS] Received event: %s gid=%s",
                                                method,
                                                gid,
                                            )
                                            task = asyncio.create_task(
                                                handle_aria2_event(state, gid, event),
                                                name=f"aria2_event_{gid}_{event}",
                                            )
                                            _event_tasks.add(task)
                                            task.add_done_callback(_event_tasks.discard)
                            except Exception as exc:
                                logger.warning("[WS] Failed to parse message: %s", exc)

                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            logger.error("[WS] WebSocket error: %s", ws.exception())
                            break

                        elif msg.type == aiohttp.WSMsgType.CLOSED:
                            logger.warning("[WS] WebSocket closed")
                            break

        except asyncio.CancelledError:
            logger.info("[WS] Listener cancelled, exiting")
            if _event_tasks:
                logger.info("[WS] Waiting for %s event tasks", len(_event_tasks))
                await asyncio.gather(*_event_tasks, return_exceptions=True)
            raise

        except Exception as exc:
            logger.warning("[WS] Connection failed: %s", exc)

        delay = _calculate_backoff(reconnect_attempt)
        reconnect_attempt += 1
        logger.info(
            "[WS] Reconnecting in %.1fs (attempt #%s)", delay, reconnect_attempt
        )

        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            logger.info("[WS] Listener cancelled, exiting")
            raise
