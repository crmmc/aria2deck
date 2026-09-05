from __future__ import annotations

import json
from collections.abc import Awaitable, Callable

from sqlalchemy import bindparam, inspect, text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.security import credential_digest, credential_prefix
from app.db.schema import SCHEMA_VERSION

DEFAULT_ARIA2_BT_STOP_TIMEOUT_SECONDS = 7 * 24 * 60 * 60
V1_APP_SETTINGS_ADDED_COLUMNS = {
    "aria2_bt_stop_timeout_seconds": (
        f"INTEGER NOT NULL DEFAULT {DEFAULT_ARIA2_BT_STOP_TIMEOUT_SECONDS}"
    ),
}
V1_ADDED_COLUMNS = {
    "app_settings": V1_APP_SETTINGS_ADDED_COLUMNS,
}
V2_GLOBAL_DOWNLOADS_ADDED_COLUMNS = {
    "bt_info_hash": "VARCHAR(40)",
}
V2_ADDED_COLUMNS = {
    "global_downloads": V2_GLOBAL_DOWNLOADS_ADDED_COLUMNS,
}
V3_GLOBAL_DOWNLOADS_ADDED_COLUMNS = {
    "size_known": "INTEGER NOT NULL DEFAULT 0 CHECK (size_known IN (0, 1))",
    "size_limit_bytes": (
        "INTEGER NOT NULL DEFAULT 0 CHECK (size_limit_bytes >= 0)"
    ),
    "disk_reserved_bytes": (
        "INTEGER NOT NULL DEFAULT 0 CHECK (disk_reserved_bytes >= 0)"
    ),
}
V3_ADDED_COLUMNS = {
    "global_downloads": V3_GLOBAL_DOWNLOADS_ADDED_COLUMNS,
}
V4_PACK_TASKS_ADDED_COLUMNS = {
    "materialized_bytes": (
        "INTEGER NOT NULL DEFAULT 0 CHECK (materialized_bytes >= 0)"
    ),
    "install_reserved_bytes": (
        "INTEGER NOT NULL DEFAULT 0 CHECK (install_reserved_bytes >= 0)"
    ),
    "retry_count": "INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0)",
    "next_retry_at_ms": (
        "INTEGER CHECK (next_retry_at_ms IS NULL OR next_retry_at_ms >= 0)"
    ),
    "prepared_content_hash": "VARCHAR(128)",
    "prepared_size_bytes": (
        "INTEGER CHECK (prepared_size_bytes IS NULL OR prepared_size_bytes >= 0)"
    ),
    "prepared_filename": "TEXT",
    "source_cleanup_pending": (
        "INTEGER NOT NULL DEFAULT 0 CHECK (source_cleanup_pending IN (0, 1))"
    ),
}
V4_PACK_TASK_SOURCES_COLUMNS = {
    "task_id": "INTEGER NOT NULL",
    "ordinal": "INTEGER NOT NULL",
    "original_user_file_id": "INTEGER NOT NULL",
    "stored_file_id": "INTEGER",
    "user_file_created_at_ms": "INTEGER",
    "content_hash": "VARCHAR(128)",
    "cleanup_state": "VARCHAR(32) NOT NULL",
    "cleanup_error": "TEXT",
    "cleanup_real_path": "TEXT",
    "cleaned_at_ms": "INTEGER",
}
V4_ADDED_COLUMNS = {
    "pack_tasks": V4_PACK_TASKS_ADDED_COLUMNS,
    "pack_task_sources": V4_PACK_TASK_SOURCES_COLUMNS,
}
V5_DELETE_COLUMNS = {
    "pending_delete": (
        "INTEGER NOT NULL DEFAULT 0 CHECK (pending_delete IN (0, 1))"
    ),
    "delete_attempts": (
        "INTEGER NOT NULL DEFAULT 0 CHECK (delete_attempts >= 0)"
    ),
    "delete_next_retry_at_ms": (
        "INTEGER CHECK (delete_next_retry_at_ms IS NULL OR "
        "delete_next_retry_at_ms >= 0)"
    ),
    "delete_lease_token": "VARCHAR(64)",
    "delete_lease_expires_at_ms": (
        "INTEGER CHECK (delete_lease_expires_at_ms IS NULL OR "
        "delete_lease_expires_at_ms >= 0)"
    ),
    "delete_error": "TEXT",
}
V5_ADDED_COLUMNS = {
    "users": V5_DELETE_COLUMNS,
    "stored_files": V5_DELETE_COLUMNS,
}

V6_CREDENTIAL_COLUMNS = {
    "users": {
        "removed": frozenset({"rpc_secret"}),
        "added": frozenset({"rpc_secret_digest", "rpc_secret_prefix"}),
    },
    "api_tokens": {
        "removed": frozenset({"token"}),
        "added": frozenset({"token_digest", "token_prefix"}),
    },
}
V6_REQUIRED_USER_COLUMNS = frozenset({
    "id", "username", "password_hash", "is_admin", "quota_bytes", "rpc_secret",
    "rpc_secret_created_at_ms", "is_initial_password", "created_at_ms", "updated_at_ms",
    "pending_delete", "delete_attempts", "delete_next_retry_at_ms",
    "delete_lease_token", "delete_lease_expires_at_ms", "delete_error",
})
V6_REQUIRED_TOKEN_COLUMNS = frozenset({
    "id", "user_id", "token", "name", "created_at_ms", "last_used_at_ms",
})
V7_STORED_FILES_ADDED_COLUMNS = {
    "content_hash_version": "VARCHAR(8) NOT NULL DEFAULT 'v1'",
    "content_object_kind": "VARCHAR(16) NOT NULL DEFAULT 'legacy'",
    "content_digest": "VARCHAR(128)",
}


Migration = Callable[[AsyncConnection], Awaitable[None]]


async def _table_names(conn: AsyncConnection) -> set[str]:
    return await conn.run_sync(
        lambda sync_conn: set(inspect(sync_conn).get_table_names())
    )


async def _column_names(conn: AsyncConnection, table_name: str) -> set[str]:
    return await conn.run_sync(
        lambda sync_conn: {
            column["name"] for column in inspect(sync_conn).get_columns(table_name)
        }
    )


async def _add_missing_columns(
    conn: AsyncConnection,
    table_name: str,
    column_definitions: dict[str, str],
) -> None:
    table_names = await _table_names(conn)
    if table_name not in table_names:
        return

    column_names = await _column_names(conn, table_name)
    for column_name, column_sql in column_definitions.items():
        if column_name not in column_names:
            await conn.execute(
                # Table, column, and type are source-defined migration constants; DDL cannot bind them.
                # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
                text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_sql}")
            )


