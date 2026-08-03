from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings


BASE_DIR = Path(__file__).resolve().parents[2]
DEFAULT_SECRET_KEY = "aria2deck-default-secret-key-change-in-production"
SHARE_JWT_SECRET_ENV = "ARIA2DECK_SHARE_JWT_SECRET"
LEGACY_SHARE_JWT_SECRET_ENV = "ARIA2C_SECRET_KEY"
INITIAL_ADMIN_PASSWORD_ENV = "ARIA2DECK_INITIAL_ADMIN_PASSWORD"
CREDENTIAL_PEPPER_ENV = "ARIA2DECK_CREDENTIAL_PEPPER"
PREVIOUS_CREDENTIAL_PEPPER_ENV = "ARIA2DECK_PREVIOUS_CREDENTIAL_PEPPER"
MIN_INITIAL_ADMIN_PASSWORD_LENGTH = 16
MIN_SECRET_KEY_BYTES = 32


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
    cors_origins: str = ""
    allow_null_origin: bool = False
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
    credential_pepper: str = Field(
        default="",
        validation_alias=CREDENTIAL_PEPPER_ENV,
    )
    previous_credential_pepper: str = Field(
        default="",
        validation_alias=PREVIOUS_CREDENTIAL_PEPPER_ENV,
    )

    class Config:
        env_prefix = "ARIA2C_"


settings = Settings()


def credential_peppers() -> tuple[str, str | None]:
    current = settings.credential_pepper or settings.secret_key
    if not current.strip() or len(current.encode("utf-8")) < MIN_SECRET_KEY_BYTES:
        raise RuntimeError(
            f"{CREDENTIAL_PEPPER_ENV} 未配置或少于 {MIN_SECRET_KEY_BYTES} 字节。"
        )
    previous = settings.previous_credential_pepper
    if not previous:
        return current, None
    if not previous.strip() or len(previous.encode("utf-8")) < MIN_SECRET_KEY_BYTES:
        raise RuntimeError(
            f"{PREVIOUS_CREDENTIAL_PEPPER_ENV} 少于 {MIN_SECRET_KEY_BYTES} 字节。"
        )
    if previous == current:
        raise RuntimeError("当前与旧 credential pepper 不能相同。")
    return current, previous


def get_initial_admin_password() -> str:
    password = settings.initial_admin_password
    if len(password) < MIN_INITIAL_ADMIN_PASSWORD_LENGTH or password.isspace():
        raise RuntimeError(
            f"{INITIAL_ADMIN_PASSWORD_ENV} 未配置或长度不足 "
            f"{MIN_INITIAL_ADMIN_PASSWORD_LENGTH} 个字符，无法创建初始管理员。"
        )
    return password


def check_secret_key() -> None:
    """Raise if production-only settings are insecure."""
    if settings.debug:
        return

    secret = settings.secret_key
    if (
        not secret.strip()
        or secret.strip() == DEFAULT_SECRET_KEY
        or len(secret.encode("utf-8")) < MIN_SECRET_KEY_BYTES
    ):
        raise RuntimeError(
            f"{SHARE_JWT_SECRET_ENV} 未配置、使用了默认值或少于 "
            f"{MIN_SECRET_KEY_BYTES} 字节，生产环境禁止启动。"
            f"请通过环境变量 {SHARE_JWT_SECRET_ENV} 配置安全密钥。"
        )
    if not settings.credential_pepper:
        raise RuntimeError(
            f"{CREDENTIAL_PEPPER_ENV} 未配置，生产环境禁止启动。"
        )
    credential_peppers()
    if settings.dev_reset_admin_password:
        raise RuntimeError(
            "ARIA2C_DEV_RESET_ADMIN_PASSWORD 仅允许在调试模式下启用。"
        )
