"""Test fixtures and configuration for pytest."""

import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# Patch settings before importing app modules
_temp_dir = tempfile.mkdtemp()
_test_db = os.path.join(_temp_dir, "test.db")
_test_download_dir = os.path.join(_temp_dir, "downloads")

os.environ["ARIA2C_DATABASE_PATH"] = _test_db
os.environ["ARIA2C_DOWNLOAD_DIR"] = _test_download_dir

from app.core.config import settings
from app.core.rate_limit import api_limiter
from app.core.security import hash_password
from app.db import init_db, execute, fetch_one
from app.database import reset_engine, init_db as init_sqlmodel_db
from app.main import app
from app.aria2.client import Aria2Client


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    """在每个测试前后清理限流器状态"""
    api_limiter.clear_all()
    yield
    api_limiter.clear_all()


@pytest.fixture(scope="function")
def temp_db() -> Generator[str, None, None]:
    """Create a fresh temporary database for each test."""
    # Create a new temp dir for each test
    temp_dir = tempfile.mkdtemp()
    db_path = os.path.join(temp_dir, "test.db")
    download_dir = os.path.join(temp_dir, "downloads")
    os.makedirs(download_dir, exist_ok=True)

    # Patch settings
    original_db_path = settings.database_path
    original_download_dir = settings.download_dir
    settings.database_path = db_path
    settings.download_dir = download_dir

    # Reset the async engine so it uses the new path
    reset_engine()

    # Initialize database (both sync and async)
    init_db()

    # Initialize SQLModel tables
    import asyncio
    asyncio.run(init_sqlmodel_db())

    yield db_path

    # Restore settings and reset engine
    settings.database_path = original_db_path
    settings.download_dir = original_download_dir
    reset_engine()


@pytest.fixture
def test_user(temp_db: str) -> dict:
    """Create a test user and return user info."""
    user_id = execute(
        """
        INSERT INTO users (username, password_hash, is_admin, created_at, quota)
        VALUES (?, ?, ?, ?, ?)
        """,
        ["testuser", hash_password("testpass"), 0, datetime.now(timezone.utc).isoformat(), 100 * 1024 * 1024 * 1024]
    )
    return {"id": user_id, "username": "testuser", "is_admin": 0, "quota": 100 * 1024 * 1024 * 1024}


@pytest.fixture
def test_admin(temp_db: str) -> dict:
    """Create a test admin user and return user info."""
    user_id = execute(
        """
        INSERT INTO users (username, password_hash, is_admin, created_at, quota)
        VALUES (?, ?, ?, ?, ?)
        """,
        ["admin", hash_password("adminpass"), 1, datetime.now(timezone.utc).isoformat(), 100 * 1024 * 1024 * 1024]
    )
    return {"id": user_id, "username": "admin", "is_admin": 1, "quota": 100 * 1024 * 1024 * 1024}


@pytest.fixture
def user_session(test_user: dict, temp_db: str) -> str:
    """Create a session for the test user."""
    session_id = "test_session_123"
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
    execute(
        "INSERT INTO sessions (id, user_id, expires_at) VALUES (?, ?, ?)",
        [session_id, test_user["id"], expires_at]
    )
    return session_id


@pytest.fixture
def admin_session(test_admin: dict, temp_db: str) -> str:
    """Create a session for the admin user."""
    session_id = "admin_session_456"
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
    execute(
        "INSERT INTO sessions (id, user_id, expires_at) VALUES (?, ?, ?)",
        [session_id, test_admin["id"], expires_at]
    )
    return session_id


@pytest.fixture
def client(temp_db: str) -> TestClient:
    """Create a test client with fresh database."""
    return TestClient(app)


@pytest.fixture
def authenticated_client(client: TestClient, user_session: str) -> TestClient:
    """Create an authenticated test client."""
    client.cookies.set(settings.session_cookie_name, user_session)
    return client


@pytest.fixture
def mock_aria2_client() -> AsyncMock:
    """Create a mock Aria2 client."""
    mock = AsyncMock(spec=Aria2Client)
    mock.add_uri.return_value = "test_gid_12345"
    mock.tell_status.return_value = {
        "gid": "test_gid_12345",
        "status": "active",
        "totalLength": "1000000",
        "completedLength": "500000",
        "downloadSpeed": "10000",
        "uploadSpeed": "0",
    }
    mock.pause.return_value = "test_gid_12345"
    mock.unpause.return_value = "test_gid_12345"
    mock.force_remove.return_value = "test_gid_12345"
    mock.remove_download_result.return_value = "OK"
    return mock


@pytest.fixture
def failed_task(test_user: dict, temp_db: str) -> dict:
    """Create a failed task in the database."""
    now = datetime.now(timezone.utc).isoformat()
    task_id = execute(
        """
        INSERT INTO tasks (owner_id, gid, uri, status, name, total_length, completed_length,
                          download_speed, upload_speed, error, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            test_user["id"], "old_gid_123", "https://example.com/file.zip", "error",
            "file.zip", 1000000, 500000, 0, 0, "Connection timeout", now, now
        ]
    )
    return fetch_one("SELECT * FROM tasks WHERE id = ?", [task_id])


@pytest.fixture
def torrent_task(test_user: dict, temp_db: str) -> dict:
    """Create a torrent task in the database."""
    now = datetime.now(timezone.utc).isoformat()
    task_id = execute(
        """
        INSERT INTO tasks (owner_id, gid, uri, status, name, total_length, completed_length,
                          download_speed, upload_speed, error, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            test_user["id"], "torrent_gid_456", "[torrent]", "error",
            "movie.mkv", 5000000000, 1000000000, 0, 0, "No seeds available", now, now
        ]
    )
    return fetch_one("SELECT * FROM tasks WHERE id = ?", [task_id])


@pytest.fixture
def other_user_task(test_admin: dict, temp_db: str) -> dict:
    """Create a task belonging to another user (admin)."""
    now = datetime.now(timezone.utc).isoformat()
    task_id = execute(
        """
        INSERT INTO tasks (owner_id, gid, uri, status, name, total_length, completed_length,
                          download_speed, upload_speed, error, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            test_admin["id"], "admin_gid_789", "https://admin.com/file.zip", "error",
            "admin_file.zip", 2000000, 0, 0, 0, "Failed", now, now
        ]
    )
    return fetch_one("SELECT * FROM tasks WHERE id = ?", [task_id])