async def _rebuild_schema_meta(conn: AsyncConnection, version: int) -> None:
    row = (
        await conn.execute(text("SELECT created_at_ms FROM schema_meta WHERE id = 1"))
    ).first()
    created_at_ms = int(row[0]) if row is not None else 0

    await conn.execute(text("ALTER TABLE schema_meta RENAME TO schema_meta_old"))
    await conn.execute(
        text(
            "CREATE TABLE schema_meta ("
            "id INTEGER NOT NULL PRIMARY KEY, "
            "version INTEGER NOT NULL, "
            "created_at_ms INTEGER NOT NULL, "
            "CONSTRAINT ck_schema_meta_single_row CHECK (id = 1), "
            "CONSTRAINT ck_schema_meta_non_negative_version CHECK (version >= 0)"
            ")"
        )
    )
    await conn.execute(
        text(
            "INSERT INTO schema_meta (id, version, created_at_ms) "
            "VALUES (1, :version, :created_at_ms)"
        ),
        {"version": version, "created_at_ms": created_at_ms},
    )
    await conn.execute(text("DROP TABLE schema_meta_old"))


async def ensure_v8_retry_attempt_schema(conn: AsyncConnection) -> None:
    """Rebuild global_downloads so retry attempts only require live uniqueness."""
    if "global_downloads" not in await _table_names(conn):
        return
    if "resource_key" not in await _column_names(conn, "global_downloads"):
        return

    rows = (
        await conn.execute(text("PRAGMA index_list('global_downloads')"))
    ).all()
    has_table_wide_unique = False
    for row in rows:
        index_name = str(row[1])
        origin = str(row[3] or "")
        quoted_index_name = index_name.replace("'", "''")
        columns = (
            await conn.execute(
                # The name is SQLite schema metadata escaped as a string literal; PRAGMA cannot bind it.
                # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
                text(f"PRAGMA index_info('{quoted_index_name}')")
            )
        ).all()
        column_names = {str(col[2]) for col in columns}
        if column_names != {"resource_key"}:
            continue
        if origin == "pk":
            continue
        sql_row = (
            await conn.execute(
                text(
                    "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = :name"
                ),
                {"name": index_name},
            )
        ).first()
        definition = str(sql_row[0] or "").lower() if sql_row else ""
        if "where" in definition and "status" in definition:
            continue
        if origin == "u" or "unique" in definition:
            has_table_wide_unique = True
            break

    if has_table_wide_unique:
        await conn.execute(
            text(
                "CREATE TABLE global_downloads_v8 ("
                "id INTEGER NOT NULL PRIMARY KEY, "
                "resource_key VARCHAR(128) NOT NULL, "
                "resource_kind VARCHAR(16) NOT NULL, "
                "source_uri TEXT NOT NULL, "
                "bt_info_hash VARCHAR(40), "
                "display_name TEXT, "
                "aria2_gid VARCHAR(32) UNIQUE, "
                "status VARCHAR(16) NOT NULL, "
                "total_bytes INTEGER NOT NULL DEFAULT 0, "
                "completed_bytes INTEGER NOT NULL DEFAULT 0, "
                "size_known INTEGER NOT NULL DEFAULT 0, "
                "size_limit_bytes INTEGER NOT NULL DEFAULT 0, "
                "disk_reserved_bytes INTEGER NOT NULL DEFAULT 0, "
                "error_code VARCHAR(64), "
                "error_message TEXT, "
                "completed_file_id INTEGER, "
                "created_at_ms INTEGER NOT NULL, "
                "updated_at_ms INTEGER NOT NULL, "
                "completed_at_ms INTEGER, "
                "CONSTRAINT ck_global_downloads_resource_kind CHECK "
                "(resource_kind IN ('http', 'magnet', 'torrent', 'other')), "
                "CONSTRAINT ck_global_downloads_status CHECK "
                "(status IN ('queued', 'active', 'waiting', 'paused', 'completed', 'failed', 'cancelled')), "
                "CONSTRAINT ck_global_downloads_size_known_bool CHECK (size_known IN (0, 1)), "
                "CONSTRAINT ck_global_downloads_size_limit_non_negative CHECK (size_limit_bytes >= 0), "
                "CONSTRAINT ck_global_downloads_disk_reserved_non_negative CHECK (disk_reserved_bytes >= 0), "
                "FOREIGN KEY(completed_file_id) REFERENCES stored_files (id)"
                ")"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO global_downloads_v8 ("
                "id, resource_key, resource_kind, source_uri, bt_info_hash, display_name, "
                "aria2_gid, status, total_bytes, completed_bytes, size_known, "
                "size_limit_bytes, disk_reserved_bytes, error_code, error_message, "
                "completed_file_id, created_at_ms, updated_at_ms, completed_at_ms"
                ") SELECT "
                "id, resource_key, resource_kind, source_uri, bt_info_hash, display_name, "
                "aria2_gid, status, total_bytes, completed_bytes, size_known, "
                "size_limit_bytes, disk_reserved_bytes, error_code, error_message, "
                "completed_file_id, created_at_ms, updated_at_ms, completed_at_ms "
                "FROM global_downloads"
            )
        )
        await conn.execute(text("DROP TABLE global_downloads"))
        await conn.execute(
            text("ALTER TABLE global_downloads_v8 RENAME TO global_downloads")
        )

    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_global_downloads_status_gid "
            "ON global_downloads (status, aria2_gid)"
        )
    )
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_global_downloads_status_disk_reserved "
            "ON global_downloads (status, disk_reserved_bytes)"
        )
    )
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_global_downloads_completed_file_id "
            "ON global_downloads (completed_file_id)"
        )
    )
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_global_downloads_resource_key "
            "ON global_downloads (resource_key)"
        )
    )
    await conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_global_downloads_live_resource "
            "ON global_downloads (resource_key) "
            "WHERE status IN ('queued', 'active', 'waiting', 'paused')"
        )
    )


async def migrate_v8(conn: AsyncConnection) -> None:
    await ensure_v8_retry_attempt_schema(conn)
    await _rebuild_schema_meta(conn, 8)


