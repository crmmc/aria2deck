"""Aria2 回调钩子处理模块

Aria2 通过 --on-download-* 参数调用外部脚本，脚本再调用此接口更新任务状态。
"""
from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import BaseModel

from app.aria2.client import Aria2Client
from app.aria2.sync import broadcast_update
from app.core.config import settings
from app.core.state import AppState, get_aria2_client
from app.db import execute, fetch_one, utc_now


router = APIRouter(prefix="/api/hooks", tags=["hooks"])


class Aria2HookPayload(BaseModel):
    """Aria2 回调请求体"""
    gid: str
    event: str  # start | pause | stop | complete | error | bt_complete


def _get_state(request: Request) -> AppState:
    return request.app.state.app_state


def _get_client(request: Request) -> Aria2Client:
    return get_aria2_client(request)


def _first_artifact_path(status: dict) -> str | None:
    """从 aria2 状态中提取第一个文件路径"""
    files = status.get("files") or []
    if not files:
        return None
    return files[0].get("path")


@router.post("/aria2")
async def aria2_hook(
    payload: Aria2HookPayload,
    request: Request,
    x_hook_secret: str | None = Header(None)
) -> dict:
    """处理 Aria2 回调事件

    事件类型:
    - start: 下载开始
    - pause: 下载暂停
    - stop: 下载停止（用户取消）
    - complete: 下载完成
    - error: 下载出错
    - bt_complete: BT 下载完成
    """
    # 验证 hook secret
    if settings.hook_secret and x_hook_secret != settings.hook_secret:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid hook secret"
        )

    gid = payload.gid
    event = payload.event
    
    # 查找对应任务
    task = fetch_one("SELECT * FROM tasks WHERE gid = ?", [gid])
    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"未找到 GID 为 {gid} 的任务"
        )
    
    client = _get_client(request)
    state = _get_state(request)
    
    # 获取 aria2 中的最新状态
    try:
        aria2_status = await client.tell_status(gid)
    except Exception:
        aria2_status = {}
    
    # 根据事件类型更新状态
    new_status = task["status"]
    error_msg = None
    artifact_path = task.get("artifact_path")
    artifact_token = task.get("artifact_token")
    
    if event == "start":
        new_status = "active"
    elif event == "pause":
        new_status = "paused"
    elif event == "stop":
        new_status = "stopped"
    elif event in ("complete", "bt_complete"):
        new_status = "complete"
        # 生成制品下载 token
        if not artifact_token:
            artifact_path = _first_artifact_path(aria2_status)
            artifact_token = uuid4().hex
    elif event == "error":
        new_status = "error"
        error_msg = aria2_status.get("errorMessage", "未知错误")
    
    # 更新数据库
    update_fields = {
        "status": new_status,
        "updated_at": utc_now(),
    }
    if error_msg:
        update_fields["error"] = error_msg
    if artifact_path:
        update_fields["artifact_path"] = artifact_path
    if artifact_token:
        update_fields["artifact_token"] = artifact_token
    
    # 更新其他字段
    if aria2_status:
        update_fields["name"] = (
            aria2_status.get("bittorrent", {}).get("info", {}).get("name")
            or aria2_status.get("files", [{}])[0].get("path", "").split("/")[-1]
            or task.get("name")
        )
        update_fields["total_length"] = int(aria2_status.get("totalLength", 0))
        update_fields["completed_length"] = int(aria2_status.get("completedLength", 0))
        update_fields["download_speed"] = int(aria2_status.get("downloadSpeed", 0))
        update_fields["upload_speed"] = int(aria2_status.get("uploadSpeed", 0))
    
    # 构建 SQL
    set_clause = ", ".join(f"{k} = ?" for k in update_fields.keys())
    params = list(update_fields.values()) + [task["id"]]
    execute(f"UPDATE tasks SET {set_clause} WHERE id = ?", params)
    
    # 广播更新到 WebSocket
    await broadcast_update(state, task["owner_id"], task["id"])
    
    return {"ok": True, "task_id": task["id"], "status": new_status}
