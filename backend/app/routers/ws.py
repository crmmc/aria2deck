from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.aria2.sync import register_ws, unregister_ws
from app.auth import get_user_by_session
from app.core.config import settings


router = APIRouter()


@router.websocket("/ws/tasks")
async def task_ws(websocket: WebSocket) -> None:
    await websocket.accept()
    session_id = websocket.cookies.get(settings.session_cookie_name)
    user = get_user_by_session(session_id)
    if not user:
        await websocket.close(code=4401)
        return
    state = websocket.app.state.app_state
    user_id = user["id"]
    await register_ws(state, user_id, websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        await unregister_ws(state, user_id, websocket)
