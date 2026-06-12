"""Tests for main.py application setup and lifespan."""

import hashlib
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient


def _client_password_hash(password: str, username: str) -> str:
    salt = hashlib.sha256(username.lower().encode("utf-8")).digest()
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        10000,
    )
    return digest.hex()


class TestSetupLogging:
    """Tests for setup_logging function."""

    def test_setup_logging_default_level(self):
        """Test logging setup with default INFO level."""
        import logging
        from app.main import setup_logging

        setup_logging()
        root_logger = logging.getLogger()

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

    def test_aria2_rpc_allows_null_origin_preflight(self):
        """Test aria2 RPC allows browser clients that send Origin: null."""
        from app.main import create_app

        app = create_app()
        client = TestClient(app)

        response = client.options(
            "/aria2/jsonrpc",
            headers={
                "Origin": "null",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )

        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == "null"
        assert "POST" in response.headers["access-control-allow-methods"]
        assert "content-type" in response.headers[
            "access-control-allow-headers"
        ].lower()


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
        from app.db.engine import transaction
        from sqlalchemy import text

        async with transaction() as conn:
            result = await conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            )
            tables = [row[0] for row in result.fetchall()]

        assert "schema_meta" in tables
        assert "app_settings" in tables
        assert "users" in tables
        assert "sessions" in tables

    @pytest.mark.asyncio
    async def test_lifespan_ensures_default_admin(self, temp_db):
        """Test lifespan creates default admin user."""
        from sqlalchemy import select

        from app.db.engine import transaction
        from app.db.schema import users
        from app.main import ensure_default_admin_v0

        await ensure_default_admin_v0()

        async with transaction() as conn:
            result = await conn.execute(
                select(users).where(users.c.username == "admin")
            )
            admin = result.mappings().first()

        assert admin is not None
        assert admin["is_admin"] == 1
        assert admin["is_initial_password"] == 1

    def test_app_startup_bootstraps_database_and_default_admin(self, tmp_path):
        """Test real FastAPI lifespan creates latest schema and default admin."""
        import sqlite3

        from fastapi.testclient import TestClient

        from app.core.config import settings
        from app.db.engine import reset_engine
        from app.db.schema import SCHEMA_VERSION
        from app.main import create_app

        db_path = tmp_path / "startup.db"
        download_dir = tmp_path / "downloads"
        old_db_path = settings.database_path
        old_download_dir = settings.download_dir
        old_debug = settings.debug
        settings.database_path = str(db_path)
        settings.download_dir = str(download_dir)
        settings.debug = True
        reset_engine()
        try:
            with (
                patch("app.main.sync_tasks", new=AsyncMock()),
                patch("app.main.listen_aria2_events", new=AsyncMock()),
                patch("app.main.run_startup_repair", new=AsyncMock()),
                patch(
                    "app.services.orphan_cleanup.cleanup_orphan_files", new=AsyncMock()
                ),
                TestClient(create_app()) as client,
            ):
                login_response = client.post(
                    "/api/auth/login",
                    json={
                        "username": "admin",
                        "password": _client_password_hash("123456", "admin"),
                    },
                )
                assert login_response.status_code == 200

            conn = sqlite3.connect(db_path)
            try:
                version = conn.execute(
                    "SELECT version FROM schema_meta WHERE id = 1"
                ).fetchone()[0]
                admin = conn.execute(
                    "SELECT is_admin, is_initial_password FROM users WHERE username = 'admin'"
                ).fetchone()
                settings_row = conn.execute(
                    "SELECT id FROM app_settings WHERE id = 1"
                ).fetchone()
            finally:
                conn.close()

            assert version == SCHEMA_VERSION
            assert admin == (1, 1)
            assert settings_row == (1,)
        finally:
            settings.database_path = old_db_path
            settings.download_dir = old_download_dir
            settings.debug = old_debug
            reset_engine()

    @pytest.mark.asyncio
    async def test_dev_reset_admin_password_uses_frontend_hash(self, temp_db):
        """Test dev reset keeps default admin compatible with frontend login hashing."""
        from app.core.security import hash_password, verify_password
        from app.main import ensure_default_admin_v0, reset_admin_password_for_dev_v0
        from app.repositories.auth import get_user_by_username, update_user

        await ensure_default_admin_v0()
        admin = await get_user_by_username("admin")
        assert admin is not None
        await update_user(
            admin["id"],
            password_hash=hash_password("other-password"),
            is_initial_password=False,
        )

        assert await reset_admin_password_for_dev_v0() is True

        reset_admin = await get_user_by_username("admin")
        assert reset_admin is not None
        assert verify_password(
            _client_password_hash("123456", "admin"), reset_admin["password_hash"]
        )
        assert reset_admin["is_initial_password"] == 1

    def test_app_startup_rejects_legacy_schema(self, tmp_path):
        """Test startup refuses a legacy database without schema metadata."""
        import sqlite3

        from fastapi.testclient import TestClient

        from app.core.config import settings
        from app.db.engine import reset_engine
        from app.main import create_app

        db_path = tmp_path / "legacy.db"
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT NOT NULL)"
        )
        conn.commit()
        conn.close()

        old_db_path = settings.database_path
        old_download_dir = settings.download_dir
        settings.database_path = str(db_path)
        settings.download_dir = str(tmp_path / "downloads")
        reset_engine()
        try:
            with pytest.raises(RuntimeError, match="Unsupported database schema"):
                with TestClient(create_app()):
                    pass
        finally:
            settings.database_path = old_db_path
            settings.download_dir = old_download_dir
            reset_engine()

    def test_app_startup_rejects_unwritable_download_dir(self, tmp_path):
        """Test startup refuses a download directory that cannot be write-probed."""
        from fastapi.testclient import TestClient

        from app.core.config import settings
        from app.db.engine import reset_engine
        from app.main import create_app

        db_path = tmp_path / "startup.db"
        download_dir = tmp_path / "downloads"
        old_db_path = settings.database_path
        old_download_dir = settings.download_dir
        old_debug = settings.debug
        settings.database_path = str(db_path)
        settings.download_dir = str(download_dir)
        settings.debug = True
        reset_engine()
        try:
            with patch(
                "app.services.storage.Path.write_bytes",
                side_effect=PermissionError("read-only filesystem"),
            ):
                with pytest.raises(
                    RuntimeError,
                    match="Download directory is not writable.*read-only filesystem",
                ):
                    with TestClient(create_app()):
                        pass
        finally:
            settings.database_path = old_db_path
            settings.download_dir = old_download_dir
            settings.debug = old_debug
            reset_engine()

    def test_app_startup_reports_download_dir_create_failure(self, tmp_path):
        """Test startup wraps download directory creation failures with path details."""
        from fastapi.testclient import TestClient

        from app.core.config import settings
        from app.db.engine import reset_engine
        from app.main import create_app

        db_path = tmp_path / "startup.db"
        download_dir = tmp_path / "downloads"
        old_db_path = settings.database_path
        old_download_dir = settings.download_dir
        old_debug = settings.debug
        settings.database_path = str(db_path)
        settings.download_dir = str(download_dir)
        settings.debug = True
        reset_engine()

        original_mkdir = Path.mkdir

        def fail_download_dir_mkdir(
            self: Path, *args: object, **kwargs: object
        ) -> None:
            if self == download_dir.resolve():
                raise PermissionError("cannot create download directory")
            original_mkdir(self, *args, **kwargs)

        try:
            with patch("app.services.storage.Path.mkdir", fail_download_dir_mkdir):
                with pytest.raises(
                    RuntimeError,
                    match=(
                        "Download directory is not writable.*"
                        "cannot create download directory"
                    ),
                ):
                    with TestClient(create_app()):
                        pass
        finally:
            settings.database_path = old_db_path
            settings.download_dir = old_download_dir
            settings.debug = old_debug
            reset_engine()


class TestApplicationState:
    """Tests for runtime integration state initialization."""

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
        from app.db.engine import check_database_integrity

        result = await check_database_integrity()
        assert result is True

    @pytest.mark.asyncio
    async def test_check_wal_integrity(self, temp_db):
        """Test WAL integrity check passes."""
        from app.db.engine import check_wal_integrity

        result = await check_wal_integrity()
        assert result is True
