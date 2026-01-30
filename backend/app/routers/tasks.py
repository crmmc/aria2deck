"""任务管理接口模块（共享下载架构）

提供任务的添加、查询、取消等功能。
实现共享下载：多用户可订阅同一下载任务。
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import shutil
import socket
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import func, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from app.aria2.client import Aria2Client
from app.auth import require_user
from app.core.config import settings
from app.core.rate_limit import api_limiter
from app.core.security import mask_url_credentials
from app.core.state import AppState, get_aria2_client, get_user_space_lock
from app.database import get_session
from app.models import (
    DownloadTask,
    User,
    UserFile,
    UserTaskSubscription,
    utc_now_str,
)
from app.routers.config import get_max_task_size, get_min_free_disk
from app.services.hash import (
    extract_info_hash_from_magnet,
    extract_info_hash_from_torrent_base64,
    get_uri_hash,
    is_http_url,
    is_magnet_link,
    is_torrent_task,
)
from app.services.http_probe import probe_url_with_get_fallback
from app.services.storage import (
    get_task_download_dir,
    get_user_space_info,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/tasks", tags=["tasks"])

# Minimum space required for magnet links (1MB)
MAGNET_MIN_SPACE = 1 * 1024 * 1024


# ========== SSRF 防护 ==========

def _is_private_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """检查 IP 是否为私有/内网地址"""
    return (
        ip.is_private or
        ip.is_loopback or
        ip.is_link_local or
        ip.is_reserved or
        ip.is_multicast
    )


def _check_url_safety(url: str) -> None:
    """检查 URL 是否安全（SSRF 防护）"""
    try:
        parsed = urlparse(url)
        scheme = parsed.scheme.lower()
        hostname = parsed.hostname

        if scheme not in ('http', 'https', 'ftp'):
            return

        if not hostname:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="无效的下载链接"
            )

        blocked_hosts = {
            'localhost', 'localhost.localdomain',
            '127.0.0.1', '::1', '0.0.0.0', '::'
        }
        if hostname.lower() in blocked_hosts:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="不允许下载本机地址"
            )

        try:
            ip = ipaddress.ip_address(hostname)
            if _is_private_ip(ip):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="不允许下载内网地址"
                )
            return
        except ValueError:
            pass

        try:
            addr_infos = socket.getaddrinfo(hostname, None)
            for addr_info in addr_infos:
                ip_str = addr_info[4][0]
                try:
                    ip = ipaddress.ip_address(ip_str)
                    if _is_private_ip(ip):
                        raise HTTPException(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"域名 {hostname} 解析到内网地址，禁止下载"
                        )
                except ValueError:
                    continue
        except socket.gaierror:
            pass

    except HTTPException:
        raise
    except Exception:
        pass


# ========== Schemas ==========

class TaskCreate(BaseModel):
    """创建任务请求体"""
    uri: str
    options: dict | None = None


class TorrentCreate(BaseModel):
    """上传种子请求体"""
    torrent: str  # Base64 encoded
    options: dict | None = None


# ========== Helpers ==========

def _get_state(request: Request) -> AppState:
    return request.app.state.app_state


def _get_client(request: Request) -> Aria2Client:
    return get_aria2_client(request)


async def _get_task_submit_lock(state: AppState, task_id: int) -> asyncio.Lock:
    """获取任务提交锁，避免并发提交同一任务"""
    async with state.lock:
        lock = state.task_submit_locks.get(task_id)
        if lock is None:
            lock = asyncio.Lock()
            state.task_submit_locks[task_id] = lock
        return lock


def _check_disk_space() -> tuple[bool, int]:
    """检查磁盘空间是否足够"""
    download_path = Path(settings.download_dir)
    download_path.mkdir(parents=True, exist_ok=True)
    disk = shutil.disk_usage(download_path)
    min_free = get_min_free_disk()
    return disk.free > min_free, disk.free


def _get_display_name(task: DownloadTask) -> str:
    """获取任务显示名称"""
    # 如果有有效的 name 且不是 [METADATA]，直接使用
    if task.name and not task.name.startswith("[METADATA]"):
        return task.name
    # 如果是磁力链接，提取 info_hash 并返回完整格式
    if task.uri and task.uri.startswith("magnet:"):
        import re
        match = re.search(r'xt=urn:btih:([a-fA-F0-9]{40}|[a-zA-Z2-7]{32})', task.uri)
        if match:
            return f"magnet:?xt=urn:btih:{match.group(1)}"
    # 其他情况返回 name 或默认值
    return task.name or "未知文件"


def _subscription_to_dict(
    subscription: UserTaskSubscription,
    task: DownloadTask,
) -> dict:
    """Convert subscription + task to API response dict"""
    # Determine effective status for user
    if subscription.status == "failed":
        effective_status = "error"
        error = subscription.error_display
    elif subscription.status == "success":
        effective_status = "complete"
        error = None
    else:
        effective_status = task.status
        error = task.error_display or task.error

    return {
        "id": subscription.id,
        "name": _get_display_name(task),
        "uri": task.uri,
        "status": effective_status,
        "total_length": task.total_length,
        "completed_length": task.completed_length,
        "download_speed": task.download_speed,
        "upload_speed": task.upload_speed,
        "frozen_space": subscription.frozen_space,
        "error": error,
        "created_at": subscription.created_at,
    }


async def _get_subscription(subscription_id: int, owner_id: int) -> tuple[UserTaskSubscription, DownloadTask] | None:
    """Get subscription and its associated task"""
    async with get_session() as db:
        result = await db.exec(
            select(UserTaskSubscription, DownloadTask)
            .join(DownloadTask, UserTaskSubscription.task_id == DownloadTask.id)
            .where(
                UserTaskSubscription.id == subscription_id,
                UserTaskSubscription.owner_id == owner_id,
            )
        )
        row = result.first()
        return row if row else None


async def _create_subscription(
    user: User,
    task: DownloadTask,
    frozen_space: int,
) -> UserTaskSubscription:
    """Create a subscription for a user to a task

    Handles concurrent creation by catching IntegrityError.
    """
    async with get_session() as db:
        subscription = UserTaskSubscription(
            owner_id=user.id,
            task_id=task.id,
            frozen_space=frozen_space,
            status="pending",
            created_at=utc_now_str(),
        )
        db.add(subscription)

        try:
            await db.commit()
            await db.refresh(subscription)
            return subscription
        except IntegrityError:
            await db.rollback()
            # Concurrent creation, re-query existing subscription
            result = await db.exec(
                select(UserTaskSubscription).where(
                    UserTaskSubscription.owner_id == user.id,
                    UserTaskSubscription.task_id == task.id,
                )
            )
            existing = result.first()
            if existing:
                return existing
            raise  # Should not happen, but re-raise for safety


async def _find_or_create_task(
    uri_hash: str,
    uri: str,
    name: str | None = None,
    total_length: int = 0,
) -> tuple[DownloadTask, bool]:
    """Find existing task or create new one.

    Handles race condition by catching IntegrityError on duplicate uri_hash.

    Returns:
        Tuple of (task, is_new)
    """
    async with get_session() as db:
        # Check for existing task
        result = await db.exec(
            select(DownloadTask).where(DownloadTask.uri_hash == uri_hash)
        )
        existing = result.first()

        if existing:
            return existing, False

        # Create new task
        task = DownloadTask(
            uri_hash=uri_hash,
            uri=uri,
            name=name,
            total_length=total_length,
            status="queued",
            created_at=utc_now_str(),
            updated_at=utc_now_str(),
        )
        db.add(task)

        try:
            await db.commit()
            await db.refresh(task)
            return task, True
        except IntegrityError:
            # Race condition: another process created the task
            await db.rollback()
            logger.info(f"Race condition on task creation: {uri_hash}, fetching existing")

            # Re-fetch the existing task
            result = await db.exec(
                select(DownloadTask).where(DownloadTask.uri_hash == uri_hash)
            )
            existing = result.first()
            if existing:
                return existing, False

            # Should not happen, but handle gracefully
            raise RuntimeError(f"Failed to create or find task: {uri_hash}")


# ========== API Endpoints ==========

@router.post("", status_code=status.HTTP_201_CREATED)
async def create_task(
    payload: TaskCreate,
    request: Request,
    user: User = Depends(require_user),
) -> dict:
    """创建新下载任务

    支持：
    - HTTP(S) URL：预检查获取大小后创建
    - 磁力链接：可用空间 > 1MB 时允许添加

    返回用户的订阅信息。
    """
    # Rate limit
    if not api_limiter.is_allowed(user.id, "create_task", limit=30, window_seconds=60):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="操作过于频繁，请稍后再试"
        )

    # SSRF protection
    _check_url_safety(payload.uri)

    # Check disk space
    disk_ok, disk_free = _check_disk_space()
    if not disk_ok:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"磁盘空间不足，剩余 {disk_free / 1024 / 1024 / 1024:.2f} GB"
        )

    # Get user space info
    space_info = await get_user_space_info(user.id, user.quota)
    available_space = space_info["available"]

    # Determine URI type and get uri_hash
    uri = payload.uri
    masked_uri = mask_url_credentials(uri)
    uri_hash: str | None = None
    name: str | None = None
    total_length: int = 0
    frozen_space: int = 0

    if is_magnet_link(uri):
        # Magnet link: extract info_hash
        uri_hash = extract_info_hash_from_magnet(uri)
        if not uri_hash:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="无效的磁力链接"
            )

        # Check minimum space for magnet
        if available_space < MAGNET_MIN_SPACE:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="可用空间不足，无法添加磁力链接"
            )

        # Magnet links don't freeze space until size is known
        frozen_space = 0

    elif is_http_url(uri):
        # HTTP(S): probe to get size and final URL
        probe_result = await probe_url_with_get_fallback(uri)

        if not probe_result.success:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"无法访问下载链接: {probe_result.error}"
            )

        # Use final URL for hash (after redirects)
        final_url = probe_result.final_url or uri
        uri_hash = get_uri_hash(final_url)
        name = probe_result.filename
        total_length = probe_result.content_length or 0

        # Check size limits
        if total_length > 0:
            max_task_size = get_max_task_size()
            if total_length > max_task_size:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"文件大小 {total_length / 1024**3:.2f} GB 超过系统限制 {max_task_size / 1024**3:.2f} GB"
                )

            if total_length > available_space:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"文件大小 {total_length / 1024**3:.2f} GB 超过可用空间 {available_space / 1024**3:.2f} GB"
                )

            frozen_space = total_length

    else:
        # Other protocols (ftp, etc.)
        uri_hash = get_uri_hash(uri)

    if not uri_hash:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="无法识别的下载链接类型"
        )

    # Find or create task
    task, is_new = await _find_or_create_task(
        uri_hash=uri_hash,
        uri=masked_uri,
        name=name,
        total_length=total_length,
    )

    # Check if user already subscribed
    async with get_session() as db:
        result = await db.exec(
            select(UserTaskSubscription).where(
                UserTaskSubscription.owner_id == user.id,
                UserTaskSubscription.task_id == task.id,
            )
        )
        existing_sub = result.first()

        if existing_sub:
            # Already subscribed - convert to dict inside session
            if existing_sub.status == "success":
                # Already completed, check if user has the file
                result = await db.exec(
                    select(UserFile).where(
                        UserFile.owner_id == user.id,
                        UserFile.stored_file_id == task.stored_file_id,
                    )
                )
                user_file = result.first()

                if user_file:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="您已拥有此文件"
                    )

            return _subscription_to_dict(existing_sub, task)

    # Handle based on task status
    if task.status == "complete" and task.stored_file_id:
        # Task already complete, create file reference directly
        from app.services.storage import create_user_file_reference

        user_file = await create_user_file_reference(
            user_id=user.id,
            stored_file_id=task.stored_file_id,
        )

        # Create subscription marked as success (use _create_subscription to handle race)
        # First try to create, if race condition occurs, _create_subscription handles it
        async with get_session() as db:
            subscription = UserTaskSubscription(
                owner_id=user.id,
                task_id=task.id,
                frozen_space=0,
                status="success",
                created_at=utc_now_str(),
            )
            db.add(subscription)

            try:
                await db.commit()
                await db.refresh(subscription)
            except IntegrityError:
                # Race condition: subscription already exists
                await db.rollback()
                result = await db.exec(
                    select(UserTaskSubscription).where(
                        UserTaskSubscription.owner_id == user.id,
                        UserTaskSubscription.task_id == task.id,
                    )
                )
                subscription = result.first()
                if not subscription:
                    raise  # Should not happen

        return _subscription_to_dict(subscription, task)

    elif task.status == "error":
        # Task failed, allow retry by creating new subscription
        # Reset task status if this is the only subscriber
        async with get_session() as db:
            result = await db.exec(
                select(UserTaskSubscription).where(
                    UserTaskSubscription.task_id == task.id,
                    UserTaskSubscription.status == "pending",
                )
            )
            pending_subs = result.all()

        if not pending_subs:
            # No pending subscribers, reset task for retry
            async with get_session() as db:
                result = await db.exec(
                    select(DownloadTask).where(DownloadTask.id == task.id)
                )
                db_task = result.first()
                if db_task:
                    db_task.status = "queued"
                    db_task.error = None
                    db_task.error_display = None
                    db_task.gid = None
                    db_task.updated_at = utc_now_str()
                    db.add(db_task)

            # Refresh task
            async with get_session() as db:
                result = await db.exec(
                    select(DownloadTask).where(DownloadTask.id == task.id)
                )
                task = result.first()

            is_new = True  # Treat as new for aria2 submission

    # Create subscription (protect space check for known-size downloads)
    if frozen_space > 0:
        state = _get_state(request)
        user_lock = await get_user_space_lock(state, user.id)
        async with user_lock:
            space_info = await get_user_space_info(user.id, user.quota)
            if frozen_space > space_info["available"]:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=(
                        f"文件大小 {total_length / 1024**3:.2f} GB 超过可用空间 "
                        f"{space_info['available'] / 1024**3:.2f} GB"
                    )
                )
            subscription = await _create_subscription(user, task, frozen_space)
    else:
        subscription = await _create_subscription(user, task, frozen_space)

    # If new task, submit to aria2
    if is_new:
        state = _get_state(request)
        client = _get_client(request)

        # Get task download directory
        task_dir = get_task_download_dir(task.id)
        options = dict(payload.options) if payload.options else {}
        options["dir"] = str(task_dir)

        async def _do_add():
            lock = await _get_task_submit_lock(state, task.id)
            async with lock:
                # Re-check task and subscriptions before submitting to aria2
                async with get_session() as db:
                    result = await db.exec(
                        select(func.count(UserTaskSubscription.id)).where(
                            UserTaskSubscription.task_id == task.id,
                            UserTaskSubscription.status == "pending",
                        )
                    )
                    pending_count = result.one()
                    if isinstance(pending_count, tuple):
                        pending_count = pending_count[0]

                    result = await db.exec(
                        select(DownloadTask).where(DownloadTask.id == task.id)
                    )
                    db_task = result.first()
                    if not db_task:
                        return

                    if pending_count == 0:
                        # No subscribers, mark as cancelled if still queued
                        if db_task.status in ("queued", "active") and db_task.gid is None:
                            db_task.status = "error"
                            db_task.error_display = "已取消"
                            db_task.updated_at = utc_now_str()
                            db.add(db_task)
                        return

                    # Already submitted or not in queued state
                    if db_task.gid or db_task.status != "queued":
                        return

                try:
                    gid = await client.add_uri([uri], options)
                    async with get_session() as db:
                        result = await db.exec(
                            select(DownloadTask).where(DownloadTask.id == task.id)
                        )
                        db_task = result.first()
                        if db_task:
                            db_task.gid = gid
                            db_task.status = "active"
                            db_task.updated_at = utc_now_str()
                            db.add(db_task)
                except Exception as exc:
                    logger.error(f"Failed to add task to aria2: {exc}")
                    async with get_session() as db:
                        result = await db.exec(
                            select(DownloadTask).where(DownloadTask.id == task.id)
                        )
                        db_task = result.first()
                        if db_task:
                            db_task.status = "error"
                            db_task.error = str(exc)
                            db_task.error_display = "添加下载任务失败"
                            db_task.updated_at = utc_now_str()
                            db.add(db_task)

            # Broadcast update to all subscribers
            await _broadcast_task_update(state, task.id)

        asyncio.create_task(_do_add())

    return _subscription_to_dict(subscription, task)


@router.post("/torrent", status_code=status.HTTP_201_CREATED)
async def create_torrent_task(
    payload: TorrentCreate,
    request: Request,
    user: User = Depends(require_user),
) -> dict:
    """通过种子文件创建下载任务"""
    # Rate limit
    if not api_limiter.is_allowed(user.id, "create_torrent", limit=10, window_seconds=60):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="操作过于频繁，请稍后再试"
        )

    # Size limit
    max_base64_length = 14 * 1024 * 1024
    if len(payload.torrent) > max_base64_length:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="种子文件过大，最大支持 10MB"
        )

    # Check disk space
    disk_ok, disk_free = _check_disk_space()
    if not disk_ok:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"磁盘空间不足，剩余 {disk_free / 1024 / 1024 / 1024:.2f} GB"
        )

    # Extract info_hash from torrent
    uri_hash = extract_info_hash_from_torrent_base64(payload.torrent)
    if not uri_hash:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="无效的种子文件"
        )

    # Get user space info
    space_info = await get_user_space_info(user.id, user.quota)
    available_space = space_info["available"]

    # Check minimum space
    if available_space < MAGNET_MIN_SPACE:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="可用空间不足"
        )

    # Find or create task
    # 构造磁力链接供前端复制
    magnet_uri = f"magnet:?xt=urn:btih:{uri_hash}"
    task, is_new = await _find_or_create_task(
        uri_hash=uri_hash,
        uri=magnet_uri,
    )

    # Check if user already subscribed
    async with get_session() as db:
        result = await db.exec(
            select(UserTaskSubscription).where(
                UserTaskSubscription.owner_id == user.id,
                UserTaskSubscription.task_id == task.id,
            )
        )
        existing_sub = result.first()

    if existing_sub:
        return _subscription_to_dict(existing_sub, task)

    # Handle completed task
    if task.status == "complete" and task.stored_file_id:
        from app.services.storage import create_user_file_reference

        await create_user_file_reference(
            user_id=user.id,
            stored_file_id=task.stored_file_id,
        )

        # Create subscription marked as success (handle race condition)
        async with get_session() as db:
            subscription = UserTaskSubscription(
                owner_id=user.id,
                task_id=task.id,
                frozen_space=0,
                status="success",
                created_at=utc_now_str(),
            )
            db.add(subscription)

            try:
                await db.commit()
                await db.refresh(subscription)
            except IntegrityError:
                # Race condition: subscription already exists
                await db.rollback()
                result = await db.exec(
                    select(UserTaskSubscription).where(
                        UserTaskSubscription.owner_id == user.id,
                        UserTaskSubscription.task_id == task.id,
                    )
                )
                subscription = result.first()
                if not subscription:
                    raise  # Should not happen

        return _subscription_to_dict(subscription, task)

    # Create subscription (no frozen space until size known)
    subscription = await _create_subscription(user, task, frozen_space=0)

    # If new task, submit to aria2
    if is_new:
        state = _get_state(request)
        client = _get_client(request)

        task_dir = get_task_download_dir(task.id)
        options = dict(payload.options) if payload.options else {}
        options["dir"] = str(task_dir)

        async def _do_add():
            lock = await _get_task_submit_lock(state, task.id)
            async with lock:
                # Re-check task and subscriptions before submitting to aria2
                async with get_session() as db:
                    result = await db.exec(
                        select(func.count(UserTaskSubscription.id)).where(
                            UserTaskSubscription.task_id == task.id,
                            UserTaskSubscription.status == "pending",
                        )
                    )
                    pending_count = result.one()
                    if isinstance(pending_count, tuple):
                        pending_count = pending_count[0]

                    result = await db.exec(
                        select(DownloadTask).where(DownloadTask.id == task.id)
                    )
                    db_task = result.first()
                    if not db_task:
                        return

                    if pending_count == 0:
                        if db_task.status in ("queued", "active") and db_task.gid is None:
                            db_task.status = "error"
                            db_task.error_display = "已取消"
                            db_task.updated_at = utc_now_str()
                            db.add(db_task)
                        return

                    if db_task.gid or db_task.status != "queued":
                        return

                try:
                    gid = await client.add_torrent(payload.torrent, [], options)
                    async with get_session() as db:
                        result = await db.exec(
                            select(DownloadTask).where(DownloadTask.id == task.id)
                        )
                        db_task = result.first()
                        if db_task:
                            db_task.gid = gid
                            db_task.status = "active"
                            db_task.updated_at = utc_now_str()
                            db.add(db_task)
                except Exception as exc:
                    logger.error(f"Failed to add torrent to aria2: {exc}")
                    async with get_session() as db:
                        result = await db.exec(
                            select(DownloadTask).where(DownloadTask.id == task.id)
                        )
                        db_task = result.first()
                        if db_task:
                            db_task.status = "error"
                            db_task.error = str(exc)
                            db_task.error_display = "添加种子任务失败"
                            db_task.updated_at = utc_now_str()
                            db.add(db_task)

            await _broadcast_task_update(state, task.id)

        asyncio.create_task(_do_add())

    return _subscription_to_dict(subscription, task)


@router.get("")
async def list_tasks(
    status_filter: str | None = None,
    user: User = Depends(require_user),
) -> list[dict]:
    """获取当前用户的任务订阅列表"""
    async with get_session() as db:
        query = (
            select(UserTaskSubscription, DownloadTask)
            .join(DownloadTask, UserTaskSubscription.task_id == DownloadTask.id)
            .where(UserTaskSubscription.owner_id == user.id)
        )

        if status_filter:
            if status_filter == "active":
                query = query.where(
                    UserTaskSubscription.status == "pending",
                    DownloadTask.status.in_(["queued", "active"]),
                )
            elif status_filter == "current":
                # 当前任务：活跃 + 失败（不包括已完成）
                query = query.where(
                    (
                        (UserTaskSubscription.status == "pending") &
                        (DownloadTask.status.in_(["queued", "active"]))
                    ) |
                    (UserTaskSubscription.status == "failed") |
                    (
                        (UserTaskSubscription.status == "pending") &
                        (DownloadTask.status == "error")
                    )
                )
            elif status_filter == "complete":
                query = query.where(UserTaskSubscription.status == "success")
            elif status_filter == "error":
                query = query.where(
                    (UserTaskSubscription.status == "failed") |
                    (
                        (UserTaskSubscription.status == "pending") &
                        (DownloadTask.status == "error")
                    )
                )

        query = query.order_by(UserTaskSubscription.id.desc())
        result = await db.exec(query)
        rows = result.all()

    return [_subscription_to_dict(sub, task) for sub, task in rows]


@router.delete("/{subscription_id}")
async def cancel_task(
    subscription_id: int,
    request: Request,
    user: User = Depends(require_user),
) -> dict:
    """取消任务订阅

    如果是唯一订阅者，同时取消 aria2 任务。
    """
    row = await _get_subscription(subscription_id, user.id)
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="任务不存在"
        )

    subscription, task = row

    # Check if this is an active task cancellation (needs history)
    is_active_cancel = subscription.status == "pending" and task.status in ("queued", "active")

    # Atomic operation: delete subscription and count remaining pending subscribers
    async with get_session() as db:
        # Step 1: Delete current subscription
        result = await db.exec(
            select(UserTaskSubscription).where(UserTaskSubscription.id == subscription_id)
        )
        db_sub = result.first()
        if db_sub:
            await db.delete(db_sub)

        # Step 2: Count remaining pending subscribers in the SAME transaction
        result = await db.exec(
            select(func.count(UserTaskSubscription.id)).where(
                UserTaskSubscription.task_id == task.id,
                UserTaskSubscription.status == "pending",
            )
        )
        remaining_count = result.one()
        if isinstance(remaining_count, tuple):
            remaining_count = remaining_count[0]

        # Step 2.1: If no remaining subscribers, mark task as cancelled (even if gid not set)
        if remaining_count == 0:
            await db.execute(
                update(DownloadTask)
                .where(
                    DownloadTask.id == task.id,
                    DownloadTask.status.in_(["queued", "active"])
                )
                .values(
                    status="error",
                    error_display="已取消",
                    updated_at=utc_now_str()
                )
            )
        # Transaction commits here

    # Write to history if this was an active task cancellation
    if is_active_cancel:
        from app.services.history import add_task_history
        await add_task_history(
            owner_id=user.id,
            task_name=_get_display_name(task),
            result="cancelled",
            reason="用户取消",
            uri=task.uri,
            total_length=task.total_length,
            created_at=subscription.created_at,
        )

    # Step 3: Only cancel aria2 task if no remaining subscribers
    if remaining_count == 0:
        # Re-check pending subscribers to avoid cancelling a newly subscribed task
        async with get_session() as db:
            result = await db.exec(
                select(func.count(UserTaskSubscription.id)).where(
                    UserTaskSubscription.task_id == task.id,
                    UserTaskSubscription.status == "pending",
                )
            )
            still_pending = result.one()
            if isinstance(still_pending, tuple):
                still_pending = still_pending[0]

            if still_pending != 0:
                return {"ok": True}

            result = await db.exec(
                select(DownloadTask).where(DownloadTask.id == task.id)
            )
            db_task = result.first()

        if db_task and db_task.gid and db_task.status in ("queued", "active", "error"):
            client = _get_client(request)
            try:
                await client.force_remove(db_task.gid)
            except Exception:
                pass
            try:
                await client.remove_download_result(db_task.gid)
            except Exception:
                pass

            # Mark task as cancelled
            async with get_session() as db:
                result = await db.exec(
                    select(DownloadTask).where(DownloadTask.id == task.id)
                )
                db_task = result.first()
                if db_task:
                    db_task.status = "error"
                    db_task.error_display = "已取消"
                    db_task.updated_at = utc_now_str()
                    db.add(db_task)

            # Clean up download directory
            from app.services.storage import cleanup_task_download_dir
            await cleanup_task_download_dir(task.id)

    return {"ok": True}


@router.delete("")
async def clear_history(user: User = Depends(require_user)) -> dict:
    """清空当前用户的已完成/失败任务订阅"""
    async with get_session() as db:
        result = await db.exec(
            select(UserTaskSubscription).where(
                UserTaskSubscription.owner_id == user.id,
                UserTaskSubscription.status.in_(["success", "failed"]),
            )
        )
        subscriptions = result.all()

        count = len(subscriptions)
        for sub in subscriptions:
            await db.delete(sub)

    return {"ok": True, "count": count}


# ========== Broadcast Helpers ==========

async def _broadcast_task_update(state: AppState, task_id: int) -> None:
    """Broadcast task update to all subscribers

    Handles connection failures gracefully.
    """
    from app.aria2.sync import unregister_ws

    async with get_session() as db:
        # Get task
        result = await db.exec(
            select(DownloadTask).where(DownloadTask.id == task_id)
        )
        task = result.first()
        if not task:
            return

        # Get all subscribers
        result = await db.exec(
            select(UserTaskSubscription).where(
                UserTaskSubscription.task_id == task_id,
            )
        )
        subscriptions = result.all()

    # Broadcast to each subscriber
    for sub in subscriptions:
        payload = _subscription_to_dict(sub, task)

        async with state.lock:
            sockets = list(state.ws_connections.get(sub.owner_id, set()))

        failed_sockets = []
        for ws in sockets:
            try:
                await ws.send_json({"type": "task_update", "task": payload})
            except Exception as e:
                logger.debug(f"WebSocket send failed for user {sub.owner_id}: {e}")
                failed_sockets.append(ws)

        # Clean up failed connections outside the iteration
        for ws in failed_sockets:
            try:
                await unregister_ws(state, sub.owner_id, ws)
            except Exception:
                pass


async def broadcast_task_update_to_subscribers(state: AppState, task_id: int) -> None:
    """Public function to broadcast task updates (used by listener/sync)"""
    await _broadcast_task_update(state, task_id)
