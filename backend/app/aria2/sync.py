"""aria2 任务同步模块（共享下载架构）

轮询 aria2 状态，作为 WebSocket 事件监听的补充机制。
主要功能：
- 同步任务进度
- 检测大小变化（HTTP 下载）
- 清理孤立任务
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

import asyncio
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import case, update
from sqlmodel import select

from app.aria2.client import Aria2Client
from app.aria2.errors import parse_error_message
from app.core.security import sanitize_string
from app.core.state import AppState
from app.database import get_session
from app.models import DownloadTask, User, UserTaskSubscription, utc_now_str

logger = logging.getLogger(__name__)

ORPHAN_GRACE_SECONDS = 60.0
ORPHAN_CLEANUP_BATCH = 50
COMPLETE_REPAIR_GRACE_SECONDS = 30.0


def _sanitize_path(file_path: str | None, task_id: int) -> str | None:
    """将绝对路径转换为文件名"""
    if not file_path:
        return None

    try:
        abs_path = Path(file_path)
        return abs_path.name if abs_path.name else file_path
    except (ValueError, OSError) as e:
        logger.debug(f"Failed to sanitize path for task {task_id}: {e}")
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
    error_display = parse_error_message(raw_error) if raw_error else None
    error_display = sanitize_string(error_display) if error_display else None

    def safe_int(val, default: int = 0) -> int:
        try:
            return int(val) if val is not None else default
        except (ValueError, TypeError):
            return default

    return {
        "status": status.get("status", "unknown"),
        "name": sanitized_name,
        "total_length": safe_int(status.get("totalLength")),
        "completed_length": safe_int(status.get("completedLength")),
        "download_speed": safe_int(status.get("downloadSpeed")),
        "upload_speed": safe_int(status.get("uploadSpeed")),
        "error": raw_error,
        "error_display": error_display,
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
    from app.core.state import get_aria2_client, get_user_space_lock
    from app.routers.config import get_max_task_size
    from app.routers.tasks import broadcast_task_update_to_subscribers
    from app.services.storage import get_user_space_info

    orphan_seen_at: dict[str, float] = {}

    while True:
        await _repair_inconsistent_completed_tasks(state)
        client = get_aria2_client()

        async with get_session() as db:
            result = await db.exec(select(DownloadTask))
            tracked_tasks = result.all()
        tracked_gids = {t.gid for t in tracked_tasks if t.gid}

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
            task_id = task.id
            if task_id is None:
                logger.warning(f"[Sync] 任务缺少 task_id，跳过 gid={gid}")
                return

            try:
                status = await client.tell_status(gid)
            except Exception as exc:
                logger.error(f"[Sync] 获取 GID {gid} 状态失败: {exc}")
                await _update_task(
                    task_id,
                    {
                        "status": "error",
                        "error": str(exc),
                        "error_display": "后端错误",
                    }
                )
                await _handle_task_stop_or_error_sync(task_id, "后端错误")
                await broadcast_task_update_to_subscribers(state, task_id)
                return

            aria2_status = status.get("status")
            total_length = int(status.get("totalLength", 0))

            # Check size when it becomes known or grows (e.g. BT metadata -> real payload)
            if aria2_status == "active" and total_length > 0 and (task.total_length or 0) < total_length:
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

                for sub, user in subscriptions:
                    if user.id is None:
                        logger.warning(f"[Sync] 用户缺少 user_id，跳过订阅 sub_id={sub.id}")
                        continue
                    user_id = user.id
                    user_lock = await get_user_space_lock(state, user_id)
                    async with user_lock:
                        space_info = await get_user_space_info(user_id, user.quota)
                        # Each user's space is independent, use available directly
                        effective_available = space_info["available"]

                        required_extra = max(total_length - (sub.frozen_space or 0), 0)
                        if required_extra <= effective_available:
                            # Use optimistic locking: only update if frozen_space < total_length
                            # This allows BT followedBy to update from metadata size to real size
                            async with get_session() as db:
                                await db.execute(
                                    update(UserTaskSubscription)
                                    .where(
                                        UserTaskSubscription.id == sub.id,
                                        UserTaskSubscription.frozen_space < total_length  # Optimistic lock
                                    )
                                    .values(frozen_space=total_length)
                                )
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
            mapped = _map_status(status, task_id)
            mapped_status = mapped["status"]
            raw_error = mapped.get("error")
            error_display = mapped.get("error_display")
            completion_handler = None
            if mapped_status == "complete":
                from app.aria2.listener import _handle_task_complete

                completion_handler = _handle_task_complete

            # Handle removed tasks as external cancellations
            if mapped_status == "removed":
                mapped_status = "error"
                error_display = "外部取消（管理员/外部客户端）"
                if raw_error is None:
                    raw_error = "removed"

            # Log and normalize error display
            if mapped_status == "error":
                if raw_error:
                    logger.error(f"[Sync] 任务 {task_id} 错误: {raw_error}")
                if not error_display:
                    error_display = "后端错误"

                # Avoid overriding user-initiated cancellation
                if task.error_display == "已取消":
                    error_display = "已取消"
                    if raw_error is None:
                        raw_error = task.error

                await _handle_task_stop_or_error_sync(task_id, error_display)

            # Handle magnet link metadata completion
            if mapped_status == "complete":
                followed_by = status.get("followedBy", [])
                if followed_by:
                    new_gid = followed_by[0]
                    logger.info(f"[Sync] 磁力链接元数据完成，更新 GID: {task.gid} -> {new_gid}")
                    await _update_task(task_id, {"gid": new_gid})
                    if gid != new_gid:
                        try:
                            await client.remove_download_result(gid)
                        except Exception as exc:
                            logger.debug(f"[Sync] 清理 metadata 任务结果失败 gid={gid} error={exc}")
                    return

            # Track peak values using SQL CASE for atomic conditional update
            current_speed = mapped["download_speed"]
            current_connections = int(status.get("connections", 0))

            async with get_session() as db:
                update_values = dict(
                    status=mapped_status,
                    name=mapped["name"],
                    total_length=mapped["total_length"],
                    completed_length=mapped["completed_length"],
                    download_speed=mapped["download_speed"],
                    upload_speed=mapped["upload_speed"],
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

                if mapped_status == "error":
                    update_values["error"] = raw_error
                    update_values["error_display"] = error_display or "后端错误"

                await db.execute(
                    update(DownloadTask)
                    .where(DownloadTask.id == task_id)
                    .values(**update_values)
                )

            if completion_handler is not None:
                await completion_handler(state, task_id, status)

            # Broadcast update
            await broadcast_task_update_to_subscribers(state, task_id)

        # Process all tasks concurrently
        await asyncio.gather(*[fetch_and_update(task) for task in tasks])

        await _cleanup_orphan_aria2_tasks(
            client=client,
            tracked_gids=tracked_gids,
            orphan_seen_at=orphan_seen_at,
            grace_seconds=ORPHAN_GRACE_SECONDS,
            max_actions=ORPHAN_CLEANUP_BATCH,
        )

        await asyncio.sleep(interval)


async def _cleanup_orphan_aria2_tasks(
    client: Aria2Client,
    tracked_gids: set[str],
    orphan_seen_at: dict[str, float],
    grace_seconds: float,
    max_actions: int,
) -> None:
    """清理 aria2 中无主任务（数据库不存在的 GID）。"""
    try:
        active = await client.tell_active()
        waiting = await client.tell_waiting(0, 1000)
        stopped = await client.tell_stopped(0, 1000)
    except Exception as exc:
        logger.warning(f"[Sync] 获取 aria2 全量任务失败，跳过孤立任务清理: {exc}")
        return

    now = time.monotonic()

    def _extract_gids(rows: list[dict]) -> list[str]:
        gids: list[str] = []
        for row in rows:
            gid = row.get("gid")
            if isinstance(gid, str) and gid:
                gids.append(gid)
        return gids

    active_gids = _extract_gids(active)
    waiting_gids = _extract_gids(waiting)
    stopped_gids = _extract_gids(stopped)

    current_orphan_gids = {
        gid
        for gid in [*active_gids, *waiting_gids, *stopped_gids]
        if gid not in tracked_gids
    }

    for gid in list(orphan_seen_at.keys()):
        if gid not in current_orphan_gids:
            orphan_seen_at.pop(gid, None)

    for gid in current_orphan_gids:
        if gid not in orphan_seen_at:
            orphan_seen_at[gid] = now
            logger.warning(f"[Sync] 发现 aria2 无主任务 gid={gid}")

    actions = 0

    # Stopped orphan tasks: safe to cleanup immediately.
    for gid in stopped_gids:
        if gid in tracked_gids:
            continue
        if actions >= max_actions:
            break
        try:
            await client.remove_download_result(gid)
            orphan_seen_at.pop(gid, None)
            actions += 1
            logger.info(f"[Sync] 已清理无主 stopped 任务 gid={gid}")
        except Exception as exc:
            logger.debug(f"[Sync] 清理无主 stopped 任务失败 gid={gid} error={exc}")

    # Active/waiting orphan tasks: cleanup only after grace period.
    for gid in [*active_gids, *waiting_gids]:
        if gid in tracked_gids:
            continue
        if actions >= max_actions:
            break

        first_seen = orphan_seen_at.get(gid, now)
        if (now - first_seen) < grace_seconds:
            continue

        try:
            await client.force_remove(gid)
        except Exception as exc:
            logger.debug(f"[Sync] force_remove 无主任务失败 gid={gid} error={exc}")

        try:
            await client.remove_download_result(gid)
        except Exception as exc:
            logger.debug(f"[Sync] remove_download_result 无主任务失败 gid={gid} error={exc}")

        orphan_seen_at.pop(gid, None)
        actions += 1
        logger.warning(f"[Sync] 已清理无主运行任务 gid={gid}")


async def _repair_inconsistent_completed_tasks(state: AppState) -> None:
    """修复完成状态但未落库为用户文件/历史的脏任务。"""
    from app.routers.tasks import broadcast_task_update_to_subscribers

    async with get_session() as db:
        result = await db.exec(
            select(DownloadTask).where(
                DownloadTask.status == "complete",
                DownloadTask.stored_file_id.is_(None),
            )
        )
        tasks = result.all()

    if not tasks:
        return

    logger.warning(f"[Sync] 检测到 {len(tasks)} 个完成但未落库任务，开始修复")
    now = datetime.now(timezone.utc)
    for task in tasks:
        task_id = task.id
        if task_id is None:
            continue

        # Grace period: avoid racing with in-flight completion handler.
        try:
            updated_at = datetime.fromisoformat(task.updated_at)
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=timezone.utc)
            age_seconds = (now - updated_at).total_seconds()
            if age_seconds < COMPLETE_REPAIR_GRACE_SECONDS:
                logger.debug(
                    f"[Sync] 跳过最近完成任务修复 task_id={task_id} age={age_seconds:.1f}s"
                )
                continue
        except (ValueError, TypeError) as e:
            # Fallback to repair when timestamp parsing fails.
            logger.warning("Failed to parse timestamp for task_id=%s: %s", task_id, e)

        reason = "下载完成但文件未入库"
        async with get_session() as db:
            result = await db.execute(
                update(DownloadTask)
                .where(
                    DownloadTask.id == task_id,
                    DownloadTask.stored_file_id.is_(None),
                )
                .values(
                    status="error",
                    error=reason,
                    error_display=reason,
                    updated_at=utc_now_str(),
                )
            )
            if result.rowcount == 0:
                logger.info(f"[Sync] 任务 {task_id} 已被 listener 处理，跳过修复")
                continue
        await _handle_task_stop_or_error_sync(task_id, reason)
        await broadcast_task_update_to_subscribers(state, task_id)


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
    task_id = task.id
    if task_id is None:
        logger.warning(f"[Sync] 取消任务缺少 task_id，跳过 gid={gid}")
        return
    if not gid:
        logger.warning(f"[Sync] 取消任务缺少 gid，task_id={task_id}")
        return

    # Stop aria2 task
    try:
        await client.force_remove(gid)
    except Exception as exc:
        logger.debug(f"[Sync] force_remove 失败 task_id={task_id} gid={gid} error={exc}")
    try:
        await client.remove_download_result(gid)
    except Exception as exc:
        logger.debug(f"[Sync] remove_download_result 失败 task_id={task_id} gid={gid} error={exc}")

    # Update task status
    async with get_session() as db:
        result = await db.exec(select(DownloadTask).where(DownloadTask.id == task_id))
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
                UserTaskSubscription.task_id == task_id,
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
    await cleanup_task_download_dir(task_id)

    # Broadcast update
    await broadcast_task_update_to_subscribers(state, task_id)


async def _handle_task_stop_or_error_sync(
    task_id: int,
    error_display: str | None,
) -> None:
    """同步路径处理任务停止/错误：释放冻结空间、标记订阅失败、写入历史记录。"""
    from app.services.history import add_task_history

    message = error_display or "后端错误"

    async with get_session() as db:
        result = await db.exec(select(DownloadTask).where(DownloadTask.id == task_id))
        task = result.first()

    task_name = (task.name or "未知任务") if task else "未知任务"
    task_uri = task.uri if task else None
    task_total_length = task.total_length if task else 0

    async with get_session() as db:
        result = await db.exec(
            select(UserTaskSubscription).where(
                UserTaskSubscription.task_id == task_id,
                UserTaskSubscription.status == "pending",
            )
        )
        subscriptions = result.all()

        for sub in subscriptions:
            await db.execute(
                update(UserTaskSubscription)
                .where(
                    UserTaskSubscription.id == sub.id,
                    UserTaskSubscription.status == "pending",
                )
                .values(
                    status="failed",
                    error_display=message,
                    frozen_space=0,
                )
            )

    for sub in subscriptions:
        await add_task_history(
            owner_id=sub.owner_id,
            task_name=task_name,
            result="failed",
            reason=message,
            uri=task_uri,
            total_length=task_total_length,
            created_at=sub.created_at,
        )

    if subscriptions:
        logger.info(f"[Sync] 任务 {task_id} 错误/停止，释放了 {len(subscriptions)} 个订阅的冻结空间")


# WebSocket helpers

async def register_ws(state: AppState, user_id: int, ws) -> None:
    async with state.lock:
        state.ws_connections.setdefault(user_id, set()).add(ws)


async def unregister_ws(state: AppState, user_id: int, ws) -> None:
    async with state.lock:
        sockets = state.ws_connections.get(user_id)
        if sockets:
            sockets.discard(ws)


async def broadcast_notification(state: AppState, user_id: int, message: str, level: str = "info"):
    async with state.lock:
        sockets = list(state.ws_connections.get(user_id, set()))
    
    notification = {"type": "notification", "message": message, "level": level}
    failed_sockets = []
    
    for ws in sockets:
        try:
            await ws.send_json(notification)
        except Exception as exc:
            logger.debug(f"[Sync] 通知发送失败 user_id={user_id} error={exc}")
            failed_sockets.append(ws)
    
    if failed_sockets:
        async with state.lock:
            user_sockets = state.ws_connections.get(user_id)
            if user_sockets:
                for ws in failed_sockets:
                    user_sockets.discard(ws)
