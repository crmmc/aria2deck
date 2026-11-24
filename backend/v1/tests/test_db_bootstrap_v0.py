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
from app.core.download_limiter import download_config
from app.core.rate_limit_config import rate_limit_config
from app.db.bootstrap import SCHEMA_VERSION, bootstrap_database, validate_schema_version
from app.db.engine import dispose_engine, get_engine, reset_engine, session_scope
from app.db.migrations import (
    DEFAULT_ARIA2_BT_STOP_TIMEOUT_SECONDS,
    V1_ADDED_COLUMNS,
    V1_APP_SETTINGS_ADDED_COLUMNS,
    V2_ADDED_COLUMNS,
    V2_GLOBAL_DOWNLOADS_ADDED_COLUMNS,
    V3_ADDED_COLUMNS,
    V3_GLOBAL_DOWNLOADS_ADDED_COLUMNS,
    V4_ADDED_COLUMNS,
    V4_PACK_TASKS_ADDED_COLUMNS,
    V5_ADDED_COLUMNS,
    V5_DELETE_COLUMNS,
    V6_CREDENTIAL_COLUMNS,
    V7_STORED_FILES_ADDED_COLUMNS,
    V12_APP_SETTINGS_ADDED_COLUMNS,
    V12_GLOBAL_DOWNLOADS_ADDED_COLUMNS,
    V12_USER_TASKS_ADDED_COLUMNS,
    V14_APP_SETTINGS_ADDED_COLUMNS,
    V15_APP_SETTINGS_ADDED_COLUMNS,
    V16_PACK_TASKS_ADDED_COLUMNS,
    V17_APP_SETTINGS_ADDED_COLUMNS,
    V18_PACK_TASKS_ADDED_COLUMNS,
    ensure_v8_retry_attempt_schema,
    migrate_v18,
    run_migrations,
)
from app.db.schema import metadata, sessions
from app.services.settings_service import load_runtime_config

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

    assert version == SCHEMA_VERSION
    assert users_exists == "users"
    assert settings_id == 1


@pytest.mark.asyncio
async def test_bootstrap_defaults_match_loaded_runtime_config(isolated_db: Path):
    await bootstrap_database()
    await load_runtime_config()

    async with get_engine().connect() as conn:
        row = (
            await conn.execute(text("SELECT * FROM app_settings WHERE id = 1"))
        ).mappings().one()

    for db_key, expected in rate_limit_config.defaults().items():
        expected_value = int(expected)
        scope = db_key.removeprefix("rate_limit_")
        assert row[db_key] == expected_value
        assert rate_limit_config.limit_for(scope) == expected_value

    for db_key, expected in download_config.defaults().items():
        expected_value = int(expected)
        attr = db_key.removeprefix("download_")
        assert row[db_key] == expected_value
        assert getattr(download_config, attr) == expected_value

    assert rate_limit_config.public_api > 0
    assert download_config.anonymous_total_connections() > 0
    assert row["rate_limit_authenticated_download"] == 0
    assert row["rate_limit_anonymous_download"] == 0


