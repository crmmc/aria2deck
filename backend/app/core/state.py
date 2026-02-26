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
    # 任务完成处理锁，避免 WebSocket 和轮询同时处理同一任务的完成事件
    task_complete_locks: Dict[int, asyncio.Lock] = field(default_factory=dict)
    # 缓存的 aria2 配置
    _cached_rpc_url: str | None = field(default=None, repr=False)
    _cached_rpc_secret: str | None = field(default=None, repr=False)


async def get_user_space_lock(state: AppState, user_id: int) -> asyncio.Lock:
    """获取用户空间锁，避免并发冻结/校验竞态"""
    async with state.lock:
        lock = state.user_space_locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            state.user_space_locks[user_id] = lock
        return lock


async def get_task_complete_lock(state: AppState, task_id: int) -> asyncio.Lock:
    async with state.lock:
        lock = state.task_complete_locks.get(task_id)
        if lock is None:
            lock = asyncio.Lock()
            state.task_complete_locks[task_id] = lock
        return lock


def get_aria2_client(request: Request | None = None, state: "AppState | None" = None) -> Aria2Client:
    """获取 aria2 客户端实例

    优先使用缓存的配置，避免在 async 上下文中做同步 DB 查询。
    配置通过 refresh_aria2_config() 刷新。
    """
    # 优先从 app.state 获取已有客户端
    if request and hasattr(request.app.state, "aria2_client"):
        return request.app.state.aria2_client

    # 从 AppState 缓存读取配置
    resolved_state = state
    if resolved_state is None and request and hasattr(request.app.state, "app_state"):
        resolved_state = request.app.state.app_state

    if resolved_state is not None:
        rpc_url = resolved_state._cached_rpc_url or settings.aria2_rpc_url
        rpc_secret = resolved_state._cached_rpc_secret or settings.aria2_rpc_secret
        return Aria2Client(rpc_url, rpc_secret)

    return Aria2Client(settings.aria2_rpc_url, settings.aria2_rpc_secret)


async def refresh_aria2_config(state: AppState) -> None:
    """从数据库异步刷新 aria2 配置到 AppState 缓存"""
    from app.routers.config import get_config_value_async

    rpc_url = await get_config_value_async("aria2_rpc_url")
    rpc_secret = await get_config_value_async("aria2_rpc_secret")
    state._cached_rpc_url = rpc_url or settings.aria2_rpc_url
    state._cached_rpc_secret = rpc_secret or settings.aria2_rpc_secret
