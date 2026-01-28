"""任务管理接口模块

提供任务的增删查改、状态控制、文件列表、制品下载等功能。
包含容量检测与用户隔离逻辑。
"""
from __future__ import annotations

import os
import asyncio
import shutil
import ipaddress
import socket
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlmodel import select

from app.aria2.client import Aria2Client
from app.aria2.sync import broadcast_update
from app.auth import require_user
from app.core.config import settings
from app.core.rate_limit import api_limiter
from app.core.security import mask_url_credentials
from app.core.state import AppState, get_aria2_client
from app.database import get_session
from app.models import Task, User
from app.routers.config import get_min_free_disk


router = APIRouter(prefix="/api/tasks", tags=["tasks"])


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ========== SSRF 防护 ==========

def _is_private_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """检查 IP 是否为私有/内网地址"""
    return (
        ip.is_private or        # 私有网络（192.168.x.x, 10.x.x.x, 172.16-31.x.x）
        ip.is_loopback or       # 回环地址（127.0.0.1, ::1）
        ip.is_link_local or     # 链路本地地址（169.254.x.x，AWS 元数据接口）
        ip.is_reserved or       # 保留地址
        ip.is_multicast         # 组播地址
    )


def _check_url_safety(url: str) -> None:
    """检查 URL 是否安全（SSRF 防护）

    只允许 http/https/ftp 协议的公网地址下载

    Args:
        url: 待检查的 URL

    Raises:
        HTTPException: URL 不安全时抛出 400 异常
    """
    try:
        parsed = urlparse(url)
        scheme = parsed.scheme.lower()
        hostname = parsed.hostname

        # 只检查 http/https/ftp 协议
        if scheme not in ('http', 'https', 'ftp'):
            # magnet、ed2k 等协议不经过 HTTP，不检查
            return

        if not hostname:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="无效的下载链接"
            )

        # 明确禁止的主机名
        blocked_hosts = {
            'localhost', 'localhost.localdomain',
            '127.0.0.1', '::1', '0.0.0.0', '::'
        }
        if hostname.lower() in blocked_hosts:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="不允许下载本机地址"
            )

        # 检查是否为 IP 地址
        try:
            ip = ipaddress.ip_address(hostname)
            if _is_private_ip(ip):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="不允许下载内网地址"
                )
            # 是公网 IP，放行
            return
        except ValueError:
            # 不是 IP，是域名，继续 DNS 解析检查
            pass

        # DNS 解析并检查所有解析结果
        try:
            # getaddrinfo 返回所有解析结果（IPv4 + IPv6）
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
                    # 解析结果不是有效 IP，跳过
                    continue

        except socket.gaierror:
            # DNS 解析失败，让 aria2 去处理，可能是临时网络问题
            pass

    except HTTPException:
        # 重新抛出我们的异常
        raise
    except Exception:
        # 其他异常不阻止下载，让 aria2 去处理
        pass


# ========== Schemas ==========

class TaskCreate(BaseModel):
    """创建任务请求体"""
    uri: str
    options: dict | None = None


class TaskStatusUpdate(BaseModel):
    """更新任务状态请求体"""
    status: str  # pause | resume


class TorrentCreate(BaseModel):
    """上传种子请求体"""
    torrent: str  # Base64 encoded
    options: dict | None = None


class PositionUpdate(BaseModel):
    """调整位置请求体"""
    position: int
    how: str = "POS_SET"  # POS_SET, POS_CUR, POS_END


# ========== Helpers ==========

def _get_state(request: Request) -> AppState:
    return request.app.state.app_state


def _get_client(request: Request) -> Aria2Client:
    return get_aria2_client(request)


async def _resolve_task(task_id_or_gid: str, owner_id: int) -> Task | None:
    """通过 ID 或 GID 查询任务

    Args:
        task_id_or_gid: 数字 ID 或 gid 字符串
        owner_id: 用户 ID

    Returns:
        任务记录，未找到返回 None
    """
    async with get_session() as db:
        if task_id_or_gid.isdigit():
            result = await db.exec(
                select(Task).where(Task.id == int(task_id_or_gid), Task.owner_id == owner_id)
            )
        else:
            result = await db.exec(
                select(Task).where(Task.gid == task_id_or_gid, Task.owner_id == owner_id)
            )
        return result.first()


