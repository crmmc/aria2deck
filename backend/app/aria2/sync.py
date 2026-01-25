from __future__ import annotations

import asyncio
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from sqlmodel import select

from app.aria2.client import Aria2Client
from app.aria2.errors import parse_error_message
from app.core.config import settings
from app.core.security import sanitize_string
from app.core.state import AppState, WS_THROTTLE_INTERVAL
from app.database import get_session
from app.models import Task


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sanitize_path(file_path: str | None, user_id: int) -> str | None:
    """将绝对路径转换为相对于用户目录的路径，避免暴露服务器路径"""
    if not file_path:
        return None

    try:
        abs_path = Path(file_path)
        user_dir = Path(settings.download_dir) / str(user_id)

        # 如果是绝对路径且在用户目录内，转换为相对路径
        if abs_path.is_absolute() and abs_path.is_relative_to(user_dir):
            return str(abs_path.relative_to(user_dir))

        # 如果已经是相对路径或无法转换，返回文件名
        return abs_path.name if abs_path.name else file_path
    except Exception:
        # 转换失败时，尝试返回文件名
        try:
            return Path(file_path).name
        except Exception:
            return file_path


def _map_status(status: dict, user_id: int) -> dict:
    """映射 aria2 状态到数据库字段，并清理路径信息"""
    raw_name = (
        status.get("bittorrent", {}).get("info", {}).get("name")
        or status.get("files", [{}])[0].get("path")
    )

    # 清理路径，避免暴露服务器绝对路径
    sanitized_name = _sanitize_path(raw_name, user_id)
    # 清理控制字符，防止日志注入
    sanitized_name = sanitize_string(sanitized_name)

    # 错误信息：先翻译，再清理控制字符
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


def _first_artifact_path(status: dict) -> str | None:
    files = status.get("files") or []
    if not files:
        return None
    return files[0].get("path")


def _move_completed_files(status: dict, user_id: int) -> str | None:
    """将完成的文件从 .incomplete 移动到用户根目录

    返回: 移动后的新路径，如果移动失败则返回原路径
    """
    files = status.get("files") or []
    if not files:
        return None

    first_file_path = files[0].get("path")
    if not first_file_path:
        return None

    src_path = Path(first_file_path)
    user_dir = Path(settings.download_dir) / str(user_id)
    incomplete_dir = user_dir / ".incomplete"

    # 检查文件是否在 .incomplete 目录中
    try:
        if not src_path.exists():
            return first_file_path
        if not src_path.is_relative_to(incomplete_dir):
            return first_file_path
    except Exception:
        return first_file_path

    # 确定要移动的是文件还是目录（BT任务通常是目录）
    # 获取相对于 .incomplete 的路径
    try:
        rel_path = src_path.relative_to(incomplete_dir)
        # 获取顶级目录或文件
        top_level = rel_path.parts[0] if rel_path.parts else None
        if not top_level:
            return first_file_path

        src_item = incomplete_dir / top_level
        dst_item = user_dir / top_level

        # 如果目标已存在，添加后缀
        if dst_item.exists():
            counter = 1
            stem = dst_item.stem
            suffix = dst_item.suffix
            while dst_item.exists():
                dst_item = user_dir / f"{stem}_{counter}{suffix}"
                counter += 1

        # 移动文件/目录
        shutil.move(str(src_item), str(dst_item))

        # 返回新路径
        new_first_file = dst_item / rel_path.relative_to(top_level) if len(rel_path.parts) > 1 else dst_item
        return str(new_first_file)
    except Exception:
        return first_file_path


async def _update_task(task_id: int, values: dict) -> None:
    async with get_session() as db:
        result = await db.exec(select(Task).where(Task.id == task_id))
        task = result.first()
        if task:
            for key, value in values.items():
                setattr(task, key, value)
            task.updated_at = utc_now()
            db.add(task)


