from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from uuid import uuid4

from app.aria2.client import Aria2Client
from app.core.config import settings
from app.core.state import AppState
from app.db import execute, fetch_all, fetch_one, utc_now


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
    
    return {
        "status": status.get("status", "unknown"),
        "name": sanitized_name,
        "total_length": int(status.get("totalLength", 0)),
        "completed_length": int(status.get("completedLength", 0)),
        "download_speed": int(status.get("downloadSpeed", 0)),
        "upload_speed": int(status.get("uploadSpeed", 0)),
        "error": status.get("errorMessage"),
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


def _update_task(task_id: int, values: dict) -> None:
    fields = []
    params = []
    for key, value in values.items():
        fields.append(f"{key} = ?")
        params.append(value)
    params.extend([utc_now(), task_id])
    execute(
        f"""
        UPDATE tasks SET {", ".join(fields)}, updated_at = ?
        WHERE id = ?
        """,
        params,
    )


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

        tasks = fetch_all(
            """
            SELECT id, gid, owner_id, status, artifact_token, artifact_path,
                   peak_download_speed, peak_connections
            FROM tasks
            WHERE gid IS NOT NULL AND status NOT IN ('removed')
            """
        )

        # 并发查询所有任务状态
        async def fetch_and_update(task: dict) -> None:
            gid = task["gid"]
            if not gid:
                return
            try:
                status = await client.tell_status(gid)
            except Exception as exc:  # noqa: BLE001
                _update_task(task["id"], {"status": "error", "error": str(exc)})
                return

            mapped = _map_status(status, task["owner_id"])
            artifact_path = task["artifact_path"]
            artifact_token = task["artifact_token"]
            if mapped["status"] == "complete" and not artifact_token:
                # 移动文件从 .incomplete 到用户根目录
                artifact_path = _move_completed_files(status, task["owner_id"])
                artifact_token = uuid4().hex

            current_speed = mapped["download_speed"]
            current_connections = int(status.get("connections", 0))
            peak_speed = task.get("peak_download_speed", 0) or 0
            peak_connections = task.get("peak_connections", 0) or 0

            if current_speed > peak_speed:
                peak_speed = current_speed
            if current_connections > peak_connections:
                peak_connections = current_connections

            _update_task(
                task["id"],
                {
                    **mapped,
                    "artifact_path": artifact_path,
                    "artifact_token": artifact_token,
                    "peak_download_speed": peak_speed,
                    "peak_connections": peak_connections,
                },
            )
            await broadcast_update(state, task["owner_id"], task["id"])

        # 并发执行所有任务更新
        await asyncio.gather(*[fetch_and_update(task) for task in tasks])
        await asyncio.sleep(interval)


async def broadcast_update(state: AppState, user_id: int, task_id: int) -> None:
    payload = fetch_one("SELECT * FROM tasks WHERE id = ?", [task_id])
    if not payload:
        return
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