async def ensure_v9_backend_snapshot_schema(conn: AsyncConnection) -> None:
    await conn.execute(
        text(
            "CREATE TABLE IF NOT EXISTS task_backend_snapshots ("
            "global_download_id INTEGER NOT NULL PRIMARY KEY, "
            "download_speed INTEGER NOT NULL DEFAULT 0, "
            "upload_speed INTEGER NOT NULL DEFAULT 0, "
            "total_length INTEGER NOT NULL DEFAULT 0, "
            "completed_length INTEGER NOT NULL DEFAULT 0, "
            "status VARCHAR(32) NOT NULL DEFAULT '', "
            "files_json TEXT NOT NULL DEFAULT '[]', "
            "raw_json TEXT NOT NULL DEFAULT '{}', "
            "updated_at_ms INTEGER NOT NULL, "
            "FOREIGN KEY(global_download_id) REFERENCES global_downloads (id) "
            "ON DELETE CASCADE"
            ")"
        )
    )
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_task_backend_snapshots_updated_at "
            "ON task_backend_snapshots (updated_at_ms)"
        )
    )


async def migrate_v9(conn: AsyncConnection) -> None:
    await ensure_v9_backend_snapshot_schema(conn)
    await _rebuild_schema_meta(conn, 9)


async def ensure_v10_rpc_secret_encrypted(conn: AsyncConnection) -> None:
    """v10: users.rpc_secret_encrypted 列，存储加密后的 RPC 密钥明文。"""
    if "users" not in await _table_names(conn):
        return
    if "rpc_secret_encrypted" not in await _column_names(conn, "users"):
        await conn.execute(
            text("ALTER TABLE users ADD COLUMN rpc_secret_encrypted TEXT")
        )


async def migrate_v10(conn: AsyncConnection) -> None:
    await ensure_v10_rpc_secret_encrypted(conn)
    await _rebuild_schema_meta(conn, 10)


async def ensure_v11_share_password_encrypted(conn: AsyncConnection) -> None:
    """v11: share_links.password_encrypted 列，存储加密后的分享密码明文。"""
    if "share_links" not in await _table_names(conn):
        return
    if "password_encrypted" not in await _column_names(conn, "share_links"):
        await conn.execute(
            text("ALTER TABLE share_links ADD COLUMN password_encrypted TEXT")
        )


async def migrate_v11(conn: AsyncConnection) -> None:
    await ensure_v11_share_password_encrypted(conn)
    await _rebuild_schema_meta(conn, 11)


V12_APP_SETTINGS_ADDED_COLUMNS = {
    "history_retention_days": (
        "INTEGER NOT NULL DEFAULT 30 "
        "CHECK (history_retention_days >= 1)"
    ),
}
V12_GLOBAL_DOWNLOADS_ADDED_COLUMNS = {
    "source_id": "INTEGER",
}
V12_USER_TASKS_ADDED_COLUMNS = {
    "history_expired_at_ms": "INTEGER",
}


async def ensure_v12_download_sources_schema(conn: AsyncConnection) -> None:
    """v12: download_sources + history retention columns."""
    await _recover_crashed_v12_swap(conn)
    await conn.execute(
        text(
            "CREATE TABLE IF NOT EXISTS download_sources ("
            "id INTEGER NOT NULL PRIMARY KEY, "
            "resource_kind VARCHAR(16) NOT NULL, "
            "payload_text TEXT NOT NULL, "
            "selection_json TEXT, "
            "options_json TEXT, "
            "content_digest VARCHAR(64), "
            "resource_identity VARCHAR(128), "
            "created_at_ms INTEGER NOT NULL, "
            "updated_at_ms INTEGER NOT NULL, "
            "purged_at_ms INTEGER, "
            "CONSTRAINT ck_download_sources_resource_kind "
            "CHECK (resource_kind IN ('http', 'magnet', 'torrent', 'other'))"
            ")"
        )
    )
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_download_sources_content_digest "
            "ON download_sources (content_digest)"
        )
    )
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_download_sources_resource_identity "
            "ON download_sources (resource_identity)"
        )
    )
    await _add_missing_columns(
        conn, "app_settings", V12_APP_SETTINGS_ADDED_COLUMNS
    )
    await _add_missing_columns(
        conn, "global_downloads", V12_GLOBAL_DOWNLOADS_ADDED_COLUMNS
    )
    await _add_missing_columns(
        conn, "user_tasks", V12_USER_TASKS_ADDED_COLUMNS
    )
    table_names = await _table_names(conn)
    if "global_downloads" not in table_names:
        return
    if "source_id" in await _column_names(conn, "global_downloads"):
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_global_downloads_source_id "
                "ON global_downloads (source_id)"
            )
        )
    fk_rows = (
        await conn.execute(text("PRAGMA foreign_key_list('global_downloads')"))
    ).mappings().all()
    has_source_fk = any(
        str(row["table"]) == "download_sources"
        and str(row["from"]) == "source_id"
        for row in fk_rows
    )
    if not has_source_fk and "source_id" in await _column_names(
        conn, "global_downloads"
    ):
        await _rebuild_global_downloads_source_fk(conn)

    # Indexes are always (re-)created idempotently: a crash between RENAME
    # and index creation must not leave the schema missing unique/index
    # protection with the FK already present (rebuild would be skipped).
    await _ensure_global_downloads_indexes(conn)


V12_GLOBAL_DOWNLOADS_CREATE = (
    "CREATE TABLE {name} ("
    "id INTEGER NOT NULL PRIMARY KEY, "
    "resource_key VARCHAR(128) NOT NULL, "
    "resource_kind VARCHAR(16) NOT NULL, "
    "source_uri TEXT NOT NULL, "
    "bt_info_hash VARCHAR(40), "
    "display_name TEXT, "
    "aria2_gid VARCHAR(32) UNIQUE, "
    "status VARCHAR(16) NOT NULL, "
    "total_bytes INTEGER NOT NULL DEFAULT 0, "
    "completed_bytes INTEGER NOT NULL DEFAULT 0, "
    "size_known INTEGER NOT NULL DEFAULT 0, "
    "size_limit_bytes INTEGER NOT NULL DEFAULT 0, "
    "disk_reserved_bytes INTEGER NOT NULL DEFAULT 0, "
    "error_code VARCHAR(64), "
    "error_message TEXT, "
    "completed_file_id INTEGER{stored_files_fk}, "
    "created_at_ms INTEGER NOT NULL, "
    "updated_at_ms INTEGER NOT NULL, "
    "completed_at_ms INTEGER, "
    "source_id INTEGER REFERENCES download_sources (id) ON DELETE SET NULL, "
    "CONSTRAINT ck_global_downloads_resource_kind "
    "CHECK (resource_kind IN ('http', 'magnet', 'torrent', 'other')), "
    "CONSTRAINT ck_global_downloads_status "
    "CHECK (status IN ('queued', 'active', 'waiting', 'paused', "
    "'completed', 'failed', 'cancelled')), "
    "CONSTRAINT ck_global_downloads_size_known_bool "
    "CHECK (size_known IN (0, 1)), "
    "CONSTRAINT ck_global_downloads_size_limit_non_negative "
    "CHECK (size_limit_bytes >= 0), "
    "CONSTRAINT ck_global_downloads_disk_reserved_non_negative "
    "CHECK (disk_reserved_bytes >= 0))"
)


