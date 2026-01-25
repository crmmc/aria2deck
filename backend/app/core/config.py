from pathlib import Path

from pydantic_settings import BaseSettings


BASE_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    app_name: str = "aria2-controler"
    debug: bool = False
    database_path: str = str(BASE_DIR / "data" / "app.db")
    session_cookie_name: str = "aria2_session"
    session_ttl_seconds: int = 60 * 60 * 12
    aria2_rpc_url: str = "http://localhost:6800/jsonrpc"
    aria2_rpc_secret: str = ""
    aria2_poll_interval: float = 2.0
    download_dir: str = str(BASE_DIR / "downloads")
    hook_secret: str = ""  # aria2 回调认证密钥（必须配置，否则 Hook 接口返回 503）
    admin_password: str = "123456"  # 初始管理员密码

    class Config:
        env_prefix = "ARIA2C_"


settings = Settings()
