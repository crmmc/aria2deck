from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import delete, text
from sqlalchemy.exc import OperationalError

from aria2deck.domain.errors import DomainError, ErrorCode
from aria2deck.infrastructure.sqlite import bootstrap as bootstrap_module
from aria2deck.infrastructure.sqlite.bootstrap import initialize_database, validate_database
from aria2deck.infrastructure.sqlite.engine import SQLiteDatabase
from aria2deck.infrastructure.sqlite.schema import (
    SCHEMA_VERSION,
    TARGET_TABLES,
    app_settings,
    download_attempts,
    tracker_cache,
)

EXPECTED_TABLES = {
    "schema_meta",
    "users",
    "sessions",
    "api_tokens",
    "app_settings",
    "download_sources",
    "download_attempts",
    "task_subscriptions",
    "content_objects",
    "content_entries",
    "user_files",
    "share_links",
    "pack_jobs",
    "pack_sources",
    "quota_accounts",
    "tracker_cache",
}


async def test_fresh_database_has_exact_target_tables_and_version(database: SQLiteDatabase) -> None:
    async with database.connection() as connection:
        rows = (await connection.execute(text(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ))).scalars().all()
        version = (await connection.execute(text("SELECT schema_version FROM schema_meta"))).scalar_one()
    assert set(rows) == EXPECTED_TABLES == set(TARGET_TABLES)
    assert version == SCHEMA_VERSION
    assert not hasattr(database, "migrate")


async def test_opening_an_existing_valid_database_does_not_rewrite_it(tmp_path: Path) -> None:
    path = tmp_path / "v2.db"
    first = SQLiteDatabase(path)
    await initialize_database(first)
    async with first.connection() as connection:
        await connection.commit()
    before = path.read_bytes()
    await first.close()

    second = SQLiteDatabase(path)
    await initialize_database(second)
    assert path.read_bytes() == before
    await second.close()


@pytest.mark.parametrize(
    "statement",
    [
        "DROP INDEX uq_download_attempts_live_resource",
        "DROP TRIGGER trg_share_links_count_monotonic",
        "ALTER TABLE users RENAME COLUMN username_key TO login_key",
        "CREATE TABLE unexpected_table(id INTEGER PRIMARY KEY)",
    ],
)
async def test_any_structural_drift_is_rejected(statement: str, tmp_path: Path) -> None:
    db = SQLiteDatabase(tmp_path / "v2.db")
    await initialize_database(db)
    async with db.connection() as connection:
        await connection.execute(text(statement))
        await connection.commit()
    with pytest.raises(DomainError) as caught:
        await validate_database(db)
    assert caught.value.code is ErrorCode.SCHEMA_MISMATCH
    await db.close()


async def test_schema_version_mismatch_is_not_migrated(tmp_path: Path) -> None:
    db = SQLiteDatabase(tmp_path / "v2.db")
    await initialize_database(db)
    async with db.connection() as connection:
        await connection.execute(text("UPDATE schema_meta SET schema_version = 99"))
        await connection.commit()
    with pytest.raises(DomainError) as caught:
        await initialize_database(db)
    assert caught.value.code is ErrorCode.SCHEMA_MISMATCH
    async with db.connection() as connection:
        assert (await connection.execute(text("SELECT schema_version FROM schema_meta"))).scalar_one() == 99
    await db.close()


async def test_partial_database_is_never_repaired_or_upgraded(tmp_path: Path) -> None:
    path = tmp_path / "v2.db"
    db = SQLiteDatabase(path)
    async with db.connection() as connection:
        await connection.execute(text("CREATE TABLE partial_legacy(id INTEGER PRIMARY KEY)"))
        await connection.commit()
    with pytest.raises(DomainError) as caught:
        await initialize_database(db)
    assert caught.value.code is ErrorCode.SCHEMA_MISMATCH
    async with db.connection() as connection:
        assert (await connection.execute(text("SELECT name FROM sqlite_master WHERE name = 'partial_legacy'"))).scalar_one()
    await db.close()


async def test_missing_database_file_and_zero_length_file_both_initialize(tmp_path: Path) -> None:
    for name in ("new.db", "empty.db"):
        path = tmp_path / name
        if name == "empty.db":
            path.touch()
        db = SQLiteDatabase(path)
        await initialize_database(db)
        await validate_database(db)
        await db.close()


@pytest.mark.parametrize("table", ["app_settings", "tracker_cache"])
async def test_missing_singleton_seed_is_rejected(table: str, tmp_path: Path) -> None:
    db = SQLiteDatabase(tmp_path / "v2.db")
    await initialize_database(db)
    async with db.connection() as connection:
        target = {"app_settings": app_settings, "tracker_cache": tracker_cache}[table]
        await connection.execute(delete(target))
        await connection.commit()
    with pytest.raises(DomainError) as caught:
        await validate_database(db)
    assert caught.value.code is ErrorCode.SCHEMA_MISMATCH
    await db.close()


async def test_failed_initialization_rolls_back_every_object(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = SQLiteDatabase(tmp_path / "v2.db")
    monkeypatch.setattr(bootstrap_module, "DDL_STATEMENTS", (*bootstrap_module.DDL_STATEMENTS, "INVALID SQL"))
    with pytest.raises(OperationalError):
        await initialize_database(db)
    async with db.connection() as connection:
        count = (await connection.execute(text("SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"))).scalar_one()
    assert count == 0
    await db.close()


def test_download_attempt_schema_persists_optional_ingestion_start() -> None:
    column = download_attempts.c.ingestion_started_at_ms
    assert column.nullable is True
    assert column.default is None