async def _recover_crashed_v12_swap(conn: AsyncConnection) -> None:
    """Self-heal half-done v12 rebuilds from an earlier crash.

    - original dropped + temp not renamed: finish the swap;
    - leftover half-copied temp table: discard it.
    """
    table_names = await _table_names(conn)
    if "global_downloads" not in table_names:
        if "global_downloads_v12_new" in table_names:
            await conn.execute(
                text(
                    "ALTER TABLE global_downloads_v12_new "
                    "RENAME TO global_downloads"
                )
            )
    elif "global_downloads_v12_new" in table_names:
        await conn.execute(text("DROP TABLE global_downloads_v12_new"))


async def _rebuild_global_downloads_source_fk(conn: AsyncConnection) -> None:
    """Attach source_id FK ON DELETE SET NULL via table rebuild.

    Order is create-new -> copy -> drop original -> rename: the original
    table is never renamed, because SQLite rewrites child FK definitions
    (user_tasks, task_backend_snapshots) to a renamed parent, leaving them
    dangling after the old table is dropped. The rebuild uses the canonical
    v12 shape (CHECK constraints, aria2_gid UNIQUE, completed_file_id FK)
    rather than pragma-derived column defs.

    Self-heals half-done rebuilds from an earlier crash: a leftover temp
    table is discarded; a crashed swap (original dropped, temp not yet
    renamed) is finished before anything else runs.
    """
    await _recover_crashed_v12_swap(conn)

    columns = await _column_names(conn, "global_downloads")
    column_list = ", ".join(
        '"' + column.replace('"', '""') + '"' for column in sorted(columns)
    )
    # Only reference stored_files when it exists (any real deployment has it);
    # a FK clause against a missing table breaks INSERT preparation on
    # minimal legacy/test schemas.
    table_names = await _table_names(conn)
    stored_files_fk = (
        " REFERENCES stored_files (id)" if "stored_files" in table_names else ""
    )
    create_sql = V12_GLOBAL_DOWNLOADS_CREATE.replace(
        "{stored_files_fk}", stored_files_fk
    )
    await conn.execute(text(create_sql.format(name="global_downloads_v12_new")))
    await conn.execute(
        # Inspector column names are SQLite-quoted identifiers; identifiers cannot be bound.
        # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
        text(
            f"INSERT INTO global_downloads_v12_new ({column_list}) "  # noqa: S608 - inspector identifiers are SQLite-quoted and cannot be bound
            f"SELECT {column_list} FROM global_downloads"
        )
    )
    await conn.execute(text("DROP TABLE global_downloads"))
    await conn.execute(
        text("ALTER TABLE global_downloads_v12_new RENAME TO global_downloads")
    )



async def _ensure_global_downloads_indexes(conn: AsyncConnection) -> None:
    """Idempotently (re)create the global_downloads indexes."""
    if "global_downloads" not in await _table_names(conn):
        return
    columns = set(await _column_names(conn, "global_downloads"))

    async def _maybe_index(sql: str, required: set[str]) -> None:
        if required <= columns:
            await conn.execute(text(sql))

    await _maybe_index(
        "CREATE INDEX IF NOT EXISTS ix_global_downloads_status_gid "
        "ON global_downloads (status, aria2_gid)",
        {"status", "aria2_gid"},
    )
    await _maybe_index(
        "CREATE INDEX IF NOT EXISTS ix_global_downloads_status_disk_reserved "
        "ON global_downloads (status, disk_reserved_bytes)",
        {"status", "disk_reserved_bytes"},
    )
    await _maybe_index(
        "CREATE INDEX IF NOT EXISTS ix_global_downloads_completed_file_id "
        "ON global_downloads (completed_file_id)",
        {"completed_file_id"},
    )
    await _maybe_index(
        "CREATE INDEX IF NOT EXISTS ix_global_downloads_resource_key "
        "ON global_downloads (resource_key)",
        {"resource_key"},
    )
    await _maybe_index(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_global_downloads_live_resource "
        "ON global_downloads (resource_key) "
        "WHERE status IN ('queued', 'active', 'waiting', 'paused')",
        {"resource_key", "status"},
    )
    await _maybe_index(
        "CREATE INDEX IF NOT EXISTS ix_global_downloads_source_id "
        "ON global_downloads (source_id)",
        {"source_id"},
    )


async def migrate_v12(conn: AsyncConnection) -> None:
    await ensure_v12_download_sources_schema(conn)
    await _rebuild_schema_meta(conn, 12)


async def ensure_v13_drop_backend_snapshots(conn: AsyncConnection) -> None:
    """v13: drop task_backend_snapshots (replaced by the in-process
    observation store; M11). Idempotent by itself: DROP IF EXISTS leaves
    nothing half-done to self-heal."""
    await conn.execute(text("DROP TABLE IF EXISTS task_backend_snapshots"))


async def migrate_v13(conn: AsyncConnection) -> None:
    await ensure_v13_drop_backend_snapshots(conn)
    await _rebuild_schema_meta(conn, 13)


V14_APP_SETTINGS_ADDED_COLUMNS = {
    "rate_limit_file_search": "INTEGER NOT NULL DEFAULT 20",
}


async def migrate_v14(conn: AsyncConnection) -> None:
    await _add_missing_columns(conn, "app_settings", V14_APP_SETTINGS_ADDED_COLUMNS)
    await _rebuild_schema_meta(conn, 14)


V15_APP_SETTINGS_ADDED_COLUMNS = {
    "tracker_fixed_list": "TEXT NOT NULL DEFAULT ''",
    "tracker_remote_urls": "TEXT NOT NULL DEFAULT ''",
    "tracker_refresh_interval_minutes": "INTEGER NOT NULL DEFAULT 0",
}


async def ensure_v15_tracker_list_schema(conn: AsyncConnection) -> None:
    """v15: app_settings tracker 三列 + tracker_list_cache 单行缓存表。"""
    await _add_missing_columns(conn, "app_settings", V15_APP_SETTINGS_ADDED_COLUMNS)
    await conn.execute(
        text(
            "CREATE TABLE IF NOT EXISTS tracker_list_cache ("
            "id INTEGER NOT NULL PRIMARY KEY, "
            "trackers_json TEXT NOT NULL DEFAULT '[]', "
            "remote_trackers_json TEXT NOT NULL DEFAULT '[]', "
            "entry_count INTEGER NOT NULL DEFAULT 0, "
            "updated_at_ms INTEGER, "
            "last_refresh_at_ms INTEGER, "
            "last_refresh_status TEXT NOT NULL DEFAULT 'never', "
            "last_refresh_failed_urls TEXT NOT NULL DEFAULT '[]', "
            "CONSTRAINT ck_tracker_list_cache_single_row CHECK (id = 1)"
            ")"
        )
    )


