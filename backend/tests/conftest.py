"""Test fixtures and configuration for pytest."""
# ruff: noqa: E402

import os
import tempfile
from typing import Generator
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert, select

# Patch settings before importing app modules
_temp_dir = tempfile.mkdtemp()
_test_db = os.path.join(_temp_dir, "test.db")
_test_download_dir = os.path.join(_temp_dir, "downloads")

os.environ["ARIA2C_DATABASE_PATH"] = _test_db
os.environ["ARIA2C_DOWNLOAD_DIR"] = _test_download_dir

from app.core.config import settings
from app.core.download_limiter import download_config, download_limiter
from app.core.rate_limit import api_limiter, login_limiter, rpc_limiter
from app.core.rate_limit_config import rate_limit_config
from app.core.request_rate_guard import scoped_rate_limiter
from app.db.bootstrap import bootstrap_database
from app.db.engine import dispose_engine, reset_engine, transaction
from app.db.schema import global_downloads, user_tasks
from app.main import app
from app.aria2.client import Aria2Client
from tests.helpers_v0 import create_session_v0, create_user_file_v0, create_user_v0, now_ms


async def create_failed_user_task_v0(
    *,
    user_id: int,
    resource_key: str,
    uri: str,
    gid: str,
    name: str,
    total_bytes: int,
    completed_bytes: int,
    error_message: str,
) -> dict:
    timestamp = now_ms()
    async with transaction() as conn:
        download = (
            await conn.execute(
                insert(global_downloads)
                .values(
                    resource_key=resource_key,
                    resource_kind="torrent" if uri == "[torrent]" else "http",
                    source_uri=uri,
                    display_name=name,
                    aria2_gid=gid,
                    status="failed",
                    total_bytes=total_bytes,
                    completed_bytes=completed_bytes,
                    error_message=error_message,
                    created_at_ms=timestamp,
                    updated_at_ms=timestamp,
                )
                .returning(global_downloads)
            )
        ).mappings().one()
        task = (
            await conn.execute(
                insert(user_tasks)
                .values(
                    user_id=user_id,
                    global_download_id=download["id"],
                    status="failed",
                    display_name=name,
                    error_message=error_message,
                    created_at_ms=timestamp,
                    updated_at_ms=timestamp,
                )
                .returning(user_tasks)
            )
        ).mappings().one()
        row = (
            await conn.execute(
                select(user_tasks, global_downloads)
                .select_from(user_tasks.join(global_downloads, user_tasks.c.global_download_id == global_downloads.c.id))
                .where(user_tasks.c.id == task["id"])
            )
        ).mappings().one()
    result = dict(row)
    result.update(
        {
            "id": task["id"],
            "owner_id": user_id,
            "gid": gid,
            "uri": uri,
            "status": "error",
            "name": name,
            "total_length": total_bytes,
            "completed_length": completed_bytes,
            "error": error_message,
        }
    )
    return result


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    """在每个测试前后清理限流器状态"""
    import asyncio
    asyncio.run(api_limiter.clear_all())
    asyncio.run(login_limiter.clear_all())
    asyncio.run(rpc_limiter.clear_all())
    asyncio.run(scoped_rate_limiter.clear_all())
    asyncio.run(download_limiter.clear_all())
    yield
    asyncio.run(api_limiter.clear_all())
    asyncio.run(login_limiter.clear_all())
    asyncio.run(rpc_limiter.clear_all())
    asyncio.run(scoped_rate_limiter.clear_all())
    asyncio.run(download_limiter.clear_all())


@pytest.fixture(scope="function")
def temp_db() -> Generator[str, None, None]:
    temp_dir = tempfile.mkdtemp()
    db_path = os.path.join(temp_dir, "test.db")
    download_dir = os.path.join(temp_dir, "downloads")
    os.makedirs(download_dir, exist_ok=True)

    original_db_path = settings.database_path
    original_download_dir = settings.download_dir
    settings.database_path = db_path
    settings.download_dir = download_dir

    import asyncio

    reset_engine()
    asyncio.run(bootstrap_database())
    from app.services.settings_service import load_runtime_config

    asyncio.run(load_runtime_config())

    yield db_path

    import asyncio
    import gc

    asyncio.run(dispose_engine())
    gc.collect()

    settings.database_path = original_db_path
    settings.download_dir = original_download_dir
    reset_engine()


@pytest.fixture
def test_user(temp_db: str) -> dict:
    import asyncio

    return asyncio.run(create_user_v0(username="testuser", password="testpass", is_admin=False))


@pytest.fixture
def test_admin(temp_db: str) -> dict:
    import asyncio

    return asyncio.run(create_user_v0(username="admin", password="adminpass", is_admin=True))


@pytest.fixture
def user_file(test_user: dict, temp_db: str) -> dict:
    import asyncio
    from pathlib import Path

    real_path = Path(settings.download_dir) / "store" / "hash_testfile.bin"
    real_path.parent.mkdir(parents=True, exist_ok=True)
    real_path.write_bytes(b"x" * 1024)
    return asyncio.run(
        create_user_file_v0(
            user_id=test_user["id"],
            real_path=real_path,
            content_hash="hash_testfile",
            display_name="testfile.bin",
            size_bytes=1024,
        )
    )


@pytest.fixture
def user_session(test_user: dict, temp_db: str) -> str:
    import asyncio

    return asyncio.run(create_session_v0(test_user["id"], "test_session_123"))


@pytest.fixture
def admin_session(test_admin: dict, temp_db: str) -> str:
    import asyncio

    return asyncio.run(create_session_v0(test_admin["id"], "admin_session_456"))


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
    import asyncio

    return asyncio.run(
        create_failed_user_task_v0(
            user_id=test_user["id"],
            resource_key="http:old_gid_123",
            uri="https://example.com/file.zip",
            gid="old_gid_123",
            name="file.zip",
            total_bytes=1000000,
            completed_bytes=500000,
            error_message="Connection timeout",
        )
    )


@pytest.fixture
def torrent_task(test_user: dict, temp_db: str) -> dict:
    """Create a torrent task in the database."""
    import asyncio

    return asyncio.run(
        create_failed_user_task_v0(
            user_id=test_user["id"],
            resource_key="torrent:torrent_gid_456",
            uri="[torrent]",
            gid="torrent_gid_456",
            name="movie.mkv",
            total_bytes=5000000000,
            completed_bytes=1000000000,
            error_message="No seeds available",
        )
    )


@pytest.fixture
def other_user_task(test_admin: dict, temp_db: str) -> dict:
    """Create a task belonging to another user (admin)."""
    import asyncio

    return asyncio.run(
        create_failed_user_task_v0(
            user_id=test_admin["id"],
            resource_key="http:admin_gid_789",
            uri="https://admin.com/file.zip",
            gid="admin_gid_789",
            name="admin_file.zip",
            total_bytes=2000000,
            completed_bytes=0,
            error_message="Failed",
        )
    )
