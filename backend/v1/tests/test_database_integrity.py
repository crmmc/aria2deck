"""数据库完整性检查测试。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import tempfile

import pytest
from sqlalchemy import select

import app.db.engine as db_engine
from app.core.config import settings
from app.db.bootstrap import default_app_settings
from app.db.schema import app_settings


@pytest.fixture(autouse=True)
def reset_db_engine():
    """每个测试前重置数据库引擎。"""
    db_engine.reset_engine()
    yield
    db_engine.reset_engine()


@pytest.mark.asyncio
async def test_database_integrity_check_passes(temp_db: str) -> None:
    result = await db_engine.check_database_integrity()
    assert result is True


@pytest.mark.asyncio
async def test_wal_integrity_check_passes(temp_db: str) -> None:
    result = await db_engine.check_wal_integrity()
    assert result is True


@pytest.mark.asyncio
async def test_engine_has_timeout_config(temp_db: str) -> None:
    engine = db_engine.get_engine()

    assert engine.pool is not None
    assert engine is not None


@pytest.mark.asyncio
async def test_database_integrity_with_corrupted_db() -> None:
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        tmp_path = tmp.name
        tmp.write(b"not a valid sqlite database file")

    original_path = settings.database_path
    try:
        settings.database_path = tmp_path
        db_engine.reset_engine()

        result = await db_engine.check_database_integrity()
        assert result is False
    finally:
        settings.database_path = original_path
        db_engine.reset_engine()
        Path(tmp_path).unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_default_app_settings_match_bootstrap_contract(temp_db: str) -> None:
    defaults = default_app_settings(123)

    assert defaults["id"] == 1
    assert defaults["max_task_size_bytes"] == 10737418240
    assert defaults["pack_format"] == "tar.zst"
    assert defaults["aria2_bt_stop_timeout_seconds"] == 7 * 24 * 60 * 60
    assert defaults["created_at_ms"] == 123
    assert defaults["updated_at_ms"] == 123


@pytest.mark.asyncio
async def test_bootstrap_app_settings_row_exists(temp_db: str) -> None:
    async with db_engine.transaction() as conn:
        row = (
            await conn.execute(
                select(
                    app_settings.c.max_task_size_bytes, app_settings.c.pack_format
                ).where(app_settings.c.id == 1)
            )
        ).one()

    assert row[0] == 10737418240
    assert row[1] == "tar.zst"


@pytest.mark.asyncio
async def test_database_integrity_check_with_issues() -> None:
    mock_result = MagicMock()
    mock_result.fetchall.return_value = [("error1",), ("error2",)]

    mock_conn = AsyncMock()
    mock_conn.execute.return_value = mock_result

    mock_engine = MagicMock()
    mock_engine.connect.return_value.__aenter__.return_value = mock_conn
    mock_engine.connect.return_value.__aexit__.return_value = None

    with patch.object(db_engine, "get_engine", return_value=mock_engine):
        result = await db_engine.check_database_integrity()
        assert result is False


@pytest.mark.asyncio
async def test_wal_integrity_check_with_busy() -> None:
    mock_result = MagicMock()
    mock_result.fetchone.return_value = (1, 10, 5)

    mock_conn = AsyncMock()
    mock_conn.execute.return_value = mock_result

    mock_engine = MagicMock()
    mock_engine.connect.return_value.__aenter__.return_value = mock_conn
    mock_engine.connect.return_value.__aexit__.return_value = None

    with patch.object(db_engine, "get_engine", return_value=mock_engine):
        result = await db_engine.check_wal_integrity()
        assert result is True


@pytest.mark.asyncio
async def test_wal_integrity_check_exception() -> None:
    mock_engine = MagicMock()
    mock_engine.connect.side_effect = Exception("Connection failed")

    with patch.object(db_engine, "get_engine", return_value=mock_engine):
        result = await db_engine.check_wal_integrity()
        assert result is False