async def migrate_v15(conn: AsyncConnection) -> None:
    await ensure_v15_tracker_list_schema(conn)
    await _rebuild_schema_meta(conn, 15)


V16_PACK_TASKS_ADDED_COLUMNS = {
    "started_at_ms": "INTEGER",
    "step": (
        "VARCHAR(16) CHECK (step IS NULL OR "
        "step IN ('validating', 'compressing', 'verifying'))"
    ),
}


async def migrate_v16(conn: AsyncConnection) -> None:
    await _add_missing_columns(conn, "pack_tasks", V16_PACK_TASKS_ADDED_COLUMNS)
    await _rebuild_schema_meta(conn, 16)


V17_APP_SETTINGS_ADDED_COLUMNS = {
    "rate_limit_create_share": "INTEGER NOT NULL DEFAULT 10",
}


async def migrate_v17(conn: AsyncConnection) -> None:
    await _add_missing_columns(conn, "app_settings", V17_APP_SETTINGS_ADDED_COLUMNS)
    await _rebuild_schema_meta(conn, 17)


V18_PACK_TASKS_ADDED_COLUMNS = {
    "step_progress": (
        "INTEGER NOT NULL DEFAULT 0 CHECK (step_progress >= 0 AND step_progress <= 100)"
    ),
    "step_started_at_ms": "INTEGER",
}


async def migrate_v18(conn: AsyncConnection) -> None:
    await _add_missing_columns(conn, "pack_tasks", V18_PACK_TASKS_ADDED_COLUMNS)
    await _rebuild_schema_meta(conn, 18)


async def migrate_v1(conn: AsyncConnection) -> None:
    await _add_missing_columns(conn, "app_settings", V1_APP_SETTINGS_ADDED_COLUMNS)
    await _rebuild_schema_meta(conn, 1)


async def migrate_v2(conn: AsyncConnection) -> None:
    await _add_missing_columns(
        conn, "global_downloads", V2_GLOBAL_DOWNLOADS_ADDED_COLUMNS
    )
    await _rebuild_schema_meta(conn, 2)


async def migrate_v3(conn: AsyncConnection) -> None:
    await _add_missing_columns(
        conn, "global_downloads", V3_GLOBAL_DOWNLOADS_ADDED_COLUMNS
    )
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_global_downloads_status_disk_reserved "
            "ON global_downloads (status, disk_reserved_bytes)"
        )
    )
    await _rebuild_schema_meta(conn, 3)


async def _create_v4_pack_source_table(conn: AsyncConnection) -> None:
    await conn.execute(
        text(
            "CREATE TABLE IF NOT EXISTS pack_task_sources ("
            "task_id INTEGER NOT NULL, ordinal INTEGER NOT NULL, "
            "original_user_file_id INTEGER NOT NULL, stored_file_id INTEGER, "
            "user_file_created_at_ms INTEGER, content_hash VARCHAR(128), "
            "cleanup_state VARCHAR(32) NOT NULL, cleanup_error TEXT, "
            "cleanup_real_path TEXT, cleaned_at_ms INTEGER, "
            "PRIMARY KEY (task_id, ordinal), "
            "CONSTRAINT fk_pack_task_sources_task_id_pack_tasks "
            "FOREIGN KEY(task_id) REFERENCES pack_tasks (id) ON DELETE CASCADE, "
            "CONSTRAINT ck_pack_task_sources_ordinal_non_negative CHECK (ordinal >= 0), "
            "CONSTRAINT ck_pack_task_sources_cleanup_state CHECK (cleanup_state IN "
            "('retained','pending','cleaned','retained_output',"
            "'identity_mismatch','unknown')), "
            "CONSTRAINT uq_pack_task_sources_task_file "
            "UNIQUE (task_id, original_user_file_id))"
        )
    )
    await _add_missing_columns(
        conn, "pack_task_sources", V4_PACK_TASK_SOURCES_COLUMNS
    )
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_pack_task_sources_identity ON "
            "pack_task_sources (original_user_file_id, stored_file_id, "
            "user_file_created_at_ms)"
        )
    )
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_pack_task_sources_task_cleanup ON "
            "pack_task_sources (task_id, cleanup_state)"
        )
    )


async def _create_v4_prepared_triggers(conn: AsyncConnection) -> None:
    invalid = (
        "NOT ((NEW.prepared_content_hash IS NULL AND "
        "NEW.prepared_size_bytes IS NULL AND NEW.prepared_filename IS NULL) OR "
        "(NEW.prepared_content_hash IS NOT NULL AND "
        "NEW.prepared_size_bytes IS NOT NULL AND NEW.prepared_filename IS NOT NULL))"
    )
    for operation in ("INSERT", "UPDATE"):
        name = f"trg_pack_tasks_prepared_fields_{operation.lower()}"
        await conn.execute(
            # Trigger name, operation, and predicate come only from source-defined constants.
            # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
            text(
                f"CREATE TRIGGER IF NOT EXISTS {name} BEFORE {operation} ON pack_tasks "
                f"WHEN {invalid} BEGIN SELECT RAISE(ABORT, "
                "'pack prepared fields must be all null or all set'); END"
            )
        )


