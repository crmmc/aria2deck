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
    await websocket.accept()
    session_id = websocket.cookies.get(settings.session_cookie_name)
    user = await get_user_by_session(session_id)
    if not user:
        await websocket.close(code=4401)
        return

    state = websocket.app.state.app_state
    user_id = user.id
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