def test_current_schema_changes_are_accounted_for_in_migration_contract():
    accounted_columns = {
        table_name: set(columns)
        for table_name, columns in SCHEMA_V0_BASELINE_COLUMNS.items()
    }
    for table_name, columns in V1_ADDED_COLUMNS.items():
        accounted_columns.setdefault(table_name, set()).update(columns)
    for table_name, columns in V2_ADDED_COLUMNS.items():
        accounted_columns.setdefault(table_name, set()).update(columns)
    for table_name, columns in V3_ADDED_COLUMNS.items():
        accounted_columns.setdefault(table_name, set()).update(columns)
    for table_name, columns in V4_ADDED_COLUMNS.items():
        accounted_columns.setdefault(table_name, set()).update(columns)
    for table_name, columns in V5_ADDED_COLUMNS.items():
        accounted_columns.setdefault(table_name, set()).update(columns)
    for table_name, replacement in V6_CREDENTIAL_COLUMNS.items():
        accounted_columns[table_name].difference_update(replacement["removed"])
        accounted_columns[table_name].update(replacement["added"])
    accounted_columns["stored_files"].update(V7_STORED_FILES_ADDED_COLUMNS)
    accounted_columns.setdefault("users", set()).add("rpc_secret_encrypted")
    accounted_columns.setdefault("share_links", set()).add("password_encrypted")
    accounted_columns["download_sources"] = {
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
    accounted_columns.setdefault("app_settings", set()).update(
        V12_APP_SETTINGS_ADDED_COLUMNS
    )
    accounted_columns.setdefault("global_downloads", set()).update(
        V12_GLOBAL_DOWNLOADS_ADDED_COLUMNS
    )
    accounted_columns.setdefault("user_tasks", set()).update(
        V12_USER_TASKS_ADDED_COLUMNS
    )
    accounted_columns.setdefault("app_settings", set()).update(
        V14_APP_SETTINGS_ADDED_COLUMNS
    )
    accounted_columns.setdefault("app_settings", set()).update(
        V15_APP_SETTINGS_ADDED_COLUMNS
    )
    accounted_columns.setdefault("pack_tasks", set()).update(
        V16_PACK_TASKS_ADDED_COLUMNS
    )
    accounted_columns.setdefault("app_settings", set()).update(
        V17_APP_SETTINGS_ADDED_COLUMNS
    )
    accounted_columns.setdefault("pack_tasks", set()).update(
        V18_PACK_TASKS_ADDED_COLUMNS
    )
    accounted_columns["tracker_list_cache"] = {
        "id",
        "trackers_json",
        "remote_trackers_json",
        "entry_count",
        "updated_at_ms",
        "last_refresh_at_ms",
        "last_refresh_status",
        "last_refresh_failed_urls",
    }

    current_columns = {
        table.name: tuple(column.name for column in table.columns)
        for table in metadata.sorted_tables
    }

    assert set(current_columns) == set(accounted_columns)
    for table_name, columns in current_columns.items():
        assert set(columns) == accounted_columns[table_name]



@pytest.mark.asyncio
async def test_v15_to_v16_adds_pack_attempt_columns_without_backfill(
    isolated_db: Path,
) -> None:
    async with get_engine().begin() as conn:
        await conn.execute(text(
            "CREATE TABLE schema_meta (id INTEGER PRIMARY KEY, version INTEGER NOT NULL, "
            "created_at_ms INTEGER NOT NULL)"
        ))
        await conn.execute(text("INSERT INTO schema_meta VALUES (1, 15, 123)"))
        await conn.execute(text(
            "CREATE TABLE pack_tasks (id INTEGER PRIMARY KEY, status VARCHAR(16) NOT NULL)"
        ))
        await conn.execute(text("INSERT INTO pack_tasks VALUES (1, 'packing')"))

        assert await run_migrations(conn, 15) == SCHEMA_VERSION
        row = (
            await conn.execute(text(
                "SELECT started_at_ms, step, step_progress, step_started_at_ms "
                "FROM pack_tasks WHERE id = 1"
            ))
        ).one()
        columns = {
            item[1] for item in (
                await conn.execute(text("PRAGMA table_info(pack_tasks)"))
            ).all()
        }

    assert row == (None, None, 0, None)
    assert {"started_at_ms", "step"} <= columns


@pytest.mark.asyncio
async def test_v17_to_v18_adds_constrained_pack_step_progress_columns(
    isolated_db: Path,
) -> None:
    async with get_engine().begin() as conn:
        await conn.execute(text(
            "CREATE TABLE schema_meta (id INTEGER PRIMARY KEY, version INTEGER NOT NULL, "
            "created_at_ms INTEGER NOT NULL)"
        ))
        await conn.execute(text("INSERT INTO schema_meta VALUES (1, 17, 123)"))
        await conn.execute(text(
            "CREATE TABLE pack_tasks (id INTEGER PRIMARY KEY, status VARCHAR(16) NOT NULL)"
        ))
        await conn.execute(text("INSERT INTO pack_tasks VALUES (1, 'packing')"))

        assert await run_migrations(conn, 17) == SCHEMA_VERSION
        row = (
            await conn.execute(text(
                "SELECT step_progress, step_started_at_ms FROM pack_tasks WHERE id = 1"
            ))
        ).one()
        assert row == (0, None)

        for invalid_progress in (-1, 101):
            with pytest.raises(IntegrityError):
                async with conn.begin_nested():
                    await conn.execute(
                        text("UPDATE pack_tasks SET step_progress = :progress WHERE id = 1"),
                        {"progress": invalid_progress},
                    )

        await migrate_v18(conn)
        await migrate_v18(conn)
        columns = [
            item[1]
            for item in (
                await conn.execute(text("PRAGMA table_info(pack_tasks)"))
            ).all()
        ]
        version = (
            await conn.execute(text("SELECT version FROM schema_meta WHERE id = 1"))
        ).scalar_one()

    assert columns.count("step_progress") == 1
    assert columns.count("step_started_at_ms") == 1
    assert version == SCHEMA_VERSION


def test_app_settings_v1_columns_are_registered_in_migration_map():
    assert V1_ADDED_COLUMNS["app_settings"] is V1_APP_SETTINGS_ADDED_COLUMNS


def test_global_downloads_v2_columns_are_registered_in_migration_map():
    assert V2_ADDED_COLUMNS["global_downloads"] is V2_GLOBAL_DOWNLOADS_ADDED_COLUMNS


def test_global_downloads_v3_columns_are_registered_in_migration_map():
    assert V3_ADDED_COLUMNS["global_downloads"] is V3_GLOBAL_DOWNLOADS_ADDED_COLUMNS


def test_pack_tasks_v4_columns_are_registered_in_migration_map():
    assert V4_ADDED_COLUMNS["pack_tasks"] is V4_PACK_TASKS_ADDED_COLUMNS


def test_deletion_v5_columns_are_registered_in_migration_map():
    assert V5_ADDED_COLUMNS["users"] is V5_DELETE_COLUMNS
    assert V5_ADDED_COLUMNS["stored_files"] is V5_DELETE_COLUMNS


def test_credential_v6_columns_replace_plaintext_columns():
    assert V6_CREDENTIAL_COLUMNS["users"]["removed"] == {"rpc_secret"}
    assert V6_CREDENTIAL_COLUMNS["api_tokens"]["removed"] == {"token"}


@pytest.mark.asyncio
async def test_v8_schema_handles_single_quote_in_index_metadata(temp_db: str) -> None:
    index_name = "ix_global_downloads_quoted'name"
    async with get_engine().begin() as conn:
        await conn.execute(
            text(
                "CREATE INDEX \"ix_global_downloads_quoted'name\" "
                "ON global_downloads (status)"
            )
        )
        await ensure_v8_retry_attempt_schema(conn)
        await ensure_v8_retry_attempt_schema(conn)
        index_names = {
            row[1]
            for row in (
                await conn.execute(text("PRAGMA index_list('global_downloads')"))
            ).all()
        }

    assert index_name in index_names


@pytest.mark.asyncio
async def test_v2_to_latest_migration_is_idempotent(isolated_db: Path):
    async with get_engine().begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE schema_meta (id INTEGER PRIMARY KEY, "
                "version INTEGER NOT NULL, created_at_ms INTEGER NOT NULL)"
            )
        )
        await conn.execute(text("INSERT INTO schema_meta VALUES (1, 2, 123)"))
        await conn.execute(
            text(
                "CREATE TABLE global_downloads ("
                "id INTEGER PRIMARY KEY, status TEXT NOT NULL)"
            )
        )
        assert await run_migrations(conn, 2) == SCHEMA_VERSION
        assert await run_migrations(conn, 2) == SCHEMA_VERSION

    async with get_engine().connect() as conn:
        columns = {
            row[1]
            for row in (
                await conn.execute(text("PRAGMA table_info(global_downloads)"))
            ).all()
        }
        indexes = {
            row[1]
            for row in (
                await conn.execute(text("PRAGMA index_list(global_downloads)"))
            ).all()
        }
    assert set(V3_GLOBAL_DOWNLOADS_ADDED_COLUMNS) <= columns
    assert "ix_global_downloads_status_disk_reserved" in indexes


