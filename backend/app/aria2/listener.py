"""aria2 WebSocket event listener for the v0 shared download model."""

from __future__ import annotations

import asyncio
import logging
import random
from urllib.parse import urlparse, urlunparse

import aiohttp

from app.aria2.gateway import get_aria2_client
from app.core.security import redact_url_for_log
from app.services import aria2_lifecycle_service as lifecycle
from app.services import backend_connectivity
from app.services.settings_service import (
    get_config_value_sync,
    get_ws_reconnect_factor,
    get_ws_reconnect_jitter,
    get_ws_reconnect_max_delay,
)

logger = logging.getLogger(__name__)

_event_tasks: set[asyncio.Task[None]] = set()
_event_tails: dict[str, asyncio.Task[None]] = {}
EVENT_SHUTDOWN_TIMEOUT = 10.0

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


async def handle_aria2_event(gid: str, event: str) -> None:
    """Handle a single aria2 event against v0 global_downloads/user_tasks."""
    client = get_aria2_client()
    try:
        aria2_status = await client.tell_status(gid)
    except Exception as exc:
        logger.warning(
            "[WS] Failed to fetch aria2 status gid=%s error_type=%s",
            gid,
            type(exc).__name__,
        )
        aria2_status = {}

    await lifecycle.handle_aria2_event(
        client=client,
        gid=gid,
        event=event,
        aria2_status=aria2_status,
    )


async def _run_ordered_event(
    previous: asyncio.Task[None] | None,
    gid: str,
    event: str,
) -> None:
    if previous is not None:
        try:
            await previous
        except Exception:
            pass
    await handle_aria2_event(gid, event)


def _event_task_done(gid: str, task: asyncio.Task[None]) -> None:
    _event_tasks.discard(task)
    if _event_tails.get(gid) is task:
        _event_tails.pop(gid, None)
    if task.cancelled():
        return
    exception = task.exception()
    if exception is not None:
        logger.error(
            "[WS] Event task failed gid=%s error_type=%s",
            gid,
            type(exception).__name__,
        )


def _schedule_event(gid: str, event: str) -> None:
    previous = _event_tails.get(gid)
    task = asyncio.create_task(
        _run_ordered_event(previous, gid, event),
        name=f"aria2_event_{gid}_{event}",
    )
    _event_tails[gid] = task
    _event_tasks.add(task)
    task.add_done_callback(lambda done: _event_task_done(gid, done))


async def _shutdown_event_tasks() -> None:
    tasks = set(_event_tasks)
    if not tasks:
        return
    logger.info("[WS] Waiting for %s event tasks", len(tasks))
    _, pending = await asyncio.wait(tasks, timeout=EVENT_SHUTDOWN_TIMEOUT)
    if not pending:
        return
    logger.warning("[WS] Cancelling %s unfinished event tasks", len(pending))
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)


async def listen_aria2_events() -> None:
    """aria2 WebSocket event listener main loop."""
    from app.core.config import settings

    reconnect_attempt = 0

    while True:
        rpc_url = get_config_value_sync("aria2_rpc_url") or settings.aria2_rpc_url
        ws_url = _http_to_ws_url(rpc_url)
        redacted_ws_url = redact_url_for_log(ws_url)

        try:
            timeout = aiohttp.ClientTimeout(connect=10, sock_connect=10, sock_read=None)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                logger.info("[WS] Connecting to aria2 WebSocket: %s", redacted_ws_url)

                async with session.ws_connect(ws_url) as ws:
                    logger.info("[WS] Connected to aria2 WebSocket")
                    reconnect_attempt = 0
                    await backend_connectivity.mark_ok()

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
                                            _schedule_event(str(gid), event)
                            except Exception as exc:
                                logger.warning(
                                    "[WS] Failed to parse message error_type=%s",
                                    type(exc).__name__,
                                )

                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            exception = ws.exception()
                            logger.error(
                                "[WS] WebSocket error url=%s error_type=%s",
                                redacted_ws_url,
                                type(exception).__name__
                                if exception is not None
                                else "None",
                            )
                            break

                        elif msg.type == aiohttp.WSMsgType.CLOSED:
                            logger.warning("[WS] WebSocket closed url=%s", redacted_ws_url)
                            break

        except asyncio.CancelledError:
            logger.info("[WS] Listener cancelled, exiting")
            await _shutdown_event_tasks()
            raise

        except Exception as exc:
            logger.warning(
                "[WS] Connection failed url=%s error_type=%s",
                redacted_ws_url,
                type(exc).__name__,
            )
            await backend_connectivity.mark_fail()

        delay = _calculate_backoff(reconnect_attempt)
        reconnect_attempt += 1
        logger.info(
            "[WS] Reconnecting url=%s in %.1fs (attempt #%s)",
            redacted_ws_url,
            delay,
            reconnect_attempt,
        )

        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            logger.info("[WS] Listener cancelled, exiting")
            await _shutdown_event_tasks()
            raise
