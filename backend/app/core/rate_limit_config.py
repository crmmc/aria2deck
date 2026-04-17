"""API 频率限制配置 - 内存缓存单例，支持数据库热更新"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class RateLimitConfig:
    """API 频率限制配置的内存缓存，更新时主动刷新"""

    def __init__(self) -> None:
        self.login: int = 5                  # 5次/5分钟
        self.create_task: int = 30           # 30次/分钟
        self.create_torrent: int = 20        # 20次/分钟
        self.create_pack: int = 5            # 5次/分钟
        self.aria2_test: int = 10            # 10次/分钟
        self.list_files: int = 60            # 60次/分钟
        self.rpc: int = 300                  # 300次/分钟
        self.change_password: int = 5        # 5次/5分钟

    # DB key → 属性名映射
    _DB_KEY_MAP: dict[str, tuple[str, int]] = {
        "rate_limit_login": ("login", 5),
        "rate_limit_create_task": ("create_task", 30),
        "rate_limit_create_torrent": ("create_torrent", 20),
        "rate_limit_create_pack": ("create_pack", 5),
        "rate_limit_aria2_test": ("aria2_test", 10),
        "rate_limit_list_files": ("list_files", 60),
        "rate_limit_rpc": ("rpc", 300),
        "rate_limit_change_password": ("change_password", 5),
    }

    async def load_from_db(self) -> None:
        """启动时从数据库加载配置"""
        from app.database import get_session
        from app.models import Config
        from sqlmodel import select

        async with get_session() as db:
            for db_key, (attr, default) in self._DB_KEY_MAP.items():
                result = await db.exec(select(Config).where(Config.key == db_key))
                row = result.first()
                try:
                    setattr(self, attr, int(row.value) if row else default)
                except (ValueError, TypeError):
                    setattr(self, attr, default)

        logger.info(
            "频率限制配置已加载 login=%s create_task=%s create_torrent=%s "
            "create_pack=%s aria2_test=%s list_files=%s rpc=%s change_password=%s",
            self.login, self.create_task, self.create_torrent,
            self.create_pack, self.aria2_test, self.list_files,
            self.rpc, self.change_password,
        )

    async def refresh(self) -> None:
        """管理员更新配置后主动刷新"""
        await self.load_from_db()

    def defaults(self) -> dict[str, str]:
        """返回 DB key → 默认值字符串的映射，供 init_default_config 使用"""
        return {db_key: str(default) for db_key, (_, default) in self._DB_KEY_MAP.items()}


rate_limit_config = RateLimitConfig()
