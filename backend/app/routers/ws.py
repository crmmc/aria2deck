import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from app.aria2.sync import register_ws, unregister_ws
from app.auth import get_user_by_session
from app.core.config import settings


logger = logging.getLogger(__name__)
router = APIRouter()

# 心跳间隔（秒）
HEARTBEAT_INTERVAL = 30


@router.websocket("/ws/tasks")
async def task_ws(websocket: WebSocket) -> None:
    client_ip = websocket.client.host if websocket.client else "unknown"
    await websocket.accept()
    session_id = websocket.cookies.get(settings.session_cookie_name)
    user = await get_user_by_session(session_id)
    if not user:
        logger.warning("WebSocket 未授权连接: path=/ws/tasks ip=%s", client_ip)
        await websocket.close(code=4401)
        return

    state = websocket.app.state.app_state
    user_id = user.id
    if user_id is None:
        logger.warning("WebSocket 用户ID为空: path=/ws/tasks ip=%s", client_ip)
        await websocket.close(code=4401)
        return
    logger.info("WebSocket 连接建立: path=/ws/tasks user_id=%s ip=%s", user_id, client_ip)
    await register_ws(state, user_id, websocket)

    async def heartbeat():
        """定时发送心跳 ping，检测连接是否存活"""
        try:
            while websocket.client_state == WebSocketState.CONNECTED:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                if websocket.client_state == WebSocketState.CONNECTED:
                    try:
                        await websocket.send_json({"type": "ping"})
                    except Exception:
                        break
        except asyncio.CancelledError:
            pass

    heartbeat_task = asyncio.create_task(heartbeat())

    try:
        while True:
            data = await websocket.receive_text()
            # 客户端发送 ping，服务端回复 pong
            if data == "ping":
                await websocket.send_text("pong")
            elif data == "pong":
                logger.debug(f"收到用户 {user_id} 的心跳响应")
    except WebSocketDisconnect:
        pass
    finally:
        heartbeat_task.cancel()
        await unregister_ws(state, user_id, websocket)
        logger.info("WebSocket 连接关闭: path=/ws/tasks user_id=%s ip=%s", user_id, client_ip)


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