async def sync_tasks(
    state: AppState,
    interval: float,
) -> None:
    """同步 aria2 任务状态到数据库

    每次循环都会动态获取最新的 aria2 配置
    """
    from app.core.state import get_aria2_client

    while True:
        # 动态获取 aria2 客户端（支持配置热更新）
        client = get_aria2_client()

        async with get_session() as db:
            result = await db.exec(
                select(Task).where(
                    Task.gid.isnot(None),
                    Task.status != "removed"
                )
            )
            tasks = result.all()

        # 并发查询所有任务状态
        async def fetch_and_update(task: Task) -> None:
            gid = task.gid
            if not gid:
                return
            try:
                status = await client.tell_status(gid)
            except Exception as exc:  # noqa: BLE001
                await _update_task(task.id, {"status": "error", "error": str(exc)})
                return

            mapped = _map_status(status, task.owner_id)
            artifact_path = task.artifact_path
            artifact_token = task.artifact_token
            if mapped["status"] == "complete" and not artifact_token:
                # 移动文件从 .incomplete 到用户根目录
                artifact_path = _move_completed_files(status, task.owner_id)
                artifact_token = uuid4().hex

            current_speed = mapped["download_speed"]
            current_connections = int(status.get("connections", 0))
            peak_speed = task.peak_download_speed or 0
            peak_connections = task.peak_connections or 0

            if current_speed > peak_speed:
                peak_speed = current_speed
            if current_connections > peak_connections:
                peak_connections = current_connections

            await _update_task(
                task.id,
                {
                    **mapped,
                    "artifact_path": artifact_path,
                    "artifact_token": artifact_token,
                    "peak_download_speed": peak_speed,
                    "peak_connections": peak_connections,
                },
            )
            await broadcast_update(state, task.owner_id, task.id)

        # 并发执行所有任务更新
        await asyncio.gather(*[fetch_and_update(task) for task in tasks])

        # 检测已完成任务的文件是否存在，若不存在则删除任务
        await _cleanup_orphaned_tasks()

        await asyncio.sleep(interval)


async def _cleanup_orphaned_tasks() -> None:
    """检测已完成任务的文件是否存在，若不存在则删除任务"""
    from app.core.state import get_aria2_client

    async with get_session() as db:
        result = await db.exec(
            select(Task).where(
                Task.status == "complete",
                Task.name.isnot(None)
            )
        )
        completed_tasks = result.all()

    for task in completed_tasks:
        user_dir = Path(settings.download_dir) / str(task.owner_id)
        file_path = user_dir / task.name

        # 检查文件是否存在
        if not file_path.exists():
            # 从 aria2 中移除记录
            if task.gid:
                client = get_aria2_client()
                try:
                    await client.remove_download_result(task.gid)
                except Exception:
                    pass

            # 标记任务为 removed，保留历史记录
            await _update_task(task.id, {"status": "removed"})


async def broadcast_update(state: AppState, user_id: int, task_id: int, force: bool = False) -> None:
    """广播任务更新到 WebSocket 客户端

    Args:
        state: 应用状态
        user_id: 用户 ID
        task_id: 任务 ID
        force: 是否强制发送（忽略节流）
    """
    # 节流检查：同一任务在短时间内只发送一次
    now = time.time()
    if not force:
        async with state.lock:
            last_time = state.last_broadcast.get(task_id, 0)
            if now - last_time < WS_THROTTLE_INTERVAL:
                return  # 跳过本次推送
            state.last_broadcast[task_id] = now

    async with get_session() as db:
        result = await db.exec(select(Task).where(Task.id == task_id))
        task = result.first()
        if not task:
            return
        # Convert to dict for JSON serialization
        payload = {
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

    async with state.lock:
        sockets = list(state.ws_connections.get(user_id, set()))
    for ws in sockets:
        try:
            await ws.send_json({"type": "task_update", "task": payload})
        except Exception:
            await unregister_ws(state, user_id, ws)


async def register_ws(state: AppState, user_id: int, ws) -> None:
    async with state.lock:
        state.ws_connections.setdefault(user_id, set()).add(ws)


async def unregister_ws(state: AppState, user_id: int, ws) -> None:
    async with state.lock:
        sockets = state.ws_connections.get(user_id)
        if sockets:
            sockets.discard(ws)
