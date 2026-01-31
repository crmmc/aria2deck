from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Dict, Set

from fastapi import WebSocket, Request

from app.aria2.client import Aria2Client
from app.core.config import settings


@dataclass
class AppState:
    pending_tasks: Dict[int, dict] = field(default_factory=dict)
    ws_connections: Dict[int, Set[WebSocket]] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # 消息节流：记录每个任务的最后推送时间 {task_id: timestamp}
    last_broadcast: Dict[int, float] = field(default_factory=dict)
    # 任务提交锁，避免并发提交同一任务
    task_submit_locks: Dict[int, asyncio.Lock] = field(default_factory=dict)
    # 用户空间锁，避免并发冻结/校验导致超额
    user_space_locks: Dict[int, asyncio.Lock] = field(default_factory=dict)


async def get_user_space_lock(state: AppState, user_id: int) -> asyncio.Lock:
    """获取用户空间锁，避免并发冻结/校验竞态"""
    async with state.lock:
        lock = state.user_space_locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            state.user_space_locks[user_id] = lock
        return lock


def get_aria2_client(request: Request | None = None) -> Aria2Client:
    """获取 aria2 客户端实例
    
    优先从数据库读取配置，如果数据库中没有配置则使用环境变量配置
    """
    from app.db import fetch_one
    
    # 尝试从数据库读取配置
    rpc_url_row = fetch_one("SELECT value FROM config WHERE key = ?", ["aria2_rpc_url"])
    rpc_secret_row = fetch_one("SELECT value FROM config WHERE key = ?", ["aria2_rpc_secret"])
    
    rpc_url = rpc_url_row["value"] if rpc_url_row else settings.aria2_rpc_url
    rpc_secret = rpc_secret_row["value"] if rpc_secret_row else settings.aria2_rpc_secret
    
    # 如果提供了 request，从 app.state 获取客户端并检查配置是否变化
    if request and hasattr(request.app.state, "aria2_client"):
        client = request.app.state.aria2_client
        # 如果配置没有变化，直接返回现有客户端
        if client._rpc_url == rpc_url and client._secret == rpc_secret:
            return client
    
    # 创建新的客户端实例
    return Aria2Client(rpc_url, rpc_secret)
