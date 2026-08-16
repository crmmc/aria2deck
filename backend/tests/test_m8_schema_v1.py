"""Tests for M8 schema v12: download_sources + history retention foundation."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.core.config import settings
from app.db.bootstrap import SCHEMA_VERSION, default_app_settings
from app.db.engine import dispose_engine, get_engine, reset_engine
from app.db.migrations import migrate_v12, run_migrations


@pytest_asyncio.fixture
async def isolated_db(tmp_path: Path) -> AsyncGenerator[Path, None]:
    original_db = settings.database_path
    await dispose_engine()
    reset_engine()
    settings.database_path = str(tmp_path / "m8.db")
    try:
        yield Path(settings.database_path)
    finally:
        await dispose_engine()
        reset_engine()
        settings.database_path = original_db


@pytest.mark.asyncio
async def test_fresh_bootstrap_has_download_sources_and_history_columns(
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
        source_columns = {
            row[1]
            for row in (
                await conn.execute(text("PRAGMA table_info(download_sources)"))
            ).all()
        }
        gd_columns = {
            row[1]
            for row in (
                await conn.execute(text("PRAGMA table_info(global_downloads)"))
            ).all()
        }
        ut_columns = {
            row[1]
            for row in (await conn.execute(text("PRAGMA table_info(user_tasks)"))).all()
        }
        settings_columns = {
            row[1]
            for row in (
                await conn.execute(text("PRAGMA table_info(app_settings)"))
            ).all()
        }
        version = (
            await conn.execute(text("SELECT version FROM schema_meta WHERE id = 1"))
        ).scalar_one()
        retention = (
            await conn.execute(
                text("SELECT history_retention_days FROM app_settings WHERE id = 1")
            )
        ).scalar_one()
        source_fk = (
            await conn.execute(text("PRAGMA foreign_key_list(global_downloads)"))
        ).mappings().all()

    assert "download_sources" in tables
    assert source_columns == {
        "id",
        "resource_kind",
        "payload_text",
        "selection_json",
        "options_json",
        "content_digest",
        "resource_identity",
        "created_at_ms",
        "updated_at_ms",
        "purged_at_ms",
    }
    assert "source_id" in gd_columns
    assert "history_expired_at_ms" in ut_columns
    assert "history_retention_days" in settings_columns
    assert retention == 30
    assert version == SCHEMA_VERSION == 13
    assert any(
        row["table"] == "download_sources"
        and row["from"] == "source_id"
        and row["to"] == "id"
        and row["on_delete"].upper() == "SET NULL"
        for row in source_fk
    )


@pytest.mark.asyncio
async def test_default_app_settings_include_history_retention_days() -> None:
    defaults = default_app_settings(1)
    assert defaults["history_retention_days"] == 30


@pytest.mark.asyncio
async def test_v11_to_v12_migration_is_idempotent(isolated_db: Path) -> None:
    async with get_engine().begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE schema_meta (id INTEGER PRIMARY KEY, "
                "version INTEGER NOT NULL, created_at_ms INTEGER NOT NULL)"
            )
        )
        await conn.execute(text("INSERT INTO schema_meta VALUES (1, 11, 123)"))
        await conn.execute(
            text(
                "CREATE TABLE app_settings ("
                "id INTEGER PRIMARY KEY, "
                "max_task_size_bytes INTEGER NOT NULL, "
                "created_at_ms INTEGER NOT NULL, "
                "updated_at_ms INTEGER NOT NULL)"
            )
        )
        await conn.execute(text("INSERT INTO app_settings VALUES (1, 1, 1, 1)"))
        await conn.execute(
            text("CREATE TABLE stored_files (id INTEGER PRIMARY KEY)")
        )
        await conn.execute(
            text(
                "CREATE TABLE global_downloads ("
                "id INTEGER PRIMARY KEY, "
                "resource_key TEXT NOT NULL, "
                "resource_kind TEXT NOT NULL, "
                "source_uri TEXT NOT NULL, "
                "status TEXT NOT NULL, "
                "created_at_ms INTEGER NOT NULL, "
                "updated_at_ms INTEGER NOT NULL)"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO global_downloads "
                "VALUES (1, 'rk', 'http', 'http://example.com/a', "
                "'completed', 1, 1)"
            )
        )
        await conn.execute(
            text(
                "CREATE TABLE user_tasks ("
                "id INTEGER PRIMARY KEY, "
                "user_id INTEGER NOT NULL, "
                "global_download_id INTEGER NOT NULL, "
                "status TEXT NOT NULL, "
                "created_at_ms INTEGER NOT NULL, "
                "updated_at_ms INTEGER NOT NULL)"
            )
        )
        await conn.execute(
            text("INSERT INTO user_tasks VALUES (1, 1, 1, 'completed', 1, 1)")
        )

        assert await run_migrations(conn, 11) == 13
        assert await run_migrations(conn, 11) == 13

        tables = {
            row[0]
            for row in (
                await conn.execute(
                    text("SELECT name FROM sqlite_master WHERE type='table'")
                )
            ).all()
        }
        source_columns = {
            row[1]
            for row in (
                await conn.execute(text("PRAGMA table_info(download_sources)"))
            ).all()
        }
        gd_columns = {
            row[1]
            for row in (
                await conn.execute(text("PRAGMA table_info(global_downloads)"))
            ).all()
        }
        ut_columns = {
            row[1]
            for row in (await conn.execute(text("PRAGMA table_info(user_tasks)"))).all()
        }
        retention = (
            await conn.execute(
                text("SELECT history_retention_days FROM app_settings WHERE id = 1")
            )
        ).scalar_one()
        source_id = (
            await conn.execute(
                text("SELECT source_id FROM global_downloads WHERE id = 1")
            )
        ).scalar_one()
        history_expired = (
            await conn.execute(
                text("SELECT history_expired_at_ms FROM user_tasks WHERE id = 1")
            )
        ).scalar_one()
        version = (
            await conn.execute(text("SELECT version FROM schema_meta WHERE id = 1"))
        ).scalar_one()
        source_fk = (
            await conn.execute(text("PRAGMA foreign_key_list(global_downloads)"))
        ).mappings().all()

    assert "download_sources" in tables
    assert {
        "id",
        "resource_kind",
        "payload_text",
        "selection_json",
        "options_json",
        "content_digest",
        "resource_identity",
        "created_at_ms",
        "updated_at_ms",
        "purged_at_ms",
    } <= source_columns
    assert "source_id" in gd_columns
    assert "history_expired_at_ms" in ut_columns
    assert retention == 30
    assert source_id is None
    assert history_expired is None
    assert version == 13
    assert any(
        row["table"] == "download_sources"
        and row["from"] == "source_id"
        and row["to"] == "id"
        and row["on_delete"].upper() == "SET NULL"
        for row in source_fk
    )


@pytest.mark.asyncio
async def test_v12_rebuild_keeps_child_fks_and_constraints(
    isolated_db: Path,
) -> None:
    """v11→v12 表重建不得改写子表外键目标（生产事故回归）。

    子表带真实 FK 定义 + 数据行；RENAME 式重建会把 user_tasks /
    task_backend_snapshots 的外键改写到旧表名并悬空。
    """
    async with get_engine().begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE schema_meta (id INTEGER PRIMARY KEY, "
                "version INTEGER NOT NULL, created_at_ms INTEGER NOT NULL)"
            )
        )
        await conn.execute(text("INSERT INTO schema_meta VALUES (1, 11, 123)"))
        await conn.execute(
            text(
                "CREATE TABLE app_settings ("
                "id INTEGER PRIMARY KEY, "
                "max_task_size_bytes INTEGER NOT NULL, "
                "created_at_ms INTEGER NOT NULL, "
                "updated_at_ms INTEGER NOT NULL)"
            )
        )
        await conn.execute(text("INSERT INTO app_settings VALUES (1, 1, 1, 1)"))
        await conn.execute(
            text("CREATE TABLE stored_files (id INTEGER PRIMARY KEY)")
        )
        await conn.execute(
            text(
                "CREATE TABLE global_downloads ("
                "id INTEGER PRIMARY KEY, "
                "resource_key TEXT NOT NULL, "
                "resource_kind TEXT NOT NULL, "
                "source_uri TEXT NOT NULL, "
                "status TEXT NOT NULL, "
                "completed_file_id INTEGER REFERENCES stored_files (id), "
                "created_at_ms INTEGER NOT NULL, "
                "updated_at_ms INTEGER NOT NULL)"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO global_downloads "
                "VALUES (1, 'rk', 'http', 'http://example.com/a', "
                "'completed', NULL, 1, 1)"
            )
        )
        await conn.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY)"))
        await conn.execute(text("INSERT INTO users VALUES (1)"))
        await conn.execute(
            text(
                "CREATE TABLE user_tasks ("
                "id INTEGER PRIMARY KEY, "
                "user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE, "
                "global_download_id INTEGER NOT NULL "
                "REFERENCES global_downloads (id) ON DELETE CASCADE, "
                "status TEXT NOT NULL, "
                "created_at_ms INTEGER NOT NULL, "
                "updated_at_ms INTEGER NOT NULL)"
            )
        )
        await conn.execute(
            text("INSERT INTO user_tasks VALUES (1, 1, 1, 'completed', 1, 1)")
        )
        await conn.execute(
            text(
                "CREATE TABLE task_backend_snapshots ("
                "global_download_id INTEGER NOT NULL "
                "REFERENCES global_downloads (id) ON DELETE CASCADE PRIMARY KEY, "
                "download_speed INTEGER NOT NULL DEFAULT 0, "
                "upload_speed INTEGER NOT NULL DEFAULT 0, "
                "total_length INTEGER NOT NULL DEFAULT 0, "
                "completed_length INTEGER NOT NULL DEFAULT 0, "
                "status VARCHAR(32) NOT NULL DEFAULT '', "
                "files_json TEXT NOT NULL DEFAULT '[]', "
                "raw_json TEXT NOT NULL DEFAULT '{}', "
                "updated_at_ms INTEGER NOT NULL)"
            )
        )
        await conn.execute(
            text("INSERT INTO task_backend_snapshots VALUES (1, 0, 0, 0, 0, '', '[]', '{}', 1)")
        )

    async with get_engine().connect() as conn:
        await conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        await conn.commit()
        try:
            async with conn.begin():
                # pin 在 v12：不跑链尾 v13，专注 v12 重建语义
                await migrate_v12(conn)
        finally:
            await conn.exec_driver_sql("PRAGMA foreign_keys=ON")
            await conn.commit()

    async with get_engine().connect() as conn:
        violations = (
            await conn.execute(text("PRAGMA foreign_key_check"))
        ).fetchall()
        ut_fks = (
            await conn.execute(text("PRAGMA foreign_key_list(user_tasks)"))
        ).mappings().all()
        snap_fks = (
            await conn.execute(
                text("PRAGMA foreign_key_list(task_backend_snapshots)")
            )
        ).mappings().all()
        gd_fks = (
            await conn.execute(text("PRAGMA foreign_key_list(global_downloads)"))
        ).mappings().all()
        gd_sql = (
            await conn.execute(
                text(
                    "SELECT sql FROM sqlite_master "
                    "WHERE type='table' AND name='global_downloads'"
                )
            )
        ).scalar_one()
        leftover = (
            await conn.execute(
                text(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
                    "AND (name LIKE '%old_v12%' OR name LIKE '%_new')"
                )
            )
        ).scalar_one()
        gd_count = (
            await conn.execute(text("SELECT COUNT(*) FROM global_downloads"))
        ).scalar_one()
        ut_count = (
            await conn.execute(text("SELECT COUNT(*) FROM user_tasks"))
        ).scalar_one()
        snap_count = (
            await conn.execute(
                text("SELECT COUNT(*) FROM task_backend_snapshots")
            )
        ).scalar_one()

    assert violations == []
    assert any(
        row["table"] == "global_downloads" and row["from"] == "global_download_id"
        for row in ut_fks
    )
    assert any(row["table"] == "global_downloads" for row in snap_fks)
    assert any(
        row["table"] == "download_sources"
        and row["from"] == "source_id"
        and row["on_delete"].upper() == "SET NULL"
        for row in gd_fks
    )
    assert any(
        row["table"] == "stored_files" and row["from"] == "completed_file_id"
        for row in gd_fks
    )
    assert "ck_global_downloads_status" in str(gd_sql)
    assert leftover == 0
    assert (gd_count, ut_count, snap_count) == (1, 1, 1)


@pytest.mark.asyncio
async def test_v12_recovers_from_leftover_temp_table(isolated_db: Path) -> None:
    """崩溃态自愈：CREATE 成功后崩溃留下的 _v12_new 残表被丢弃，迁移照常完成。"""
    from app.db.migrations import ensure_v12_download_sources_schema

    async with get_engine().begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE schema_meta (id INTEGER PRIMARY KEY, "
                "version INTEGER NOT NULL, created_at_ms INTEGER NOT NULL)"
            )
        )
        await conn.execute(text("INSERT INTO schema_meta VALUES (1, 11, 123)"))
        await conn.execute(
            text("CREATE TABLE stored_files (id INTEGER PRIMARY KEY)")
        )
        # v11 原表 + 崩溃残留的半拷贝临时表
        await conn.execute(
            text(
                "CREATE TABLE global_downloads ("
                "id INTEGER PRIMARY KEY, "
                "resource_key TEXT NOT NULL, "
                "resource_kind TEXT NOT NULL, "
                "source_uri TEXT NOT NULL, "
                "status TEXT NOT NULL, "
                "created_at_ms INTEGER NOT NULL, "
                "updated_at_ms INTEGER NOT NULL)"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO global_downloads VALUES "
                "(1, 'rk', 'http', 'http://example.com/a', 'active', 1, 1)"
            )
        )
        await conn.execute(
            text("CREATE TABLE global_downloads_v12_new (id INTEGER PRIMARY KEY)")
        )

        await ensure_v12_download_sources_schema(conn)

        tables = {
            row[0]
            for row in (
                await conn.execute(
                    text("SELECT name FROM sqlite_master WHERE type='table'")
                )
            ).all()
        }
        assert "global_downloads_v12_new" not in tables
        count = (
            await conn.execute(text("SELECT COUNT(*) FROM global_downloads"))
        ).scalar_one()
        assert count == 1
        source_fk = (
            await conn.execute(text("PRAGMA foreign_key_list(global_downloads)"))
        ).mappings().all()
        assert any(
            row["table"] == "download_sources"
            and row["from"] == "source_id"
            and row["on_delete"].upper() == "SET NULL"
            for row in source_fk
        )


@pytest.mark.asyncio
async def test_v12_recovers_from_crashed_swap(isolated_db: Path) -> None:
    """崩溃态自愈：DROP 原表后、RENAME 前崩溃 —— 残表被扶正为正式表。"""
    from app.db.migrations import ensure_v12_download_sources_schema

    async with get_engine().begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE schema_meta (id INTEGER PRIMARY KEY, "
                "version INTEGER NOT NULL, created_at_ms INTEGER NOT NULL)"
            )
        )
        await conn.execute(text("INSERT INTO schema_meta VALUES (1, 11, 123)"))
        await conn.execute(
            text(
                "CREATE TABLE download_sources ("
                "id INTEGER PRIMARY KEY, "
                "resource_kind VARCHAR(16) NOT NULL, "
                "payload_text TEXT NOT NULL, "
                "content_digest VARCHAR(64), "
                "resource_identity VARCHAR(128), "
                "created_at_ms INTEGER NOT NULL, "
                "updated_at_ms INTEGER NOT NULL)"
            )
        )
        # 崩溃现场：原表已 DROP，重建的新表卡在改名前
        await conn.execute(
            text(
                "CREATE TABLE global_downloads_v12_new ("
                "id INTEGER PRIMARY KEY, "
                "resource_key VARCHAR(128) NOT NULL, "
                "resource_kind VARCHAR(16) NOT NULL, "
                "source_uri TEXT NOT NULL, "
                "status VARCHAR(16) NOT NULL, "
                "source_id INTEGER REFERENCES download_sources (id) "
                "ON DELETE SET NULL, "
                "created_at_ms INTEGER NOT NULL, "
                "updated_at_ms INTEGER NOT NULL)"
            )
        )

        await ensure_v12_download_sources_schema(conn)

        tables = {
            row[0]
            for row in (
                await conn.execute(
                    text("SELECT name FROM sqlite_master WHERE type='table'")
                )
            ).all()
        }
        assert "global_downloads" in tables
        assert "global_downloads_v12_new" not in tables
        fk = (
            await conn.execute(text("PRAGMA foreign_key_list(global_downloads)"))
        ).mappings().all()
        assert any(row["table"] == "download_sources" for row in fk)
        idx = {
            row[0]
            for row in (
                await conn.execute(
                    text("SELECT name FROM sqlite_master WHERE type='index'")
                )
            ).all()
        }
        assert "ix_global_downloads_resource_key" in idx
