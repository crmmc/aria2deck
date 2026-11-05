from __future__ import annotations

from typing import cast

from fastapi import APIRouter, WebSocket

from ..application.downloads.ws_service import (
    WS_NOT_FOUND,
    WebSocketTaskService,
)
from .dependencies import SESSION_COOKIE

router = APIRouter(tags=["websocket"])


@router.websocket("/ws/tasks")
async def ws_tasks(websocket: WebSocket) -> None:
    service = cast(WebSocketTaskService, websocket.app.state.ws_service)
    session_id = websocket.cookies.get(SESSION_COOKIE)
    origin = websocket.headers.get("origin")
    ip = str(websocket.client.host if websocket.client else "unknown")
    await service.handle(
        websocket,
        session_id=session_id,
        origin=origin,
        ip=ip,
        host=websocket.headers.get("host"),
        forwarded_proto=websocket.headers.get("x-forwarded-proto"),
    )


@router.websocket("/ws/{subpath:path}")
async def ws_unknown(websocket: WebSocket, subpath: str) -> None:
    await websocket.close(WS_NOT_FOUND)
