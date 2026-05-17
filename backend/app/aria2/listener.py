"""aria2 WebSocket event listener for the v0 shared download model."""

from __future__ import annotations

import asyncio
import logging
import random
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse, urlunparse

import aiohttp
from sqlalchemy import update

from app.aria2.failed_task_cleanup import (
    cleanup_failed_task_artifacts,
    get_representative_owner_id,
)
from app.db.engine import transaction
from app.db.schema import global_downloads, user_tasks
from app.repositories.downloads import (
    ACTIVE_USER_TASK_STATUSES,
    get_global_download_by_gid,
    mark_global_download_failed,
    now_ms,
    update_global_download,
)
from app.services.download_service import complete_global_download

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
COMPLETE_SOURCE_RETRY_COUNT = 5
COMPLETE_SOURCE_RETRY_INTERVAL = 1.0


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
    from app.routers.config import (
        get_ws_reconnect_factor,
        get_ws_reconnect_jitter,
        get_ws_reconnect_max_delay,
    )

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

        if attempt < COMPLETE_SOURCE_RETRY_COUNT - 1:
            await asyncio.sleep(COMPLETE_SOURCE_RETRY_INTERVAL)

        if not completion_gid:
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

    return None


def _safe_int(value: str | int | None, default: int = 0) -> int:
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _extract_display_name(
    aria2_status: dict[str, Any],
    fallback: str | None,
) -> str | None:
    raw_name = aria2_status.get("bittorrent", {}).get("info", {}).get("name") or (
        aria2_status.get("files") or [{}]
    )[0].get("path")
    if isinstance(raw_name, str) and raw_name:
        return Path(raw_name).name or raw_name
    return fallback


def _progress_values(
    aria2_status: dict[str, Any],
    display_name_fallback: str | None,
) -> dict[str, Any]:
    values: dict[str, Any] = {}
    if aria2_status:
        values["total_bytes"] = _safe_int(aria2_status.get("totalLength"))
        values["completed_bytes"] = _safe_int(aria2_status.get("completedLength"))
        display_name = _extract_display_name(aria2_status, display_name_fallback)
        if display_name:
            values["display_name"] = display_name
    return values


async def _resolve_download_for_gid(
    gid: str,
    aria2_status: dict[str, Any],
) -> dict[str, Any] | None:
    download = await get_global_download_by_gid(gid)
    if download is not None:
        return download

    following_gid = aria2_status.get("followingGid") if aria2_status else None
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
    if not values:
        return None

    timestamp = now_ms()
    row_values = {**values, "updated_at_ms": timestamp}
    async with transaction() as conn:
        row = (
            (
                await conn.execute(
                    update(global_downloads)
                    .where(
                        global_downloads.c.id == download_id,
                        global_downloads.c.status.in_(ACTIVE_USER_TASK_STATUSES),
                        global_downloads.c.completed_file_id.is_(None),
                    )
                    .values(**row_values)
                    .returning(global_downloads)
                )
            )
            .mappings()
            .first()
        )
    return dict(row) if row else None


async def _update_download_and_active_user_tasks(
    *,
    download_id: int,
    global_values: dict[str, Any],
    user_status: str | None = None,
) -> bool:
    updated = await _guarded_update_global_download(download_id, global_values)
    if updated is None:
        return False

    timestamp = now_ms()
    async with transaction() as conn:
        if user_status is not None:
            await conn.execute(
                update(user_tasks)
                .where(
                    user_tasks.c.global_download_id == download_id,
                    user_tasks.c.status.in_(ACTIVE_USER_TASK_STATUSES),
                )
                .values(status=user_status, updated_at_ms=timestamp)
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

        files = aria2_status.get("files", [])
        if not isinstance(files, list):
            files = []

        task_name = _extract_display_name(
            aria2_status,
            str(current.get("display_name") or current.get("source_uri") or ""),
        )
        task_dir = get_downloading_dir() / str(download_id)
        source_path = await _resolve_complete_source_with_retry(
            completion_gid=completion_gid,
            task_dir=task_dir,
            files=files,
            task_name=task_name,
            state=state,
        )
        if source_path is None:
            logger.error(
                "%s Download completed but source path was not found id=%s gid=%s dir=%s files=%s",
                log_prefix,
                download_id,
                completion_gid,
                task_dir,
                len(files),
            )
            return False

        original_name = task_name or source_path.name
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
    from app.aria2.errors import parse_error_message
    from app.core.state import get_aria2_client, get_task_complete_lock
    from app.routers.tasks import broadcast_task_update_to_subscribers

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
    progress_values = _progress_values(aria2_status, display_name_fallback)

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
        followed_by = aria2_status.get("followedBy") or []
        if followed_by:
            new_gid = str(followed_by[0])
            logger.info(
                "[WS] Metadata download complete, updating GID: %s -> %s", gid, new_gid
            )
            await _update_download_and_active_user_tasks(
                download_id=download_id,
                global_values={
                    "aria2_gid": new_gid,
                    "status": "active",
                    **progress_values,
                },
                user_status="active",
            )
            if gid != new_gid:
                await _remove_download_result_best_effort(client, gid, "[WS]")
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
            raw_error = str(aria2_status.get("errorMessage") or "后端错误")
            message = parse_error_message(raw_error)
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
    from app.routers.config import get_config_value

    reconnect_attempt = 0

    while True:
        rpc_url = get_config_value("aria2_rpc_url") or settings.aria2_rpc_url
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
