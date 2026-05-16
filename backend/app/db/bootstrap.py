from __future__ import annotations

import time

from sqlalchemy import insert, inspect, select, text

from app.core.config import settings
from app.db.engine import apply_sqlite_pragmas, get_engine
from app.db.schema import SCHEMA_VERSION, app_settings, metadata, schema_meta


def now_ms() -> int:
    return int(time.time() * 1000)


def default_app_settings(timestamp_ms: int) -> dict:
    return {
        "id": 1,
        "max_task_size_bytes": 10 * 1024 * 1024 * 1024,
        "min_free_disk_bytes": 1024 * 1024 * 1024,
        "aria2_rpc_url": settings.aria2_rpc_url,
        "aria2_rpc_secret": settings.aria2_rpc_secret,
        "hidden_file_extensions_json": "[]",
        "pack_format": "tar.zst",
        "pack_compression_level": 5,
        "ws_reconnect_max_delay": 30,
        "ws_reconnect_jitter": "0.2",
        "ws_reconnect_factor": "2.0",
        "site_title": "Aria2Deck",
        "rate_limit_account_security": 10,
        "rate_limit_authenticated_api": 0,
        "rate_limit_public_api": 0,
        "rate_limit_share_access": 60,
        "rate_limit_authenticated_download": 0,
        "rate_limit_anonymous_download": 0,
        "rate_limit_create_task": 30,
        "rate_limit_create_torrent": 10,
        "rate_limit_create_pack": 10,
        "rate_limit_aria2_test": 10,
        "rate_limit_rpc": 120,
        "download_total_connections": 0,
        "download_authenticated_reserved_connections": 0,
        "download_authenticated_per_user_connections": 0,
        "download_authenticated_per_file_connections": 0,
        "download_anonymous_base_connections": 0,
        "download_anonymous_borrow_connections": 0,
        "download_anonymous_per_ip_connections": 0,
        "download_anonymous_per_file_connections": 0,
        "created_at_ms": timestamp_ms,
        "updated_at_ms": timestamp_ms,
    }


async def _table_names() -> set[str]:
    async with get_engine().connect() as conn:
        return set(await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_table_names()))


async def validate_schema_version() -> None:
    tables = await _table_names()
    if not tables:
        return
    if "schema_meta" not in tables:
        raise RuntimeError(
            "Unsupported database schema: schema_meta is missing. "
            "This greenfield v0 release only supports an empty database or schema version 0. "
            "Back up and rebuild the database file."
        )

    async with get_engine().connect() as conn:
        row = (await conn.execute(select(schema_meta.c.version).where(schema_meta.c.id == 1))).first()
    if row is None:
        raise RuntimeError("Unsupported database schema: schema_meta row is missing; expected version 0.")
    version = int(row[0])
    if version != SCHEMA_VERSION:
        raise RuntimeError(f"Unsupported database schema: expected version 0, got {version}. Rebuild the database.")


async def bootstrap_database() -> None:
    await apply_sqlite_pragmas()
    tables = await _table_names()
    if tables:
        await validate_schema_version()
        return

    timestamp_ms = now_ms()
    async with get_engine().begin() as conn:
        await conn.execute(text("PRAGMA foreign_keys=ON"))
        await conn.run_sync(metadata.create_all)
        await conn.execute(insert(schema_meta).values(id=1, version=SCHEMA_VERSION, created_at_ms=timestamp_ms))
        await conn.execute(insert(app_settings).values(default_app_settings(timestamp_ms)))
