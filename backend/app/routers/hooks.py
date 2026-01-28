"""Aria2 回调钩子处理模块

Aria2 通过 --on-download-* 参数调用外部脚本，脚本再调用此接口更新任务状态。

关键功能：
- 磁力链接 followingGid 跟踪：磁力链接解析后会创建新任务（新 GID），通过 followingGid 关联
- BT 任务空间检查：在 start 事件时检查任务大小，超过用户可用空间则立即终止
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import BaseModel
from sqlmodel import select

from app.aria2.client import Aria2Client
from app.aria2.errors import parse_error_message
from app.aria2.sync import _cancel_and_delete_task, broadcast_update, broadcast_notification
from app.core.config import settings
from app.core.state import AppState, get_aria2_client
from app.database import get_session
from app.models import Task, User
from app.routers.config import get_max_task_size


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/hooks", tags=["hooks"])


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def _get_user_available_space(user: User) -> int:
    """获取用户实际可用空间（考虑配额和机器空间限制）

    返回: 用户可用空间（字节）
    """
    import shutil

    # 计算用户已使用的空间
    user_dir = Path(settings.download_dir) / str(user.id)
    used_space = 0
    if user_dir.exists():
        for file_path in user_dir.rglob("*"):
            if file_path.is_file():
                try:
                    used_space += file_path.stat().st_size
                except Exception:
                    pass

    # 用户配额
    user_quota = user.quota if user.quota else 100 * 1024 * 1024 * 1024  # 默认 100GB

    # 获取机器实际剩余空间
    download_path = Path(settings.download_dir)
    download_path.mkdir(parents=True, exist_ok=True)
    disk = shutil.disk_usage(download_path)
    machine_free = disk.free

    # 用户理论可用空间（基于配额）
    user_free_by_quota = max(0, user_quota - used_space)

    # 实际可用空间 = min(用户配额剩余, 机器剩余空间)
    return min(user_free_by_quota, machine_free)


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

    特殊处理:
    - 磁力链接 followingGid：磁力链接解析后会创建新任务，通过 followingGid 关联到原任务
    - BT 任务空间检查：在 start 事件时检查任务大小，超过可用空间则终止任务
    """
    # 验证 hook secret（必须配置）
    if not settings.hook_secret:
        logger.error(
            "Hook secret 未配置，拒绝回调请求。"
            "请设置 ARIA2C_HOOK_SECRET 环境变量并重启服务。"
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Hook secret not configured. Set ARIA2C_HOOK_SECRET environment variable."
        )

    if x_hook_secret != settings.hook_secret:
        # 记录认证失败，便于排查 aria2 配置问题
        logger.warning(
            f"Hook 认证失败：收到的 secret 不匹配 (GID: {payload.gid}, Event: {payload.event})。"
            f"请检查 aria2 的 hook 脚本中传递的 secret 是否正确。"
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid hook secret"
        )

    gid = payload.gid
    event = payload.event
    client = _get_client(request)
    state = _get_state(request)

    # 先获取 aria2 状态（后续需要用到）
    try:
        aria2_status = await client.tell_status(gid)
    except Exception:
        aria2_status = {}

    # 查找对应任务
    async with get_session() as db:
        result = await db.exec(select(Task).where(Task.gid == gid))
        task = result.first()

    # 如果找不到任务，尝试通过 followingGid 查找（磁力链接转换场景）
    gid_updated = False
    if not task and aria2_status:
        following_gid = aria2_status.get("followingGid")
        if following_gid:
            logger.info(f"GID {gid} 未找到，尝试通过 followingGid {following_gid} 查找")
            async with get_session() as db:
                result = await db.exec(select(Task).where(Task.gid == following_gid))
                task = result.first()
                if task:
                    # 找到原任务，更新 GID 为新的 GID
                    logger.info(f"找到原任务 {task.id}，更新 GID: {following_gid} -> {gid}")
                    task.gid = gid
                    gid_updated = True
                    db.add(task)

    if not task:
        # 仍然找不到，可能是孤立任务
        logger.warning(f"未找到 GID 为 {gid} 的任务（followingGid: {aria2_status.get('followingGid')}）")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"未找到 GID 为 {gid} 的任务"
        )

    # 获取任务所有者信息（用于空间检查）
    user: User | None = None
    async with get_session() as db:
        result = await db.exec(select(User).where(User.id == task.owner_id))
        user = result.first()

    # ========== BT 任务空间检查（在 start 事件时执行）==========
    # 这是防止用户添加超大磁力链接/种子的关键检查点
    # 超标时直接删除任务，通过 WebSocket 通知用户
    if event == "start" and aria2_status and user:
        total_length = int(aria2_status.get("totalLength", 0))
        if total_length > 0:
            task_name = (
                aria2_status.get("bittorrent", {}).get("info", {}).get("name")
                or aria2_status.get("files", [{}])[0].get("path", "").split("/")[-1]
                or "未知任务"
            )

            # 检查系统最大任务限制
            max_task_size = get_max_task_size()
            if total_length > max_task_size:
                logger.warning(
                    f"任务 {task.id} 大小 {total_length / 1024**3:.2f} GB "
                    f"超过系统限制 {max_task_size / 1024**3:.2f} GB，终止任务"
                )
                await _cancel_and_delete_task(
                    client, state, task, aria2_status,
                    f"已取消：大小 {total_length / 1024**3:.2f} GB 超过系统限制 {max_task_size / 1024**3:.2f} GB"
                )
                return {"ok": True, "task_id": task.id, "status": "cancelled", "reason": "exceeded_max_task_size"}

            # 检查用户可用空间
            user_available = _get_user_available_space(user)
            if total_length > user_available:
                logger.warning(
                    f"任务 {task.id} 大小 {total_length / 1024**3:.2f} GB "
                    f"超过用户可用空间 {user_available / 1024**3:.2f} GB，终止任务"
                )
                await _cancel_and_delete_task(
                    client, state, task, aria2_status,
                    f"已取消：大小 {total_length / 1024**3:.2f} GB 超过可用空间 {user_available / 1024**3:.2f} GB"
                )
                return {"ok": True, "task_id": task.id, "status": "cancelled", "reason": "insufficient_space"}

    # ========== 正常事件处理 ==========
    # 根据事件类型更新状态
    new_status = task.status
    error_msg = None
    artifact_path = task.artifact_path
    artifact_token = task.artifact_token

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
        raw_error = aria2_status.get("errorMessage", "未知错误")
        error_msg = parse_error_message(raw_error)

    # 更新数据库
    async with get_session() as db:
        result = await db.exec(select(Task).where(Task.id == task.id))
        db_task = result.first()
        if db_task:
            db_task.status = new_status
            db_task.updated_at = utc_now()

            # 如果 GID 被更新了（followingGid 场景），同步更新
            if gid_updated:
                db_task.gid = gid

            if error_msg:
                db_task.error = error_msg
            if artifact_path:
                db_task.artifact_path = artifact_path
            if artifact_token:
                db_task.artifact_token = artifact_token

            # 更新其他字段
            if aria2_status:
                db_task.name = (
                    aria2_status.get("bittorrent", {}).get("info", {}).get("name")
                    or aria2_status.get("files", [{}])[0].get("path", "").split("/")[-1]
                    or db_task.name
                )
                db_task.total_length = int(aria2_status.get("totalLength", 0))
                db_task.completed_length = int(aria2_status.get("completedLength", 0))
                db_task.download_speed = int(aria2_status.get("downloadSpeed", 0))
                db_task.upload_speed = int(aria2_status.get("uploadSpeed", 0))

            db.add(db_task)

    # 广播更新到 WebSocket（状态变更强制推送）
    await broadcast_update(state, task.owner_id, task.id, force=True)

    return {"ok": True, "task_id": task.id, "status": new_status}