@pytest.mark.asyncio
async def test_v3_to_v4_migration_is_idempotent(isolated_db: Path):
    async with get_engine().begin() as conn:
        await conn.execute(text(
            "CREATE TABLE schema_meta (id INTEGER PRIMARY KEY, "
            "version INTEGER NOT NULL, created_at_ms INTEGER NOT NULL)"
        ))
        await conn.execute(text("INSERT INTO schema_meta VALUES (1, 3, 123)"))
        await conn.execute(text(
            "CREATE TABLE user_storage_usage (user_id INTEGER PRIMARY KEY, "
            "used_bytes INTEGER NOT NULL, reserved_bytes INTEGER NOT NULL, "
            "updated_at_ms INTEGER NOT NULL)"
        ))
        await conn.execute(text(
            "INSERT INTO user_storage_usage VALUES (1, 0, 20, 1)"
        ))
        await conn.execute(text(
            "CREATE TABLE pack_tasks (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, "
            "source_user_file_ids_json TEXT NOT NULL, source_size_bytes INTEGER NOT NULL, "
            "reserved_bytes INTEGER NOT NULL, output_stored_file_id INTEGER, "
            "status TEXT NOT NULL, error_message TEXT, updated_at_ms INTEGER NOT NULL, "
            "finished_at_ms INTEGER)"
        ))
        for task_id, sources in ((1, "[2, 1]"), (2, "[1,2]")):
            await conn.execute(
                text(
                    "INSERT INTO pack_tasks "
                    "(id, user_id, source_user_file_ids_json, source_size_bytes, "
                    "reserved_bytes, status, updated_at_ms) "
                    "VALUES (:id, 1, :sources, 10, 10, 'pending', :id)"
                ),
                {"id": task_id, "sources": sources},
            )
        assert await run_migrations(conn, 3) == SCHEMA_VERSION
        assert await run_migrations(conn, 3) == SCHEMA_VERSION

    async with get_engine().connect() as conn:
        columns = {
            row[1] for row in (
                await conn.execute(text("PRAGMA table_info(pack_tasks)"))
            ).all()
        }
        indexes = {
            row[1] for row in (
                await conn.execute(text("PRAGMA index_list(pack_tasks)"))
            ).all()
        }
        task_rows = (
            await conn.execute(text(
                "SELECT source_user_file_ids_json, status, reserved_bytes "
                "FROM pack_tasks ORDER BY id"
            ))
        ).all()
        reserved = (
            await conn.execute(text(
                "SELECT reserved_bytes FROM user_storage_usage WHERE user_id = 1"
            ))
        ).scalar_one()
    assert set(V4_PACK_TASKS_ADDED_COLUMNS) <= columns
    assert {"ix_pack_tasks_recovery", "uq_pack_tasks_active_sources"} <= indexes
    assert task_rows == [("[1,2]", "pending", 10), ("[1,2]", "failed", 0)]
    assert reserved == 10