async def _normalize_v4_pack_sources(conn: AsyncConnection) -> None:
    rows = (
        await conn.execute(
            text(
                "SELECT id, user_id, source_user_file_ids_json, status, "
                "output_stored_file_id, reserved_bytes, prepared_content_hash, "
                "prepared_size_bytes, prepared_filename "
                "FROM pack_tasks ORDER BY id"
            )
        )
    ).mappings().all()
    normalized: dict[int, str | None] = {}
    completed: set[tuple[int, str]] = set()
    broken_prepared: set[int] = set()
    for row in rows:
        prepared_count = sum(
            row[name] is not None
            for name in (
                "prepared_content_hash",
                "prepared_size_bytes",
                "prepared_filename",
            )
        )
        if prepared_count not in {0, 3}:
            broken_prepared.add(int(row["id"]))
        try:
            values = json.loads(row["source_user_file_ids_json"])
        except (TypeError, json.JSONDecodeError):
            values = None
        valid = (
            isinstance(values, list)
            and bool(values)
            and all(
                isinstance(value, int) and not isinstance(value, bool) and value > 0
                for value in values
            )
            and len(values) == len(set(values))
        )
        source_ids = values if valid else []
        canonical = (
            json.dumps(sorted(source_ids), separators=(",", ":")) if valid else None
        )
        normalized[int(row["id"])] = canonical
        if (
            canonical is not None
            and row["status"] == "completed"
            and row["output_stored_file_id"] is not None
        ):
            completed.add((int(row["user_id"]), canonical))

    active: set[tuple[int, str]] = set()
    rejected: list[int] = []
    released_by_user: dict[int, int] = {}
    for row in rows:
        if row["status"] not in {"pending", "packing"}:
            continue
        canonical = normalized[int(row["id"])]
        key = (int(row["user_id"]), canonical or "")
        if (
            canonical is None
            or int(row["id"]) in broken_prepared
            or key in completed
            or key in active
        ):
            rejected.append(int(row["id"]))
            user_id = int(row["user_id"])
            released_by_user[user_id] = released_by_user.get(user_id, 0) + max(
                0, int(row["reserved_bytes"] or 0)
            )
        else:
            active.add(key)
    if broken_prepared:
        await conn.execute(
            text(
                "UPDATE pack_tasks SET prepared_content_hash = NULL, "
                "prepared_size_bytes = NULL, prepared_filename = NULL, "
                "error_message = COALESCE(error_message, "
                "'服务升级时发现不完整的打包恢复记录') WHERE id IN :ids"
            ).bindparams(bindparam("ids", expanding=True)),
            {"ids": sorted(broken_prepared)},
        )
    if rejected:
        for user_id, released in released_by_user.items():
            await conn.execute(
                text(
                    "UPDATE user_storage_usage SET reserved_bytes = "
                    "MAX(0, reserved_bytes - :released) WHERE user_id = :user_id"
                ),
                {"released": released, "user_id": user_id},
            )
        await conn.execute(
            text(
                "UPDATE pack_tasks SET status = 'failed', reserved_bytes = 0, "
                "materialized_bytes = 0, install_reserved_bytes = 0, "
                "retry_count = 0, next_retry_at_ms = NULL, "
                "error_message = '服务升级时清理了无效或重复打包任务', "
                "finished_at_ms = updated_at_ms WHERE id IN :ids"
            ).bindparams(bindparam("ids", expanding=True)),
            {"ids": rejected},
        )
    for task_id, canonical in normalized.items():
        if canonical is not None:
            await conn.execute(
                text(
                    "UPDATE pack_tasks SET source_user_file_ids_json = :sources "
                    "WHERE id = :task_id"
                ),
                {"sources": canonical, "task_id": task_id},
            )


async def _backfill_v4_pack_sources(conn: AsyncConnection) -> None:
    rows = (
        await conn.execute(
            text(
                "SELECT id, user_id, source_user_file_ids_json, status, "
                "reserved_bytes, delete_source, source_cleanup_pending, "
                "output_stored_file_id, created_at_ms FROM pack_tasks "
                "WHERE status IN ('pending','packing') OR "
                "(status = 'completed' AND delete_source = 1) ORDER BY id"
            )
        )
    ).mappings().all()
    for task in rows:
        task_id = int(task["id"])
        existing = (
            await conn.execute(
                text("SELECT 1 FROM pack_task_sources WHERE task_id = :task_id LIMIT 1"),
                {"task_id": task_id},
            )
        ).first()
        if existing is not None:
            continue
        source_ids = json.loads(task["source_user_file_ids_json"])
        identities = (
            await conn.execute(
                text(
                    "SELECT uf.id, uf.stored_file_id, uf.created_at_ms, sf.content_hash "
                    "FROM user_files uf JOIN stored_files sf ON sf.id = uf.stored_file_id "
                    "WHERE uf.user_id = :user_id AND uf.id IN :ids"
                ).bindparams(bindparam("ids", expanding=True)),
                {"user_id": int(task["user_id"]), "ids": source_ids},
            )
        ).mappings().all()
        by_id = {
            int(row["id"]): row
            for row in identities
            if int(row["created_at_ms"]) <= int(task["created_at_ms"])
        }
        uncertain = len(by_id) != len(source_ids)
        active = task["status"] in {"pending", "packing"}
        unresolved_obligation = (
            not active
            and bool(task["delete_source"])
            and bool(task["source_cleanup_pending"])
        )
        if active and uncertain:
            reserved = max(0, int(task["reserved_bytes"] or 0))
            await conn.execute(
                text(
                    "UPDATE user_storage_usage SET reserved_bytes = "
                    "MAX(0, reserved_bytes - :reserved) WHERE user_id = :user_id"
                ),
                {"reserved": reserved, "user_id": int(task["user_id"])},
            )
            await conn.execute(
                text(
                    "UPDATE pack_tasks SET status = 'failed', reserved_bytes = 0, "
                    "materialized_bytes = 0, install_reserved_bytes = 0, "
                    "retry_count = 0, next_retry_at_ms = NULL, "
                    "source_cleanup_pending = 0, "
                    "error_message = '服务升级时无法确认打包源身份，已停止任务', "
                    "finished_at_ms = updated_at_ms WHERE id = :task_id"
                ),
                {"task_id": task_id},
            )
        for ordinal, source_id in enumerate(source_ids):
            identity = by_id.get(int(source_id))
            if identity is None and unresolved_obligation:
                state, error = "unknown", "升级时无法确认待清理源文件身份"
            elif identity is None:
                state, error = "retained", "升级时无法确认历史源身份，按已保留处理"
            elif active and uncertain or not bool(task["delete_source"]):
                state, error = "retained", None
            elif active:
                state, error = "pending", None
            elif identity["stored_file_id"] == task["output_stored_file_id"]:
                state, error = "retained_output", None
            elif bool(task["source_cleanup_pending"]):
                state, error = "pending", None
            else:
                state, error = "retained", None
            await conn.execute(
                text(
                    "INSERT INTO pack_task_sources (task_id, ordinal, "
                    "original_user_file_id, stored_file_id, user_file_created_at_ms, "
                    "content_hash, cleanup_state, cleanup_error) VALUES "
                    "(:task_id,:ordinal,:source_id,:stored_id,:created_at,:hash,:state,:error)"
                ),
                {
                    "task_id": task_id, "ordinal": ordinal, "source_id": source_id,
                    "stored_id": identity["stored_file_id"] if identity else None,
                    "created_at": identity["created_at_ms"] if identity else None,
                    "hash": identity["content_hash"] if identity else None,
                    "state": state, "error": error,
                },
            )
        if unresolved_obligation and uncertain:
            await conn.execute(
                text(
                    "UPDATE pack_tasks SET source_cleanup_pending = 0, "
                    "error_message = '待清理源文件身份无法确认，已停止自动删除' "
                    "WHERE id = :task_id"
                ),
                {"task_id": task_id},
            )


