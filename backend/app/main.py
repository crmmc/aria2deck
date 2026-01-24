import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.aria2.client import Aria2Client
from app.aria2.sync import sync_tasks
from app.core.config import settings
from app.core.state import AppState
from app.db import ensure_default_admin, init_db
from app.database import init_db as init_sqlmodel_db, get_session, init_default_config
from app.routers import aria2_rpc, auth, config, files, hooks, stats, tasks, users, ws


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

    # Initialize default config values
    async with get_session() as session:
        await init_default_config(session)

    # Ensure default admin exists
    ensure_default_admin()

    sync_task = asyncio.create_task(
        sync_tasks(app.state.app_state, settings.aria2_poll_interval)
    )
    yield
    # Shutdown
    sync_task.cancel()
    try:
        await sync_task
    except asyncio.CancelledError:
        pass


def create_app() -> FastAPI:
    app = FastAPI(title=settings.app_name, debug=settings.debug, lifespan=lifespan)
    app.state.app_state = AppState()
    app.state.aria2_client = Aria2Client(settings.aria2_rpc_url, settings.aria2_rpc_secret)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(auth.router)
    app.include_router(users.router)
    app.include_router(tasks.router)
    app.include_router(files.router)
    app.include_router(stats.router)
    app.include_router(config.router)
    app.include_router(hooks.router)
    app.include_router(ws.router)
    app.include_router(aria2_rpc.router)

    # 静态导出时，Next.js 生成的是 /tasks.html 而不是 /tasks/index.html
    # 这里通过中间件统一把无后缀路径映射到对应 HTML，避免直接刷新 404
    static_dir = Path(__file__).parent.parent / "static"
    if static_dir.exists():
        def html_path(name: str) -> Path:
            return static_dir / name

        alias_map = {
            "/login": "login.html",
            "/tasks": "tasks.html",
            "/tasks/detail": "tasks/detail.html",
            "/files": "files.html",
            "/users": "users.html",
            "/settings": "settings.html",
            "/history": "history.html",
            "/profile": "profile.html",
        }

        @app.middleware("http")
        async def static_alias_middleware(request: Request, call_next):
            path = request.url.path.rstrip("/") or "/"
            if path in alias_map:
                target = html_path(alias_map[path])
                if target.exists():
                    return FileResponse(target)
            return await call_next(request)

    # 挂载静态文件用于服务前端
    if static_dir.exists():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")

    return app


app = create_app()
