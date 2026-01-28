"""aria2 WebSocket 事件监听器

通过 WebSocket 连接 aria2，订阅事件通知，实现毫秒级响应。
与轮询机制 (sync_tasks) 并行运行，事件驱动为主、轮询为辅。

关键特性：
- 自动重连：指数退避 + 抖动算法 (1s -> 60s max, +/- 20% jitter)
- 事件处理：复用 hooks.py 逻辑
- 优雅关闭：支持 CancelledError
"""
from __future__ import annotations

import asyncio
import logging
import random
from typing import TYPE_CHECKING
from urllib.parse import urlparse, urlunparse

import aiohttp

if TYPE_CHECKING:
    from app.core.state import AppState

logger = logging.getLogger(__name__)

# 事件方法到内部事件名的映射
EVENT_MAP = {
    "aria2.onDownloadStart": "start",
    "aria2.onDownloadPause": "pause",
    "aria2.onDownloadStop": "stop",
    "aria2.onDownloadComplete": "complete",
    "aria2.onDownloadError": "error",
    "aria2.onBtDownloadComplete": "bt_complete",
}

# 重连参数默认值（可通过配置覆盖）
RECONNECT_BASE_DELAY = 1.0      # 初始延迟（秒）


def _http_to_ws_url(http_url: str) -> str:
    """将 HTTP RPC URL 转换为 WebSocket URL

    Args:
        http_url: HTTP URL, e.g., "http://localhost:6800/jsonrpc"

    Returns:
        WebSocket URL, e.g., "ws://localhost:6800/jsonrpc"
    """
    parsed = urlparse(http_url)
    if parsed.scheme == "https":
        ws_scheme = "wss"
    else:
        ws_scheme = "ws"
    return urlunparse((ws_scheme, parsed.netloc, parsed.path, "", "", ""))


def _calculate_backoff(
    attempt: int,
    max_delay: float | None = None,
    jitter: float | None = None,
    factor: float | None = None,
) -> float:
    """计算指数退避延迟，带抖动

    Args:
        attempt: 重连尝试次数（从 0 开始）
        max_delay: 最大延迟（秒），None 时从配置读取
        jitter: 抖动系数 (0-1)，None 时从配置读取
        factor: 指数因子，None 时从配置读取

    Returns:
        延迟时间（秒），包含随机抖动
    """
    from app.routers.config import (
        get_ws_reconnect_factor,
        get_ws_reconnect_jitter,
        get_ws_reconnect_max_delay,
    )

    if max_delay is None:
        max_delay = get_ws_reconnect_max_delay()
    if jitter is None:
        jitter = get_ws_reconnect_jitter()
    if factor is None:
        factor = get_ws_reconnect_factor()

    base_delay = min(RECONNECT_BASE_DELAY * (factor ** attempt), max_delay)
    jitter_offset = base_delay * jitter * (2 * random.random() - 1)
    return base_delay + jitter_offset


