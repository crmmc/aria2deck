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

from app.aria2.failed_task_cleanup import cleanup_failed_task_artifacts

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
COMPLETE_SOURCE_RETRY_COUNT = 5
COMPLETE_SOURCE_RETRY_INTERVAL = 1.0


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


def _list_task_dir_entries(task_dir: Path) -> list[Path]:
    """列出任务目录内的真实载荷条目，忽略 aria2 控制文件。"""
    if not task_dir.exists() or not task_dir.is_dir():
        return []
    try:
        return [p for p in task_dir.iterdir() if not p.name.endswith(".aria2")]
    except OSError as e:
        logger.error(f"Failed to list task directory {task_dir}: {e}")
        return []


def _resolve_complete_source_path(
    task_dir: Path,
    files: list[dict],
    task_name: str | None,
) -> Path | None:
    """从 aria2 files 列表 + task 目录推断应入库的源路径。"""
    task_candidates: list[Path] = []
    external_candidates: list[Path] = []

    for file_item in files:
        if not isinstance(file_item, dict):
            continue
        raw_path = file_item.get("path")
        if not raw_path or not isinstance(raw_path, str):
            continue

        file_path = Path(raw_path)
        try:
            rel_path = file_path.relative_to(task_dir)
            if rel_path.parts:
                task_candidates.append(task_dir / rel_path.parts[0])
            else:
                task_candidates.append(task_dir)
            continue
        except (OSError, ValueError) as e:
            logger.debug(f"Failed to resolve path {file_path} relative to {task_dir}: {e}")
            pass

        external_candidates.append(file_path)

    existing_task_candidates: list[Path] = []
    seen: set[Path] = set()
    for candidate in task_candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if candidate.exists():
            existing_task_candidates.append(candidate)

    # BT 根目录包含多个顶层条目时，移动整个 task 目录，避免丢文件。
    if len(existing_task_candidates) > 1 and task_dir.exists():
        return task_dir
    if len(existing_task_candidates) == 1:
        return existing_task_candidates[0]

    task_entries = _list_task_dir_entries(task_dir)
    if len(task_entries) > 1:
        return task_dir
    if len(task_entries) == 1:
        return task_entries[0]

    for candidate in external_candidates:
        if candidate.exists():
            return candidate

    if task_name and task_dir.exists():
        named_candidate = task_dir / task_name
        if named_candidate.exists():
            return named_candidate

    return None


