from __future__ import annotations

import logging

from fastapi import WebSocket

from app.core.state import AppState, get_aria2_client
from app.repositories.downloads import list_user_tasks_for_download
from app.services.task_projection import build_rest_task_response
from app.services.task_runtime import fetch_cached_live_status_for_row


logger = logging.getLogger(__name__)


async def register_ws(state: AppState, user_id: int, ws: WebSocket) -> None:
    async with state.lock:
        state.ws_connections.setdefault(user_id, set()).add(ws)


async def unregister_ws(state: AppState, user_id: int, ws: WebSocket) -> None:
    async with state.lock:
        sockets = state.ws_connections.get(user_id)
        if sockets:
            sockets.discard(ws)


async def broadcast_task_update_to_subscribers(state: AppState, task_id: int) -> None:
    rows = await list_user_tasks_for_download(task_id)
    client = get_aria2_client(state=state)
    live_by_gid: dict[str, dict] = {}

    for row in rows:
        owner_id = int(row["user_id"])
        live = await fetch_cached_live_status_for_row(
            row,
            client,
            state,
            logger,
            live_by_gid,
        )
        payload = build_rest_task_response(row, live)

        async with state.lock:
            sockets = list(state.ws_connections.get(owner_id, set()))

        failed_sockets = []
        for ws in sockets:
            try:
                await ws.send_json({"type": "task_update", "task": payload})
            except Exception as exc:
                logger.debug("WebSocket send failed for user %s: %s", owner_id, exc)
                failed_sockets.append(ws)

        for ws in failed_sockets:
            try:
                await unregister_ws(state, owner_id, ws)
            except Exception as exc:
                logger.warning(
                    "Failed to unregister websocket for user %s: %s", owner_id, exc
                )


async def broadcast_notification(
    state: AppState,
    user_id: int,
    message: str,
    level: str = "info",
) -> None:
    async with state.lock:
        sockets = list(state.ws_connections.get(user_id, set()))

    notification = {"type": "notification", "message": message, "level": level}
    failed_sockets = []

    for ws in sockets:
        try:
            await ws.send_json(notification)
        except Exception as exc:
            logger.debug("Notification send failed user_id=%s error=%s", user_id, exc)
            failed_sockets.append(ws)

    if failed_sockets:
        async with state.lock:
            user_sockets = state.ws_connections.get(user_id)
            if user_sockets:
                for ws in failed_sockets:
                    user_sockets.discard(ws)