def _get_user_download_dir(user_id: int) -> str:
    """获取用户专属下载目录（用于隔离）"""
    base = Path(settings.download_dir).resolve()
    user_dir = base / str(user_id)
    user_dir.mkdir(parents=True, exist_ok=True)
    return str(user_dir)


def _get_user_incomplete_dir(user_id: int) -> str:
    """获取用户的 .incomplete 目录（下载中文件存放位置）"""
    base = Path(settings.download_dir).resolve()
    incomplete_dir = base / str(user_id) / ".incomplete"
    incomplete_dir.mkdir(parents=True, exist_ok=True)
    return str(incomplete_dir)


def _check_disk_space() -> tuple[bool, int]:
    """检查磁盘空间是否足够

    返回: (是否足够, 剩余空间字节)
    """
    download_path = Path(settings.download_dir)
    download_path.mkdir(parents=True, exist_ok=True)
    disk = shutil.disk_usage(download_path)
    min_free = get_min_free_disk()
    return disk.free > min_free, disk.free


def _task_to_dict(task: Task) -> dict:
    """Convert Task model to dict for API response"""
    return {
        "id": task.id,
        "owner_id": task.owner_id,
        "gid": task.gid,
        "uri": task.uri,
        "status": task.status,
        "name": task.name,
        "total_length": task.total_length,
        "completed_length": task.completed_length,
        "download_speed": task.download_speed,
        "upload_speed": task.upload_speed,
        "error": task.error,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "artifact_path": task.artifact_path,
        "artifact_token": task.artifact_token,
        "peak_download_speed": task.peak_download_speed,
        "peak_connections": task.peak_connections,
    }


# ========== API Endpoints ==========

@router.post("", status_code=status.HTTP_201_CREATED)
async def create_task(payload: TaskCreate, request: Request, user: User = Depends(require_user)) -> dict:
    """创建新下载任务

    会进行以下检查:
    1. 频率限制：每用户每分钟最多 30 次
    2. SSRF 防护：禁止下载内网地址
    3. 磁盘剩余空间是否低于最小阈值
    4. 强制设置下载目录到用户专属目录（隔离）

    注：任务大小和用户配额检查在 aria2 hook 中统一处理
    """
    # 频率限制：每用户每分钟最多 30 次
    if not api_limiter.is_allowed(user.id, "create_task", limit=30, window_seconds=60):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="操作过于频繁，请稍后再试"
        )

    # SSRF 防护：检查 URL 安全性
    _check_url_safety(payload.uri)

    # 检查磁盘空间（最小阈值）
    disk_ok, disk_free = _check_disk_space()
    if not disk_ok:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"磁盘空间不足，剩余 {disk_free / 1024 / 1024 / 1024:.2f} GB"
        )

    # 强制设置用户专属下载目录（隔离）- 下载到 .incomplete 目录
    user_incomplete_dir = _get_user_incomplete_dir(user.id)
    options = dict(payload.options) if payload.options else {}
    options["dir"] = user_incomplete_dir  # 下载中的文件放在 .incomplete 目录

    # 存储到数据库时脱敏 URL 凭证
    masked_uri = mask_url_credentials(payload.uri)

    # 创建任务记录
    async with get_session() as db:
        task = Task(
            owner_id=user.id,
            uri=masked_uri,  # 存储脱敏后的 URI
            status="queued",
            created_at=utc_now(),
            updated_at=utc_now()
        )
        db.add(task)
        await db.commit()
        await db.refresh(task)
        task_id = task.id

    state = _get_state(request)
    async with state.lock:
        state.pending_tasks[task_id] = {"uri": masked_uri}

    async def _do_add():
        client = _get_client(request)
        try:
            # 发送给 aria2 时使用原始 URI（包含凭证）
            gid = await client.add_uri([payload.uri], options)
            async with get_session() as db:
                result = await db.exec(select(Task).where(Task.id == task_id))
                db_task = result.first()
                if db_task:
                    db_task.gid = gid
                    db_task.status = "active"
                    db_task.updated_at = utc_now()
                    db.add(db_task)
        except Exception as exc:  # noqa: BLE001
            async with get_session() as db:
                result = await db.exec(select(Task).where(Task.id == task_id))
                db_task = result.first()
                if db_task:
                    db_task.status = "error"
                    db_task.error = str(exc)
                    db_task.updated_at = utc_now()
                    db.add(db_task)
        finally:
            async with state.lock:
                state.pending_tasks.pop(task_id, None)
            await broadcast_update(state, user.id, task_id, force=True)

    asyncio.create_task(_do_add())

    async with get_session() as db:
        result = await db.exec(select(Task).where(Task.id == task_id))
        task = result.first()
        return _task_to_dict(task)


