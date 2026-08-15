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
from app.db.migrations import run_migrations


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
    assert version == SCHEMA_VERSION == 12
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

        assert await run_migrations(conn, 11) == 12
        assert await run_migrations(conn, 11) == 12

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
    assert version == 12
    assert any(
        row["table"] == "download_sources"
        and row["from"] == "source_id"
        and row["to"] == "id"
        and row["on_delete"].upper() == "SET NULL"
        for row in source_fk
    )
