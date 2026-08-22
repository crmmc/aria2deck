"""Tests for v9 task_backend_snapshots schema (M3 T01; reworked in M11 T04).

v13 已删除 task_backend_snapshots（观测改走进程内 observation_store）：
- fresh bootstrap 断言表不再被重建且版本为 14；
- 升级链测试直接 pin ``migrate_v9`` 验证 append-only 历史 DDL，
  不再跑到链尾。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import text

from app.db.bootstrap import SCHEMA_VERSION
from app.db.engine import get_engine


@pytest.mark.asyncio
async def test_fresh_bootstrap_does_not_create_task_backend_snapshots(
    temp_db: str,
) -> None:
    async with get_engine().connect() as conn:
        tables = {
            row[0]
            for row in (
                await conn.execute(
                    text("SELECT name FROM sqlite_master WHERE type='table'")
                )
            ).all()
        }
        version = (
            await conn.execute(
                text("SELECT version FROM schema_meta WHERE id = 1")
            )
        ).scalar_one()

    assert "task_backend_snapshots" not in tables
    assert version == SCHEMA_VERSION == 15


@pytest.mark.asyncio
async def test_migrate_v9_creates_task_backend_snapshots(
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
    from app.db.engine import dispose_engine, reset_engine
    from app.db.migrations import migrate_v9

    original_db = settings.database_path
    settings.database_path = db_path
    reset_engine()
    try:
        async with get_engine().begin() as conn:
            await conn.execute(text("PRAGMA foreign_keys=ON"))
            # pin 在 v9：只验证历史 DDL 本身
            await migrate_v9(conn)

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
        assert version == 9
    finally:
        await dispose_engine()
        reset_engine()
        settings.database_path = original_db
