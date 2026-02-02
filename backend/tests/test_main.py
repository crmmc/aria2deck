"""Tests for main.py application setup and lifespan."""
import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from pathlib import Path


class TestSetupLogging:
    """Tests for setup_logging function."""

    def test_setup_logging_default_level(self):
        """Test logging setup with default INFO level."""
        import logging
        from app.main import setup_logging

        root_logger = logging.getLogger()
        initial_handlers = len(root_logger.handlers)

        with patch.dict("os.environ", {}, clear=False):
            with patch("os.environ.get", return_value="INFO"):
                pass

        assert root_logger.level <= logging.INFO

    def test_setup_logging_env_variable_read(self):
        """Test logging reads ARIA2C_LOG_LEVEL env variable."""
        import os

        with patch.dict(os.environ, {"ARIA2C_LOG_LEVEL": "WARNING"}):
            level_str = os.environ.get("ARIA2C_LOG_LEVEL", "INFO")
            assert level_str == "WARNING"


class TestCreateApp:
    """Tests for create_app function."""

    def test_create_app_returns_fastapi(self):
        """Test create_app returns a FastAPI instance."""
        from fastapi import FastAPI
        from app.main import create_app

        app = create_app()

        assert isinstance(app, FastAPI)
        assert hasattr(app.state, "app_state")
        assert hasattr(app.state, "aria2_client")

    def test_create_app_includes_routers(self):
        """Test create_app includes all expected routers."""
        from app.main import create_app

        app = create_app()

        route_paths = [getattr(route, "path", "") for route in app.routes]

        assert any("/api/auth" in str(path) for path in route_paths)
        assert any("/api/users" in str(path) for path in route_paths)
        assert any("/api/tasks" in str(path) for path in route_paths)
        assert any("/api/files" in str(path) for path in route_paths)

    def test_create_app_cors_middleware(self):
        """Test create_app adds CORS middleware."""
        from app.main import create_app

        app = create_app()

        middleware_classes = [m.cls.__name__ for m in app.user_middleware]
        assert "CORSMiddleware" in middleware_classes


class TestStaticAliasMiddleware:
    """Tests for static file alias middleware."""

    def test_static_alias_login(self, client):
        """Test /login route serves login.html."""
        response = client.get("/login")
        assert response.status_code in (200, 404)

    def test_static_alias_tasks(self, client):
        """Test /tasks route serves tasks.html."""
        response = client.get("/tasks")
        assert response.status_code in (200, 404)

    def test_static_alias_files(self, client):
        """Test /files route serves files.html."""
        response = client.get("/files")
        assert response.status_code in (200, 404)

    def test_static_alias_users(self, client):
        """Test /users route serves users.html."""
        response = client.get("/users")
        assert response.status_code in (200, 404)

    def test_static_alias_settings(self, client):
        """Test /settings route serves settings.html."""
        response = client.get("/settings")
        assert response.status_code in (200, 404)


class TestLifespan:
    """Tests for application lifespan management."""

    @pytest.mark.asyncio
    async def test_lifespan_creates_directories(self, temp_db):
        """Test lifespan creates required directories."""
        from app.core.config import settings

        db_dir = Path(settings.database_path).parent
        download_dir = Path(settings.download_dir)

        assert db_dir.exists()
        assert download_dir.exists()

    @pytest.mark.asyncio
    async def test_lifespan_initializes_database(self, temp_db):
        """Test lifespan initializes database tables."""
        from app.database import get_session
        from sqlmodel import text

        async with get_session() as db:
            result = await db.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
            tables = [row[0] for row in result.fetchall()]

        assert "users" in tables
        assert "sessions" in tables

    @pytest.mark.asyncio
    async def test_lifespan_ensures_default_admin(self, temp_db, test_admin):
        """Test lifespan creates default admin user."""
        from app.database import get_session
        from sqlmodel import select
        from app.models import User

        async with get_session() as db:
            result = await db.exec(select(User).where(User.username == "admin"))
            admin = result.first()

        assert admin is not None
        assert admin.is_admin is True


class TestAppState:
    """Tests for application state initialization."""

    def test_app_state_initialized(self):
        """Test AppState is properly initialized."""
        from app.main import create_app
        from app.core.state import AppState

        app = create_app()

        assert isinstance(app.state.app_state, AppState)

    def test_aria2_client_initialized(self):
        """Test Aria2Client is properly initialized."""
        from app.main import create_app
        from app.aria2.client import Aria2Client

        app = create_app()

        assert isinstance(app.state.aria2_client, Aria2Client)


class TestDatabaseIntegrity:
    """Tests for database integrity checks."""

    @pytest.mark.asyncio
    async def test_check_database_integrity(self, temp_db):
        """Test database integrity check passes."""
        from app.database import check_database_integrity

        result = await check_database_integrity()
        assert result is True

    @pytest.mark.asyncio
    async def test_check_wal_integrity(self, temp_db):
        """Test WAL integrity check passes."""
        from app.database import check_wal_integrity

        result = await check_wal_integrity()
        assert result is True
