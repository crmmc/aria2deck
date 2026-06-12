"""aria2 WebSocket event listener for the v0 shared download model."""

from __future__ import annotations

import asyncio
import logging
import random
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse, urlunparse

import aiohttp

from app.services import aria2_lifecycle_service as lifecycle
from app.services.settings_service import (
    get_config_value_sync,
    get_ws_reconnect_factor,
    get_ws_reconnect_jitter,
    get_ws_reconnect_max_delay,
)

if TYPE_CHECKING:
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
COMPLETE_SOURCE_RETRY_COUNT = lifecycle.COMPLETE_SOURCE_RETRY_COUNT
COMPLETE_SOURCE_RETRY_INTERVAL = lifecycle.COMPLETE_SOURCE_RETRY_INTERVAL
DOWNLOAD_DIR_NOT_FOUND_MESSAGE = lifecycle.DOWNLOAD_DIR_NOT_FOUND_MESSAGE
DOWNLOAD_FILE_NOT_FOUND_MESSAGE = lifecycle.DOWNLOAD_FILE_NOT_FOUND_MESSAGE
COMPLETED_SIZE_MISMATCH_MESSAGE = lifecycle.COMPLETED_SIZE_MISMATCH_MESSAGE


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
    return lifecycle._list_task_dir_entries(task_dir)


def _source_not_found_error(task_dir: Path) -> tuple[str, str]:
    return lifecycle.source_not_found_error(task_dir)


def _payload_size_bytes(path: Path) -> int:
    return lifecycle.payload_size_bytes(path)


def _expected_completed_size(
    aria2_status: dict[str, Any],
    source_path: Path,
) -> int | None:
    return lifecycle.expected_completed_size(aria2_status, source_path)


def _completed_size_mismatch_error(
    *,
    source_path: Path,
    expected_bytes: int,
    actual_bytes: int,
) -> tuple[str, str]:
    return lifecycle.completed_size_mismatch_error(
        source_path=source_path,
        expected_bytes=expected_bytes,
        actual_bytes=actual_bytes,
    )


def _resolve_complete_source_path(
    task_dir: Path,
    files: list[dict[str, Any]],
    task_name: str | None,
) -> Path | None:
    """Infer the completed payload path from aria2 files plus task directory."""
    return lifecycle.resolve_complete_source_path(task_dir, files, task_name)


async def _resolve_complete_source_with_retry(
    completion_gid: str | None,
    task_dir: Path,
    files: list[dict[str, Any]],
    task_name: str | None,
    state: AppState | None = None,
) -> Path | None:
    """Retry source resolution briefly while aria2 flushes final paths."""
    from app.core.state import get_aria2_client

    client = get_aria2_client(state=state)
    return await lifecycle.resolve_complete_source_with_retry(
        completion_gid=completion_gid,
        task_dir=task_dir,
        files=files,
        task_name=task_name,
        client=client,
    )


def _first_followed_gid(aria2_status: dict[str, Any]) -> str | None:
    from app.aria2.download_ops import first_followed_gid

    return first_followed_gid(aria2_status)


def _following_gid(aria2_status: dict[str, Any]) -> str | None:
    from app.aria2.download_ops import following_gid

    return following_gid(aria2_status)


async def _resolve_download_for_gid(
    gid: str,
    aria2_status: dict[str, Any],
) -> dict[str, Any] | None:
    return await lifecycle.resolve_download_for_gid(gid, aria2_status)


async def _guarded_update_global_download(
    download_id: int,
    values: dict[str, Any],
) -> dict[str, Any] | None:
    return await lifecycle._guarded_update_global_download(download_id, values)


async def _update_download_and_active_user_tasks(
    *,
    download_id: int,
    global_values: dict[str, Any],
    user_status: str | None = None,
) -> bool:
    return await lifecycle.update_download_and_active_user_tasks(
        download_id=download_id,
        global_values=global_values,
        user_status=user_status,
    )


async def _switch_to_followed_download(
    *,
    client: lifecycle.Aria2LifecycleClient,
    download_id: int,
    metadata_gid: str | None,
    followed_gid: str,
    display_name_fallback: str | None,
    metadata_values: dict[str, Any] | None = None,
    log_prefix: str,
) -> bool:
    download = await lifecycle.resolve_download_for_gid(
        metadata_gid or followed_gid, {}
    )
    if download is None:
        return False
    return await lifecycle.switch_to_followed_download(
        client=client,
        download=download,
        metadata_gid=metadata_gid,
        followed_gid=followed_gid,
        display_name_fallback=display_name_fallback,
        log_prefix=log_prefix,
    )


async def _refresh_followed_gid(
    client: lifecycle.Aria2LifecycleClient,
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
    client: lifecycle.Aria2LifecycleClient,
    download: dict[str, Any],
    metadata_gid: str | None,
    display_name_fallback: str | None,
    log_prefix: str,
) -> bool:
    followed_gid = await _refresh_followed_gid(client, metadata_gid, log_prefix)
    if followed_gid is None:
        return False

    return await lifecycle.switch_to_followed_download(
        client=client,
        download=download,
        metadata_gid=metadata_gid,
        followed_gid=followed_gid,
        display_name_fallback=display_name_fallback,
        log_prefix=log_prefix,
    )


async def _defer_metadata_completion_if_handoff_pending(
    *,
    client: lifecycle.Aria2LifecycleClient,
    download: dict[str, Any],
    aria2_status: dict[str, Any],
    metadata_gid: str | None,
    display_name_fallback: str | None,
    log_prefix: str,
) -> bool:
    return await lifecycle.defer_metadata_completion_if_handoff_pending(
        client=client,
        download=download,
        aria2_status=aria2_status,
        metadata_gid=metadata_gid,
        display_name_fallback=display_name_fallback,
        log_prefix=log_prefix,
    )


async def _remove_download_result_best_effort(
    client: lifecycle.Aria2LifecycleClient,
    gid: str,
    log_prefix: str,
) -> None:
    await lifecycle._remove_download_result_best_effort(client, gid, log_prefix)


async def handle_v0_download_complete(
    *,
    state: AppState,
    client: lifecycle.Aria2LifecycleClient,
    download: dict[str, Any],
    aria2_status: dict[str, Any],
    completion_gid: str | None,
    log_prefix: str = "[WS]",
    allow_metadata_handoff_defer: bool = True,
) -> bool:
    """Index a completed v0 download and attach it to active user tasks."""
    return await lifecycle.handle_v0_download_complete(
        state=state,
        client=client,
        download=download,
        aria2_status=aria2_status,
        completion_gid=completion_gid,
        log_prefix=log_prefix,
        allow_metadata_handoff_defer=allow_metadata_handoff_defer,
    )


async def handle_aria2_event(
    state: AppState,
    gid: str,
    event: str,
) -> None:
    """Handle a single aria2 event against v0 global_downloads/user_tasks."""
    from app.core.state import get_aria2_client

    client = get_aria2_client(state=state)
    try:
        aria2_status = await client.tell_status(gid)
    except Exception as exc:
        logger.warning("[WS] Failed to fetch aria2 status gid=%s error=%s", gid, exc)
        aria2_status = {}

    await lifecycle.handle_aria2_event(
        state=state,
        client=client,
        gid=gid,
        event=event,
        aria2_status=aria2_status,
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
