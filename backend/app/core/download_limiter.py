"""下载接口频率限制 + 三层并发连接限制"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

# ── 下载配置内存缓存 ──────────────────────────────────────────

class DownloadConfig:
    """下载相关配置的内存缓存，更新时主动刷新"""

    def __init__(self) -> None:
        self.rate_limit: int = 300           # 频率限制（次/分钟，0=不限制）
        self.max_connections: int = 100      # 全局最大并发连接数（0=不限制）
        self.per_user_connections: int = 16  # 单用户最大并发连接数（0=不限制）
        self.per_file_connections: int = 8   # 单文件最大并发连接数（0=不限制）

    async def load_from_db(self) -> None:
        """启动时从数据库加载配置"""
        from app.database import get_session
        from app.models import Config
        from sqlmodel import select

        mapping = {
            "download_rate_limit": ("rate_limit", int, 300),
            "download_max_connections": ("max_connections", int, 100),
            "download_per_user_connections": ("per_user_connections", int, 16),
            "download_per_file_connections": ("per_file_connections", int, 8),
        }

        async with get_session() as db:
            for db_key, (attr, conv, default) in mapping.items():
                result = await db.exec(select(Config).where(Config.key == db_key))
                row = result.first()
                try:
                    setattr(self, attr, conv(row.value) if row else default)
                except (ValueError, TypeError):
                    setattr(self, attr, default)

        logger.info(
            "下载配置已加载 rate_limit=%s max_conn=%s per_user=%s per_file=%s",
            self.rate_limit, self.max_connections,
            self.per_user_connections, self.per_file_connections,
        )

    async def refresh(self) -> None:
        """管理员更新配置后主动刷新"""
        await self.load_from_db()


download_config = DownloadConfig()


# ── 并发连接追踪器 ────────────────────────────────────────────

class DownloadLimiter:
    """三层并发连接限制：全局 / 单用户 / 单文件"""

    def __init__(self) -> None:
        self._global_count: int = 0
        self._user_counts: dict[int, int] = {}
        self._user_file_counts: dict[str, int] = {}
        self._lock: asyncio.Lock = asyncio.Lock()

    async def try_acquire(
        self,
        global_limit: int,
        user_id: int | None = None,
        per_user_limit: int = 0,
        file_hash: str | None = None,
        per_file_limit: int = 0,
    ) -> bool:
        """尝试获取连接许可，返回 True 表示成功"""
        async with self._lock:
            # 全局限制
            if global_limit > 0 and self._global_count >= global_limit:
                return False
            # 单用户限制
            if user_id is not None and per_user_limit > 0:
                if self._user_counts.get(user_id, 0) >= per_user_limit:
                    return False
            # 单文件限制
            uf_key: str | None = None
            if user_id is not None and file_hash is not None and per_file_limit > 0:
                uf_key = f"{user_id}:{file_hash}"
                if self._user_file_counts.get(uf_key, 0) >= per_file_limit:
                    return False

            # 全部通过，递增计数
            self._global_count += 1
            if user_id is not None:
                self._user_counts[user_id] = self._user_counts.get(user_id, 0) + 1
            if uf_key is not None:
                self._user_file_counts[uf_key] = self._user_file_counts.get(uf_key, 0) + 1
            return True

    async def release(
        self,
        user_id: int | None = None,
        file_hash: str | None = None,
    ) -> None:
        """释放连接许可"""
        async with self._lock:
            self._global_count = max(0, self._global_count - 1)
            if user_id is not None:
                cnt = self._user_counts.get(user_id, 0) - 1
                if cnt <= 0:
                    self._user_counts.pop(user_id, None)
                else:
                    self._user_counts[user_id] = cnt
            if user_id is not None and file_hash is not None:
                uf_key = f"{user_id}:{file_hash}"
                cnt = self._user_file_counts.get(uf_key, 0) - 1
                if cnt <= 0:
                    self._user_file_counts.pop(uf_key, None)
                else:
                    self._user_file_counts[uf_key] = cnt


download_limiter = DownloadLimiter()
