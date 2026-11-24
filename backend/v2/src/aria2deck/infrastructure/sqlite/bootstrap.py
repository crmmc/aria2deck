from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import Result, select, text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.pool import StaticPool

from ...domain.errors import DomainError, ErrorCode
from ...domain.settings import SystemSettings
from ..security import CredentialManager
from .engine import SQLiteDatabase
from .schema import (
    DDL_STATEMENTS,
    SCHEMA_VERSION,
    app_settings,
    schema_meta,
    tracker_cache,
)


@dataclass(frozen=True, slots=True)
class ColumnStructure:
    name: str
    type: str
    required: bool
    default: str | None
    primary_key_position: int


@dataclass(frozen=True, slots=True)
class ForeignKeyStructure:
    table: str
    source: str
    target: str
    on_update: str
    on_delete: str


@dataclass(frozen=True, slots=True)
class IndexStructure:
    table: str
    sql: str | None
    unique: bool
    origin: str
    partial: bool
    columns: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TableStructure:
    sql: str
    columns: tuple[ColumnStructure, ...]
    foreign_keys: tuple[ForeignKeyStructure, ...]


@dataclass(frozen=True, slots=True)
class SchemaSnapshot:
    tables: dict[str, TableStructure]
    indexes: dict[str, IndexStructure]
    triggers: dict[str, str]


def _normalized_sql(value: object) -> str:
    return " ".join(str(value).strip().rstrip(";").split())


def _rows(result: Result[Any]) -> list[dict[str, object]]:
    return [dict(cast(Mapping[str, object], row._mapping)) for row in result.all()]


async def _snapshot(connection: AsyncConnection) -> SchemaSnapshot:
    table_rows = _rows(
        await connection.execute(
            text(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        )
    )
    trigger_rows = _rows(
        await connection.execute(
            text(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type = 'trigger' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        )
    )
    tables: dict[str, TableStructure] = {}
    indexes: dict[str, IndexStructure] = {}
    for row in table_rows:
        table_name = str(row["name"])
        column_rows = _rows(
            await connection.execute(
                text(
                    "SELECT name, type, \"notnull\", dflt_value, pk "
                    "FROM pragma_table_xinfo(:table_name) WHERE hidden = 0 ORDER BY cid"
                ),
                {"table_name": table_name},
            )
        )
        columns = tuple(
            ColumnStructure(
                name=str(column["name"]),
                type=str(column["type"]).upper(),
                required=bool(column["notnull"]),
                default=cast(str | None, column["dflt_value"]),
                primary_key_position=int(cast(int, column["pk"])),
            )
            for column in column_rows
        )
        foreign_key_rows = _rows(
            await connection.execute(
                text(
                    "SELECT \"table\", \"from\", \"to\", on_update, on_delete "
                    "FROM pragma_foreign_key_list(:table_name) ORDER BY id DESC, seq"
                ),
                {"table_name": table_name},
            )
        )
        foreign_keys = tuple(
            ForeignKeyStructure(
                table=str(key["table"]),
                source=str(key["from"]),
                target=str(key["to"]),
                on_update=str(key["on_update"]),
                on_delete=str(key["on_delete"]),
            )
            for key in foreign_key_rows
        )
        tables[table_name] = TableStructure(
            sql=_normalized_sql(row["sql"]), columns=columns, foreign_keys=foreign_keys
        )
        index_rows = _rows(
            await connection.execute(
                text(
                    "SELECT name, \"unique\", origin, partial "
                    "FROM pragma_index_list(:table_name) ORDER BY name"
                ),
                {"table_name": table_name},
            )
        )
        for index in index_rows:
            index_name = str(index["name"])
            index_sql_row = _rows(
                await connection.execute(
                    text("SELECT sql FROM sqlite_master WHERE type = 'index' AND name = :name"),
                    {"name": index_name},
                )
            )
            index_sql = _normalized_sql(index_sql_row[0]["sql"]) if index_sql_row else None
            index_column_rows = _rows(
                await connection.execute(
                    text(
                        "SELECT name FROM pragma_index_info(:index_name) ORDER BY seqno"
                    ),
                    {"index_name": index_name},
                )
            )
            indexes[index_name] = IndexStructure(
                table=table_name,
                sql=index_sql,
                unique=bool(index["unique"]),
                origin=str(index["origin"]),
                partial=bool(index["partial"]),
                columns=tuple(str(column["name"]) for column in index_column_rows),
            )
    triggers = {
        str(row["name"]): _normalized_sql(row["sql"])
        for row in trigger_rows
    }
    return SchemaSnapshot(tables=tables, indexes=indexes, triggers=triggers)


async def _expected_snapshot() -> SchemaSnapshot:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    try:
        async with engine.connect() as connection:
            for statement in DDL_STATEMENTS:
                await connection.exec_driver_sql(statement)
            return await _snapshot(connection)
    finally:
        await engine.dispose()


def _mismatch() -> DomainError:
    return DomainError(ErrorCode.SCHEMA_MISMATCH, "数据库结构与当前 V2 定义不一致，拒绝启动")


async def validate_database(database: SQLiteDatabase) -> None:
    expected = await _expected_snapshot()
    async with database.connection() as connection:
        actual = await _snapshot(connection)
        if actual != expected:
            raise _mismatch()
        version_rows = _rows(await connection.execute(select(schema_meta)))
        settings_count = (await connection.execute(select(app_settings.c.id))).all()
        tracker_count = (await connection.execute(select(tracker_cache.c.id))).all()
    if len(version_rows) != 1 or version_rows[0]["id"] != 1 or version_rows[0]["schema_version"] != SCHEMA_VERSION:
        raise _mismatch()
    if len(settings_count) != 1 or settings_count[0][0] != 1:
        raise _mismatch()
    if len(tracker_count) != 1 or tracker_count[0][0] != 1:
        raise _mismatch()


async def initialize_database(
    database: SQLiteDatabase,
    *,
    initial_settings: SystemSettings | None = None,
    credentials: CredentialManager | None = None,
) -> None:
    database.path.parent.mkdir(parents=True, exist_ok=True)
    failure: BaseException | None = None
    async with database.connection() as connection:
        await connection.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            existing = await connection.execute(
                text(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' LIMIT 1"
                )
            )
            if existing.first() is None:
                for statement in DDL_STATEMENTS:
                    await connection.exec_driver_sql(statement)
                await connection.execute(
                    schema_meta.insert().values(id=1, schema_version=SCHEMA_VERSION, created_at_ms=0)
                )
                if initial_settings is None:
                    await connection.execute(app_settings.insert().values(id=1))
                else:
                    if credentials is None:
                        raise DomainError(
                            ErrorCode.CONFIGURATION_INVALID, "全新数据库缺少配置加密器"
                        )
                    secret = initial_settings.aria2_rpc_secret
                    await connection.execute(
                        app_settings.insert().values(
                            id=1,
                            aria2_rpc_url=initial_settings.aria2_rpc_url,
                            aria2_rpc_secret_ciphertext=(
                                credentials.encrypt(secret) if secret is not None else None
                            ),
                        )
                    )
                await connection.execute(tracker_cache.insert().values(id=1))
            await connection.commit()
        except BaseException as error:
            failure = error
            await connection.rollback()
    if failure is not None:
        raise failure
    await validate_database(database)
