import asyncio
import logging
import os
from urllib.parse import urlsplit

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.auth import get_user_by_session
from app.core.config import settings
from app.services.task_broadcast import register_ws, unregister_ws

logger = logging.getLogger(__name__)
router = APIRouter()

WS_CLOSE_UNAUTHORIZED = 4401
WS_CLOSE_FORBIDDEN = 4403
WS_CLOSE_LIMIT_EXCEEDED = 4429
SESSION_REVALIDATION_INTERVAL_SECONDS = 30.0
DEV_TASK_WS_ORIGINS = {
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:8080",
    "http://127.0.0.1:8080",
}


def _origin_key(value: str) -> tuple[str, str, int] | None:
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            return None
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return None
    return parsed.scheme, parsed.hostname.lower(), port


def _origin_allowed(websocket: WebSocket) -> bool:
    origin = websocket.headers.get("origin")
    if not origin:
        return False
    origin_key = _origin_key(origin)
    if origin_key is None:
        return False

    request_scheme = "https" if websocket.url.scheme == "wss" else "http"
    request_host = websocket.url.hostname
    request_port = websocket.url.port or (443 if request_scheme == "https" else 80)
    if request_host and origin_key == (
        request_scheme,
        request_host.lower(),
        request_port,
    ):
        return True

    configured_origins: set[str] = set()
    if settings.debug:
        configured_origins.update(DEV_TASK_WS_ORIGINS)
    configured_origins.update(
        origin.strip()
        for origin in os.environ.get("ARIA2C_CORS_ORIGINS", "").split(",")
        if origin.strip()
    )
    return any(_origin_key(allowed) == origin_key for allowed in configured_origins)


async def _revalidate_session(
    websocket: WebSocket,
    session_id: str,
    user_id: int,
) -> None:
    try:
        while True:
            await asyncio.sleep(SESSION_REVALIDATION_INTERVAL_SECONDS)
            user = await get_user_by_session(session_id)
            if user is not None and user.id == user_id:
                continue
            await unregister_ws(user_id, websocket)
            await websocket.close(code=WS_CLOSE_UNAUTHORIZED)
            return
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("WebSocket 会话复验失败 user_id=%s", user_id)
        await unregister_ws(user_id, websocket)
        try:
            await websocket.close(code=1011)
        except Exception as exc:  # noqa: BLE001  # socket close is best effort during teardown
            logger.debug("WebSocket 关闭失败 error_type=%s", type(exc).__name__)

@router.websocket("/ws/tasks")
async def task_ws(websocket: WebSocket) -> None:
    client_ip = websocket.client.host if websocket.client else "unknown"
    origin = websocket.headers.get("origin")
    if not _origin_allowed(websocket):
        logger.warning(
            "WebSocket Origin 被拒绝: path=/ws/tasks ip=%s origin=%s",
            client_ip,
            origin or "<missing>",
        )
        await websocket.close(code=WS_CLOSE_FORBIDDEN)
        return

    session_id = websocket.cookies.get(settings.session_cookie_name)
    user = await get_user_by_session(session_id)
    if not user or not session_id:
        logger.warning("WebSocket 未授权连接: path=/ws/tasks ip=%s", client_ip)
        await websocket.close(code=WS_CLOSE_UNAUTHORIZED)
        return

    user_id = user.id
    reservation = await register_ws(user_id, session_id, client_ip)
    if reservation is None:
        logger.warning(
            "WebSocket 连接数超限: path=/ws/tasks user_id=%s ip=%s",
            user_id,
            client_ip,
        )
        await websocket.close(code=WS_CLOSE_LIMIT_EXCEEDED)
        return

    accepted = False
    activated = False
    revalidation_task: asyncio.Task[None] | None = None
    try:
        await websocket.accept()
        accepted = True
        activated = await reservation.activate(websocket)
        if not activated:
            logger.warning(
                "WebSocket 预留槽位失效: path=/ws/tasks user_id=%s ip=%s",
                user_id,
                client_ip,
            )
            await websocket.close(code=WS_CLOSE_UNAUTHORIZED)
            return

        logger.info(
            "WebSocket 连接建立: path=/ws/tasks user_id=%s ip=%s",
            user_id,
            client_ip,
        )
        revalidation_task = asyncio.create_task(
            _revalidate_session(websocket, session_id, user_id)
        )
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
            elif data == "pong":
                logger.debug("收到用户 %s 的心跳响应", user_id)
    except WebSocketDisconnect:
        pass
    finally:
        if revalidation_task is not None:
            revalidation_task.cancel()
            await asyncio.gather(revalidation_task, return_exceptions=True)
        await reservation.release()
        await unregister_ws(user_id, websocket)
        if accepted:
            logger.info(
                "WebSocket 连接关闭: path=/ws/tasks user_id=%s ip=%s",
                user_id,
                client_ip,
            )


@router.websocket("/ws/tasks/")
async def task_ws_trailing_slash(websocket: WebSocket) -> None:
    """兼容带尾斜杠的 WebSocket 路径。"""
    await task_ws(websocket)


@router.websocket("/{full_path:path}")
async def unknown_ws(websocket: WebSocket, full_path: str) -> None:
    """兜底未知 WebSocket 路径，避免落入 StaticFiles 触发 AssertionError。"""
    client_ip = websocket.client.host if websocket.client else "unknown"
    logger.warning("未知 WebSocket 路径: /%s ip=%s", full_path, client_ip)
    await websocket.accept()
    await websocket.close(code=4404)