async def handle_aria2_event(
    state: AppState,
    gid: str,
    event: str,
) -> None:
    """处理 aria2 事件

    复用 hooks.py 的核心逻辑：
    1. 获取 aria2 状态
    2. 查找任务（支持 followingGid）
    3. 空间检查（start 事件）
    4. 更新数据库
    5. 广播到前端

    Args:
        state: 应用状态
        gid: 任务 GID
        event: 事件类型 (start, pause, stop, complete, error, bt_complete)
    """
    from uuid import uuid4

    from sqlmodel import select

    from app.aria2.errors import parse_error_message
    from app.aria2.sync import (
        _cancel_and_delete_task,
        _move_completed_files,
        broadcast_update,
    )
    from app.core.state import get_aria2_client
    from app.database import get_session
    from app.models import Task, User
    from app.routers.config import get_max_task_size
    from app.routers.hooks import _get_user_available_space

    def utc_now() -> str:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat()

    client = get_aria2_client()

    # 1. 获取 aria2 状态
    try:
        aria2_status = await client.tell_status(gid)
    except Exception as exc:
        logger.warning(f"获取 GID {gid} 状态失败: {exc}")
        aria2_status = {}

    # 2. 查找任务
    async with get_session() as db:
        result = await db.exec(select(Task).where(Task.gid == gid))
        task = result.first()

    # 2.1 通过 followingGid 查找（磁力链接转换场景）
    gid_updated = False
    if not task and aria2_status:
        following_gid = aria2_status.get("followingGid")
        if following_gid:
            logger.info(f"[WS] GID {gid} 未找到，尝试通过 followingGid {following_gid} 查找")
            async with get_session() as db:
                result = await db.exec(select(Task).where(Task.gid == following_gid))
                task = result.first()
                if task:
                    logger.info(f"[WS] 找到原任务 {task.id}，更新 GID: {following_gid} -> {gid}")
                    task.gid = gid
                    gid_updated = True
                    db.add(task)

    if not task:
        logger.debug(f"[WS] 未找到 GID {gid} 对应的任务，忽略事件")
        return

    # 3. 获取用户信息
    user: User | None = None
    async with get_session() as db:
        result = await db.exec(select(User).where(User.id == task.owner_id))
        user = result.first()

    # 4. 空间检查（仅 start 事件）
    if event == "start" and aria2_status and user:
        total_length = int(aria2_status.get("totalLength", 0))
        if total_length > 0:
            task_name = (
                aria2_status.get("bittorrent", {}).get("info", {}).get("name")
                or aria2_status.get("files", [{}])[0].get("path", "").split("/")[-1]
                or "未知任务"
            )

            # 4.1 检查系统最大任务限制
            max_task_size = get_max_task_size()
            if total_length > max_task_size:
                logger.warning(
                    f"[WS] 任务 {task.id} 大小 {total_length / 1024**3:.2f} GB "
                    f"超过系统限制 {max_task_size / 1024**3:.2f} GB，终止并删除任务"
                )
                await _cancel_and_delete_task(
                    client, state, task, aria2_status,
                    f"已取消：大小 {total_length / 1024**3:.2f} GB 超过系统限制 {max_task_size / 1024**3:.2f} GB"
                )
                return

            # 4.2 检查用户可用空间
            user_available = _get_user_available_space(user)
            if total_length > user_available:
                logger.warning(
                    f"[WS] 任务 {task.id} 大小 {total_length / 1024**3:.2f} GB "
                    f"超过用户可用空间 {user_available / 1024**3:.2f} GB，终止并删除任务"
                )
                await _cancel_and_delete_task(
                    client, state, task, aria2_status,
                    f"已取消：大小 {total_length / 1024**3:.2f} GB 超过可用空间 {user_available / 1024**3:.2f} GB"
                )
                return

    # 5. 更新数据库状态
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
        if not artifact_token:
            # 移动文件从 .incomplete 到用户根目录
            artifact_path = _move_completed_files(aria2_status, task.owner_id)
            artifact_token = uuid4().hex
    elif event == "error":
        new_status = "error"
        raw_error = aria2_status.get("errorMessage", "未知错误")
        error_msg = parse_error_message(raw_error)

    async with get_session() as db:
        result = await db.exec(select(Task).where(Task.id == task.id))
        db_task = result.first()
        if db_task:
            db_task.status = new_status
            db_task.updated_at = utc_now()

            if gid_updated:
                db_task.gid = gid
            if error_msg:
                db_task.error = error_msg
            if artifact_path:
                db_task.artifact_path = artifact_path
            if artifact_token:
                db_task.artifact_token = artifact_token

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

    # 6. 广播到前端（状态变更强制推送）
    await broadcast_update(state, task.owner_id, task.id, force=True)
    logger.debug(f"[WS] 事件处理完成: GID={gid}, event={event}, status={new_status}")


async def listen_aria2_events(state: AppState) -> None:
    """aria2 WebSocket 事件监听器主循环

    - 连接 aria2 WebSocket 端点
    - 接收并处理事件通知
    - 断开后自动重连（指数退避 + 抖动）

    Args:
        state: 应用状态，用于广播更新
    """
    from app.core.config import settings
    from app.routers.config import get_config_value

    reconnect_attempt = 0

    while True:
        # 动态获取 aria2 配置
        rpc_url = get_config_value("aria2_rpc_url")
        if not rpc_url:
            rpc_url = settings.aria2_rpc_url

        ws_url = _http_to_ws_url(rpc_url)

        try:
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                logger.info(f"[WS] 正在连接 aria2 WebSocket: {ws_url}")

                async with session.ws_connect(ws_url) as ws:
                    logger.info("[WS] 已连接 aria2 WebSocket")
                    reconnect_attempt = 0  # 连接成功，重置重连计数

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
                                            logger.debug(f"[WS] 收到事件: {method}, GID={gid}")
                                            # 异步处理事件，不阻塞消息接收
                                            asyncio.create_task(
                                                handle_aria2_event(state, gid, event),
                                                name=f"aria2_event_{gid}_{event}"
                                            )
                            except Exception as exc:
                                logger.warning(f"[WS] 解析消息失败: {exc}")

                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            logger.error(f"[WS] WebSocket 错误: {ws.exception()}")
                            break

                        elif msg.type == aiohttp.WSMsgType.CLOSED:
                            logger.warning("[WS] WebSocket 连接已关闭")
                            break

        except asyncio.CancelledError:
            logger.info("[WS] 监听器任务被取消，正在退出")
            raise

        except Exception as exc:
            logger.warning(f"[WS] 连接失败: {exc}")

        # 计算重连延迟
        delay = _calculate_backoff(reconnect_attempt)
        reconnect_attempt += 1
        logger.info(f"[WS] {delay:.1f} 秒后重连 (尝试 #{reconnect_attempt})")

        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            logger.info("[WS] 监听器任务被取消，正在退出")
            raise