async def ensure_v4_pack_schema(conn: AsyncConnection) -> None:
    await _add_missing_columns(conn, "pack_tasks", V4_PACK_TASKS_ADDED_COLUMNS)
    table_names = await _table_names(conn)
    if "pack_tasks" not in table_names:
        return
    await _normalize_v4_pack_sources(conn)
    await _create_v4_pack_source_table(conn)
    if {"user_files", "stored_files", "user_storage_usage"} <= table_names:
        await _backfill_v4_pack_sources(conn)
    await _create_v4_prepared_triggers(conn)
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_pack_tasks_recovery "
            "ON pack_tasks (status, source_cleanup_pending, prepared_content_hash)"
        )
    )
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_pack_tasks_dispatch "
            "ON pack_tasks (status, next_retry_at_ms, id)"
        )
    )
    await conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_pack_tasks_active_sources "
            "ON pack_tasks (user_id, source_user_file_ids_json) "
            "WHERE status IN ('pending', 'packing')"
        )
    )


async def migrate_v4(conn: AsyncConnection) -> None:
    await ensure_v4_pack_schema(conn)
    await _rebuild_schema_meta(conn, 4)


async def ensure_v5_deletion_schema(conn: AsyncConnection) -> None:
    for table_name in ("users", "stored_files"):
        await _add_missing_columns(conn, table_name, V5_DELETE_COLUMNS)
    table_names = await _table_names(conn)
    if "users" in table_names:
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_users_delete_due ON users "
                "(pending_delete, delete_next_retry_at_ms, "
                "delete_lease_expires_at_ms, id)"
            )
        )
    if "stored_files" in table_names:
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_stored_files_delete_due "
                "ON stored_files (pending_delete, delete_next_retry_at_ms, "
                "delete_lease_expires_at_ms, id)"
            )
        )


async def _create_v6_users_table(conn: AsyncConnection) -> None:
    await conn.execute(
        text(
            "CREATE TABLE users_v6 ("
            "id INTEGER NOT NULL PRIMARY KEY, username VARCHAR(50) NOT NULL UNIQUE, "
            "password_hash TEXT NOT NULL, is_admin INTEGER NOT NULL DEFAULT 0, "
            "quota_bytes INTEGER NOT NULL, rpc_secret_digest VARCHAR(64) UNIQUE, "
            "rpc_secret_prefix VARCHAR(24), rpc_secret_created_at_ms INTEGER, "
            "is_initial_password INTEGER NOT NULL DEFAULT 0, created_at_ms INTEGER NOT NULL, "
            "updated_at_ms INTEGER NOT NULL, pending_delete INTEGER NOT NULL DEFAULT 0, "
            "delete_attempts INTEGER NOT NULL DEFAULT 0, delete_next_retry_at_ms INTEGER, "
            "delete_lease_token VARCHAR(64), delete_lease_expires_at_ms INTEGER, "
            "delete_error TEXT, "
            "CONSTRAINT ck_users_is_admin_bool CHECK (is_admin IN (0, 1)), "
            "CONSTRAINT ck_users_initial_password_bool CHECK (is_initial_password IN (0, 1)), "
            "CONSTRAINT ck_users_pending_delete_bool CHECK (pending_delete IN (0, 1)), "
            "CONSTRAINT ck_users_delete_attempts_non_negative CHECK (delete_attempts >= 0)"
            ")"
        )
    )


async def _create_v6_api_tokens_table(conn: AsyncConnection) -> None:
    await conn.execute(
        text(
            "CREATE TABLE api_tokens_v6 ("
            "id INTEGER NOT NULL PRIMARY KEY, user_id INTEGER NOT NULL, "
            "token_digest VARCHAR(64) NOT NULL UNIQUE, token_prefix VARCHAR(24) NOT NULL, "
            "name VARCHAR(200), created_at_ms INTEGER NOT NULL, last_used_at_ms INTEGER, "
            "FOREIGN KEY(user_id) REFERENCES users_v6 (id) ON DELETE CASCADE"
            ")"
        )
    )


async def _foreign_keys_enabled(conn: AsyncConnection) -> bool:
    return bool((await conn.execute(text("PRAGMA foreign_keys"))).scalar_one())


async def _ensure_v6_credential_indexes(conn: AsyncConnection) -> None:
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_users_delete_due ON users "
            "(pending_delete, delete_next_retry_at_ms, delete_lease_expires_at_ms, id)"
        )
    )
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_api_tokens_user_created ON api_tokens "
            "(user_id, created_at_ms)"
        )
    )


