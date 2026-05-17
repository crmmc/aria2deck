from __future__ import annotations

from collections.abc import AsyncGenerator
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import insert, text
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.db.bootstrap import SCHEMA_VERSION, bootstrap_database, validate_schema_version
from app.db.engine import dispose_engine, get_engine, reset_engine, session_scope
from app.db.schema import sessions


@pytest_asyncio.fixture
async def isolated_db(tmp_path: Path) -> AsyncGenerator[Path, None]:
    original_db = settings.database_path
    await dispose_engine()
    reset_engine()
    settings.database_path = str(tmp_path / "app.db")
    try:
        yield Path(settings.database_path)
    finally:
        await dispose_engine()
        reset_engine()
        settings.database_path = original_db


@pytest.mark.asyncio
async def test_bootstrap_creates_v0_schema(isolated_db: Path):
    await bootstrap_database()

    async with get_engine().connect() as conn:
        version = (
            await conn.execute(text("SELECT version FROM schema_meta WHERE id = 1"))
        ).scalar_one()
        users_exists = (
            await conn.execute(
                text(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='users'"
                )
            )
        ).scalar_one()
        settings_id = (
            await conn.execute(text("SELECT id FROM app_settings"))
        ).scalar_one()

    assert version == SCHEMA_VERSION == 0
    assert users_exists == "users"
    assert settings_id == 1


@pytest.mark.asyncio
async def test_validate_accepts_existing_v0_schema(isolated_db: Path):
    await bootstrap_database()
    assert await validate_schema_version() is None


@pytest.mark.asyncio
async def test_bootstrap_rejects_legacy_database_without_schema_meta(isolated_db: Path):
    conn = sqlite3.connect(isolated_db)
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT NOT NULL)")
    conn.commit()
    conn.close()

    reset_engine()
    with pytest.raises(RuntimeError, match="Unsupported database schema"):
        await bootstrap_database()


@pytest.mark.asyncio
async def test_bootstrap_rejects_wrong_version(isolated_db: Path):
    conn = sqlite3.connect(isolated_db)
    conn.execute(
        "CREATE TABLE schema_meta (id INTEGER PRIMARY KEY, version INTEGER NOT NULL, created_at_ms INTEGER NOT NULL)"
    )
    conn.execute(
        "INSERT INTO schema_meta (id, version, created_at_ms) VALUES (1, 99, 1)"
    )
    conn.commit()
    conn.close()

    reset_engine()
    with pytest.raises(RuntimeError, match="expected version 0"):
        await bootstrap_database()


@pytest.mark.asyncio
async def test_engine_dispose_is_idempotent(isolated_db: Path):
    await bootstrap_database()
    await dispose_engine()
    await dispose_engine()

    async with get_engine().connect() as conn:
        value = (await conn.execute(text("SELECT 1"))).scalar_one()

    assert value == 1


@pytest.mark.asyncio
async def test_session_scope_executes_query(isolated_db: Path):
    await bootstrap_database()

    async with session_scope() as session:
        value = (await session.execute(text("SELECT 1"))).scalar_one()

    assert value == 1


@pytest.mark.asyncio
async def test_foreign_keys_apply_to_each_session_connection(isolated_db: Path):
    await bootstrap_database()

    async with session_scope():
        with pytest.raises(IntegrityError):
            async with session_scope() as session:
                await session.execute(
                    insert(sessions).values(
                        id="missing-user-session",
                        user_id=999,
                        expires_at_ms=1,
                        created_at_ms=1,
                    )
                )


def test_legacy_db_module_cli_fails_loudly():
    backend_dir = Path(__file__).resolve().parents[1]

    result = subprocess.run(
        [sys.executable, "-m", "app.db"],
        cwd=backend_dir,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "app.db CLI was removed" in result.stderr