@router.post("/torrent", status_code=status.HTTP_201_CREATED)
async def create_torrent_task(
    payload: TorrentCreate,
    request: Request,
    user: User = Depends(require_user)
) -> dict:
    """通过种子文件创建下载任务

    会进行以下检查:
    1. 频率限制：每用户每分钟最多 10 次
    2. Base64 大小限制（约 10MB，即 base64 长度约 14MB）
    3. 磁盘剩余空间是否低于最小阈值
    4. 强制设置下载目录到用户专属目录（隔离）

    注：任务大小和用户配额检查在 aria2 hook 中统一处理
    """
    # 频率限制：每用户每分钟最多 10 次
    if not api_limiter.is_allowed(user.id, "create_torrent", limit=10, window_seconds=60):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="操作过于频繁，请稍后再试"
        )

    # 校验 Base64 大小（约 10MB 限制，base64 编码后约 14MB）
    max_base64_length = 14 * 1024 * 1024  # 14MB in characters
    if len(payload.torrent) > max_base64_length:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="种子文件过大，最大支持 10MB"
        )

    # 检查磁盘空间（最小阈值）
    disk_ok, disk_free = _check_disk_space()
    if not disk_ok:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"磁盘空间不足，剩余 {disk_free / 1024 / 1024 / 1024:.2f} GB"
        )

    # 强制设置用户专属下载目录（隔离）- 下载到 .incomplete 目录
    user_incomplete_dir = _get_user_incomplete_dir(user.id)
    options = dict(payload.options) if payload.options else {}
    options["dir"] = user_incomplete_dir  # 下载中的文件放在 .incomplete 目录

    # 创建任务记录（uri 字段设为 "[torrent]" 标识种子任务）
    async with get_session() as db:
        task = Task(
            owner_id=user.id,
            uri="[torrent]",
            status="queued",
            created_at=utc_now(),
            updated_at=utc_now()
        )
        db.add(task)
        await db.commit()
        await db.refresh(task)
        task_id = task.id

    state = _get_state(request)
    async with state.lock:
        state.pending_tasks[task_id] = {"uri": "[torrent]"}

    async def _do_add():
        client = _get_client(request)
        try:
            gid = await client.add_torrent(payload.torrent, [], options)
            async with get_session() as db:
                result = await db.exec(select(Task).where(Task.id == task_id))
                db_task = result.first()
                if db_task:
                    db_task.gid = gid
                    db_task.status = "active"
                    db_task.updated_at = utc_now()
                    db.add(db_task)
        except Exception as exc:  # noqa: BLE001
            async with get_session() as db:
                result = await db.exec(select(Task).where(Task.id == task_id))
                db_task = result.first()
                if db_task:
                    db_task.status = "error"
                    db_task.error = str(exc)
                    db_task.updated_at = utc_now()
                    db.add(db_task)
        finally:
            async with state.lock:
                state.pending_tasks.pop(task_id, None)
            await broadcast_update(state, user.id, task_id, force=True)

    asyncio.create_task(_do_add())

    async with get_session() as db:
        result = await db.exec(select(Task).where(Task.id == task_id))
        task = result.first()
        return _task_to_dict(task)


@router.get("")
async def list_tasks(
    status_filter: str | None = None,
    user: User = Depends(require_user)
) -> list[dict]:
    """获取当前用户的任务列表

    可选参数:
    - status_filter: 状态筛选 (active, paused, complete, error, queued, waiting, stopped)
    """
    async with get_session() as db:
        if status_filter:
            result = await db.exec(
                select(Task)
                .where(Task.owner_id == user.id, Task.status == status_filter)
                .order_by(Task.id.desc())
            )
        else:
            result = await db.exec(
                select(Task)
                .where(Task.owner_id == user.id, Task.status != "removed")
                .order_by(Task.id.desc())
            )
        tasks = result.all()
        return [_task_to_dict(t) for t in tasks]