async def ensure_v6_credentials_schema(conn: AsyncConnection) -> None:
    await conn.execute(text("PRAGMA secure_delete=ON"))
    table_names = await _table_names(conn)
    if "users" not in table_names:
        return
    user_columns = await _column_names(conn, "users")
    if "rpc_secret_digest" in user_columns:
        if "rpc_secret" in user_columns:
            raise RuntimeError("凭证迁移状态无效：users 同时包含明文和摘要字段")
        if "api_tokens" in table_names:
            await _ensure_v6_credential_indexes(conn)
        return
    if not V6_REQUIRED_USER_COLUMNS <= user_columns:
        return

    has_tokens = "api_tokens" in table_names
    if has_tokens and not V6_REQUIRED_TOKEN_COLUMNS <= await _column_names(conn, "api_tokens"):
        return
    dependent_tables = {
        "sessions", "user_tasks", "user_files", "share_links",
        "pack_tasks", "user_storage_usage",
    } & table_names
    if dependent_tables and await _foreign_keys_enabled(conn):
        raise RuntimeError("v6 凭证迁移必须在关闭 SQLite 外键执行的连接中运行")

    user_rows = (await conn.execute(text("SELECT * FROM users"))).mappings().all()
    token_rows = (
        (await conn.execute(text("SELECT * FROM api_tokens"))).mappings().all()
        if has_tokens
        else []
    )
    await _create_v6_users_table(conn)
    if has_tokens:
        await _create_v6_api_tokens_table(conn)

    user_values = []
    for row in user_rows:
        secret = row["rpc_secret"]
        user_values.append({
            **{name: row[name] for name in V6_REQUIRED_USER_COLUMNS - {"rpc_secret"}},
            "rpc_secret_digest": credential_digest("rpc-secret", str(secret)) if secret else None,
            "rpc_secret_prefix": credential_prefix(str(secret)) if secret else None,
        })
    if user_values:
        await conn.execute(
            text(
                "INSERT INTO users_v6 (id, username, password_hash, is_admin, quota_bytes, "
                "rpc_secret_digest, rpc_secret_prefix, rpc_secret_created_at_ms, "
                "is_initial_password, created_at_ms, updated_at_ms, pending_delete, "
                "delete_attempts, delete_next_retry_at_ms, delete_lease_token, "
                "delete_lease_expires_at_ms, delete_error) VALUES "
                "(:id,:username,:password_hash,:is_admin,:quota_bytes,:rpc_secret_digest,"
                ":rpc_secret_prefix,:rpc_secret_created_at_ms,:is_initial_password,"
                ":created_at_ms,:updated_at_ms,:pending_delete,:delete_attempts,"
                ":delete_next_retry_at_ms,:delete_lease_token,:delete_lease_expires_at_ms,"
                ":delete_error)"
            ),
            user_values,
        )


    if has_tokens and token_rows:
        token_values = [
            {
                "id": row["id"],
                "user_id": row["user_id"],
                "token_digest": credential_digest("api-token", str(row["token"])),
                "token_prefix": credential_prefix(str(row["token"])),
                "name": row["name"],
                "created_at_ms": row["created_at_ms"],
                "last_used_at_ms": row["last_used_at_ms"],
            }
            for row in token_rows
        ]
        await conn.execute(
            text(
                "INSERT INTO api_tokens_v6 (id, user_id, token_digest, token_prefix, "
                "name, created_at_ms, last_used_at_ms) VALUES "
                "(:id,:user_id,:token_digest,:token_prefix,:name,:created_at_ms,"
                ":last_used_at_ms)"
            ),
            token_values,
        )
    if has_tokens:
        await conn.execute(text("DROP TABLE api_tokens"))
    await conn.execute(text("DROP TABLE users"))
    await conn.execute(text("ALTER TABLE users_v6 RENAME TO users"))
    if has_tokens:
        await conn.execute(text("ALTER TABLE api_tokens_v6 RENAME TO api_tokens"))
        await _ensure_v6_credential_indexes(conn)

    violations = (await conn.execute(text("PRAGMA foreign_key_check"))).all()
    if violations:
        raise RuntimeError("v6 凭证迁移后外键校验失败")


async def _create_v7_identity_triggers(conn: AsyncConnection) -> None:
    invalid = (
        "NOT ((NEW.content_hash_version = \"v1\" AND "
        "NEW.content_object_kind = \"legacy\" AND "
        "(NEW.content_digest IS NULL OR NEW.content_digest = NEW.content_hash)) OR "
        "(NEW.content_hash_version = \"v2\" AND "
        "NEW.content_object_kind IN (\"file\", \"directory\") AND "
        "length(NEW.content_digest) = 64 AND "
        "NEW.content_digest NOT GLOB \"*[^0-9a-f]*\" AND "
        "NEW.content_hash = \"v2:\" || NEW.content_object_kind || \":\" || "
        "NEW.content_digest))"
    )
    for operation in ("INSERT", "UPDATE"):
        await conn.execute(
            # Trigger operation and identity predicate come only from source-defined constants.
            # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
            text(
                f"CREATE TRIGGER IF NOT EXISTS trg_stored_files_content_identity_"
                f"{operation.lower()} BEFORE {operation} ON stored_files "
                f"WHEN {invalid} BEGIN SELECT RAISE(ABORT, "
                "\"stored file content identity invalid\"); END"
            )
        )


async def ensure_v7_content_identity_schema(conn: AsyncConnection) -> None:
    if "stored_files" not in await _table_names(conn):
        return
    await _add_missing_columns(conn, "stored_files", V7_STORED_FILES_ADDED_COLUMNS)
    await conn.execute(
        text(
            "UPDATE stored_files SET content_hash_version = \"v1\", "
            "content_object_kind = \"legacy\", content_digest = content_hash "
            "WHERE content_hash_version = \"v1\" AND "
            "content_object_kind = \"legacy\" AND content_digest IS NULL"
        )
    )
    invalid = (
        "(content_hash_version != \"v1\" OR content_object_kind != \"legacy\" "
        "OR content_digest != content_hash) AND "
        "(content_hash_version != \"v2\" OR "
        "content_object_kind NOT IN (\"file\", \"directory\") OR "
        "length(content_digest) != 64 OR content_digest GLOB \"*[^0-9a-f]*\" OR "
        "content_hash != \"v2:\" || content_object_kind || \":\" || content_digest)"
    )
    row = await conn.execute(
        # The identity predicate is source-defined; stored row values are not interpolated.
        # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
        text(f"SELECT 1 FROM stored_files WHERE {invalid} LIMIT 1")  # noqa: S608  # internal migration predicate; identifiers cannot be bound
    )
    if row.first():
        raise RuntimeError("v7 内容身份迁移发现无效记录")
    await conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_stored_files_content_identity "
            "ON stored_files (content_hash_version, content_object_kind, content_digest) "
            "WHERE content_digest IS NOT NULL"
        )
    )
    await _create_v7_identity_triggers(conn)


async def migrate_v7(conn: AsyncConnection) -> None:
    await ensure_v7_content_identity_schema(conn)
    await _rebuild_schema_meta(conn, 7)


async def migrate_v6(conn: AsyncConnection) -> None:
    await ensure_v6_credentials_schema(conn)
    await _rebuild_schema_meta(conn, 6)


async def migrate_v5(conn: AsyncConnection) -> None:
    await ensure_v5_deletion_schema(conn)
    await _rebuild_schema_meta(conn, 5)


MIGRATIONS: dict[int, Migration] = {
    1: migrate_v1,
    2: migrate_v2,
    3: migrate_v3,
    4: migrate_v4,
    5: migrate_v5,
    6: migrate_v6,
    7: migrate_v7,
    8: migrate_v8,
    9: migrate_v9,
    10: migrate_v10,
    11: migrate_v11,
    12: migrate_v12,
    13: migrate_v13,
    14: migrate_v14,
    15: migrate_v15,
    16: migrate_v16,
    17: migrate_v17,
    18: migrate_v18,
}


async def run_migrations(conn: AsyncConnection, current_version: int) -> int:
    if current_version > SCHEMA_VERSION:
        raise RuntimeError(
            "Unsupported database schema: database version "
            f"{current_version} is newer than supported version {SCHEMA_VERSION}."
        )

    version = current_version
    while version < SCHEMA_VERSION:
        next_version = version + 1
        migration = MIGRATIONS.get(next_version)
        if migration is None:
            raise RuntimeError(
                f"Unsupported database schema: migration {version} -> {next_version} is missing."
            )
        await migration(conn)
        version = next_version

    return version
