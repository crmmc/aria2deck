"""Process-local download-backend connectivity state.

Tracks whether this service can currently reach the external download backend.
Public API consumers only see role-scoped status/message pairs; raw connection
details stay in logs and admin settings tooling.
"""

from __future__ import annotations

import asyncio
import time
from typing import Literal

StatusValue = Literal["ok", "degraded"]

USER_OK_MESSAGE = "服务运行正常"
USER_DEGRADED_MESSAGE = "服务器遇到错误，请联系管理员"
ADMIN_OK_MESSAGE = "下载后端连接正常"
ADMIN_DEGRADED_MESSAGE = "无法连接到下载后端"

# Require consecutive failures before flipping ok → degraded to avoid flicker.
_FAILURE_THRESHOLD = 2

_lock = asyncio.Lock()
_connected = True
_consecutive_failures = 0
_last_checked_at: float | None = None
_last_ok_at: float | None = None


def reset_for_tests() -> None:
    """Reset in-memory state between unit tests."""
    global _connected, _consecutive_failures, _last_checked_at, _last_ok_at
    _connected = True
    _consecutive_failures = 0
    _last_checked_at = None
    _last_ok_at = None


async def mark_ok() -> None:
    """Record a successful reachability probe."""
    global _connected, _consecutive_failures, _last_checked_at, _last_ok_at
    now = time.time()
    async with _lock:
        _connected = True
        _consecutive_failures = 0
        _last_checked_at = now
        _last_ok_at = now


async def mark_fail() -> None:
    """Record a failed reachability probe with debounce."""
    global _connected, _consecutive_failures, _last_checked_at
    now = time.time()
    async with _lock:
        _last_checked_at = now
        _consecutive_failures += 1
        if _consecutive_failures >= _FAILURE_THRESHOLD:
            _connected = False


def snapshot(*, is_admin: bool) -> dict[str, dict[str, str]]:
    """Build a role-scoped public payload. Never includes RPC URLs or raw errors."""
    status: StatusValue = "ok" if _connected else "degraded"
    if is_admin:
        message = ADMIN_OK_MESSAGE if status == "ok" else ADMIN_DEGRADED_MESSAGE
    else:
        message = USER_OK_MESSAGE if status == "ok" else USER_DEGRADED_MESSAGE
    return {
        "download_backend": {
            "status": status,
            "message": message,
        }
    }


def is_connected() -> bool:
    return _connected