@router.get("/{task_id}")
async def get_task(task_id: str, user: User = Depends(require_user)) -> dict:
    """获取任务详情

    支持通过数字 ID 或 gid 查询任务。
    """
    task = await _resolve_task(task_id, user.id)
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")
    return _task_to_dict(task)


@router.get("/{task_id}/detail")
async def get_task_detail(task_id: str, request: Request, user: User = Depends(require_user)) -> dict:
    """获取任务详细信息（包含 aria2 实时状态）

    支持通过数字 ID 或 gid 查询任务。
    """
    task = await _resolve_task(task_id, user.id)
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")

    task_dict = _task_to_dict(task)

    # 如果任务有 gid，从 aria2 获取详细状态
    if task.gid:
        client = _get_client(request)
        try:
            aria2_status = await client.tell_status(task.gid)
            # 合并数据库信息和 aria2 实时信息
            task_dict["aria2_detail"] = {
                "num_seeders": aria2_status.get("numSeeders"),
                "connections": aria2_status.get("connections"),
                "bitfield": aria2_status.get("bitfield"),
                "info_hash": aria2_status.get("infoHash"),
                "num_pieces": aria2_status.get("numPieces"),
                "piece_length": aria2_status.get("pieceLength"),
                "bittorrent": aria2_status.get("bittorrent"),
                "dir": aria2_status.get("dir"),
                "following_gid": aria2_status.get("followingGid"),
                "belonging_to": aria2_status.get("belongsTo"),
            }
        except Exception:
            pass

    return task_dict


@router.delete("/{task_id}")
async def delete_task(
    task_id: str,
    request: Request,
    delete_files: bool = False,
    user: User = Depends(require_user)
) -> dict:
    """删除任务

    会同时从 Aria2 中移除下载任务、清理下载记录和 .aria2 控制文件。
    支持通过数字 ID 或 gid 查询任务。

    参数:
    - delete_files: 是否同时删除下载的文件（默认 False）
    """
    task = await _resolve_task(task_id, user.id)
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")

    client = _get_client(request)
    if task.gid:
        try:
            await client.force_remove(task.gid)
        except Exception:
            pass
        try:
            await client.remove_download_result(task.gid)
        except Exception:
            pass

    user_dir = Path(settings.download_dir) / str(user.id)

    # 清理 .aria2 控制文件
    if task.name:
        aria2_file = user_dir / f"{task.name}.aria2"
        if aria2_file.exists():
            try:
                aria2_file.unlink()
            except Exception:
                pass

    # 如果需要删除文件
    if delete_files and task.name:
        file_path = user_dir / task.name
        if file_path.exists():
            try:
                if file_path.is_file():
                    file_path.unlink()
                elif file_path.is_dir():
                    shutil.rmtree(file_path)
            except Exception:
                pass

    async with get_session() as db:
        result = await db.exec(select(Task).where(Task.id == task.id))
        db_task = result.first()
        if db_task:
            db_task.status = "removed"
            db_task.updated_at = utc_now()
            db.add(db_task)

    return {"ok": True}


@router.delete("")
async def clear_history(
    request: Request,
    delete_files: bool = False,
    user: User = Depends(require_user)
) -> dict:
    """清空当前用户的所有历史记录

    只删除已完成、错误、已停止的任务，不删除活跃任务。
    同时清理相关的 .aria2 控制文件。

    参数:
    - delete_files: 是否同时删除下载的文件（默认 False）
    """
    # 获取所有可以删除的历史任务
    async with get_session() as db:
        result = await db.exec(
            select(Task).where(
                Task.owner_id == user.id,
                Task.status.in_(["complete", "error", "stopped", "removed"])
            )
        )
        tasks = result.all()

    client = _get_client(request)
    user_dir = Path(settings.download_dir) / str(user.id)

    # 从 aria2 中清理
    for task in tasks:
        if task.gid:
            try:
                await client.force_remove(task.gid)
            except Exception:
                pass
            try:
                await client.remove_download_result(task.gid)
            except Exception:
                pass

        # 清理 .aria2 控制文件
        if task.name:
            aria2_file = user_dir / f"{task.name}.aria2"
            if aria2_file.exists():
                try:
                    aria2_file.unlink()
                except Exception:
                    pass

        # 如果需要删除文件
        if delete_files and task.name:
            file_path = user_dir / task.name
            if file_path.exists():
                try:
                    if file_path.is_file():
                        file_path.unlink()
                    elif file_path.is_dir():
                        shutil.rmtree(file_path)
                except Exception:
                    pass

    # 标记为已删除
    async with get_session() as db:
        result = await db.exec(
            select(Task).where(
                Task.owner_id == user.id,
                Task.status.in_(["complete", "error", "stopped", "removed"])
            )
        )
        for task in result.all():
            task.status = "removed"
            task.updated_at = utc_now()
            db.add(task)

    return {"ok": True, "count": len(tasks)}


