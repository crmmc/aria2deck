# ruff: noqa: E402

import asyncio
import logging
import os
import time
from collections.abc import Awaitable
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import FastAPI, Request
from sqlalchemy import text


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

from app.http.request_body_limit import (
    MAX_HTTP_REQUEST_BODY_BYTES,
    RequestBodyLimitMiddleware,
)

from app.aria2.client import Aria2Client
from app.aria2.gateway import get_aria2_client, update_cached_aria2_config
from app.aria2.listener import listen_aria2_events
from app.aria2.sync import sync_tasks
from app.core.config import settings
from app.db.bootstrap import bootstrap_database
from app.db.engine import (
    check_database_integrity,
    check_wal_integrity,
    dispose_engine,
    get_engine,
)
from app.repositories.auth import (
    count_admins,
    create_user,
    get_user_by_username,
    update_user,
)
from app.routers import (
    aria2_rpc,
    auth,
    config,
    files,
    health,
    history,
    internal_fetch,
    shares,
    stats,
    storage,
    system,
    tasks,
    users,
    ws,
)
from app.services.repair import (
    purge_terminal_residual_gids,
    rebuild_active_download_accounting,
    run_startup_repair,
)
from app.services.storage import verify_download_dir_writable


async def ensure_default_admin_v0() -> None:
    from app.core.config import get_initial_admin_password
    from app.core.security import derive_client_password_hash, hash_password

    if await count_admins() > 0:
        return
    client_hash = derive_client_password_hash(get_initial_admin_password(), "admin")
    await create_user(
        username="admin",
        password_hash=hash_password(client_hash),
        is_admin=True,
        quota_bytes=100 * 1024 * 1024 * 1024,
        is_initial_password=True,
    )


async def reset_admin_password_for_dev_v0() -> bool:
    from app.core.config import get_initial_admin_password
    from app.core.security import derive_client_password_hash, hash_password

    existing = await get_user_by_username("admin")
    if not existing:
        return False
    client_hash = derive_client_password_hash(
        get_initial_admin_password(), str(existing["username"])
    )
    await update_user(
        existing["id"],
        password_hash=hash_password(client_hash),
        is_initial_password=True,
    )
    return True


async def _run_background_task(name: str, worker: Awaitable[None]) -> None:
    try:
        await worker
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("后台任务意外退出: %s", name)
        raise


def redact_url_for_log(url: str) -> str:
    """Return only the URL path so logs cannot expose query-string secrets."""
    return urlsplit(url).path or "/"


def _register_background_worker(
    app: FastAPI,
    name: str,
    task: asyncio.Task[None],
) -> None:
    worker: dict[str, Any] = {
        "task": task,
        "status": "running",
        "started_at": time.monotonic(),
        "last_observed_at": time.monotonic(),
        "error": None,
    }
    app.state.background_workers[name] = worker

    def record_completion(completed: asyncio.Task[None]) -> None:
        worker["last_observed_at"] = time.monotonic()
        if completed.cancelled():
            worker["status"] = "cancelled"
            return
        error = completed.exception()
        worker["status"] = "failed" if error else "stopped"
        worker["error"] = (
            f"{name} 异常退出: {type(error).__name__}"
            if error
            else f"{name} 意外结束"
        )

    task.add_done_callback(record_completion)


