from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import delete, text
from sqlalchemy.exc import IntegrityError, OperationalError

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
    users,
)

EXPECTED_TABLES = {
    "schema_meta",
    "users",
    "sessions",
    "api_tokens",
    "app_settings",
    "download_sources",
    "download_attempts",
    "attempt_manifests",
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


async def test_fresh_database_defaults_new_accounts_to_active(database: SQLiteDatabase) -> None:
    async with database.connection() as connection:
        await connection.execute(
            text(
                "INSERT INTO users (username, username_key, password_hash, is_admin, quota_bytes, "
                "is_initial_password, credential_generation, pending_delete, delete_retry_count, "
                "created_at_ms, updated_at_ms) "
                "VALUES ('u', 'u', 'h', 0, 0, 0, 1, 0, 0, 0, 0)"
            )
        )
        await connection.commit()
        status = (await connection.execute(text("SELECT status FROM users"))).scalar_one()
    assert status == "active"
    assert SCHEMA_VERSION == 3


def test_users_status_column_is_required_with_active_default() -> None:
    column = users.c.status
    assert column.nullable is False
    assert str(column.server_default.arg) == "active"


def test_download_attempt_schema_persists_optional_ingestion_start() -> None:
    column = download_attempts.c.ingestion_started_at_ms
    assert column.nullable is True
    assert column.default is None


async def test_attempt_manifest_is_one_required_row_per_attempt(database: SQLiteDatabase) -> None:
    """清单表由 attempt 派生：一次尝试一份、内容必填、尝试删除时跟着走。"""
    async with database.connection() as connection:
        await connection.execute(
            text(
                "INSERT INTO download_sources (id, kind, resource_key, payload_json, options_json, "
                "display_name, created_at_ms) VALUES (1, 'http', 'rk', '{}', '{}', 'f.bin', 1)"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO download_attempts (id, source_id, resource_key, gid, status, "
                "submission_state, completed_length, physical_reserved_bytes, created_at_ms, "
                "updated_at_ms) VALUES (1, 1, 'rk', 'gid', 'completed', 'submitted', 1, 1, 1, 1)"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO attempt_manifests (attempt_id, files_json, created_at_ms) "
                "VALUES (1, '[{\"path\": \"a.bin\", \"size\": 1}]', 1)"
            )
        )
        await connection.commit()
        stored = (
            await connection.execute(text("SELECT files_json FROM attempt_manifests"))
        ).scalar_one()
        assert json.loads(stored) == [{"path": "a.bin", "size": 1}]

        with pytest.raises(IntegrityError):
            await connection.execute(
                text(
                    "INSERT INTO attempt_manifests (attempt_id, files_json, created_at_ms) "
                    "VALUES (1, '[]', 1)"
                )
            )
        await connection.rollback()
        with pytest.raises(IntegrityError):
            await connection.execute(
                text(
                    "INSERT INTO attempt_manifests (attempt_id, created_at_ms) VALUES (2, 1)"
                )
            )
        await connection.rollback()

        await connection.execute(text("DELETE FROM download_attempts WHERE id = 1"))
        await connection.commit()
        remaining = (
            await connection.execute(text("SELECT COUNT(*) FROM attempt_manifests"))
        ).scalar_one()
    assert remaining == 0