@router.put("/{task_id}/status")
async def update_task_status(
    task_id: str,
    payload: TaskStatusUpdate,
    request: Request,
    user: User = Depends(require_user)
) -> dict:
    """更新任务状态（暂停/恢复）

    支持通过数字 ID 或 gid 查询任务。

    请求体:
    - status: "pause" 或 "resume"
    """
    task = await _resolve_task(task_id, user.id)
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")
    if not task.gid:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="任务尚未开始")

    client = _get_client(request)
    action = payload.status

    if action == "pause":
        await client.pause(task.gid)
        async with get_session() as db:
            result = await db.exec(select(Task).where(Task.id == task.id))
            db_task = result.first()
            if db_task:
                db_task.status = "paused"
                db_task.updated_at = utc_now()
                db.add(db_task)
    elif action == "resume":
        await client.unpause(task.gid)
        async with get_session() as db:
            result = await db.exec(select(Task).where(Task.id == task.id))
            db_task = result.first()
            if db_task:
                db_task.status = "active"
                db_task.updated_at = utc_now()
                db.add(db_task)
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="不支持的操作，请使用 pause 或 resume"
        )

    state = _get_state(request)
    await broadcast_update(state, user.id, task.id, force=True)

    return {"ok": True, "status": action}


@router.post("/{task_id}/retry")
async def retry_task(
    task_id: str,
    request: Request,
    user: User = Depends(require_user)
) -> dict:
    """Retry a failed task by creating new download and removing old record

    Supports querying by numeric ID or gid.

    Prerequisites:
    - Task must exist and belong to current user
    - Task must NOT be a torrent task (uri != "[torrent]")

    On success:
    - Creates new aria2 task with original URI
    - Deletes old task record (files remain on disk)
    - Returns new task info

    On failure:
    - Old task remains unchanged
    - Returns error details

    注：任务大小和用户配额检查在 aria2 hook 中统一处理
    """
    # 1. Fetch and validate task
    task = await _resolve_task(task_id, user.id)
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")

    # 2. Check if torrent task
    if task.uri == "[torrent]":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="种子任务无法重试，请重新上传种子文件"
        )

    # 3. Reuse create_task logic for validation and task creation
    original_uri = task.uri

    # Check disk space (minimum threshold)
    disk_ok, disk_free = _check_disk_space()
    if not disk_ok:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"磁盘空间不足，剩余 {disk_free / 1024 / 1024 / 1024:.2f} GB"
        )

    # 4. Create new task record
    user_dir = _get_user_download_dir(user.id)
    options = {"dir": user_dir}

    async with get_session() as db:
        new_task = Task(
            owner_id=user.id,
            uri=original_uri,
            status="queued",
            created_at=utc_now(),
            updated_at=utc_now()
        )
        db.add(new_task)
        await db.commit()
        await db.refresh(new_task)
        new_task_id = new_task.id

    state = _get_state(request)
    async with state.lock:
        state.pending_tasks[new_task_id] = {"uri": original_uri}

    # 5. Add to aria2 and handle result
    client = _get_client(request)
    try:
        gid = await client.add_uri([original_uri], options)
        async with get_session() as db:
            result = await db.exec(select(Task).where(Task.id == new_task_id))
            db_task = result.first()
            if db_task:
                db_task.gid = gid
                db_task.status = "active"
                db_task.updated_at = utc_now()
                db.add(db_task)

        # 6. Success: Clean up old task from aria2 if it has gid
        if task.gid:
            try:
                await client.force_remove(task.gid)
            except Exception:
                pass
            try:
                await client.remove_download_result(task.gid)
            except Exception:
                pass

        # 7. Delete old task record (keep files on disk)
        async with get_session() as db:
            result = await db.exec(select(Task).where(Task.id == task.id))
            old_task = result.first()
            if old_task:
                await db.delete(old_task)

    except Exception as exc:
        # Aria2 failed: delete the failed new task, keep old task
        async with get_session() as db:
            result = await db.exec(select(Task).where(Task.id == new_task_id))
            failed_task = result.first()
            if failed_task:
                await db.delete(failed_task)
        async with state.lock:
            state.pending_tasks.pop(new_task_id, None)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"创建 aria2 任务失败: {exc}"
        )

    # Success: clean up pending state
    async with state.lock:
        state.pending_tasks.pop(new_task_id, None)

    # 8. Return new task info
    async with get_session() as db:
        result = await db.exec(select(Task).where(Task.id == new_task_id))
        new_task = result.first()
        await broadcast_update(state, user.id, new_task_id, force=True)
        return _task_to_dict(new_task)


