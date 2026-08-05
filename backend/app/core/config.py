from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings


BASE_DIR = Path(__file__).resolve().parents[2]
DEFAULT_SECRET_KEY = "aria2deck-default-secret-key-change-in-production"
SHARE_JWT_SECRET_ENV = "ARIA2DECK_SHARE_JWT_SECRET"
LEGACY_SHARE_JWT_SECRET_ENV = "ARIA2C_SECRET_KEY"
INITIAL_ADMIN_PASSWORD_ENV = "ARIA2DECK_INITIAL_ADMIN_PASSWORD"
CREDENTIAL_PEPPER_ENV = "ARIA2DECK_CREDENTIAL_PEPPER"
INTERNAL_BASE_URL_ENV = "ARIA2DECK_INTERNAL_BASE_URL"
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
    internal_base_url: str = Field(
        default="",
        validation_alias=INTERNAL_BASE_URL_ENV,
    )
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

    class Config:
        env_prefix = "ARIA2C_"


settings = Settings()


def get_credential_pepper() -> str:
    current = settings.credential_pepper or settings.secret_key
    if not current.strip() or len(current.encode("utf-8")) < MIN_SECRET_KEY_BYTES:
        raise RuntimeError(
            f"{CREDENTIAL_PEPPER_ENV} 未配置或少于 {MIN_SECRET_KEY_BYTES} 字节。"
        )
    return current


def get_internal_base_url() -> str:
    from ipaddress import ip_address
    from urllib.parse import urlsplit, urlunsplit

    raw_url = settings.internal_base_url.strip() or f"http://127.0.0.1:{settings.port}"
    try:
        parsed = urlsplit(raw_url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise RuntimeError(f"{INTERNAL_BASE_URL_ENV} 格式无效") from exc
    if port is not None and port <= 0:
        raise RuntimeError(f"{INTERNAL_BASE_URL_ENV} 端口无效")
    if (
        parsed.scheme not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise RuntimeError(f"{INTERNAL_BASE_URL_ENV} 必须是无凭据、无路径的 HTTP(S) 地址")
    if parsed.scheme == "http":
        try:
            address = ip_address(hostname)
        except ValueError:
            if "." in hostname and hostname.lower() != "localhost":
                raise RuntimeError(
                    f"{INTERNAL_BASE_URL_ENV} 使用 HTTP 时必须指向 loopback、内网 IP 或单标签服务名"
                )
        else:
            if (
                address.is_unspecified
                or address.is_link_local
                or address.is_multicast
                or address.is_reserved
            ):
                raise RuntimeError(
                    f"{INTERNAL_BASE_URL_ENV} 指向不可用的主机地址"
                )
            if not (address.is_loopback or address.is_private):
                raise RuntimeError(
                    f"{INTERNAL_BASE_URL_ENV} 使用公网地址时必须启用 HTTPS"
                )
    netloc = f"[{hostname}]" if ":" in hostname else hostname
    if port is not None:
        netloc = f"{netloc}:{port}"
    return urlunsplit((parsed.scheme, netloc, "", "", ""))


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
    if settings.credential_pepper == secret:
        raise RuntimeError(
            f"{CREDENTIAL_PEPPER_ENV} 不得与 {SHARE_JWT_SECRET_ENV} 使用相同值；"
            "两者职责不同，应分别生成独立随机密钥。"
        )
    get_credential_pepper()
    if settings.dev_reset_admin_password:
        raise RuntimeError(
            "ARIA2C_DEV_RESET_ADMIN_PASSWORD 仅允许在调试模式下启用。"
        )
