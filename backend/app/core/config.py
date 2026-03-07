from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings


BASE_DIR = Path(__file__).resolve().parents[2]
DEFAULT_SECRET_KEY = "aria2deck-default-secret-key-change-in-production"
SHARE_JWT_SECRET_ENV = "ARIA2DECK_SHARE_JWT_SECRET"
LEGACY_SHARE_JWT_SECRET_ENV = "ARIA2C_SECRET_KEY"


class Settings(BaseSettings):
    app_name: str = "aria2-controler"
    debug: bool = False
    dev_reset_admin_password: bool = False
    database_path: str = str(BASE_DIR / "data" / "app.db")
    session_cookie_name: str = "aria2_session"
    session_ttl_seconds: int = 60 * 60 * 12
    aria2_rpc_url: str = "http://localhost:6800/jsonrpc"
    aria2_rpc_secret: str = ""
    aria2_poll_interval: float = 2.0
    download_dir: str = str(BASE_DIR / "downloads")
    secret_key: str = Field(
        default=DEFAULT_SECRET_KEY,
        validation_alias=AliasChoices(SHARE_JWT_SECRET_ENV, LEGACY_SHARE_JWT_SECRET_ENV),
    )

    # Rate limits (times per minute)
    rate_limit_login: int = 5
    rate_limit_create_task: int = 60
    rate_limit_create_torrent: int = 20
    rate_limit_download_file: int = 300
    rate_limit_create_pack: int = 10
    rate_limit_aria2_test: int = 60
    rate_limit_list_files: int = 30
    rate_limit_rpc: int = 600

    class Config:
        env_prefix = "ARIA2C_"


settings = Settings()


def check_secret_key() -> None:
    """Raise if secret_key is still the insecure default in non-debug mode."""
    if not settings.debug and settings.secret_key == DEFAULT_SECRET_KEY:
        raise RuntimeError(
            f"{SHARE_JWT_SECRET_ENV} 使用了默认值，生产环境禁止启动。"
            f"请通过环境变量 {SHARE_JWT_SECRET_ENV} 配置安全密钥。"
        )
