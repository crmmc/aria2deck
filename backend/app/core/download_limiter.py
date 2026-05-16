"""下载并发配置与连接分配器。"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import StrEnum

logger = logging.getLogger(__name__)


class DownloadRejectReason(StrEnum):
    SYSTEM_TOTAL = "system_total"
    AUTHENTICATED_PER_USER = "authenticated_per_user"
    AUTHENTICATED_PER_FILE = "authenticated_per_file"
    ANONYMOUS_POOL = "anonymous_pool"
    ANONYMOUS_PER_IP = "anonymous_per_ip"
    ANONYMOUS_PER_FILE = "anonymous_per_file"


class DownloadIdentity(StrEnum):
    AUTHENTICATED = "authenticated"
    ANONYMOUS = "anonymous"


class DownloadConfig:
    """下载并发配置的内存缓存。"""

    _DB_KEY_MAP: dict[str, tuple[str, int, tuple[str, ...]]] = {
        "download_total_connections": (
            "total_connections",
            100,
            ("download_max_connections",),
        ),
        "download_authenticated_reserved_connections": (
            "authenticated_reserved_connections",
            60,
            (),
        ),
        "download_authenticated_per_user_connections": (
            "authenticated_per_user_connections",
            16,
            ("download_per_user_connections",),
        ),
        "download_authenticated_per_file_connections": (
            "authenticated_per_file_connections",
            8,
            ("download_per_file_connections",),
        ),
        "download_anonymous_base_connections": (
            "anonymous_base_connections",
            20,
            (),
        ),
        "download_anonymous_borrow_connections": (
            "anonymous_borrow_connections",
            20,
            (),
        ),
        "download_anonymous_per_ip_connections": (
            "anonymous_per_ip_connections",
            4,
            (),
        ),
        "download_anonymous_per_file_connections": (
            "anonymous_per_file_connections",
            2,
            (),
        ),
    }

    def __init__(self) -> None:
        self.total_connections: int = 100
        self.authenticated_reserved_connections: int = 60
        self.authenticated_per_user_connections: int = 16
        self.authenticated_per_file_connections: int = 8
        self.anonymous_base_connections: int = 20
        self.anonymous_borrow_connections: int = 20
        self.anonymous_per_ip_connections: int = 4
        self.anonymous_per_file_connections: int = 2

    async def load_from_db(self) -> None:
        """启动时从数据库加载配置。"""
        from app.repositories.settings import get_settings_row

        row = await get_settings_row()

        for db_key, (attr, default, _legacy_keys) in self._DB_KEY_MAP.items():
            value = row.get(db_key, default) if row else default
            try:
                value = int(value)
            except (TypeError, ValueError):
                value = default
            setattr(self, attr, value)

        self.validate()
        logger.info(
            "下载并发配置已加载 total=%s auth_reserved=%s auth_per_user=%s "
            "auth_per_file=%s anon_base=%s anon_borrow=%s anon_per_ip=%s anon_per_file=%s",
            self.total_connections,
            self.authenticated_reserved_connections,
            self.authenticated_per_user_connections,
            self.authenticated_per_file_connections,
            self.anonymous_base_connections,
            self.anonymous_borrow_connections,
            self.anonymous_per_ip_connections,
            self.anonymous_per_file_connections,
        )

    async def refresh(self) -> None:
        """管理员更新配置后主动刷新。"""
        await self.load_from_db()

    def defaults(self) -> dict[str, str]:
        """返回新配置键的默认值字符串。"""
        return {
            db_key: str(default) for db_key, (_, default, _) in self._DB_KEY_MAP.items()
        }

    def anonymous_total_connections(self) -> int:
        """匿名下载允许占用的总连接数。"""
        return self.anonymous_base_connections + self.anonymous_borrow_connections

    def validate(self) -> None:
        """校验并发配置的关键约束。"""
        if self.total_connections <= 0:
            return

        allocated = (
            self.authenticated_reserved_connections
            + self.anonymous_base_connections
            + self.anonymous_borrow_connections
        )
        if allocated > self.total_connections:
            raise ValueError(
                "下载并发配置无效：保底与匿名配额总和不能超过系统总连接上限"
            )


download_config = DownloadConfig()


class DownloadLease:
    """下载连接占用句柄。"""

    def __init__(
        self,
        manager: DownloadAccessManager,
        identity: DownloadIdentity,
        subject_key: str,
        file_hash: str,
    ) -> None:
        self._manager = manager
        self.identity = identity
        self.subject_key = subject_key
        self.file_hash = file_hash
        self._released = False

    async def release(self) -> None:
        """释放当前下载连接占用。"""
        if self._released:
            return
        self._released = True
        await self._manager.release(self)


@dataclass(slots=True)
class DownloadAcquireResult:
    """下载连接获取结果。"""

    allowed: bool
    reason: DownloadRejectReason | None = None
    lease: DownloadLease | None = None

    def detail(self) -> str:
        """生成用户可见的拒绝提示。"""
        mapping = {
            DownloadRejectReason.SYSTEM_TOTAL: "下载连接数已达系统上限，请稍后再试",
            DownloadRejectReason.AUTHENTICATED_PER_USER: "当前账号的下载连接数已达上限，请稍后再试",
            DownloadRejectReason.AUTHENTICATED_PER_FILE: "当前文件的下载连接数已达上限，请稍后再试",
            DownloadRejectReason.ANONYMOUS_POOL: "匿名下载连接数已达上限，请稍后再试",
            DownloadRejectReason.ANONYMOUS_PER_IP: "当前来源的下载连接数已达上限，请稍后再试",
            DownloadRejectReason.ANONYMOUS_PER_FILE: "当前文件的匿名下载连接数已达上限，请稍后再试",
        }
        return mapping.get(self.reason, "下载连接数已达上限，请稍后再试")


class DownloadAccessManager:
    """下载连接分配器。"""

    def __init__(self) -> None:
        self._authenticated_active = 0
        self._anonymous_active = 0
        self._authenticated_user_counts: dict[str, int] = {}
        self._authenticated_user_file_counts: dict[str, int] = {}
        self._anonymous_ip_counts: dict[str, int] = {}
        self._anonymous_ip_file_counts: dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def acquire_authenticated(
        self, user_id: int, file_hash: str
    ) -> DownloadAcquireResult:
        """获取已登录下载连接。"""
        subject_key = str(user_id)
        user_file_key = f"{subject_key}:{file_hash}"
        async with self._lock:
            if self._total_limit_reached():
                return DownloadAcquireResult(False, DownloadRejectReason.SYSTEM_TOTAL)
            if self._limit_reached(
                self._authenticated_user_counts,
                subject_key,
                download_config.authenticated_per_user_connections,
            ):
                return DownloadAcquireResult(
                    False, DownloadRejectReason.AUTHENTICATED_PER_USER
                )
            if self._limit_reached(
                self._authenticated_user_file_counts,
                user_file_key,
                download_config.authenticated_per_file_connections,
            ):
                return DownloadAcquireResult(
                    False, DownloadRejectReason.AUTHENTICATED_PER_FILE
                )

            self._authenticated_active += 1
            self._increment(self._authenticated_user_counts, subject_key)
            self._increment(self._authenticated_user_file_counts, user_file_key)
            lease = DownloadLease(
                self, DownloadIdentity.AUTHENTICATED, subject_key, file_hash
            )
            return DownloadAcquireResult(True, lease=lease)

    async def acquire_anonymous(
        self, client_ip: str, file_hash: str
    ) -> DownloadAcquireResult:
        """获取匿名下载连接。"""
        ip_file_key = f"{client_ip}:{file_hash}"
        async with self._lock:
            if self._total_limit_reached():
                return DownloadAcquireResult(False, DownloadRejectReason.SYSTEM_TOTAL)
            if self._anonymous_pool_limit_reached():
                return DownloadAcquireResult(False, DownloadRejectReason.ANONYMOUS_POOL)
            if self._limit_reached(
                self._anonymous_ip_counts,
                client_ip,
                download_config.anonymous_per_ip_connections,
            ):
                return DownloadAcquireResult(
                    False, DownloadRejectReason.ANONYMOUS_PER_IP
                )
            if self._limit_reached(
                self._anonymous_ip_file_counts,
                ip_file_key,
                download_config.anonymous_per_file_connections,
            ):
                return DownloadAcquireResult(
                    False, DownloadRejectReason.ANONYMOUS_PER_FILE
                )

            self._anonymous_active += 1
            self._increment(self._anonymous_ip_counts, client_ip)
            self._increment(self._anonymous_ip_file_counts, ip_file_key)
            lease = DownloadLease(
                self, DownloadIdentity.ANONYMOUS, client_ip, file_hash
            )
            return DownloadAcquireResult(True, lease=lease)

    async def release(self, lease: DownloadLease) -> None:
        """释放下载连接。"""
        async with self._lock:
            if lease.identity == DownloadIdentity.AUTHENTICATED:
                self._authenticated_active = max(0, self._authenticated_active - 1)
                self._decrement(self._authenticated_user_counts, lease.subject_key)
                self._decrement(
                    self._authenticated_user_file_counts,
                    f"{lease.subject_key}:{lease.file_hash}",
                )
                return

            self._anonymous_active = max(0, self._anonymous_active - 1)
            self._decrement(self._anonymous_ip_counts, lease.subject_key)
            self._decrement(
                self._anonymous_ip_file_counts,
                f"{lease.subject_key}:{lease.file_hash}",
            )

    async def clear_all(self) -> None:
        """清空所有计数（测试用）。"""
        async with self._lock:
            self._authenticated_active = 0
            self._anonymous_active = 0
            self._authenticated_user_counts.clear()
            self._authenticated_user_file_counts.clear()
            self._anonymous_ip_counts.clear()
            self._anonymous_ip_file_counts.clear()

    def _total_limit_reached(self) -> bool:
        total_limit = download_config.total_connections
        if total_limit <= 0:
            return False
        return self._total_active() >= total_limit

    def _anonymous_pool_limit_reached(self) -> bool:
        anonymous_limit = download_config.anonymous_total_connections()
        if anonymous_limit <= 0:
            return True
        return self._anonymous_active >= anonymous_limit

    def _total_active(self) -> int:
        return self._authenticated_active + self._anonymous_active

    @staticmethod
    def _limit_reached(counts: dict[str, int], key: str, limit: int) -> bool:
        return limit > 0 and counts.get(key, 0) >= limit

    @staticmethod
    def _increment(counts: dict[str, int], key: str) -> None:
        counts[key] = counts.get(key, 0) + 1

    @staticmethod
    def _decrement(counts: dict[str, int], key: str) -> None:
        next_count = counts.get(key, 0) - 1
        if next_count <= 0:
            counts.pop(key, None)
            return
        counts[key] = next_count


download_limiter = DownloadAccessManager()