@pytest.mark.asyncio
async def test_v4_migration_backfills_confirmed_and_unknown_source_identities(
    isolated_db: Path,
) -> None:
    async with get_engine().begin() as conn:
        await conn.execute(text(
            "CREATE TABLE schema_meta (id INTEGER PRIMARY KEY, version INTEGER, "
            "created_at_ms INTEGER)"
        ))
        await conn.execute(text("INSERT INTO schema_meta VALUES (1, 3, 1)"))
        await conn.execute(text(
            "CREATE TABLE user_storage_usage (user_id INTEGER PRIMARY KEY, "
            "used_bytes INTEGER, reserved_bytes INTEGER, updated_at_ms INTEGER)"
        ))
        await conn.execute(text("INSERT INTO user_storage_usage VALUES (1,0,10,1)"))
        await conn.execute(text(
            "CREATE TABLE stored_files (id INTEGER PRIMARY KEY, content_hash TEXT)"
        ))
        await conn.execute(text(
            "INSERT INTO stored_files VALUES (10,'confirmed_hash'),(20,'reused_hash')"
        ))
        await conn.execute(text(
            "CREATE TABLE user_files (id INTEGER PRIMARY KEY, user_id INTEGER, "
            "stored_file_id INTEGER, created_at_ms INTEGER)"
        ))
        await conn.execute(text(
            "INSERT INTO user_files VALUES (1,1,10,100),(2,1,20,300)"
        ))
        await conn.execute(text(
            "CREATE TABLE pack_tasks (id INTEGER PRIMARY KEY, user_id INTEGER, "
            "source_user_file_ids_json TEXT, source_size_bytes INTEGER, "
            "reserved_bytes INTEGER, output_stored_file_id INTEGER, "
            "delete_source INTEGER, source_cleanup_pending INTEGER DEFAULT 0, "
            "status TEXT, error_message TEXT, "
            "created_at_ms INTEGER, updated_at_ms INTEGER, finished_at_ms INTEGER)"
        ))
        await conn.execute(text(
            "INSERT INTO pack_tasks VALUES "
            "(1,1,'[1]',10,10,NULL,1,0,'pending',NULL,200,200,NULL),"
            "(2,1,'[2]',10,0,99,1,0,'completed',NULL,200,200,200),"
            "(3,1,'[3]',10,0,98,1,1,'completed',NULL,200,200,200)"
        ))
        assert await run_migrations(conn, 3) == SCHEMA_VERSION

    async with get_engine().connect() as conn:
        sources = (
            await conn.execute(text(
                "SELECT task_id, stored_file_id, user_file_created_at_ms, "
                "cleanup_state,cleanup_error FROM pack_task_sources ORDER BY task_id"
            ))
        ).all()
        tasks = (
            await conn.execute(text(
                "SELECT id,status,source_cleanup_pending,error_message "
                "FROM pack_tasks ORDER BY id"
            ))
        ).all()
    assert sources[0][:4] == (1, 10, 100, "pending")
    assert sources[1][:4] == (2, None, None, "retained")
    assert "按已保留处理" in sources[1][4]
    assert sources[2][:4] == (3, None, None, "unknown")
    assert "待清理" in sources[2][4]
    assert tasks[0][:3] == (1, "pending", 0)
    assert tasks[1][1:] == ("completed", 0, None)
    assert tasks[2][1:3] == ("completed", 0)
    assert "停止自动删除" in tasks[2][3]


