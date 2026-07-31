"""请求频率限制配置缓存。"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from typing import Any

logger = logging.getLogger(__name__)


class RateLimitConfig:
    """请求频率限制配置的内存缓存，支持数据库热更新。"""

    _WINDOWS: dict[str, int] = {
        "account_security": 300,
        "authenticated_api": 60,
        "create_task": 60,
        "create_torrent": 60,
        "create_pack": 60,
        "create_share": 60,
        "aria2_test": 60,
        "public_api": 60,
        "share_access": 60,
        "rpc": 60,
    }

    _DB_KEY_MAP: dict[str, tuple[str, int, tuple[str, ...]]] = {
        "rate_limit_account_security": (
            "account_security",
            5,
            ("rate_limit_login", "rate_limit_change_password"),
        ),
        "rate_limit_authenticated_api": (
            "authenticated_api",
            60,
            ("rate_limit_list_files",),
        ),
        "rate_limit_create_task": ("create_task", 30, ()),
        "rate_limit_create_torrent": ("create_torrent", 20, ()),
        "rate_limit_create_pack": ("create_pack", 5, ()),
        "rate_limit_aria2_test": ("aria2_test", 10, ()),
        "rate_limit_public_api": ("public_api", 60, ()),
        "rate_limit_share_access": ("share_access", 5, ()),
        "rate_limit_rpc": ("rpc", 300, ()),
    }

    def __init__(self) -> None:
        self.account_security: int = 5
        self.authenticated_api: int = 60
        self.create_task: int = 30
        self.create_torrent: int = 20
        self.create_pack: int = 5
        self.create_share: int = 10
        self.aria2_test: int = 10
        self.public_api: int = 60
        self.share_access: int = 5
        self.rpc: int = 300

    def load_from_settings(self, row: Mapping[str, Any] | None) -> None:
        for db_key, (attr, default, _legacy_keys) in self._DB_KEY_MAP.items():
            value = row.get(db_key, default) if row else default
            try:
                value = int(value)
            except (TypeError, ValueError):
                value = default
            setattr(self, attr, value)

        logger.info(
            "频率限制配置已加载 account_security=%s authenticated_api=%s "
            "create_task=%s create_torrent=%s create_pack=%s create_share=%s "
            "aria2_test=%s public_api=%s share_access=%s rpc=%s",
            self.account_security,
            self.authenticated_api,
            self.create_task,
            self.create_torrent,
            self.create_pack,
            self.create_share,
            self.aria2_test,
            self.public_api,
            self.share_access,
            self.rpc,
        )

    def defaults(self) -> dict[str, str]:
        """返回新配置键的默认值字符串。"""
        return {
            db_key: str(default) for db_key, (_, default, _) in self._DB_KEY_MAP.items()
        }

    def limit_for(self, scope: str) -> int:
        """根据 scope 返回当前生效的限流值。"""
        return int(getattr(self, scope))

    def window_for(self, scope: str) -> int:
        """根据 scope 返回时间窗口（秒）。"""
        return self._WINDOWS[scope]

    @staticmethod
    def _iter_db_keys(primary: str, legacy_keys: Iterable[str]) -> tuple[str, ...]:
        return (primary, *legacy_keys)


rate_limit_config = RateLimitConfig()
