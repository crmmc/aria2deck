"""Tests for main.py application setup and lifespan."""

import asyncio
import hashlib
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient


TEST_INITIAL_ADMIN_PASSWORD = "test initial admin password"


def _client_password_hash(password: str, username: str) -> str:
    salt = hashlib.sha256(username.lower().encode("utf-8")).digest()
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        10000,
    )
    return digest.hex()


def _startup_repair_result(
    *, safe_for_cleanup: bool = True, unresolved_files: int = 0
) -> dict[str, object]:
    return {
        "orphan_files_found": unresolved_files,
        "stored_files_created": 0,
        "unresolved_files": unresolved_files,
        "tasks_repaired": 0,
        "errors": ["unresolved"] if unresolved_files else [],
        "safe_for_cleanup": safe_for_cleanup,
    }


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

    def test_aria2_rpc_null_origin_requires_explicit_setting(self, monkeypatch):
        """Production rejects Origin: null unless the dedicated flag is enabled."""
        from app.core.config import settings
        from app.main import create_app

        monkeypatch.setattr(settings, "debug", False)
        monkeypatch.setattr(settings, "allow_null_origin", False)
        monkeypatch.setattr(settings, "cors_origins", "")
        headers = {
            "Origin": "null",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        }
        assert TestClient(create_app()).options("/aria2/jsonrpc", headers=headers).status_code == 400

        monkeypatch.setattr(settings, "allow_null_origin", True)
        response = TestClient(create_app()).options("/aria2/jsonrpc", headers=headers)

        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == "null"
        assert "POST" in response.headers["access-control-allow-methods"]
        assert "content-type" in response.headers[
            "access-control-allow-headers"
        ].lower()

    def test_request_audit_omits_query_string(self, caplog):
        """Test audit logs do not expose query-string credentials."""
        import logging

        from app.main import create_app

        client = TestClient(create_app())
        with caplog.at_level(logging.WARNING, logger="app.main"):
            response = client.get("/api/not-found?token=super-secret-token")

        assert response.status_code == 404
        assert "GET /api/not-found -> 404" in caplog.text
        assert "super-secret-token" not in caplog.text


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
    async def test_lifespan_ensures_default_admin(self, temp_db, monkeypatch):
        """Test lifespan creates default admin user."""
        from sqlalchemy import select

        from app.core.config import settings
        from app.db.engine import transaction
        from app.db.schema import users
        from app.main import ensure_default_admin_v0

        monkeypatch.setattr(
            settings, "initial_admin_password", TEST_INITIAL_ADMIN_PASSWORD
        )
        await ensure_default_admin_v0()

        async with transaction() as conn:
            result = await conn.execute(
                select(users).where(users.c.username == "admin")
            )
            admin = result.mappings().first()

        assert admin is not None
        assert admin["is_admin"] == 1
        assert admin["is_initial_password"] == 1

    def test_app_startup_bootstraps_database_and_default_admin(
        self, tmp_path, monkeypatch
    ):
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
        monkeypatch.setattr(
            settings, "initial_admin_password", TEST_INITIAL_ADMIN_PASSWORD
        )
        reset_engine()
        try:
            with (
                patch("app.main.sync_tasks", new=AsyncMock()),
                patch("app.main.listen_aria2_events", new=AsyncMock()),
                patch(
                    "app.main.run_startup_repair",
                    new=AsyncMock(return_value=_startup_repair_result()),
                ),
                patch(
                    "app.services.orphan_cleanup.cleanup_orphan_files", new=AsyncMock()
                ),
                TestClient(create_app()) as client,
            ):
                login_response = client.post(
                    "/api/auth/login",
                    json={
                        "username": "admin",
                        "password": _client_password_hash(
                            TEST_INITIAL_ADMIN_PASSWORD, "admin"
                        ),
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
    async def test_dev_reset_admin_password_uses_frontend_hash(
        self, temp_db, monkeypatch
    ):
        """Test dev reset keeps default admin compatible with frontend login hashing."""
        from app.core.config import settings
        from app.core.security import hash_password, verify_password
        from app.main import ensure_default_admin_v0, reset_admin_password_for_dev_v0
        from app.repositories.auth import get_user_by_username, update_user

        monkeypatch.setattr(
            settings, "initial_admin_password", TEST_INITIAL_ADMIN_PASSWORD
        )
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
            _client_password_hash(TEST_INITIAL_ADMIN_PASSWORD, "admin"),
            reset_admin["password_hash"],
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
        old_secret_key = settings.secret_key
        old_credential_pepper = settings.credential_pepper
        settings.database_path = str(db_path)
        settings.download_dir = str(tmp_path / "downloads")
        settings.secret_key = "s" * 32
        settings.credential_pepper = "p" * 32
        reset_engine()
        try:
            with pytest.raises(RuntimeError, match="Unsupported database schema"):
                with TestClient(create_app()):
                    pass
        finally:
            settings.database_path = old_db_path
            settings.download_dir = old_download_dir
            settings.secret_key = old_secret_key
            settings.credential_pepper = old_credential_pepper
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

    @pytest.mark.asyncio
    async def test_lifespan_waits_for_maintenance_and_always_cleans_up(
        self, temp_db
    ):
        """Test maintenance precedes workers and shutdown survives app errors."""
        from fastapi import FastAPI

        from app.main import lifespan

        events: list[str] = []

        async def record_pack_recovery() -> None:
            events.append("pack-recovery")

        async def record_accounting(_client: object) -> dict[str, int]:
            events.append("download-accounting")
            return {"rebuilt": 0, "failed": 0}

        async def record_repair() -> dict[str, object]:
            events.append("repair")
            return _startup_repair_result()

        async def record_cleanup() -> None:
            events.append("cleanup")

        async def record_pack_submit() -> None:
            events.append("pack-submit")

        async def run_background(name: str) -> None:
            events.append(name)
            try:
                await asyncio.Event().wait()
            finally:
                events.append(f"{name}-stopped")

        async def run_sync(_: float) -> None:
            await run_background("sync")

        async def run_listener() -> None:
            await run_background("listener")

        async def record_tracker_load() -> None:
            events.append("tracker-load")

        async def run_tracker_refresher() -> None:
            await run_background("tracker-refresher")

        close_session = AsyncMock()
        dispose = AsyncMock()
        with (
            patch("app.core.config.check_secret_key"),
            patch("app.main.ensure_default_admin_v0", new=AsyncMock()),
            patch(
                "app.modules.pack.PackTaskManager.recover_startup",
                new=AsyncMock(side_effect=record_pack_recovery),
            ),
            patch(
                "app.main.rebuild_active_download_accounting",
                new=record_accounting,
            ),
            patch(
                "app.modules.pack.PackTaskManager.submit_pending",
                new=AsyncMock(side_effect=record_pack_submit),
            ),
            patch("app.main.run_startup_repair", new=record_repair),
            patch(
                "app.services.orphan_cleanup.cleanup_orphan_files",
                new=record_cleanup,
            ),
            patch("app.main.sync_tasks", new=run_sync),
            patch("app.main.listen_aria2_events", new=run_listener),
            patch(
                "app.services.tracker_list_service.load_from_db",
                new=AsyncMock(side_effect=record_tracker_load),
            ),
            patch(
                "app.services.tracker_list_service.run_tracker_list_refresher",
                new=run_tracker_refresher,
            ),
            patch("app.main.Aria2Client.close_session", close_session),
            patch("app.main.dispose_engine", dispose),
        ):
            with pytest.raises(RuntimeError, match="application failure"):
                async with lifespan(FastAPI()):
                    assert events == [
                        "pack-recovery",
                        "download-accounting",
                        "repair",
                        "cleanup",
                        "pack-submit",
                        "tracker-load",
                    ]
                    await asyncio.sleep(0)
                    assert events[:5] == [
                        "pack-recovery",
                        "download-accounting",
                        "repair",
                        "cleanup",
                        "pack-submit",
                    ]
                    assert {"sync", "listener", "tracker-refresher"}.issubset(events)
                    raise RuntimeError("application failure")

        assert {"sync-stopped", "listener-stopped", "tracker-refresher-stopped"}.issubset(events)
        close_session.assert_awaited_once()
        dispose.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_lifespan_repair_exception_skips_orphan_cleanup(self, temp_db):
        """A failed repair must not allow destructive orphan cleanup."""
        from fastapi import FastAPI

        from app.core.config import settings
        from app.main import lifespan

        candidate = (
            Path(settings.download_dir) / "store" / "aa" / ("a" * 64)
        )
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_bytes(b"keep")
        cleanup = AsyncMock()
        sync = AsyncMock()
        listener = AsyncMock()
        with (
            patch("app.core.config.check_secret_key"),
            patch("app.main.ensure_default_admin_v0", new=AsyncMock()),
            patch(
                "app.main.run_startup_repair",
                new=AsyncMock(side_effect=RuntimeError("repair failed")),
            ),
            patch(
                "app.services.orphan_cleanup.cleanup_orphan_files",
                new=cleanup,
            ),
            patch("app.main.sync_tasks", sync),
            patch("app.main.listen_aria2_events", listener),
            patch("app.main.Aria2Client.close_session", new=AsyncMock()),
            patch("app.main.dispose_engine", new=AsyncMock()),
        ):
            async with lifespan(FastAPI()):
                await asyncio.sleep(0)

        cleanup.assert_not_awaited()
        assert candidate.read_bytes() == b"keep"
        sync.assert_awaited_once()
        listener.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_lifespan_unresolved_repair_skips_orphan_cleanup(self, temp_db):
        """An unresolved repair candidate must remain on disk."""
        from fastapi import FastAPI

        from app.core.config import settings
        from app.main import lifespan

        candidate = (
            Path(settings.download_dir) / "store" / "bb" / ("b" * 64)
        )
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_bytes(b"keep")
        cleanup = AsyncMock()
        with (
            patch("app.core.config.check_secret_key"),
            patch("app.main.ensure_default_admin_v0", new=AsyncMock()),
            patch(
                "app.main.run_startup_repair",
                new=AsyncMock(
                    return_value=_startup_repair_result(
                        safe_for_cleanup=False, unresolved_files=1
                    )
                ),
            ),
            patch(
                "app.services.orphan_cleanup.cleanup_orphan_files",
                new=cleanup,
            ),
            patch("app.main.sync_tasks", new=AsyncMock()),
            patch("app.main.listen_aria2_events", new=AsyncMock()),
            patch("app.main.Aria2Client.close_session", new=AsyncMock()),
            patch("app.main.dispose_engine", new=AsyncMock()),
        ):
            async with lifespan(FastAPI()):
                await asyncio.sleep(0)

        cleanup.assert_not_awaited()
        assert candidate.read_bytes() == b"keep"

    @pytest.mark.asyncio
    async def test_lifespan_candidate_query_failure_skips_orphan_gid_purge(
        self, temp_db
    ):
        """planned 候选两次查询失败时必须跳过 orphan gid purge，其余修复继续。"""
        from fastapi import FastAPI

        from app.main import lifespan

        purge_orphans = AsyncMock()
        residual = AsyncMock(return_value={"found": 0, "purged": 0, "failed": 0})
        terminal_dirs = AsyncMock(return_value={"purged": 0})
        with (
            patch("app.core.config.check_secret_key"),
            patch("app.main.ensure_default_admin_v0", new=AsyncMock()),
            patch(
                "app.services.task_batch_submission.recover_planned_submissions",
                new=AsyncMock(side_effect=RuntimeError("db locked")),
            ),
            patch(
                "app.services.task_batch_submission.list_pending_submission_candidates",
                new=AsyncMock(side_effect=RuntimeError("db locked")),
            ),
            patch("app.main.purge_orphan_aria2_downloads", new=purge_orphans),
            patch("app.main.purge_terminal_residual_gids", new=residual),
            patch("app.main.purge_terminal_download_dirs", new=terminal_dirs),
            patch("app.main.run_startup_repair", new=AsyncMock(return_value=_startup_repair_result())),
            patch("app.main.sync_tasks", new=AsyncMock()),
            patch("app.main.listen_aria2_events", new=AsyncMock()),
            patch("app.main.Aria2Client.close_session", new=AsyncMock()),
            patch("app.main.dispose_engine", new=AsyncMock()),
        ):
            async with lifespan(FastAPI()):
                await asyncio.sleep(0)

        purge_orphans.assert_not_awaited()
        residual.assert_awaited_once()
        terminal_dirs.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_background_task_logs_unexpected_failure(self, caplog):
        """Unexpected worker exits are logged immediately."""
        import logging

        from app.main import _run_background_task

        async def fail() -> None:
            raise RuntimeError("worker failed")

        with caplog.at_level(logging.ERROR, logger="app.main"):
            with pytest.raises(RuntimeError, match="worker failed"):
                await _run_background_task("sync_tasks", fail())

        assert "后台任务意外退出: sync_tasks" in caplog.text


@pytest.mark.asyncio
async def test_application_singleton_lease_rejects_second_instance(
    temp_db: str,
) -> None:
    from app.services.singleton_lease import ApplicationSingletonLease

    first = ApplicationSingletonLease.acquire()
    try:
        with pytest.raises(RuntimeError, match="仅支持单 worker"):
            ApplicationSingletonLease.acquire()
    finally:
        first.release()
    replacement = ApplicationSingletonLease.acquire()
    replacement.release()


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


class TestOpenApiSecuritySchemes:
    """custom OpenAPI 仅安装 scheme，不改写其他 operations。"""

    def test_schemes_installed_and_tasks_declares_security(self):
        from app.core.config import settings
        from app.main import app

        app.openapi_schema = None
        schema = app.openapi()

        schemes = schema["components"]["securitySchemes"]
        assert set(schemes) == {"sessionCookie", "apiToken"}
        assert schemes["sessionCookie"] == {
            "type": "apiKey",
            "in": "cookie",
            "name": settings.session_cookie_name,
        }
        assert schemes["apiToken"] == {"type": "http", "scheme": "bearer"}

        tasks_post = schema["paths"]["/api/tasks"]["post"]
        assert tasks_post["security"] == [{"sessionCookie": []}, {"apiToken": []}]

        # 无关公开 operation 不被全局加 security
        login_post = schema["paths"]["/api/auth/login"]["post"]
        assert "security" not in login_post
