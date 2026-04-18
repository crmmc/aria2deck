import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, Request


logger = logging.getLogger(__name__)


def setup_logging():
    """配置日志：同时输出到控制台和文件"""
    # 从环境变量获取日志级别，默认 INFO
    log_level_str = os.environ.get("ARIA2C_LOG_LEVEL", "INFO").upper()
    log_level = getattr(logging, log_level_str, logging.INFO)

    # 日志格式
    log_format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"
    formatter = logging.Formatter(log_format, datefmt=date_format)

    # 根 logger
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    for handler in list(root_logger.handlers):
        if getattr(handler, "_aria2deck_handler", False):
            root_logger.removeHandler(handler)
            handler.close()

    # 控制台 handler
    console_handler = logging.StreamHandler()
    setattr(console_handler, "_aria2deck_handler", True)
    console_handler.setLevel(log_level)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # 文件 handler（日志目录：data/logs）
    from app.core.config import settings
    log_dir = Path(settings.database_path).parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "app.log"

    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=5,
        encoding="utf-8",
    )
    setattr(file_handler, "_aria2deck_handler", True)
    file_handler.setLevel(log_level)
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    logging.info(f"日志已配置: 级别={log_level_str}, 文件={log_file}")


# 初始化日志
setup_logging()
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.aria2.client import Aria2Client
from app.aria2.listener import listen_aria2_events
from app.aria2.sync import sync_tasks
from app.core.config import settings
from app.core.state import AppState
from app.db import ensure_default_admin, init_db, reset_admin_password_for_dev
from app.database import (
    init_db as init_sqlmodel_db,
    get_session,
    init_default_config,
    check_database_integrity,
    check_wal_integrity,
)
from app.routers import (
    aria2_rpc,
    auth,
    config,
    files,
    health,
    history,
    shares,
    stats,
    storage,
    tasks,
    users,
    ws,
)
from app.services.repair import run_startup_repair


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理器"""
    # Startup
    Path(settings.database_path).parent.mkdir(parents=True, exist_ok=True)
    Path(settings.download_dir).mkdir(parents=True, exist_ok=True)

    # Initialize database schema (using old init_db for backward compatibility)
    init_db()

    # Initialize SQLModel tables (creates tables if they don't exist)
    await init_sqlmodel_db()

    # 数据库完整性检查
    db_ok = await check_database_integrity()
    if not db_ok:
        raise RuntimeError("数据库完整性检查失败，请检查日志。可能需要从备份恢复数据库。")

    # WAL 完整性检查
    wal_ok = await check_wal_integrity()
    if not wal_ok:
        logger.warning("WAL 文件检查发现问题，但不影响启动。建议检查磁盘空间和文件系统。")

    # Initialize default config values
    async with get_session() as session:
        await init_default_config(session)

    # Check secret key safety
    from app.core.config import check_secret_key
    check_secret_key()

    # Refresh aria2 config cache from DB
    from app.core.state import refresh_aria2_config
    await refresh_aria2_config(app.state.app_state)

    # 加载下载配置到内存
    from app.core.download_limiter import download_config
    await download_config.load_from_db()

    # 加载频率限制配置到内存
    from app.core.rate_limit_config import rate_limit_config
    await rate_limit_config.load_from_db()

    # Ensure default admin exists
    ensure_default_admin()

    # Development mode helper: reset admin password without clearing DB
    if settings.dev_reset_admin_password:
        reset_admin_password_for_dev()

    async def safe_startup_repair():
        try:
            await run_startup_repair()
        except Exception:
            logger.exception("启动修复任务失败")

    async def safe_orphan_cleanup():
        try:
            from app.services.orphan_cleanup import cleanup_orphan_files
            await cleanup_orphan_files()
        except Exception:
            logger.exception("孤儿文件清理失败")

    async def safe_startup_maintenance():
        """串行执行启动维护任务：先修复再清理，避免竞态"""
        await safe_startup_repair()
        await safe_orphan_cleanup()

    asyncio.create_task(safe_startup_maintenance())

    sync_task = asyncio.create_task(
        sync_tasks(app.state.app_state, settings.aria2_poll_interval)
    )
    listener_task = asyncio.create_task(
        listen_aria2_events(app.state.app_state)
    )
    yield
    # Shutdown
    sync_task.cancel()
    listener_task.cancel()
    try:
        await sync_task
    except asyncio.CancelledError:
        logger.debug("sync_tasks 已取消")
    try:
        await listener_task
    except asyncio.CancelledError:
        logger.debug("listen_aria2_events 已取消")
    # 关闭 aiohttp Session
    await Aria2Client.close_session()


def create_app() -> FastAPI:
    app = FastAPI(title=settings.app_name, debug=settings.debug, lifespan=lifespan)
    app.state.app_state = AppState()
    app.state.aria2_client = Aria2Client(settings.aria2_rpc_url, settings.aria2_rpc_secret)

    def _build_request_target(request: Request) -> str:
        path = request.url.path
        query = request.url.query
        if not query:
            return path
        return f"{path}?{query}"

    def _should_audit_request(path: str) -> bool:
        # 调试模式下记录所有 HTTP 请求，便于排查反代/RPC问题
        if settings.debug:
            return True
        # 非调试模式仅记录 API 请求，避免静态资源日志过多
        return path.startswith("/api")

    @app.middleware("http")
    async def request_audit_middleware(request: Request, call_next):
        path = request.url.path
        if not _should_audit_request(path):
            return await call_next(request)

        request_id = request.headers.get("X-Request-ID") or uuid4().hex
        request.state.request_id = request_id
        method = request.method
        target = _build_request_target(request)
        client_ip = request.client.host if request.client else "unknown"
        start_time = time.perf_counter()

        try:
            response = await call_next(request)
        except Exception:
            duration_ms = (time.perf_counter() - start_time) * 1000
            user_id = getattr(request.state, "auth_user_id", None)
            logger.exception(
                "[HTTP] %s %s -> 500 %.1fms request_id=%s user_id=%s ip=%s",
                method,
                target,
                duration_ms,
                request_id,
                user_id,
                client_ip,
            )
            raise

        duration_ms = (time.perf_counter() - start_time) * 1000
        status_code = response.status_code
        response.headers["X-Request-ID"] = request_id
        user_id = getattr(request.state, "auth_user_id", None)

        log_msg = (
            "[HTTP] %s %s -> %s %.1fms request_id=%s user_id=%s ip=%s"
        )
        log_args = (
            method,
            target,
            status_code,
            duration_ms,
            request_id,
            user_id,
            client_ip,
        )

        if status_code >= 500:
            logger.error(log_msg, *log_args)
        elif status_code >= 400:
            logger.warning(log_msg, *log_args)
        else:
            logger.info(log_msg, *log_args)

        return response

    # CORS 配置
    cors_origins = [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:8080",
        "http://127.0.0.1:8080",
        "https://ariang.mayswind.net",
        "https://ariang.js.org",
    ]
    # 从环境变量添加额外的 CORS 域名（逗号分隔）
    extra_origins = os.environ.get("ARIA2C_CORS_ORIGINS", "")
    if extra_origins:
        for origin in extra_origins.split(","):
            origin = origin.strip()
            if origin and origin not in cors_origins:
                cors_origins.append(origin)
    # 仅在 debug 模式下允许 null origin（本地文件调试）
    if settings.debug:
        cors_origins.append("null")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(auth.router)
    app.include_router(users.router)
    app.include_router(tasks.router)
    app.include_router(files.router)
    app.include_router(history.router)
    app.include_router(stats.router)
    app.include_router(config.router)
    app.include_router(storage.router)
    app.include_router(health.router)
    app.include_router(ws.router)
    app.include_router(aria2_rpc.router)
    app.include_router(shares.router)

    # 静态导出时，Next.js 产物是 *.html 文件。
    # 这里仅在生产静态托管层把无后缀页面路径映射到对应 HTML，
    # 前端源码与开发态路由仍应统一使用无后缀 URL。
    static_dir = Path(__file__).parent.parent / "static"
    if static_dir.exists():
        def html_path(name: str) -> Path:
            return static_dir / name

        alias_map = {
            "/": "tasks.html",
            "/login": "login.html",
            "/tasks": "tasks.html",
            "/tasks/detail": "tasks/detail.html",
            "/files": "files.html",
            "/users": "users.html",
            "/settings": "settings.html",
            "/history": "history.html",
            "/profile": "profile.html",
            "/storage": "storage.html",
            "/shares": "shares.html",
        }

        @app.middleware("http")
        async def static_alias_middleware(request: Request, call_next):
            path = request.url.path.rstrip("/") or "/"
            if path in alias_map:
                target = html_path(alias_map[path])
                if target.exists():
                    return FileResponse(target)
            # 公开分享页面：/s/{code} 统一返回 s/[code].html
            if path.startswith("/s/") and len(path) > 3:
                share_html = static_dir / "s" / "_.html"
                if share_html.exists():
                    return FileResponse(share_html)
            return await call_next(request)

    # 挂载静态文件用于服务前端
    if static_dir.exists():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")

    return app


app = create_app()
