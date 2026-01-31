"""aria2 WebSocket 事件监听器（共享下载架构）

通过 WebSocket 连接 aria2，订阅事件通知，实现毫秒级响应。
与轮询机制 (sync_tasks) 并行运行，事件驱动为主、轮询为辅。

关键特性：
- 自动重连：指数退避 + 抖动算法 (1s -> 60s max, +/- 20% jitter)
- 共享下载：处理多用户订阅同一任务的场景
- 空间检查：磁力链接解析后检查订阅者空间
"""
from __future__ import annotations

import asyncio
import logging
import random
from pathlib import Path
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

# 重连参数默认值
RECONNECT_BASE_DELAY = 1.0


def _http_to_ws_url(http_url: str) -> str:
    """将 HTTP RPC URL 转换为 WebSocket URL"""
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
    """计算指数退避延迟，带抖动"""
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

    1. 获取 aria2 状态
    2. 查找任务（支持 followingGid）
    3. 空间检查（start 事件，检查所有订阅者）
    4. 更新数据库
    5. 处理完成事件（创建 StoredFile 和 UserFile）
    6. 广播到所有订阅者
    """
    from sqlalchemy import update
    from sqlmodel import select

    from app.aria2.errors import parse_error_message
    from app.core.state import get_aria2_client, get_user_space_lock
    from app.database import get_session
    from app.models import (
        DownloadTask,
        User,
        UserTaskSubscription,
        utc_now_str,
    )
    from app.routers.config import get_max_task_size
    from app.routers.tasks import broadcast_task_update_to_subscribers
    from app.services.storage import get_user_space_info

    client = get_aria2_client()

    # 1. 获取 aria2 状态
    try:
        aria2_status = await client.tell_status(gid)
    except Exception as exc:
        logger.warning(f"获取 GID {gid} 状态失败: {exc}")
        aria2_status = {}

    # 2. 查找任务
    async with get_session() as db:
        result = await db.exec(select(DownloadTask).where(DownloadTask.gid == gid))
        task = result.first()

    # 2.1 通过 followingGid 查找（磁力链接转换场景）
    gid_updated = False
    if not task and aria2_status:
        following_gid = aria2_status.get("followingGid")
        if following_gid:
            logger.info(f"[WS] GID {gid} 未找到，尝试通过 followingGid {following_gid} 查找")
            async with get_session() as db:
                result = await db.exec(select(DownloadTask).where(DownloadTask.gid == following_gid))
                task = result.first()
                if task:
                    logger.info(f"[WS] 找到原任务 {task.id}，更新 GID: {following_gid} -> {gid}")
                    task.gid = gid
                    gid_updated = True
                    db.add(task)

    if not task:
        logger.debug(f"[WS] 未找到 GID {gid} 对应的任务，忽略事件")
        return

    task_id = task.id

    # 3. 空间检查（仅 start 事件，检查所有订阅者）
    if event == "start" and aria2_status:
        total_length = int(aria2_status.get("totalLength", 0))
        if total_length > 0:
            # 3.1 检查系统最大任务限制
            max_task_size = get_max_task_size()
            if total_length > max_task_size:
                logger.warning(
                    f"[WS] 任务 {task_id} 大小 {total_length / 1024**3:.2f} GB "
                    f"超过系统限制 {max_task_size / 1024**3:.2f} GB，终止任务"
                )
                await _cancel_task(
                    client, state, task, aria2_status,
                    f"已取消：大小 {total_length / 1024**3:.2f} GB 超过系统限制"
                )
                return

            # 3.2 检查所有订阅者的空间
            async with get_session() as db:
                result = await db.exec(
                    select(UserTaskSubscription, User)
                    .join(User, UserTaskSubscription.owner_id == User.id)
                    .where(
                        UserTaskSubscription.task_id == task_id,
                        UserTaskSubscription.status == "pending",
                    )
                )
                subscriptions = result.all()

            valid_subscribers = []

            for sub, user in subscriptions:
                user_lock = await get_user_space_lock(state, user.id)
                async with user_lock:
                    space_info = await get_user_space_info(user.id, user.quota)
                    # Each user's space is independent, use available directly
                    effective_available = space_info["available"]

                    if total_length <= effective_available:
                        # Use optimistic locking: only update if frozen_space is still 0
                        async with get_session() as db:
                            result = await db.execute(
                                update(UserTaskSubscription)
                                .where(
                                    UserTaskSubscription.id == sub.id,
                                    UserTaskSubscription.frozen_space == 0  # Optimistic lock
                                )
                                .values(frozen_space=total_length)
                            )

                            if result.rowcount > 0:
                                valid_subscribers.append((sub, user))
                            else:
                                # Already frozen by another process, re-check current state
                                async with get_session() as db:
                                    refreshed = await db.exec(
                                        select(UserTaskSubscription).where(
                                            UserTaskSubscription.id == sub.id
                                        )
                                    )
                                    current = refreshed.first()
                                    if current and current.status == "pending" and current.frozen_space > 0:
                                        valid_subscribers.append((sub, user))
                    else:
                        # Mark subscription as failed atomically
                        logger.warning(
                            f"[WS] 用户 {user.id} 空间不足，标记订阅 {sub.id} 失败"
                        )
                        async with get_session() as db:
                            await db.execute(
                                update(UserTaskSubscription)
                                .where(UserTaskSubscription.id == sub.id)
                                .values(
                                    status="failed",
                                    error_display="用户配额空间不足",
                                    frozen_space=0
                                )
                            )

            # If no valid subscribers, cancel the task
            if not valid_subscribers:
                logger.warning(f"[WS] 任务 {task_id} 没有有效订阅者，取消任务")
                await _cancel_task(
                    client, state, task, aria2_status,
                    "所有订阅者空间不足"
                )
                return

    # 4. 更新数据库状态
    new_status = task.status
    error_msg = None
    error_display = None

    if event == "start":
        new_status = "active"
    elif event == "pause":
        new_status = "paused"
    elif event == "stop":
        new_status = "error"
        error_display = "外部取消（管理员/外部客户端）"
        logger.info(f"[WS] 任务 {task_id} 外部取消")
    elif event == "complete":
        # 检查是否是磁力链接元数据下载完成
        followed_by = aria2_status.get("followedBy", [])
        if followed_by:
            new_gid = followed_by[0]
            logger.info(f"[WS] 磁力链接元数据下载完成，更新 GID: {gid} -> {new_gid}")
            async with get_session() as db:
                result = await db.exec(select(DownloadTask).where(DownloadTask.id == task_id))
                db_task = result.first()
                if db_task:
                    db_task.gid = new_gid
                    db_task.updated_at = utc_now_str()
                    db.add(db_task)
            return
        else:
            new_status = "complete"
    elif event == "bt_complete":
        new_status = "complete"
    elif event == "error":
        new_status = "error"
        raw_error = aria2_status.get("errorMessage", "后端错误")
        error_msg = raw_error
        error_display = parse_error_message(raw_error)
        logger.error(f"[WS] 任务 {task_id} 错误: {raw_error}")

    # Update task in database
    async with get_session() as db:
        result = await db.exec(select(DownloadTask).where(DownloadTask.id == task_id))
        db_task = result.first()
        if db_task:
            db_task.status = new_status
            db_task.updated_at = utc_now_str()

            if gid_updated:
                db_task.gid = gid
            if error_msg:
                db_task.error = error_msg
            if error_display:
                # 避免 stop 事件覆盖用户主动取消的状态
                if event == "stop" and db_task.error_display == "已取消":
                    pass
                else:
                    db_task.error_display = error_display

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

    # 5. 处理完成事件
    if new_status == "complete":
        await _handle_task_complete(state, task_id, aria2_status)

    # 5.1 处理 stop/error 事件 - 释放冻结空间并标记订阅失败
    if event in ("stop", "error"):
        await _handle_task_stop_or_error(task_id, error_display)

    # 6. 广播到所有订阅者
    await broadcast_task_update_to_subscribers(state, task_id)
    logger.debug(f"[WS] 事件处理完成: GID={gid}, event={event}, status={new_status}")


async def _handle_task_complete(
    state: AppState,
    task_id: int,
    aria2_status: dict,
) -> None:
    """处理任务完成事件

    1. 移动文件到 store
    2. 创建 StoredFile 记录
    3. 为所有成功的订阅者创建 UserFile 引用
    4. 释放冻结空间
    """
    from sqlalchemy import update
    from sqlmodel import select

    from app.database import get_session
    from app.models import DownloadTask, UserTaskSubscription, UserFile, StoredFile, utc_now_str
    from app.services.storage import (
        cleanup_task_download_dir,
        get_task_download_dir,
        move_to_store,
    )

    # Get task with idempotency check
    async with get_session() as db:
        result = await db.exec(select(DownloadTask).where(DownloadTask.id == task_id))
        task = result.first()

    if not task:
        return

    # Idempotency check: skip if already processed
    if task.stored_file_id is not None:
        logger.debug(f"[WS] Task {task_id} already processed (stored_file_id={task.stored_file_id}), skipping")
        return

    # Additional check: verify task status is complete
    if task.status != "complete":
        logger.warning(f"[WS] Task {task_id} status is {task.status}, not complete, skipping")
        return

    # Get source file path
    files = aria2_status.get("files", [])
    if not files:
        logger.error(f"[WS] 任务 {task_id} 完成但没有文件信息")
        return

    first_file_path = files[0].get("path")
    if not first_file_path:
        logger.error(f"[WS] 任务 {task_id} 完成但文件路径为空")
        return

    source_path = Path(first_file_path)

    # Determine the actual item to move (file or top-level directory)
    task_dir = get_task_download_dir(task_id)
    try:
        if source_path.is_relative_to(task_dir):
            rel_path = source_path.relative_to(task_dir)
            top_level = rel_path.parts[0] if rel_path.parts else None
            if top_level:
                source_path = task_dir / top_level
    except Exception:
        pass

    if not source_path.exists():
        logger.error(f"[WS] 任务 {task_id} 完成但源文件不存在: {source_path}")
        return

    # Get original name
    original_name = task.name or source_path.name

    try:
        # Move to store and create StoredFile
        stored_file = await move_to_store(source_path, original_name)

        # Update task with stored_file_id atomically using compare-and-swap pattern
        async with get_session() as db:
            result = await db.execute(
                update(DownloadTask)
                .where(
                    DownloadTask.id == task_id,
                    DownloadTask.stored_file_id.is_(None)  # Only update if not already set
                )
                .values(
                    stored_file_id=stored_file.id,
                    completed_at=utc_now_str()
                )
            )

            if result.rowcount == 0:
                # Another process already set stored_file_id
                logger.info(f"[WS] Task {task_id} already processed by another handler")
                await cleanup_task_download_dir(task_id)
                return

        # Create UserFile references for all pending subscribers
        async with get_session() as db:
            result = await db.exec(
                select(UserTaskSubscription).where(
                    UserTaskSubscription.task_id == task_id,
                    UserTaskSubscription.status == "pending",
                )
            )
            subscriptions = result.all()

        for sub in subscriptions:
            # Create file reference and update status in single transaction
            # Use retry logic to handle UNIQUE constraint race condition
            try:
                async with get_session() as db:
                    # Update subscription status first; skip if no longer pending
                    result = await db.execute(
                        update(UserTaskSubscription)
                        .where(
                            UserTaskSubscription.id == sub.id,
                            UserTaskSubscription.status == "pending",
                        )
                        .values(status="success", frozen_space=0)
                    )
                    if result.rowcount == 0:
                        continue

                    # Check if reference already exists
                    result = await db.exec(
                        select(UserFile).where(
                            UserFile.owner_id == sub.owner_id,
                            UserFile.stored_file_id == stored_file.id,
                        )
                    )
                    existing_ref = result.first()

                    if not existing_ref:
                        # Create file reference
                        user_file = UserFile(
                            owner_id=sub.owner_id,
                            stored_file_id=stored_file.id,
                            display_name=original_name,
                            created_at=utc_now_str(),
                        )
                        db.add(user_file)

                        # Increment reference count
                        await db.execute(
                            update(StoredFile)
                            .where(StoredFile.id == stored_file.id)
                            .values(ref_count=StoredFile.ref_count + 1)
                        )
            except Exception as e:
                # Race condition: another process created the UserFile between our check and insert
                # The transaction rolled back, so subscription status is still "pending"
                # Retry: just update subscription status (UserFile already exists)
                logger.debug(
                    f"[WS] UserFile creation race for sub {sub.id}, retrying status update: {e}"
                )
                try:
                    async with get_session() as db:
                        await db.execute(
                            update(UserTaskSubscription)
                            .where(
                                UserTaskSubscription.id == sub.id,
                                UserTaskSubscription.status == "pending",
                            )
                            .values(status="success", frozen_space=0)
                        )
                except Exception as retry_err:
                    logger.warning(
                        f"[WS] Failed to update subscription {sub.id} status after race: {retry_err}"
                    )

            # Write to history
            from app.services.history import add_task_history
            await add_task_history(
                owner_id=sub.owner_id,
                task_name=original_name,
                result="completed",
                reason="下载完成",
                uri=task.uri,
                total_length=task.total_length,
                created_at=sub.created_at,
            )

        logger.info(f"[WS] 任务 {task_id} 完成，创建了 {len(subscriptions)} 个用户文件引用")

    except Exception as e:
        logger.error(f"[WS] 处理任务 {task_id} 完成事件失败: {e}")

    # Clean up task download directory
    await cleanup_task_download_dir(task_id)


async def _handle_task_stop_or_error(
    task_id: int,
    error_display: str | None,
) -> None:
    """处理任务停止或错误事件

    释放所有订阅者的冻结空间并标记订阅为失败。
    """
    from sqlalchemy import update
    from sqlmodel import select

    from app.database import get_session
    from app.models import UserTaskSubscription

    async with get_session() as db:
        # 获取所有 pending 状态的订阅
        result = await db.exec(
            select(UserTaskSubscription).where(
                UserTaskSubscription.task_id == task_id,
                UserTaskSubscription.status == "pending",
            )
        )
        subscriptions = result.all()

        # 更新所有订阅：释放冻结空间，标记为失败
        message = error_display or "后端错误"
        for sub in subscriptions:
            await db.execute(
                update(UserTaskSubscription)
                .where(
                    UserTaskSubscription.id == sub.id,
                    UserTaskSubscription.status == "pending",
                )
                .values(
                    status="failed",
                    frozen_space=0,
                    error_display=message,
                )
            )

    logger.info(f"[WS] 任务 {task_id} 停止/错误，释放了 {len(subscriptions)} 个订阅的冻结空间")


async def _cancel_task(
    client,
    state: AppState,
    task,
    aria2_status: dict,
    error_message: str,
) -> None:
    """取消任务并通知所有订阅者"""
    from sqlmodel import select

    from app.database import get_session
    from app.models import DownloadTask, UserTaskSubscription, utc_now_str
    from app.routers.tasks import broadcast_task_update_to_subscribers
    from app.services.storage import cleanup_task_download_dir

    gid = task.gid

    # Stop aria2 task
    try:
        await client.force_remove(gid)
    except Exception:
        pass
    try:
        await client.remove_download_result(gid)
    except Exception:
        pass

    # Update task status
    async with get_session() as db:
        result = await db.exec(select(DownloadTask).where(DownloadTask.id == task.id))
        db_task = result.first()
        if db_task:
            db_task.status = "error"
            db_task.gid = None
            db_task.error_display = error_message
            db_task.download_speed = 0
            db_task.upload_speed = 0
            db_task.updated_at = utc_now_str()
            if aria2_status:
                db_task.name = (
                    aria2_status.get("bittorrent", {}).get("info", {}).get("name")
                    or aria2_status.get("files", [{}])[0].get("path", "").split("/")[-1]
                    or db_task.name
                )
                db_task.total_length = int(aria2_status.get("totalLength", 0))
            db.add(db_task)

    # Mark all pending subscriptions as failed and record history
    async with get_session() as db:
        result = await db.exec(
            select(UserTaskSubscription).where(
                UserTaskSubscription.task_id == task.id,
                UserTaskSubscription.status == "pending",
            )
        )
        subscriptions = result.all()

        for sub in subscriptions:
            sub.status = "failed"
            sub.error_display = error_message
            sub.frozen_space = 0
            db.add(sub)

    # Record history for each failed subscription
    from app.services.history import add_task_history
    task_name = (
        aria2_status.get("bittorrent", {}).get("info", {}).get("name")
        or aria2_status.get("files", [{}])[0].get("path", "").split("/")[-1]
        or task.name
        or "未知任务"
    ) if aria2_status else (task.name or "未知任务")

    for sub in subscriptions:
        await add_task_history(
            owner_id=sub.owner_id,
            task_name=task_name,
            result="failed",
            reason=error_message,
            uri=task.uri,
            total_length=int(aria2_status.get("totalLength", 0)) if aria2_status else task.total_length,
            created_at=sub.created_at,
        )

    # Clean up download directory
    await cleanup_task_download_dir(task.id)

    # Broadcast update
    await broadcast_task_update_to_subscribers(state, task.id)


async def listen_aria2_events(state: AppState) -> None:
    """aria2 WebSocket 事件监听器主循环"""
    from app.core.config import settings
    from app.routers.config import get_config_value

    reconnect_attempt = 0

    while True:
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
                    reconnect_attempt = 0

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

        delay = _calculate_backoff(reconnect_attempt)
        reconnect_attempt += 1
        logger.info(f"[WS] {delay:.1f} 秒后重连 (尝试 #{reconnect_attempt})")

        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            logger.info("[WS] 监听器任务被取消，正在退出")
            raise
