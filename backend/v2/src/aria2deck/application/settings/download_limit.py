"""下载并发连接限流（A6）：对齐旧版 DownloadAccessManager 的执行语义。

会话下载按（用户, 文件）计数，公开分享下载按（IP, 内容）计数，
两类共享系统总连接上限；任一档超限抛 RATE_LIMITED（429 中文提示）。
0 值语义对齐旧版：系统总量与各分档 0=不限，匿名池 0=全面拒绝匿名下载。
占用在响应结束后释放；进程内计数，锁内判定+登记保证原子。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from ...domain.errors import DomainError, ErrorCode
from ...domain.settings import SystemSettings, effective_download_limits

_TOTAL_KEY = "total"
_ANONYMOUS_KEY = "anonymous"

_SYSTEM_TOTAL_MESSAGE = "下载连接数已达系统上限，请稍后再试"
_AUTH_PER_USER_MESSAGE = "当前账号的下载连接数已达上限，请稍后再试"
_AUTH_PER_FILE_MESSAGE = "当前文件的下载连接数已达上限，请稍后再试"
_ANONYMOUS_POOL_MESSAGE = "匿名下载连接数已达上限，请稍后再试"
_ANONYMOUS_PER_IP_MESSAGE = "当前来源的下载连接数已达上限，请稍后再试"
_ANONYMOUS_PER_FILE_MESSAGE = "当前文件的匿名下载连接数已达上限，请稍后再试"


class DownloadConnectionLease:
    """一次下载连接占用句柄；release 幂等。"""

    def __init__(self, manager: DownloadConnectionLimiter, keys: tuple[str, ...]) -> None:
        self._manager = manager
        self._keys = keys
        self._released = False

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        await self._manager._release(self._keys)  # noqa: SLF001  # 句柄即管理器的占用凭证


class DownloadConnectionLimiter:
    def __init__(self, *, settings: Callable[[], SystemSettings]) -> None:
        self.settings = settings
        self._counts: dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def acquire_authenticated(self, *, user_id: int, file_key: str) -> DownloadConnectionLease:
        """会话下载连接：系统总量 → 每用户 → 每用户文件，逐档检查。"""
        keys = (
            _TOTAL_KEY,
            f"auth-user:{user_id}",
            f"auth-user-file:{user_id}:{file_key}",
        )
        checks = (
            (_TOTAL_KEY, "total_connections", _SYSTEM_TOTAL_MESSAGE),
            (keys[1], "authenticated_per_user_connections", _AUTH_PER_USER_MESSAGE),
            (keys[2], "authenticated_per_file_connections", _AUTH_PER_FILE_MESSAGE),
        )
        return await self._acquire(keys, checks)

    async def acquire_anonymous(self, *, client_ip: str, file_key: str) -> DownloadConnectionLease:
        """公开分享下载连接：系统总量 → 匿名池 → 每来源 IP → 每来源文件。"""
        keys = (
            _TOTAL_KEY,
            _ANONYMOUS_KEY,
            f"anon-ip:{client_ip}",
            f"anon-ip-file:{client_ip}:{file_key}",
        )
        checks = (
            (_TOTAL_KEY, "total_connections", _SYSTEM_TOTAL_MESSAGE),
            (_ANONYMOUS_KEY, "anonymous_total_connections", _ANONYMOUS_POOL_MESSAGE),
            (keys[2], "anonymous_per_ip_connections", _ANONYMOUS_PER_IP_MESSAGE),
            (keys[3], "anonymous_per_file_connections", _ANONYMOUS_PER_FILE_MESSAGE),
        )
        return await self._acquire(keys, checks, reject_on_zero=frozenset({"anonymous_total_connections"}))

    async def _acquire(
        self,
        keys: tuple[str, ...],
        checks: tuple[tuple[str, str, str], ...],
        *,
        reject_on_zero: frozenset[str] = frozenset(),
    ) -> DownloadConnectionLease:
        limits = effective_download_limits(self.settings().download_connection_limits)
        async with self._lock:
            for key, limit_key, message in checks:
                limit = limits[limit_key]
                # 0 值语义对齐旧版：档位 0=不限；匿名池 0=拒绝全部匿名下载（可整体关闭匿名通道）。
                if limit <= 0:
                    if limit_key in reject_on_zero:
                        raise DomainError(ErrorCode.RATE_LIMITED, message)
                    continue
                if self._counts.get(key, 0) >= limit:
                    raise DomainError(ErrorCode.RATE_LIMITED, message)
            for key in keys:
                self._counts[key] = self._counts.get(key, 0) + 1
        return DownloadConnectionLease(self, keys)

    async def _release(self, keys: tuple[str, ...]) -> None:
        async with self._lock:
            for key in keys:
                remaining = self._counts.get(key, 0) - 1
                if remaining > 0:
                    self._counts[key] = remaining
                else:
                    self._counts.pop(key, None)
