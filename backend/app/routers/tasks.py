"""任务管理接口模块

提供任务的增删查改、状态控制、文件列表、制品下载等功能。
包含容量检测与用户隔离逻辑。
"""
from __future__ import annotations

import os
import asyncio
import shutil
from pathlib import Path

import aiohttp
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.aria2.client import Aria2Client
from app.aria2.sync import broadcast_update
from app.auth import require_user
from app.core.config import settings
from app.core.state import AppState, get_aria2_client
from app.db import execute, fetch_all, fetch_one, utc_now
from app.routers.config import get_max_task_size, get_min_free_disk


router = APIRouter(prefix="/api/tasks", tags=["tasks"])


# ========== Schemas ==========

class TaskCreate(BaseModel):
    """创建任务请求体"""
    uri: str
    options: dict | None = None


class TaskStatusUpdate(BaseModel):
    """更新任务状态请求体"""
    status: str  # pause | resume


# ========== Helpers ==========

def _get_state(request: Request) -> AppState:
    return request.app.state.app_state


def _get_client(request: Request) -> Aria2Client:
    return get_aria2_client(request)


def _get_user_download_dir(user_id: int) -> str:
    """获取用户专属下载目录（用于隔离）"""
    base = Path(settings.download_dir).resolve()
    user_dir = base / str(user_id)
    user_dir.mkdir(parents=True, exist_ok=True)
    return str(user_dir)


async def _check_url_size(uri: str) -> int | None:
    """通过 HEAD 请求获取文件大小（仅 HTTP/HTTPS）
    
    返回: 文件大小（字节），无法获取时返回 None
    """
    if not uri.lower().startswith(("http://", "https://")):
        return None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.head(uri, allow_redirects=True, timeout=10) as resp:
                content_length = resp.headers.get("Content-Length")
                if content_length:
                    return int(content_length)
    except Exception:
        pass
    return None


def _check_disk_space() -> tuple[bool, int]:
    """检查磁盘空间是否足够
    
    返回: (是否足够, 剩余空间字节)
    """
    download_path = Path(settings.download_dir)
    download_path.mkdir(parents=True, exist_ok=True)
    disk = shutil.disk_usage(download_path)
    min_free = get_min_free_disk()
    return disk.free > min_free, disk.free


def _get_user_available_space(user: dict) -> int:
    """获取用户实际可用空间（考虑配额和机器空间限制）
    
    返回: 用户可用空间（字节）
    """
    # 计算用户已使用的空间
    user_dir = Path(settings.download_dir) / str(user["id"])
    used_space = 0
    if user_dir.exists():
        for file_path in user_dir.rglob("*"):
            if file_path.is_file():
                try:
                    used_space += file_path.stat().st_size
                except Exception:
                    pass
    
    # 用户配额
    user_quota = user.get("quota", 100 * 1024 * 1024 * 1024)  # 默认 100GB
    
    # 获取机器实际剩余空间
    download_path = Path(settings.download_dir)
    download_path.mkdir(parents=True, exist_ok=True)
    disk = shutil.disk_usage(download_path)
    machine_free = disk.free
    
    # 用户理论可用空间（基于配额）
    user_free_by_quota = max(0, user_quota - used_space)
    
    # 实际可用空间 = min(用户配额剩余, 机器剩余空间)
    return min(user_free_by_quota, machine_free)


# ========== API Endpoints ==========