async def _database_ready() -> bool:
    try:
        async with get_engine().connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception:
        return False
    return True


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理器"""
    background_tasks: list[asyncio.Task[None]] = []
    app.state.background_workers = {}
    singleton_lease = None

    async def safe_startup_repair() -> bool:
        try:
            result = await run_startup_repair()
            if not result["safe_for_cleanup"]:
                logger.error(
                    "启动修复存在未解决项: unresolved=%d errors=%d",
                    result["unresolved_files"],
                    len(result["errors"]),
                )
                return False
            return True
        except Exception:
            logger.exception("启动修复任务失败")
            return False

    async def safe_orphan_cleanup() -> None:
        try:
            from app.services.orphan_cleanup import cleanup_orphan_files

            await cleanup_orphan_files()
        except Exception:
            logger.exception("孤儿文件清理失败")

    try:
        from app.services.singleton_lease import ApplicationSingletonLease

        singleton_lease = ApplicationSingletonLease.acquire()
        Path(settings.database_path).parent.mkdir(parents=True, exist_ok=True)
        verify_download_dir_writable()
        from app.core.config import check_secret_key, get_internal_base_url

        check_secret_key()
        get_internal_base_url()
        await bootstrap_database()

        if not await check_database_integrity():
            raise RuntimeError(
                "数据库完整性检查失败，请检查日志。可能需要从备份恢复数据库。"
            )
        if not await check_wal_integrity():
            logger.warning(
                "WAL 文件检查发现问题，但不影响启动。建议检查磁盘空间和文件系统。"
            )

        from app.services.settings_service import load_runtime_config, refresh_aria2_config

        await refresh_aria2_config()
        await load_runtime_config()

        from app.services.aria2_lifecycle_service import (
            reconcile_legacy_http_downloads_v0,
        )

        await reconcile_legacy_http_downloads_v0(get_aria2_client())

        await ensure_default_admin_v0()

        if settings.dev_reset_admin_password:
            await reset_admin_password_for_dev_v0()

        from app.services.deletion_cleanup import DeletionCleanupManager
        from app.services.pack import PackTaskManager

        await DeletionCleanupManager.recover_startup()
        await PackTaskManager.recover_startup()

        residual = await purge_terminal_residual_gids(get_aria2_client())
        logger.info(
            "启动 residual 清理完成: found=%d purged=%d failed=%d",
            residual["found"],
            residual["purged"],
            residual["failed"],
        )

        accounting = await rebuild_active_download_accounting(get_aria2_client())
        logger.info(
            "启动下载预算重建完成: rebuilt=%d failed=%d",
            accounting["rebuilt"],
            accounting["failed"],
        )

        if await safe_startup_repair():
            await safe_orphan_cleanup()
        else:
            logger.warning("启动修复未安全完成，跳过孤儿文件清理")

        await DeletionCleanupManager.start()
        await PackTaskManager.start_dispatcher()
        await PackTaskManager.submit_pending()
        deletion_task = DeletionCleanupManager._worker_task
        pack_task = PackTaskManager._dispatcher_task
        if deletion_task is None or pack_task is None:
            raise RuntimeError("后台维护任务未能启动")
        _register_background_worker(app, "deletion", deletion_task)
        _register_background_worker(app, "pack", pack_task)

        sync_task = asyncio.create_task(
            _run_background_task(
                "sync_tasks", sync_tasks(settings.aria2_poll_interval)
            ),
            name="sync_tasks",
        )
        listener_task = asyncio.create_task(
            _run_background_task("listen_aria2_events", listen_aria2_events()),
            name="listen_aria2_events",
        )
        background_tasks.extend((sync_task, listener_task))
        _register_background_worker(app, "sync", sync_task)
        _register_background_worker(app, "listener", listener_task)
        yield
    finally:
        for task in background_tasks:
            task.cancel()
        if background_tasks:
            await asyncio.gather(*background_tasks, return_exceptions=True)
        try:
            from app.services.deletion_cleanup import DeletionCleanupManager
            from app.services.pack import PackTaskManager

            await DeletionCleanupManager.shutdown()
            await PackTaskManager.shutdown()
            await Aria2Client.close_session()
        finally:
            try:
                await dispose_engine()
            finally:
                if singleton_lease is not None:
                    singleton_lease.release()


def create_app() -> FastAPI:
    app = FastAPI(title=settings.app_name, debug=settings.debug, lifespan=lifespan)
    app.add_middleware(
        RequestBodyLimitMiddleware,
        max_body_bytes=MAX_HTTP_REQUEST_BODY_BYTES,
    )
    update_cached_aria2_config(
        rpc_url=settings.aria2_rpc_url,
        rpc_secret=settings.aria2_rpc_secret,
    )
    app.state.aria2_client = Aria2Client(
        settings.aria2_rpc_url, settings.aria2_rpc_secret
    )
    app.state.database_ready = _database_ready

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
        target = redact_url_for_log(str(request.url))
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

        log_msg = "[HTTP] %s %s -> %s %.1fms request_id=%s user_id=%s ip=%s"
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
    if settings.debug or settings.allow_null_origin:
        cors_origins.append("null")
    # 从环境变量添加额外的 CORS 域名（逗号分隔）
    extra_origins = settings.cors_origins
    if extra_origins:
        for origin in extra_origins.split(","):
            origin = origin.strip()
            if origin == "null" and not (settings.debug or settings.allow_null_origin):
                continue
            if origin and origin not in cors_origins:
                cors_origins.append(origin)
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
    app.include_router(tasks.v2_router)
    app.include_router(files.router)
    app.include_router(history.router)
    app.include_router(history.v2_router)
    app.include_router(stats.router)
    app.include_router(config.router)
    app.include_router(storage.router)
    app.include_router(health.router)
    app.include_router(system.router)
    app.include_router(ws.router)
    app.include_router(internal_fetch.router)
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
