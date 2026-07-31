from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings


BASE_DIR = Path(__file__).resolve().parents[2]
DEFAULT_SECRET_KEY = "aria2deck-default-secret-key-change-in-production"
SHARE_JWT_SECRET_ENV = "ARIA2DECK_SHARE_JWT_SECRET"
LEGACY_SHARE_JWT_SECRET_ENV = "ARIA2C_SECRET_KEY"
INITIAL_ADMIN_PASSWORD_ENV = "ARIA2DECK_INITIAL_ADMIN_PASSWORD"
MIN_INITIAL_ADMIN_PASSWORD_LENGTH = 16


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
    host: str = "0.0.0.0"
    port: int = 8001
    secret_key: str = Field(
        default=DEFAULT_SECRET_KEY,
        validation_alias=AliasChoices(SHARE_JWT_SECRET_ENV, LEGACY_SHARE_JWT_SECRET_ENV),
    )
    initial_admin_password: str = Field(
        default="",
        validation_alias=INITIAL_ADMIN_PASSWORD_ENV,
    )

    class Config:
        env_prefix = "ARIA2C_"


settings = Settings()


def get_initial_admin_password() -> str:
    password = settings.initial_admin_password
    if len(password) < MIN_INITIAL_ADMIN_PASSWORD_LENGTH or password.isspace():
        raise RuntimeError(
            f"{INITIAL_ADMIN_PASSWORD_ENV} 未配置或长度不足 "
            f"{MIN_INITIAL_ADMIN_PASSWORD_LENGTH} 个字符，无法创建初始管理员。"
        )
    return password


def check_secret_key() -> None:
    """Raise if secret_key is missing or insecure in non-debug mode."""
    if not settings.debug and (
        not settings.secret_key.strip()
        or settings.secret_key == DEFAULT_SECRET_KEY
    ):
        raise RuntimeError(
            f"{SHARE_JWT_SECRET_ENV} 未配置或使用了默认值，生产环境禁止启动。"
            f"请通过环境变量 {SHARE_JWT_SECRET_ENV} 配置安全密钥。"
        )
