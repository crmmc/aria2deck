from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Dict, Set
from weakref import WeakValueDictionary

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
    # 使用弱引用字典自动回收不再被引用的锁，避免长期运行字典无限增长
    task_submit_locks: WeakValueDictionary[int, asyncio.Lock] = field(default_factory=WeakValueDictionary)
    # 用户空间锁，避免并发冻结/校验导致超额（自动回收）
    user_space_locks: WeakValueDictionary[int, asyncio.Lock] = field(default_factory=WeakValueDictionary)
    # 任务完成处理锁，避免 WebSocket 和轮询同时处理同一任务的完成事件（自动回收）
    task_complete_locks: WeakValueDictionary[int, asyncio.Lock] = field(default_factory=WeakValueDictionary)
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


async def release_user_space_lock(state: AppState, user_id: int, lock: asyncio.Lock) -> None:
    """回收空闲用户空间锁，避免字典无限增长"""
    async with state.lock:
        current = state.user_space_locks.get(user_id)
        if current is lock and not lock.locked():
            state.user_space_locks.pop(user_id, None)


async def get_task_complete_lock(state: AppState, task_id: int) -> asyncio.Lock:
    async with state.lock:
        lock = state.task_complete_locks.get(task_id)
        if lock is None:
            lock = asyncio.Lock()
            state.task_complete_locks[task_id] = lock
        return lock


async def release_task_complete_lock(state: AppState, task_id: int, lock: asyncio.Lock) -> None:
    """回收空闲任务完成锁，避免字典无限增长"""
    async with state.lock:
        current = state.task_complete_locks.get(task_id)
        if current is lock and not lock.locked():
            state.task_complete_locks.pop(task_id, None)


async def release_task_submit_lock(state: AppState, task_id: int, lock: asyncio.Lock) -> None:
    """回收空闲任务提交锁，避免字典无限增长"""
    async with state.lock:
        current = state.task_submit_locks.get(task_id)
        if current is lock and not lock.locked():
            state.task_submit_locks.pop(task_id, None)


def _resolve_aria2_config(state: "AppState | None") -> tuple[str, str]:
    if state is None:
        return settings.aria2_rpc_url, settings.aria2_rpc_secret

    rpc_url = state._cached_rpc_url
    if rpc_url is None:
        rpc_url = settings.aria2_rpc_url

    rpc_secret = state._cached_rpc_secret
    if rpc_secret is None:
        rpc_secret = settings.aria2_rpc_secret

    return rpc_url, rpc_secret


def get_aria2_client(request: Request | None = None, state: "AppState | None" = None) -> Aria2Client:
    """获取 aria2 客户端实例

    优先使用缓存的配置，避免在 async 上下文中做同步 DB 查询。
    配置通过 refresh_aria2_config() 刷新。
    """
    # 从 AppState 缓存读取配置
    resolved_state = state
    if resolved_state is None and request and hasattr(request.app.state, "app_state"):
        resolved_state = request.app.state.app_state

    rpc_url, rpc_secret = _resolve_aria2_config(resolved_state)

    # request 路径优先复用 app.state.client，但若配置已变更则原地替换
    if request and hasattr(request.app.state, "aria2_client"):
        client = request.app.state.aria2_client
        if client._rpc_url == rpc_url and client._secret == rpc_secret:
            return client
        new_client = Aria2Client(rpc_url, rpc_secret)
        request.app.state.aria2_client = new_client
        return new_client

    return Aria2Client(rpc_url, rpc_secret)


async def refresh_aria2_config(state: AppState) -> None:
    """从数据库异步刷新 aria2 配置到 AppState 缓存"""
    from app.routers.config import get_config_value_async

    rpc_url = await get_config_value_async("aria2_rpc_url")
    rpc_secret = await get_config_value_async("aria2_rpc_secret")
    state._cached_rpc_url = rpc_url if rpc_url is not None else settings.aria2_rpc_url
    state._cached_rpc_secret = rpc_secret if rpc_secret is not None else settings.aria2_rpc_secret
