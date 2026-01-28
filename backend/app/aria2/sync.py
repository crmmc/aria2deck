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
from app.models import Task, User


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_user_available_space(user: User) -> int:
    """获取用户实际可用空间（考虑配额和机器空间限制）

    Args:
        user: 用户对象

    Returns:
        用户可用空间（字节）
    """
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


def delete_path_with_aria2(target: Path) -> bool:
    """删除文件/目录，并清理对应的 .aria2 控制文件

    Args:
        target: 要删除的文件或目录路径

    Returns:
        是否成功删除主文件/目录
    """
    if not target.exists():
        return False

    try:
        if target.is_symlink():
            # 只删除符号链接本身
            target.unlink()
        elif target.is_file():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target)
        else:
            return False

        # 清理对应的 .aria2 控制文件
        aria2_file = target.parent / f"{target.name}.aria2"
        if aria2_file.exists() and aria2_file.is_file():
            try:
                aria2_file.unlink()
            except Exception:
                pass  # 静默失败，不影响主文件删除结果

        return True
    except Exception:
        return False


def _sanitize_path(file_path: str | None, user_id: int) -> str | None:
    """将绝对路径转换为文件名，避免暴露服务器路径和 .incomplete 目录"""
    if not file_path:
        return None

    try:
        abs_path = Path(file_path)
        user_dir = Path(settings.download_dir) / str(user_id)

        # 如果是绝对路径且在用户目录内，提取相对路径
        if abs_path.is_absolute() and abs_path.is_relative_to(user_dir):
            rel_path = abs_path.relative_to(user_dir)
            # 去除 .incomplete 前缀，只保留文件名
            parts = rel_path.parts
            if parts and parts[0] == ".incomplete":
                # 跳过 .incomplete，返回后面的路径
                if len(parts) > 1:
                    return str(Path(*parts[1:]))
                return None
            return str(rel_path)

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


async def _cancel_and_delete_task(
    client,
    state,
    task: Task,
    aria2_status: dict,
    notification_message: str,
) -> None:
    """取消超限任务，保留历史记录以便重试

    Args:
        client: aria2 客户端
        state: 应用状态
        task: 任务对象
        aria2_status: aria2 状态字典
        notification_message: 通知用户的消息（同时作为错误信息保存）
    """
    gid = task.gid

    # 1. 停止 aria2 任务
    try:
        await client.force_remove(gid)
    except Exception:
        pass
    try:
        await client.remove_download_result(gid)
    except Exception:
        pass

    # 2. 删除关联文件（包括 .aria2 控制文件）
    user_dir = Path(settings.download_dir) / str(task.owner_id)
    incomplete_dir = user_dir / ".incomplete"

    files = aria2_status.get("files", [])
    for f in files:
        file_path = f.get("path", "")
        if file_path:
            delete_path_with_aria2(Path(file_path))

    # 对于 BT 下载，可能有顶层目录的 .aria2 文件
    bt_info = aria2_status.get("bittorrent", {}).get("info", {})
    bt_name = bt_info.get("name")
    if bt_name:
        for base_dir in [user_dir, incomplete_dir]:
            bt_path = base_dir / bt_name
            delete_path_with_aria2(bt_path)

    # 3. 更新数据库：标记为 error 状态，清除 gid 以便重试
    # 保留 uri 和 total_length，方便用户查看历史和重试
    async with get_session() as db:
        result = await db.exec(select(Task).where(Task.id == task.id))
        db_task = result.first()
        if db_task:
            db_task.status = "error"
            db_task.gid = None  # 清除 gid，aria2 任务已移除
            db_task.error = notification_message
            db_task.download_speed = 0
            db_task.upload_speed = 0
            db_task.updated_at = utc_now()
            # 保留 name 用于显示
            if not db_task.name and aria2_status:
                db_task.name = (
                    aria2_status.get("bittorrent", {}).get("info", {}).get("name")
                    or aria2_status.get("files", [{}])[0].get("path", "").split("/")[-1]
                )
            # 保留 total_length 用于显示大小
            if aria2_status:
                db_task.total_length = int(aria2_status.get("totalLength", 0))
            db.add(db_task)

    # 4. 通知用户
    await broadcast_notification(
        state,
        task.owner_id,
        notification_message,
        level="error"
    )

    # 5. 广播任务状态更新到前端
    await broadcast_update(state, task.owner_id, task.id, force=True)


async def sync_tasks(
    state: AppState,
    interval: float,
) -> None:
    """同步 aria2 任务状态到数据库

    每次循环都会动态获取最新的 aria2 配置
    同时检查活动任务的大小是否超过限制（HTTP 下载启动时 totalLength 可能为 0）
    """
    import logging
    from app.core.state import get_aria2_client
    from app.routers.config import get_max_task_size

    logger = logging.getLogger(__name__)

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

            # 空间检查：仅对 active 任务且 totalLength 首次变为非零时检查
            aria2_status = status.get("status")
            total_length = int(status.get("totalLength", 0))

            # 只有当 DB 中的 total_length 为 0，而 aria2 返回的 totalLength > 0 时才检查
            # 这表示大小刚刚变为已知（HTTP 下载收到 Content-Length 响应）
            if aria2_status == "active" and total_length > 0 and (task.total_length or 0) == 0:
                task_name = (
                    status.get("bittorrent", {}).get("info", {}).get("name")
                    or status.get("files", [{}])[0].get("path", "").split("/")[-1]
                    or "未知任务"
                )

                # 检查系统最大任务限制
                max_task_size = get_max_task_size()
                if total_length > max_task_size:
                    logger.warning(
                        f"[Sync] 任务 {task.id} 大小 {total_length / 1024**3:.2f} GB "
                        f"超过系统限制 {max_task_size / 1024**3:.2f} GB，终止并删除任务"
                    )
                    await _cancel_and_delete_task(
                        client, state, task, status,
                        f"已取消：大小 {total_length / 1024**3:.2f} GB 超过系统限制 {max_task_size / 1024**3:.2f} GB"
                    )
                    return

                # 检查用户可用空间
                async with get_session() as db:
                    result = await db.exec(select(User).where(User.id == task.owner_id))
                    user = result.first()

                if user:
                    user_available = get_user_available_space(user)
                    if total_length > user_available:
                        logger.warning(
                            f"[Sync] 任务 {task.id} 大小 {total_length / 1024**3:.2f} GB "
                            f"超过用户可用空间 {user_available / 1024**3:.2f} GB，终止并删除任务"
                        )
                        await _cancel_and_delete_task(
                            client, state, task, status,
                            f"已取消：大小 {total_length / 1024**3:.2f} GB 超过可用空间 {user_available / 1024**3:.2f} GB"
                        )
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


async def broadcast_notification(
    state: AppState,
    user_id: int,
    message: str,
    level: str = "error"
) -> None:
    """广播通知消息到 WebSocket 客户端

    Args:
        state: 应用状态
        user_id: 用户 ID
        message: 通知消息
        level: 消息级别 (info, warning, error)
    """
    async with state.lock:
        sockets = list(state.ws_connections.get(user_id, set()))
    for ws in sockets:
        try:
            await ws.send_json({
                "type": "notification",
                "level": level,
                "message": message,
            })
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