@router.post("", status_code=status.HTTP_201_CREATED)
async def create_task(payload: TaskCreate, request: Request, user: dict = Depends(require_user)) -> dict:
    """创建新下载任务
    
    会进行以下检查:
    1. 磁盘剩余空间是否足够
    2. 用户可用空间是否足够（考虑配额和机器空间限制）
    3. HTTP/HTTPS 任务会检查文件大小是否超过限制
    4. 强制设置下载目录到用户专属目录（隔离）
    """
    # 检查磁盘空间
    disk_ok, disk_free = _check_disk_space()
    if not disk_ok:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"磁盘空间不足，剩余 {disk_free / 1024 / 1024 / 1024:.2f} GB"
        )
    
    # 检查文件大小（HTTP/HTTPS）
    max_size = get_max_task_size()
    file_size = await _check_url_size(payload.uri)
    if file_size is not None and file_size > max_size:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"文件大小 {file_size / 1024 / 1024 / 1024:.2f} GB 超过系统限制 {max_size / 1024 / 1024 / 1024:.2f} GB"
        )
    
    # 检查用户可用空间（考虑配额和机器空间限制）
    user_available = _get_user_available_space(user)
    if file_size is not None and file_size > user_available:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"文件大小 {file_size / 1024 / 1024 / 1024:.2f} GB 超过您的可用空间 {user_available / 1024 / 1024 / 1024:.2f} GB"
        )
    
    # 强制设置用户专属下载目录（隔离）
    user_dir = _get_user_download_dir(user["id"])
    options = dict(payload.options) if payload.options else {}
    options["dir"] = user_dir  # 强制覆盖，确保隔离
    
    # 创建任务记录
    task_id = execute(
        """
        INSERT INTO tasks (owner_id, uri, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        [user["id"], payload.uri, "queued", utc_now(), utc_now()],
    )
    
    state = _get_state(request)
    async with state.lock:
        state.pending_tasks[task_id] = {"uri": payload.uri}

    async def _do_add():
        client = _get_client(request)
        try:
            gid = await client.add_uri([payload.uri], options)
            execute(
                "UPDATE tasks SET gid = ?, status = ?, updated_at = ? WHERE id = ?",
                [gid, "active", utc_now(), task_id]
            )
        except Exception as exc:  # noqa: BLE001
            execute(
                "UPDATE tasks SET status = ?, error = ?, updated_at = ? WHERE id = ?",
                ["error", str(exc), utc_now(), task_id],
            )
        finally:
            async with state.lock:
                state.pending_tasks.pop(task_id, None)
            await broadcast_update(state, user["id"], task_id)

    asyncio.create_task(_do_add())
    return fetch_one("SELECT * FROM tasks WHERE id = ?", [task_id])


@router.get("")
def list_tasks(
    status_filter: str | None = None,
    user: dict = Depends(require_user)
) -> list[dict]:
    """获取当前用户的任务列表
    
    可选参数:
    - status_filter: 状态筛选 (active, paused, complete, error, queued, waiting, stopped)
    """
    if status_filter:
        return fetch_all(
            "SELECT * FROM tasks WHERE owner_id = ? AND status = ? ORDER BY id DESC",
            [user["id"], status_filter]
        )
    return fetch_all(
        "SELECT * FROM tasks WHERE owner_id = ? AND status != 'removed' ORDER BY id DESC",
        [user["id"]]
    )


@router.get("/{task_id}")
def get_task(task_id: int, user: dict = Depends(require_user)) -> dict:
    """获取任务详情"""
    task = fetch_one(
        "SELECT * FROM tasks WHERE id = ? AND owner_id = ?",
        [task_id, user["id"]]
    )
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")
    return task


@router.get("/{task_id}/detail")
async def get_task_detail(task_id: int, request: Request, user: dict = Depends(require_user)) -> dict:
    """获取任务详细信息（包含 aria2 实时状态）"""
    task = fetch_one(
        "SELECT * FROM tasks WHERE id = ? AND owner_id = ?",
        [task_id, user["id"]]
    )
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")
    
    # 如果任务有 gid，从 aria2 获取详细状态
    if task.get("gid"):
        client = _get_client(request)
        try:
            aria2_status = await client.tell_status(task["gid"])
            # 合并数据库信息和 aria2 实时信息
            return {
                **task,
                "aria2_detail": {
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
            }
        except Exception:
            pass
    
    return task


@router.delete("/{task_id}")
async def delete_task(
    task_id: int, 
    request: Request, 
    delete_files: bool = False,
    user: dict = Depends(require_user)
) -> dict:
    """删除任务
    
    会同时从 Aria2 中移除下载任务、清理下载记录和 .aria2 控制文件。
    
    参数:
    - delete_files: 是否同时删除下载的文件（默认 False）
    """
    task = fetch_one(
        "SELECT * FROM tasks WHERE id = ? AND owner_id = ?",
        [task_id, user["id"]]
    )
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")
    
    client = _get_client(request)
    if task.get("gid"):
        try:
            await client.force_remove(task["gid"])
        except Exception:
            pass
        try:
            await client.remove_download_result(task["gid"])
        except Exception:
            pass
    
    user_dir = Path(settings.download_dir) / str(user["id"])
    
    # 清理 .aria2 控制文件
    if task.get("name"):
        aria2_file = user_dir / f"{task['name']}.aria2"
        if aria2_file.exists():
            try:
                aria2_file.unlink()
            except Exception:
                pass
    
    # 如果需要删除文件
    if delete_files and task.get("name"):
        file_path = user_dir / task["name"]
        if file_path.exists():
            try:
                if file_path.is_file():
                    file_path.unlink()
                elif file_path.is_dir():
                    shutil.rmtree(file_path)
            except Exception:
                pass
    
    execute(
        "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
        ["removed", utc_now(), task_id]
    )
    return {"ok": True}


@router.delete("")
async def clear_history(
    request: Request, 
    delete_files: bool = False,
    user: dict = Depends(require_user)
) -> dict:
    """清空当前用户的所有历史记录
    
    只删除已完成、错误、已停止的任务，不删除活跃任务。
    同时清理相关的 .aria2 控制文件。
    
    参数:
    - delete_files: 是否同时删除下载的文件（默认 False）
    """
    # 获取所有可以删除的历史任务
    tasks = fetch_all(
        """
        SELECT id, gid, name FROM tasks 
        WHERE owner_id = ? AND status IN ('complete', 'error', 'stopped', 'removed')
        """,
        [user["id"]]
    )
    
    client = _get_client(request)
    user_dir = Path(settings.download_dir) / str(user["id"])
    
    # 从 aria2 中清理
    for task in tasks:
        if task.get("gid"):
            try:
                await client.force_remove(task["gid"])
            except Exception:
                pass
            try:
                await client.remove_download_result(task["gid"])
            except Exception:
                pass
        
        # 清理 .aria2 控制文件
        if task.get("name"):
            aria2_file = user_dir / f"{task['name']}.aria2"
            if aria2_file.exists():
                try:
                    aria2_file.unlink()
                except Exception:
                    pass
        
        # 如果需要删除文件
        if delete_files and task.get("name"):
            file_path = user_dir / task["name"]
            if file_path.exists():
                try:
                    if file_path.is_file():
                        file_path.unlink()
                    elif file_path.is_dir():
                        shutil.rmtree(file_path)
                except Exception:
                    pass
    
    # 标记为已删除
    execute(
        """
        UPDATE tasks SET status = ?, updated_at = ? 
        WHERE owner_id = ? AND status IN ('complete', 'error', 'stopped', 'removed')
        """,
        ["removed", utc_now(), user["id"]]
    )
    
    return {"ok": True, "count": len(tasks)}


@router.put("/{task_id}/status")
async def update_task_status(
    task_id: int,
    payload: TaskStatusUpdate,
    request: Request,
    user: dict = Depends(require_user)
) -> dict:
    """更新任务状态（暂停/恢复）
    
    请求体:
    - status: "pause" 或 "resume"
    """
    task = fetch_one(
        "SELECT * FROM tasks WHERE id = ? AND owner_id = ?",
        [task_id, user["id"]]
    )
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")
    if not task.get("gid"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="任务尚未开始")
    
    client = _get_client(request)
    action = payload.status
    
    if action == "pause":
        await client.pause(task["gid"])
        execute(
            "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
            ["paused", utc_now(), task_id]
        )
    elif action == "resume":
        await client.unpause(task["gid"])
        execute(
            "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
            ["active", utc_now(), task_id]
        )
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="不支持的操作，请使用 pause 或 resume"
        )
    
    state = _get_state(request)
    await broadcast_update(state, user["id"], task_id)
    
    return {"ok": True, "status": action}


@router.get("/{task_id}/files")
async def get_task_files(
    task_id: int,
    request: Request,
    user: dict = Depends(require_user)
) -> list[dict]:
    """获取任务文件列表
    
    返回 Aria2 中该任务的所有文件信息。
    """
    task = fetch_one(
        "SELECT * FROM tasks WHERE id = ? AND owner_id = ?",
        [task_id, user["id"]]
    )
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")
    if not task.get("gid"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="任务尚未开始")
    
    client = _get_client(request)
    try:
        files = await client.get_files(task["gid"])
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"获取文件列表失败: {exc}"
        )
    
    # 格式化文件信息，将绝对路径转换为相对于用户目录的路径
    user_dir = Path(settings.download_dir) / str(user["id"])
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
def download_artifact(token: str, user: dict = Depends(require_user)) -> FileResponse:
    """下载完成的制品文件
    
    需要使用任务完成后生成的 artifact_token 访问。
    """
    task = fetch_one(
        "SELECT artifact_path, owner_id FROM tasks WHERE artifact_token = ?",
        [token],
    )
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="制品不存在")
    if task["owner_id"] != user["id"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权访问")
    
    artifact_path = task["artifact_path"]
    if not artifact_path or not os.path.isfile(artifact_path):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="文件不存在")
    
    return FileResponse(path=artifact_path, filename=os.path.basename(artifact_path))
