"""aria2 任务同步模块（共享下载架构）

轮询 aria2 状态，作为 WebSocket 事件监听的补充机制。
主要功能：
- 同步任务进度
- 检测大小变化（HTTP 下载）
- 清理孤立任务
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import time
from pathlib import Path

from sqlalchemy import case, update
from sqlmodel import select

from app.aria2.client import Aria2Client
from app.aria2.errors import parse_error_message
from app.core.config import settings
from app.core.security import sanitize_string
from app.core.state import AppState, WS_THROTTLE_INTERVAL
from app.database import get_session
from app.models import DownloadTask, User, UserTaskSubscription, utc_now_str

logger = logging.getLogger(__name__)


def delete_path_with_aria2(target: Path) -> bool:
    """删除文件/目录，并清理对应的 .aria2 控制文件"""
    if not target.exists():
        return False

    try:
        if target.is_symlink():
            target.unlink()
        elif target.is_file():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target)
        else:
            return False

        aria2_file = target.parent / f"{target.name}.aria2"
        if aria2_file.exists() and aria2_file.is_file():
            try:
                aria2_file.unlink()
            except Exception:
                pass

        return True
    except Exception:
        return False


def _sanitize_path(file_path: str | None, task_id: int) -> str | None:
    """将绝对路径转换为文件名"""
    if not file_path:
        return None

    try:
        abs_path = Path(file_path)
        return abs_path.name if abs_path.name else file_path
    except Exception:
        return file_path


def _map_status(status: dict, task_id: int) -> dict:
    """映射 aria2 状态到数据库字段"""
    raw_name = (
        status.get("bittorrent", {}).get("info", {}).get("name")
        or status.get("files", [{}])[0].get("path")
    )

    sanitized_name = _sanitize_path(raw_name, task_id)
    sanitized_name = sanitize_string(sanitized_name)

    raw_error = status.get("errorMessage")
    error_msg = parse_error_message(raw_error) if raw_error else None
    error_msg = sanitize_string(error_msg)

    return {
        "status": status.get("status", "unknown"),
        "name": sanitized_name,
        "total_length": int(status.get("totalLength", 0)),
        "completed_length": int(status.get("completedLength", 0)),
        "download_speed": int(status.get("downloadSpeed", 0)),
        "upload_speed": int(status.get("uploadSpeed", 0)),
        "error": error_msg,
    }


async def _update_task(task_id: int, values: dict) -> None:
    """更新任务字段"""
    async with get_session() as db:
        result = await db.exec(select(DownloadTask).where(DownloadTask.id == task_id))
        task = result.first()
        if task:
            for key, value in values.items():
                setattr(task, key, value)
            task.updated_at = utc_now_str()
            db.add(task)


async def sync_tasks(
    state: AppState,
    interval: float,
) -> None:
    """同步 aria2 任务状态到数据库

    作为 WebSocket 事件监听的补充机制。
    """
    from app.core.state import get_aria2_client
    from app.routers.config import get_max_task_size
    from app.routers.tasks import broadcast_task_update_to_subscribers
    from app.services.storage import get_user_space_info

    while True:
        client = get_aria2_client()

        # Get all active tasks
        async with get_session() as db:
            result = await db.exec(
                select(DownloadTask).where(
                    DownloadTask.gid.isnot(None),
                    DownloadTask.status.in_(["queued", "active"]),
                )
            )
            tasks = result.all()

        async def fetch_and_update(task: DownloadTask) -> None:
            gid = task.gid
            if not gid:
                return

            try:
                status = await client.tell_status(gid)
            except Exception as exc:
                await _update_task(task.id, {"status": "error", "error": str(exc)})
                return

            aria2_status = status.get("status")
            total_length = int(status.get("totalLength", 0))

            # Check size when it becomes known
            if aria2_status == "active" and total_length > 0 and (task.total_length or 0) == 0:
                task_name = (
                    status.get("bittorrent", {}).get("info", {}).get("name")
                    or status.get("files", [{}])[0].get("path", "").split("/")[-1]
                    or "未知任务"
                )

                # Check system limit
                max_task_size = get_max_task_size()
                if total_length > max_task_size:
                    logger.warning(
                        f"[Sync] 任务 {task.id} 大小 {total_length / 1024**3:.2f} GB "
                        f"超过系统限制，终止任务"
                    )
                    await _cancel_task_sync(
                        client, state, task, status,
                        f"已取消：大小超过系统限制"
                    )
                    return

                # Check all subscribers' space
                async with get_session() as db:
                    result = await db.exec(
                        select(UserTaskSubscription, User)
                        .join(User, UserTaskSubscription.owner_id == User.id)
                        .where(
                            UserTaskSubscription.task_id == task.id,
                            UserTaskSubscription.status == "pending",
                        )
                    )
                    subscriptions = result.all()

                valid_subscribers = []
                cumulative_frozen = 0  # Track frozen space within this transaction

                for sub, user in subscriptions:
                    space_info = await get_user_space_info(user.id, user.quota)
                    # Subtract cumulative frozen space from available (for same-user multiple subscriptions)
                    effective_available = space_info["available"] - cumulative_frozen

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
                                cumulative_frozen += total_length
                            # If rowcount == 0, already frozen by another process, skip
                    else:
                        # Mark subscription as failed atomically
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

                if not valid_subscribers:
                    logger.warning(f"[Sync] 任务 {task.id} 没有有效订阅者，取消任务")
                    await _cancel_task_sync(
                        client, state, task, status,
                        "所有订阅者空间不足"
                    )
                    return

            # Update task status with atomic peak value updates
            mapped = _map_status(status, task.id)

            # Handle magnet link metadata completion
            if mapped["status"] == "complete":
                followed_by = status.get("followedBy", [])
                if followed_by:
                    new_gid = followed_by[0]
                    logger.info(f"[Sync] 磁力链接元数据完成，更新 GID: {task.gid} -> {new_gid}")
                    await _update_task(task.id, {"gid": new_gid})
                    return

            # Track peak values using SQL CASE for atomic conditional update
            current_speed = mapped["download_speed"]
            current_connections = int(status.get("connections", 0))

            async with get_session() as db:
                await db.execute(
                    update(DownloadTask)
                    .where(DownloadTask.id == task.id)
                    .values(
                        status=mapped["status"],
                        name=mapped["name"],
                        total_length=mapped["total_length"],
                        completed_length=mapped["completed_length"],
                        download_speed=mapped["download_speed"],
                        upload_speed=mapped["upload_speed"],
                        error=mapped.get("error"),
                        updated_at=utc_now_str(),
                        # Atomic peak value update: only update if new value is greater
                        peak_download_speed=case(
                            (DownloadTask.peak_download_speed < current_speed, current_speed),
                            else_=DownloadTask.peak_download_speed
                        ),
                        peak_connections=case(
                            (DownloadTask.peak_connections < current_connections, current_connections),
                            else_=DownloadTask.peak_connections
                        ),
                    )
                )

            # Broadcast update
            await broadcast_task_update_to_subscribers(state, task.id)

        # Process all tasks concurrently
        await asyncio.gather(*[fetch_and_update(task) for task in tasks])

        await asyncio.sleep(interval)


async def _cancel_task_sync(
    client: Aria2Client,
    state: AppState,
    task: DownloadTask,
    aria2_status: dict,
    error_message: str,
) -> None:
    """取消任务（sync 版本）"""
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

    # Mark all pending subscriptions as failed
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

    # Clean up download directory
    await cleanup_task_download_dir(task.id)

    # Broadcast update
    await broadcast_task_update_to_subscribers(state, task.id)


# WebSocket helpers

async def broadcast_update(state: AppState, user_id: int, task_id: int, force: bool = False) -> None:
    """广播任务更新到 WebSocket 客户端（兼容旧代码）

    新架构使用 broadcast_task_update_to_subscribers
    """
    from app.routers.tasks import broadcast_task_update_to_subscribers
    await broadcast_task_update_to_subscribers(state, task_id)


async def broadcast_notification(
    state: AppState,
    user_id: int,
    message: str,
    level: str = "error"
) -> None:
    """广播通知消息到 WebSocket 客户端

    Handles connection failures gracefully with automatic cleanup.
    """
    async with state.lock:
        sockets = list(state.ws_connections.get(user_id, set()))

    failed_sockets = []
    for ws in sockets:
        try:
            await ws.send_json({
                "type": "notification",
                "level": level,
                "message": message,
            })
        except Exception as e:
            logger.debug(f"WebSocket send failed for user {user_id}: {e}")
            failed_sockets.append(ws)

    # Clean up failed connections outside the iteration
    for ws in failed_sockets:
        try:
            await unregister_ws(state, user_id, ws)
        except Exception:
            pass


async def register_ws(state: AppState, user_id: int, ws) -> None:
    async with state.lock:
        state.ws_connections.setdefault(user_id, set()).add(ws)


async def unregister_ws(state: AppState, user_id: int, ws) -> None:
    async with state.lock:
        sockets = state.ws_connections.get(user_id)
        if sockets:
            sockets.discard(ws)