async def _resolve_complete_source_with_retry(
    completion_gid: str | None,
    task_dir: Path,
    files: list[dict],
    task_name: str | None,
    state: "AppState | None" = None,
) -> Path | None:
    """在完成事件短时间路径抖动时进行重试，降低误判失败概率。"""
    from app.core.state import get_aria2_client

    latest_files = files
    client = get_aria2_client(state=state)

    for attempt in range(COMPLETE_SOURCE_RETRY_COUNT):
        source = _resolve_complete_source_path(task_dir, latest_files, task_name)
        if source:
            return source

        if attempt < COMPLETE_SOURCE_RETRY_COUNT - 1:
            await asyncio.sleep(COMPLETE_SOURCE_RETRY_INTERVAL)

        if not completion_gid:
            continue

        try:
            refreshed_status = await client.tell_status(completion_gid)
            refreshed_files = refreshed_status.get("files", [])
            if isinstance(refreshed_files, list):
                latest_files = refreshed_files
        except Exception as exc:
            logger.debug(f"[WS] 刷新完成任务状态失败 gid={completion_gid} error={exc}")

    return None


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

    client = get_aria2_client(state=state)

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
    if task_id is None:
        logger.warning(f"[WS] 任务缺少 task_id，忽略事件 gid={gid} event={event}")
        return

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
                if user.id is None:
                    logger.warning(f"[WS] 用户缺少 user_id，跳过订阅 sub_id={sub.id}")
                    continue
                user_id = user.id
                user_lock = await get_user_space_lock(state, user_id)
                async with user_lock:
                    space_info = await get_user_space_info(user_id, user.quota)
                    effective_available = space_info["available"]

                    required_extra = max(total_length - (sub.frozen_space or 0), 0)
                    if required_extra <= effective_available:
                        MAX_RETRIES = 3
                        update_success = False
                        
                        for attempt in range(MAX_RETRIES):
                            should_retry = False
                            async with get_session() as db:
                                result = await db.exec(
                                    select(UserTaskSubscription).where(
                                        UserTaskSubscription.id == sub.id
                                    )
                                )
                                current_sub = result.first()
                                
                                if not current_sub:
                                    logger.warning(
                                        f"[WS] 订阅 {sub.id} 在重试时已被删除"
                                    )
                                    break
                                
                                if current_sub.frozen_space >= total_length:
                                    update_success = True
                                    break
                                
                                update_result = await db.execute(
                                    update(UserTaskSubscription)
                                    .where(
                                        UserTaskSubscription.id == sub.id,
                                        UserTaskSubscription.frozen_space == current_sub.frozen_space,
                                    )
                                    .values(frozen_space=total_length)
                                )
                                
                                if update_result.rowcount > 0:
                                    update_success = True
                                    break
                                
                                if attempt < MAX_RETRIES - 1:
                                    should_retry = True
                            
                            if should_retry:
                                backoff_ms = 10 * (attempt + 1)
                                logger.debug(
                                    f"[WS] 订阅 {sub.id} frozen_space 更新冲突，"
                                    f"重试 {attempt + 1}/{MAX_RETRIES}，等待 {backoff_ms}ms"
                                )
                                await asyncio.sleep(backoff_ms / 1000.0)
                        
                        if not update_success:
                            logger.warning(
                                f"[WS] 订阅 {sub.id} frozen_space 更新失败，"
                                f"已重试 {MAX_RETRIES} 次"
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
            if gid != new_gid:
                try:
                    await client.remove_download_result(gid)
                except Exception as exc:
                    logger.debug(f"[WS] 清理 metadata 任务结果失败 gid={gid} error={exc}")
            return
        else:
            new_status = None
    elif event == "bt_complete":
        new_status = None
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
            if new_status is not None:
                db_task.status = new_status
            db_task.updated_at = utc_now_str()

            if gid_updated:
                db_task.gid = gid
            if error_msg:
                db_task.error = error_msg
            if error_display:
                if event == "stop" and db_task.error_display == "已取消":
                    pass
                else:
                    db_task.error_display = error_display

            if event in ("stop", "error"):
                db_task.gid = None

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

    if event in ("complete", "bt_complete"):
        await _handle_task_complete(state, task_id, aria2_status)

    # 5.1 处理 stop/error 事件 - 释放冻结空间并标记订阅失败
    if event in ("stop", "error"):
        await _handle_task_stop_or_error(task_id, error_display)
        await cleanup_failed_task_artifacts(
            client=client,
            task_id=task_id,
            gid=gid,
            log_prefix="[WS]",
        )

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
    from app.core.state import get_aria2_client, get_task_complete_lock
    from app.services.history import add_task_history
    from app.services.storage import (
        cleanup_task_download_dir,
        get_downloading_dir,
        move_to_store,
    )

    lock = await get_task_complete_lock(state, task_id)
    async with lock:
        async with get_session() as db:
            result = await db.exec(select(DownloadTask).where(DownloadTask.id == task_id))
            task = result.first()

        if not task:
            return

        aria2_status_value = aria2_status.get("status")
        should_process_complete = (
            task.status == "complete"
            or aria2_status_value == "complete"
        )
        if not should_process_complete:
            logger.debug(
                "[WS] Skip complete handler for task %s: task.status=%s aria2.status=%s",
                task_id,
                task.status,
                aria2_status_value,
            )
            return

        if task.stored_file_id is not None:
            logger.debug(f"[WS] Task {task_id} already processed (stored_file_id={task.stored_file_id}), skipping")
            return

        completion_gid = task.gid

        files = aria2_status.get("files", [])
        if not isinstance(files, list):
            files = []
        task_dir = get_downloading_dir() / str(task_id)
        source_path = await _resolve_complete_source_with_retry(
            completion_gid=completion_gid,
            task_dir=task_dir,
            files=files,
            task_name=task.name,
            state=state,
        )
        if source_path is None:
            logger.error(
                f"[WS] 任务 {task_id} 完成但无法定位源文件 task_dir={task_dir} "
                f"files_count={len(files)} gid={completion_gid}"
            )
            return

        original_name = task.name or source_path.name

        try:
            stored_file = await move_to_store(source_path, original_name)
            if stored_file.id is None:
                logger.error(f"[WS] StoredFile 缺少 id，task_id={task_id}")
                return

            async with get_session() as db:
                await db.execute(
                    update(DownloadTask)
                    .where(
                        DownloadTask.id == task_id,
                        DownloadTask.stored_file_id.is_(None)
                    )
                    .values(
                        status="complete",
                        stored_file_id=stored_file.id,
                        completed_at=utc_now_str()
                    )
                )

            async with get_session() as db:
                verify_result = await db.exec(select(DownloadTask).where(DownloadTask.id == task_id))
                verify_task = verify_result.first()
                if not verify_task or verify_task.stored_file_id != stored_file.id:
                    logger.info(f"[WS] Task {task_id} already processed by another handler")
                    await cleanup_task_download_dir(task_id)
                    return

            async with get_session() as db:
                result = await db.exec(
                    select(UserTaskSubscription).where(
                        UserTaskSubscription.task_id == task_id,
                        UserTaskSubscription.status == "pending",
                    )
                )
                subscriptions = result.all()

            for sub in subscriptions:
                should_record_history = False
                MAX_RETRIES = 3
                user_file_created = False

                for attempt in range(MAX_RETRIES):
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

                            sub_result = await db.exec(
                                select(UserTaskSubscription).where(UserTaskSubscription.id == sub.id)
                            )
                            current_sub = sub_result.first()
                            if not current_sub or current_sub.status != "success":
                                break

                            result = await db.exec(
                                select(UserFile).where(
                                    UserFile.owner_id == sub.owner_id,
                                    UserFile.stored_file_id == stored_file.id,
                                )
                            )
                            existing_ref = result.first()

                            if not existing_ref:
                                user_file = UserFile(
                                    owner_id=sub.owner_id,
                                    stored_file_id=stored_file.id,
                                    display_name=original_name,
                                    created_at=utc_now_str(),
                                )
                                db.add(user_file)

                                await db.execute(
                                    update(StoredFile)
                                    .where(StoredFile.id == stored_file.id)
                                    .values(ref_count=StoredFile.ref_count + 1)
                                )
                            user_file_created = True
                            should_record_history = True
                            break
                    except Exception as e:
                        logger.warning(
                            f"[WS] UserFile creation attempt {attempt + 1}/{MAX_RETRIES} failed for sub {sub.id}: {e}"
                        )
                        if attempt < MAX_RETRIES - 1:
                            await asyncio.sleep(0.01 * (attempt + 1))

                if not user_file_created:
                    logger.error(
                        f"[WS] Failed to create UserFile after {MAX_RETRIES} attempts for sub_id={sub.id}"
                    )
                    try:
                        async with get_session() as db:
                            await db.execute(
                                update(UserTaskSubscription)
                                .where(UserTaskSubscription.id == sub.id)
                                .values(status="failed", error_display="文件引用创建失败")
                            )
                    except Exception as final_err:
                        logger.error(
                            f"[WS] Failed to mark subscription {sub.id} as failed: {final_err}"
                        )
                    continue

                if not should_record_history:
                    continue

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
            return

        await cleanup_task_download_dir(task_id)

        if completion_gid:
            try:
                client = get_aria2_client(state=state)
                await client.remove_download_result(completion_gid)
            except Exception as exc:
                logger.debug(f"[WS] 清理完成任务记录失败 gid={completion_gid} error={exc}")

            async with get_session() as db:
                await db.execute(
                    update(DownloadTask)
                    .where(
                        DownloadTask.id == task_id,
                        DownloadTask.gid == completion_gid,
                    )
                    .values(
                        gid=None,
                        updated_at=utc_now_str(),
                    )
                )


async def _handle_task_stop_or_error(
    task_id: int,
    error_display: str | None,
) -> None:
    """处理任务停止或错误事件

    释放所有订阅者的冻结空间并标记订阅为失败，写入历史记录。
    """
    from sqlalchemy import update
    from sqlmodel import select

    from app.database import get_session
    from app.models import DownloadTask, UserTaskSubscription
    from app.services.history import add_task_history

    message = error_display or "后端错误"

    # 获取任务信息（用于历史记录）
    async with get_session() as db:
        result = await db.exec(select(DownloadTask).where(DownloadTask.id == task_id))
        task = result.first()

    task_name = (task.name or "未知任务") if task else "未知任务"
    task_uri = task.uri if task else None
    task_total_length = task.total_length if task else 0

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

    # 写入历史记录
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
    except Exception as exc:
        logger.debug(f"[WS] force_remove 失败 task_id={task.id} gid={gid} error={exc}")
    try:
        await client.remove_download_result(gid)
    except Exception as exc:
        logger.debug(f"[WS] remove_download_result 失败 task_id={task.id} gid={gid} error={exc}")

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
            timeout = aiohttp.ClientTimeout(connect=10, sock_connect=10, sock_read=None)
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
