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
from app.db.migrations import (
    DEFAULT_ARIA2_BT_STOP_TIMEOUT_SECONDS,
    V1_ADDED_COLUMNS,
    V1_APP_SETTINGS_ADDED_COLUMNS,
)
from app.db.schema import metadata, sessions

SCHEMA_V0_BASELINE_COLUMNS: dict[str, tuple[str, ...]] = {
    "app_settings": (
        "id",
        "max_task_size_bytes",
        "min_free_disk_bytes",
        "aria2_rpc_url",
        "aria2_rpc_secret",
        "hidden_file_extensions_json",
        "pack_format",
        "pack_compression_level",
        "ws_reconnect_max_delay",
        "ws_reconnect_jitter",
        "ws_reconnect_factor",
        "site_title",
        "rate_limit_account_security",
        "rate_limit_authenticated_api",
        "rate_limit_public_api",
        "rate_limit_share_access",
        "rate_limit_authenticated_download",
        "rate_limit_anonymous_download",
        "rate_limit_create_task",
        "rate_limit_create_torrent",
        "rate_limit_create_pack",
        "rate_limit_aria2_test",
        "rate_limit_rpc",
        "download_total_connections",
        "download_authenticated_reserved_connections",
        "download_authenticated_per_user_connections",
        "download_authenticated_per_file_connections",
        "download_anonymous_base_connections",
        "download_anonymous_borrow_connections",
        "download_anonymous_per_ip_connections",
        "download_anonymous_per_file_connections",
        "created_at_ms",
        "updated_at_ms",
    ),
    "schema_meta": ("id", "version", "created_at_ms"),
    "stored_files": (
        "id",
        "content_hash",
        "real_path",
        "size_bytes",
        "is_directory",
        "original_name",
        "created_at_ms",
    ),
    "users": (
        "id",
        "username",
        "password_hash",
        "is_admin",
        "quota_bytes",
        "rpc_secret",
        "rpc_secret_created_at_ms",
        "is_initial_password",
        "created_at_ms",
        "updated_at_ms",
    ),
    "api_tokens": (
        "id",
        "user_id",
        "token",
        "name",
        "created_at_ms",
        "last_used_at_ms",
    ),
    "global_downloads": (
        "id",
        "resource_key",
        "resource_kind",
        "source_uri",
        "display_name",
        "aria2_gid",
        "status",
        "total_bytes",
        "completed_bytes",
        "error_code",
        "error_message",
        "completed_file_id",
        "created_at_ms",
        "updated_at_ms",
        "completed_at_ms",
    ),
    "pack_tasks": (
        "id",
        "user_id",
        "source_user_file_ids_json",
        "source_size_bytes",
        "reserved_bytes",
        "output_name",
        "output_stored_file_id",
        "delete_source",
        "status",
        "progress",
        "error_message",
        "created_at_ms",
        "updated_at_ms",
        "finished_at_ms",
    ),
    "sessions": ("id", "user_id", "expires_at_ms", "created_at_ms"),
    "stored_file_entries": (
        "id",
        "stored_file_id",
        "relative_path",
        "parent_path",
        "name",
        "size_bytes",
        "is_dir",
        "mtime_ms",
        "sort_key",
    ),
    "user_files": (
        "id",
        "user_id",
        "stored_file_id",
        "display_name",
        "created_at_ms",
        "updated_at_ms",
    ),
    "user_storage_usage": (
        "user_id",
        "used_bytes",
        "reserved_bytes",
        "updated_at_ms",
    ),
    "share_links": (
        "id",
        "share_code",
        "owner_id",
        "user_file_id",
        "password_hash",
        "expires_at_ms",
        "max_downloads",
        "download_count",
        "status",
        "created_at_ms",
        "last_accessed_at_ms",
    ),
    "user_tasks": (
        "id",
        "user_id",
        "global_download_id",
        "status",
        "reserved_bytes",
        "display_name",
        "error_message",
        "created_at_ms",
        "updated_at_ms",
        "finished_at_ms",
    ),
}


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
async def test_bootstrap_creates_latest_schema(isolated_db: Path):
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

    assert version == SCHEMA_VERSION == 1
    assert users_exists == "users"
    assert settings_id == 1


