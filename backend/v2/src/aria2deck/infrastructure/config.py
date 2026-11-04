from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from ..domain.errors import DomainError, ErrorCode
from ..domain.settings import SystemSettings


@dataclass(frozen=True, slots=True)
class Settings:
    database_path: Path
    download_directory: Path
    static_directory: Path
    debug: bool
    aria2_rpc_url: str = "http://127.0.0.1:6800/jsonrpc"
    internal_base_url: str = "http://127.0.0.1:8001"
    aria2_rpc_secret: str | None = field(default=None, repr=False)
    share_jwt_secret: str | None = field(default=None, repr=False)
    credential_pepper: str | None = field(default=None, repr=False)
    initial_admin_password: str | None = field(default=None, repr=False)
    session_ttl_seconds: int = 43200

    @property
    def instance_lock_path(self) -> Path:
        return self.database_path.with_name(self.database_path.name + ".lock")

    @classmethod
    def from_env(cls, environment: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if environment is None else environment
        configured_data_directory = env.get("ARIA2DECK_DATA_DIR")
        if configured_data_directory is not None and not configured_data_directory.strip():
            raise DomainError(ErrorCode.CONFIGURATION_INVALID, "V2 数据目录不能为空")
        data_directory = (
            Path(configured_data_directory).expanduser()
            if configured_data_directory
            else Path.home() / ".local" / "share" / "aria2deck"
        ).resolve()
        database_value = env.get("ARIA2DECK_DATABASE_PATH")
        if database_value is not None and not database_value.strip():
            raise DomainError(ErrorCode.CONFIGURATION_INVALID, "V2 数据库路径不能为空")
        debug = env.get("ARIA2DECK_DEBUG", "false").lower() == "true"
        share_secret = env.get("ARIA2DECK_SHARE_JWT_SECRET")
        pepper = env.get("ARIA2DECK_CREDENTIAL_PEPPER")
        if not debug:
            if share_secret is None or pepper is None or len(share_secret) < 32 or len(pepper) < 32:
                raise DomainError(
                    ErrorCode.CONFIGURATION_INVALID,
                    "生产环境必须配置至少 32 字节的分享密钥和凭证密钥",
                )
            if share_secret == pepper:
                raise DomainError(ErrorCode.CONFIGURATION_INVALID, "分享密钥和凭证密钥不能相同")
        aria2_rpc_url = env.get(
            "ARIA2DECK_ARIA2_RPC_URL", "http://127.0.0.1:6800/jsonrpc"
        )
        aria2_rpc_secret = env.get("ARIA2DECK_ARIA2_RPC_SECRET")
        internal_base_url = env.get(
            "ARIA2DECK_INTERNAL_BASE_URL", "http://127.0.0.1:8001"
        ).rstrip("/")
        try:
            internal = urlsplit(internal_base_url)
            _ = internal.port
        except ValueError:
            raise DomainError(ErrorCode.CONFIGURATION_INVALID, "内部网关地址无效") from None
        if (
            internal.scheme not in {"http", "https"}
            or not internal.hostname
            or internal.username is not None
            or internal.password is not None
            or internal.query
            or internal.fragment
        ):
            raise DomainError(ErrorCode.CONFIGURATION_INVALID, "内部网关地址无效")
        try:
            SystemSettings(
                aria2_rpc_url=aria2_rpc_url, aria2_rpc_secret=aria2_rpc_secret
            )
        except DomainError:
            raise DomainError(
                ErrorCode.CONFIGURATION_INVALID, "aria2 RPC 地址或密钥配置无效"
            ) from None
        return cls(
            database_path=(
                Path(database_value).expanduser()
                if database_value is not None
                else data_directory / "v2.db"
            ),
            download_directory=Path(
                env.get("ARIA2DECK_DOWNLOAD_DIR", str(data_directory / "downloads"))
            ).expanduser(),
            static_directory=Path(env.get("ARIA2DECK_STATIC_DIR", "static")),
            debug=debug,
            aria2_rpc_url=aria2_rpc_url,
            internal_base_url=internal_base_url,
            aria2_rpc_secret=aria2_rpc_secret,
            share_jwt_secret=share_secret,
            credential_pepper=pepper,
            initial_admin_password=env.get("ARIA2DECK_INITIAL_ADMIN_PASSWORD") or None,
        )

    def ensure_directories(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.download_directory.mkdir(parents=True, exist_ok=True)