@pytest.mark.asyncio
async def test_v4_migration_repairs_half_prepared_and_installs_constraints(
    isolated_db: Path,
) -> None:
    async with get_engine().begin() as conn:
        await conn.execute(text(
            "CREATE TABLE schema_meta (id INTEGER PRIMARY KEY, version INTEGER, "
            "created_at_ms INTEGER)"
        ))
        await conn.execute(text("INSERT INTO schema_meta VALUES (1,3,1)"))
        await conn.execute(text(
            "CREATE TABLE user_storage_usage (user_id INTEGER PRIMARY KEY, "
            "used_bytes INTEGER, reserved_bytes INTEGER, updated_at_ms INTEGER)"
        ))
        await conn.execute(text("INSERT INTO user_storage_usage VALUES (1,0,10,1)"))
        await conn.execute(text(
            "CREATE TABLE pack_tasks (id INTEGER PRIMARY KEY, user_id INTEGER, "
            "source_user_file_ids_json TEXT, source_size_bytes INTEGER, "
            "reserved_bytes INTEGER, output_stored_file_id INTEGER, status TEXT, "
            "error_message TEXT, prepared_content_hash TEXT, updated_at_ms INTEGER, "
            "finished_at_ms INTEGER)"
        ))
        await conn.execute(text(
            "INSERT INTO pack_tasks VALUES (1,1,'[1]',1,10,NULL,'packing',"
            "NULL,'half',1,NULL)"
        ))
        await run_migrations(conn, 3)
        row = (
            await conn.execute(text(
                "SELECT status,reserved_bytes,prepared_content_hash,"
                "prepared_size_bytes,prepared_filename FROM pack_tasks WHERE id=1"
            ))
        ).one()
        assert row == ("failed", 0, None, None, None)
        with pytest.raises(IntegrityError):
            await conn.execute(text(
                "UPDATE pack_tasks SET prepared_content_hash='half' WHERE id=1"
            ))

    async with get_engine().connect() as conn:
        definitions = {
            row[0]: "".join(str(row[1]).lower().split())
            for row in (
                await conn.execute(text(
                    "SELECT name,sql FROM sqlite_master WHERE name IN "
                    "('ix_pack_tasks_recovery','ix_pack_tasks_dispatch',"
                    "'uq_pack_tasks_active_sources',"
                    "'ix_pack_task_sources_identity',"
                    "'ix_pack_task_sources_task_cleanup',"
                    "'trg_pack_tasks_prepared_fields_insert',"
                    "'trg_pack_tasks_prepared_fields_update')"
                ))
            ).all()
        }
    assert "next_retry_at_ms" in definitions["ix_pack_tasks_dispatch"]
    assert "createuniqueindex" in definitions["uq_pack_tasks_active_sources"]
    assert "original_user_file_id" in definitions["ix_pack_task_sources_identity"]
    assert "cleanup_state" in definitions["ix_pack_task_sources_task_cleanup"]
    assert "beforeinsertonpack_tasks" in definitions["trg_pack_tasks_prepared_fields_insert"]
    assert "beforeupdateonpack_tasks" in definitions["trg_pack_tasks_prepared_fields_update"]


@pytest.mark.asyncio
async def test_validate_accepts_existing_v0_schema(isolated_db: Path):
    await bootstrap_database()
    await validate_schema_version()


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
        global_download_columns = {
            row[1]
            for row in (
                await conn.execute(text("PRAGMA table_info(global_downloads)"))
            ).all()
        }

    assert version == SCHEMA_VERSION
    assert timeout_seconds == DEFAULT_ARIA2_BT_STOP_TIMEOUT_SECONDS
    assert {
        "bt_info_hash",
        "size_known",
        "size_limit_bytes",
        "disk_reserved_bytes",
    } <= global_download_columns
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