def test_current_schema_changes_are_accounted_for_in_migration_contract():
    accounted_columns = {
        table_name: set(columns)
        for table_name, columns in SCHEMA_V0_BASELINE_COLUMNS.items()
    }
    for table_name, columns in V1_ADDED_COLUMNS.items():
        accounted_columns.setdefault(table_name, set()).update(columns)

    current_columns = {
        table.name: tuple(column.name for column in table.columns)
        for table in metadata.sorted_tables
    }

    assert set(current_columns) == set(accounted_columns)
    for table_name, columns in current_columns.items():
        assert set(columns) == accounted_columns[table_name]


def test_app_settings_v1_columns_are_registered_in_migration_map():
    assert V1_ADDED_COLUMNS["app_settings"] is V1_APP_SETTINGS_ADDED_COLUMNS


@pytest.mark.asyncio
async def test_validate_accepts_existing_v0_schema(isolated_db: Path):
    await bootstrap_database()
    assert await validate_schema_version() is None


@pytest.mark.asyncio
async def test_bootstrap_migrates_existing_v0_schema_to_latest_version(
    isolated_db: Path,
):
    await bootstrap_database()

    async with get_engine().begin() as conn:
        await conn.execute(
            text("ALTER TABLE app_settings DROP COLUMN aria2_bt_stop_timeout_seconds")
        )
        await conn.execute(text("DROP TABLE schema_meta"))
        await conn.execute(
            text(
                "CREATE TABLE schema_meta ("
                "id INTEGER PRIMARY KEY, "
                "version INTEGER NOT NULL, "
                "created_at_ms INTEGER NOT NULL, "
                "CONSTRAINT ck_schema_meta_single_row CHECK (id = 1), "
                "CONSTRAINT ck_schema_meta_version_0 CHECK (version = 0)"
                ")"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO schema_meta (id, version, created_at_ms) "
                "VALUES (1, 0, 123)"
            )
        )

    await bootstrap_database()

    async with get_engine().connect() as conn:
        version = (
            await conn.execute(text("SELECT version FROM schema_meta WHERE id = 1"))
        ).scalar_one()
        timeout_seconds = (
            await conn.execute(
                text(
                    "SELECT aria2_bt_stop_timeout_seconds "
                    "FROM app_settings WHERE id = 1"
                )
            )
        ).scalar_one()
        schema_meta_sql = (
            await conn.execute(
                text(
                    "SELECT sql FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'schema_meta'"
                )
            )
        ).scalar_one()

    assert version == SCHEMA_VERSION == 1
    assert timeout_seconds == DEFAULT_ARIA2_BT_STOP_TIMEOUT_SECONDS
    assert "version = 0" not in schema_meta_sql
    assert "version >= 0" in schema_meta_sql


@pytest.mark.asyncio
async def test_bootstrap_rejects_existing_latest_schema_with_unrepaired_missing_column(
    isolated_db: Path,
):
    await bootstrap_database()

    async with get_engine().begin() as conn:
        await conn.execute(text("ALTER TABLE users DROP COLUMN quota_bytes"))

    with pytest.raises(
        RuntimeError, match="missing required columns.*users.quota_bytes"
    ):
        await bootstrap_database()


@pytest.mark.asyncio
async def test_bootstrap_rejects_existing_latest_schema_with_missing_required_table(
    isolated_db: Path,
):
    await bootstrap_database()

    async with get_engine().begin() as conn:
        await conn.execute(text("DROP TABLE user_storage_usage"))

    with pytest.raises(
        RuntimeError, match="missing required tables: user_storage_usage"
    ):
        await bootstrap_database()


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
async def test_bootstrap_rejects_future_version(isolated_db: Path):
    conn = sqlite3.connect(isolated_db)
    conn.execute(
        "CREATE TABLE schema_meta ("
        "id INTEGER PRIMARY KEY, "
        "version INTEGER NOT NULL, "
        "created_at_ms INTEGER NOT NULL, "
        "CONSTRAINT ck_schema_meta_non_negative_version CHECK (version >= 0)"
        ")"
    )
    conn.execute(
        "INSERT INTO schema_meta (id, version, created_at_ms) VALUES (1, 99, 1)"
    )
    conn.commit()
    conn.close()

    reset_engine()
    with pytest.raises(RuntimeError, match="newer than supported version"):
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