@router.put("/{task_id}/position")
async def update_task_position(
    task_id: str,
    payload: PositionUpdate,
    request: Request,
    user: User = Depends(require_user)
) -> dict:
    """调整任务在下载队列中的位置

    支持通过数字 ID 或 gid 查询任务。
    只有等待中（waiting）的任务可以调整位置。

    请求体:
    - position: 目标位置
    - how: 调整方式，可选 "POS_SET"（绝对位置）、"POS_CUR"（相对当前）、"POS_END"（相对末尾）
    """
    task = await _resolve_task(task_id, user.id)
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")
    if not task.gid:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="任务尚未开始")
    if task.status != "waiting":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="只有等待中的任务可以调整位置"
        )

    # 验证 how 参数
    valid_how = ("POS_SET", "POS_CUR", "POS_END")
    if payload.how not in valid_how:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"无效的调整方式，请使用 {', '.join(valid_how)}"
        )

    client = _get_client(request)
    try:
        new_position = await client.change_position(task.gid, payload.position, payload.how)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"调整位置失败: {exc}"
        )

    return {"ok": True, "new_position": new_position}


@router.get("/{task_id}/files")
async def get_task_files(
    task_id: str,
    request: Request,
    user: User = Depends(require_user)
) -> list[dict]:
    """获取任务文件列表

    支持通过数字 ID 或 gid 查询任务。
    返回 Aria2 中该任务的所有文件信息。
    """
    task = await _resolve_task(task_id, user.id)
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")
    if not task.gid:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="任务尚未开始")

    client = _get_client(request)
    try:
        files = await client.get_files(task.gid)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"获取文件列表失败: {exc}"
        )

    # 格式化文件信息，将绝对路径转换为相对于用户目录的路径
    user_dir = Path(settings.download_dir) / str(user.id)
    result = []
    for f in files:
        file_path = f.get("path", "")
        # 转换为相对路径
        try:
            abs_path = Path(file_path)
            if abs_path.is_absolute() and abs_path.is_relative_to(user_dir):
                file_path = str(abs_path.relative_to(user_dir))
        except Exception:
            pass  # 如果转换失败，保持原路径

        result.append({
            "index": int(f.get("index", 0)),
            "path": file_path,
            "length": int(f.get("length", 0)),
            "completed_length": int(f.get("completedLength", 0)),
            "selected": f.get("selected") == "true",
        })
    return result


@router.get("/artifacts/{token}")
async def download_artifact(token: str, user: User = Depends(require_user)) -> FileResponse:
    """下载完成的制品文件

    需要使用任务完成后生成的 artifact_token 访问。
    """
    async with get_session() as db:
        result = await db.exec(select(Task).where(Task.artifact_token == token))
        task = result.first()

    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="制品不存在")
    if task.owner_id != user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权访问")

    artifact_path = task.artifact_path
    if not artifact_path or not os.path.isfile(artifact_path):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="文件不存在")

    return FileResponse(path=artifact_path, filename=os.path.basename(artifact_path))
