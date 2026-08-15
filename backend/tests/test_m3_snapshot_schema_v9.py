"""Tests for v9 task_backend_snapshots schema (M3 T01)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import text

from app.db.bootstrap import SCHEMA_VERSION, bootstrap_database
from app.db.engine import get_engine
from app.db.migrations import run_migrations


@pytest.mark.asyncio
async def test_fresh_bootstrap_creates_task_backend_snapshots(temp_db: str) -> None:
    async with get_engine().connect() as conn:
        tables = {
            row[0]
            for row in (
                await conn.execute(
                    text("SELECT name FROM sqlite_master WHERE type='table'")
                )
            ).all()
        }
        columns = {
            row[1]
            for row in (
                await conn.execute(
                    text("PRAGMA table_info(task_backend_snapshots)")
                )
            ).all()
        }
        indexes = {
            row[1]
            for row in (
                await conn.execute(
                    text("PRAGMA index_list(task_backend_snapshots)")
                )
            ).all()
        }
        version = (
            await conn.execute(
                text("SELECT version FROM schema_meta WHERE id = 1")
            )
        ).scalar_one()

    assert "task_backend_snapshots" in tables
    assert columns == {
        "global_download_id",
        "download_speed",
        "upload_speed",
        "total_length",
        "completed_length",
        "status",
        "files_json",
        "raw_json",
        "updated_at_ms",
    }
    assert "ix_task_backend_snapshots_updated_at" in indexes
    assert version == SCHEMA_VERSION == 12


@pytest.mark.asyncio
async def test_v8_to_v9_upgrade_creates_task_backend_snapshots(
    tmp_path: Path,
) -> None:
    db_path = str(tmp_path / "app.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE schema_meta ("
        "id INTEGER PRIMARY KEY, "
        "version INTEGER NOT NULL, "
        "created_at_ms INTEGER NOT NULL)"
    )
    conn.execute("INSERT INTO schema_meta VALUES (1, 8, 123)")
    conn.execute(
        "CREATE TABLE global_downloads ("
        "id INTEGER PRIMARY KEY, "
        "resource_key TEXT NOT NULL, "
        "resource_kind TEXT NOT NULL, "
        "source_uri TEXT NOT NULL, "
        "status TEXT NOT NULL, "
        "created_at_ms INTEGER NOT NULL, "
        "updated_at_ms INTEGER NOT NULL)"
    )
    conn.commit()
    conn.close()

    from app.core.config import settings
    from app.db.engine import reset_engine

    original_db = settings.database_path
    settings.database_path = db_path
    reset_engine()
    try:
        async with get_engine().begin() as conn:
            await conn.execute(text("PRAGMA foreign_keys=ON"))
            result = await run_migrations(conn, 8)
        assert result == 12

        async with get_engine().connect() as conn:
            tables = {
                row[0]
                for row in (
                    await conn.execute(
                        text("SELECT name FROM sqlite_master WHERE type='table'")
                    )
                ).all()
            }
            columns = {
                row[1]
                for row in (
                    await conn.execute(
                        text("PRAGMA table_info(task_backend_snapshots)")
                    )
                ).all()
            }
            indexes = {
                row[1]
                for row in (
                    await conn.execute(
                        text("PRAGMA index_list(task_backend_snapshots)")
                    )
                ).all()
            }
            version = (
                await conn.execute(
                    text("SELECT version FROM schema_meta WHERE id = 1")
                )
            ).scalar_one()

        assert "task_backend_snapshots" in tables
        assert columns == {
            "global_download_id",
            "download_speed",
            "upload_speed",
            "total_length",
            "completed_length",
            "status",
            "files_json",
            "raw_json",
            "updated_at_ms",
        }
        assert "ix_task_backend_snapshots_updated_at" in indexes
        assert version == 12
    finally:
        from app.db.engine import dispose_engine

        await dispose_engine()
        reset_engine()
        settings.database_path = original_db
