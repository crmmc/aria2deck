from __future__ import annotations

import logging
import time

from sqlalchemy import insert, inspect, select, text

from app.core.config import settings
from app.core.download_limiter import download_config
from app.core.rate_limit_config import rate_limit_config
from app.db.engine import (
    apply_sqlite_pragmas,
    clear_credential_scrub_marker,
    credential_scrub_marker_path,
    get_engine,
    mark_credential_scrub_pending,
    scrub_legacy_credential_pages,
)
from app.db.migrations import (
    DEFAULT_ARIA2_BT_STOP_TIMEOUT_SECONDS,
    ensure_v4_pack_schema,
    ensure_v5_deletion_schema,
    ensure_v6_credentials_schema,
    ensure_v7_content_identity_schema,
    ensure_v8_retry_attempt_schema,
    ensure_v10_rpc_secret_encrypted,
    ensure_v11_share_password_encrypted,
    ensure_v12_download_sources_schema,
    run_migrations,
)
from app.db.schema import SCHEMA_VERSION, app_settings, metadata, schema_meta

logger = logging.getLogger(__name__)


def now_ms() -> int:
    return int(time.time() * 1000)


def default_app_settings(timestamp_ms: int) -> dict:
    rate_limit_defaults = {
        key: int(value) for key, value in rate_limit_config.defaults().items()
    }
    download_defaults = {
        key: int(value) for key, value in download_config.defaults().items()
    }
    return {
        "id": 1,
        "max_task_size_bytes": 10 * 1024 * 1024 * 1024,
        "min_free_disk_bytes": 1024 * 1024 * 1024,
        "aria2_rpc_url": settings.aria2_rpc_url,
        "aria2_rpc_secret": settings.aria2_rpc_secret,
        "aria2_bt_stop_timeout_seconds": DEFAULT_ARIA2_BT_STOP_TIMEOUT_SECONDS,
        "hidden_file_extensions_json": "[]",
        "pack_format": "tar.zst",
        "pack_compression_level": 5,
        "ws_reconnect_max_delay": 30,
        "ws_reconnect_jitter": "0.2",
        "ws_reconnect_factor": "2.0",
        "site_title": "Aria2Deck",
        **rate_limit_defaults,
        # Deprecated request-rate fields remain unlimited for schema compatibility.
        "rate_limit_authenticated_download": 0,
        "rate_limit_anonymous_download": 0,
        **download_defaults,
        "history_retention_days": 30,
        "created_at_ms": timestamp_ms,
        "updated_at_ms": timestamp_ms,
    }


async def _table_names() -> set[str]:
    async with get_engine().connect() as conn:
        return set(
            await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_table_names())
        )


async def load_schema_version() -> int | None:
    tables = await _table_names()
    if not tables:
        return None
    if "schema_meta" not in tables:
        raise RuntimeError(
            "Unsupported database schema: schema_meta is missing. "
            "This release only supports an empty database or a versioned database. "
            "Back up and rebuild the database file."
        )

    async with get_engine().connect() as conn:
        row = (
            await conn.execute(
                select(schema_meta.c.version).where(schema_meta.c.id == 1)
            )
        ).first()
    if row is None:
        raise RuntimeError("Unsupported database schema: schema_meta row is missing.")
    return int(row[0])


async def validate_schema_version() -> None:
    version = await load_schema_version()
    if version is None:
        return
    if version > SCHEMA_VERSION:
        raise RuntimeError(
            "Unsupported database schema: database version "
            f"{version} is newer than supported version {SCHEMA_VERSION}."
        )


