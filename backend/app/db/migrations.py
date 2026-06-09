from __future__ import annotations

from collections.abc import Awaitable, Callable

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncConnection

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


async def migrate_v1(conn: AsyncConnection) -> None:
    await _add_missing_columns(conn, "app_settings", V1_APP_SETTINGS_ADDED_COLUMNS)
    await _rebuild_schema_meta(conn, 1)


async def migrate_v2(conn: AsyncConnection) -> None:
    await _add_missing_columns(
        conn, "global_downloads", V2_GLOBAL_DOWNLOADS_ADDED_COLUMNS
    )
    await _rebuild_schema_meta(conn, 2)


MIGRATIONS: dict[int, Migration] = {
    1: migrate_v1,
    2: migrate_v2,
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