async def validate_current_schema_shape() -> None:
    expected_columns = {
        table.name: {column.name for column in table.columns}
        for table in metadata.sorted_tables
    }
    async with get_engine().connect() as conn:
        actual_columns = await conn.run_sync(
            lambda sync_conn: {
                table_name: {
                    column["name"]
                    for column in inspect(sync_conn).get_columns(table_name)
                }
                for table_name in inspect(sync_conn).get_table_names()
            }
        )
        foreign_key_violations = (
            await conn.execute(text("PRAGMA foreign_key_check"))
        ).all()
        schema_objects = {
            str(row[0]): str(row[1] or "")
            for row in (
                await conn.execute(
                    text(
                        "SELECT name, sql FROM sqlite_master "
                        "WHERE type IN (\"index\", \"trigger\")"
                    )
                )
            ).all()
        }

    if foreign_key_violations:
        raise RuntimeError("Unsupported database schema: foreign key check failed.")

    missing_tables = sorted(set(expected_columns) - set(actual_columns))
    if missing_tables:
        raise RuntimeError(
            "Unsupported database schema: missing required tables: "
            + ", ".join(missing_tables)
        )

    missing_columns: list[str] = []
    for table_name, column_names in sorted(expected_columns.items()):
        missing_columns.extend(
            f"{table_name}.{column_name}"
            for column_name in sorted(column_names - actual_columns[table_name])
        )
    if missing_columns:
        raise RuntimeError(
            "Unsupported database schema: missing required columns: "
            + ", ".join(missing_columns)
        )

    required_fragments = {
        "ix_global_downloads_resource_key": (
            "onglobal_downloads(resource_key)",
        ),
        "uq_global_downloads_live_resource": (
            "createuniqueindex",
            "onglobal_downloads(resource_key)",
            "wherestatusin('queued','active','waiting','paused')",
        ),
        "uq_stored_files_content_identity": (
            "createuniqueindex",
            "onstored_files(content_hash_version,content_object_kind,content_digest)",
        ),
        "trg_stored_files_content_identity_insert": (
            "beforeinsertonstored_files",
            "new.content_hash_version",
            "raise(abort",
        ),
        "trg_stored_files_content_identity_update": (
            "beforeupdateonstored_files",
            "new.content_hash_version",
            "raise(abort",
        ),
        "ix_pack_tasks_recovery": (
            "onpack_tasks(status,source_cleanup_pending,prepared_content_hash)",
        ),
        "ix_pack_tasks_dispatch": (
            "onpack_tasks(status,next_retry_at_ms,id)",
        ),
        "uq_pack_tasks_active_sources": (
            "createuniqueindex",
            "onpack_tasks(user_id,source_user_file_ids_json)",
            "wherestatusin('pending','packing')",
        ),
        "ix_pack_task_sources_identity": (
            "onpack_task_sources(original_user_file_id,stored_file_id,"
            "user_file_created_at_ms)",
        ),
        "ix_pack_task_sources_task_cleanup": (
            "onpack_task_sources(task_id,cleanup_state)",
        ),
        "trg_pack_tasks_prepared_fields_insert": (
            "beforeinsertonpack_tasks",
            "new.prepared_content_hash",
            "raise(abort",
        ),
        "trg_pack_tasks_prepared_fields_update": (
            "beforeupdateonpack_tasks",
            "new.prepared_content_hash",
            "raise(abort",
        ),
        "ix_users_delete_due": (
            "onusers(pending_delete,delete_next_retry_at_ms,"
            "delete_lease_expires_at_ms,id)",
        ),
        "ix_stored_files_delete_due": (
            "onstored_files(pending_delete,delete_next_retry_at_ms,"
            "delete_lease_expires_at_ms,id)",
        ),
    }
    invalid_objects = []
    for name, fragments in required_fragments.items():
        definition = "".join(schema_objects.get(name, "").lower().split())
        if not definition or any(fragment not in definition for fragment in fragments):
            invalid_objects.append(name)
    if invalid_objects:
        raise RuntimeError(
            "Unsupported database schema: missing or invalid schema objects: "
            + ", ".join(invalid_objects)
        )


async def _migrate_existing_database(version: int) -> None:
    if version < 6:
        mark_credential_scrub_pending()
    async with get_engine().connect() as conn:
        await conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        await conn.commit()
        try:
            async with conn.begin():
                await run_migrations(conn, version)
                await ensure_v4_pack_schema(conn)
                await ensure_v5_deletion_schema(conn)
                await ensure_v6_credentials_schema(conn)
                await ensure_v7_content_identity_schema(conn)
                await ensure_v8_retry_attempt_schema(conn)
                await ensure_v10_rpc_secret_encrypted(conn)
                await ensure_v11_share_password_encrypted(conn)
                await ensure_v12_download_sources_schema(conn)
        finally:
            await conn.exec_driver_sql("PRAGMA foreign_keys=ON")
            await conn.commit()


async def _truncate_wal_after_bootstrap() -> int | None:
    """Truncate the WAL on every bootstrap startup, not only after a
    migration. Idempotent, sub-second work that keeps the WAL file from
    lingering; a busy checkpoint only downgrades to a warning (same shape
    as scrub_legacy_credential_pages)."""
    try:
        async with get_engine().connect() as conn:
            checkpoint = (
                await conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
            ).first()
            await conn.commit()
    except Exception:
        logger.warning("迁移后 WAL checkpoint 执行失败", exc_info=True)
        return None
    busy = int(checkpoint[0]) if checkpoint is not None else None
    if busy != 0:
        logger.warning("迁移后 WAL checkpoint busy=%s，WAL 未截断", busy)
    return busy


async def _finish_pending_credential_scrub() -> None:
    if not credential_scrub_marker_path().exists():
        return
    if await scrub_legacy_credential_pages():
        clear_credential_scrub_marker()


async def bootstrap_database() -> None:
    await apply_sqlite_pragmas()
    tables = await _table_names()
    if tables:
        version = await load_schema_version()
        if version is None:
            return
        await _migrate_existing_database(version)
        await _truncate_wal_after_bootstrap()
        await _finish_pending_credential_scrub()
        await validate_current_schema_shape()
        return

    timestamp_ms = now_ms()
    async with get_engine().begin() as conn:
        await conn.execute(text("PRAGMA foreign_keys=ON"))
        await conn.run_sync(metadata.create_all)
        await ensure_v4_pack_schema(conn)
        await ensure_v5_deletion_schema(conn)
        await ensure_v6_credentials_schema(conn)
        await ensure_v7_content_identity_schema(conn)
        await ensure_v8_retry_attempt_schema(conn)
        await ensure_v10_rpc_secret_encrypted(conn)
        await ensure_v11_share_password_encrypted(conn)
        await ensure_v12_download_sources_schema(conn)
        await conn.execute(
            insert(schema_meta).values(
                id=1, version=SCHEMA_VERSION, created_at_ms=timestamp_ms
            )
        )
        await conn.execute(
            insert(app_settings).values(default_app_settings(timestamp_ms))
        )
