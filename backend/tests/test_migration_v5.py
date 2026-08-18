from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.auth import get_user_by_rpc_secret
from app.core.config import settings
from app.core.security import credential_digest
from app.db.bootstrap import bootstrap_database
from app.db.engine import (
    credential_scrub_marker_path,
    dispose_engine,
    get_engine,
    reset_engine,
)
from app.db.migrations import V5_DELETE_COLUMNS, V7_STORED_FILES_ADDED_COLUMNS, run_migrations
from app.db.schema import metadata
from app.repositories import auth as auth_repo


@pytest_asyncio.fixture
async def v4_database(tmp_path: Path) -> AsyncGenerator[Path, None]:
    original_db = settings.database_path
    await dispose_engine()
    reset_engine()
    settings.database_path = str(tmp_path / "v4.db")
    try:
        yield Path(settings.database_path)
    finally:
        await dispose_engine()
        reset_engine()
        settings.database_path = original_db


@pytest.mark.asyncio
async def test_v4_to_v5_deletion_migration_is_idempotent(
    v4_database: Path,
) -> None:
    async with get_engine().begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE schema_meta (id INTEGER PRIMARY KEY, "
                "version INTEGER NOT NULL, created_at_ms INTEGER NOT NULL)"
            )
        )
        await conn.execute(text("INSERT INTO schema_meta VALUES (1, 4, 123)"))
        await conn.execute(
            text("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT)")
        )
        await conn.execute(text("INSERT INTO users VALUES (1, 'existing')"))
        await conn.execute(
            text(
                "CREATE TABLE stored_files "
                "(id INTEGER PRIMARY KEY, content_hash TEXT)"
            )
        )
        await conn.execute(text("INSERT INTO stored_files VALUES (2, 'hash')"))

        assert await run_migrations(conn, 4) == 14
        assert await run_migrations(conn, 4) == 14

        for table_name in ("users", "stored_files"):
            columns = {
                str(row[1])
                for row in (
                    await conn.execute(text(f"PRAGMA table_info({table_name})"))
                ).all()
            }
            assert set(V5_DELETE_COLUMNS) <= columns
            if table_name == "stored_files":
                assert set(V7_STORED_FILES_ADDED_COLUMNS) <= columns
            row = (
                await conn.execute(
                    text(
                        f"SELECT pending_delete, delete_attempts, "
                        f"delete_next_retry_at_ms, delete_lease_token, "
                        f"delete_lease_expires_at_ms, delete_error "
                        f"FROM {table_name}"
                    )
                )
            ).one()
            assert tuple(row) == (0, 0, None, None, None, None)

        indexes = {
            str(row[0])
            for row in (
                await conn.execute(
                    text(
                        "SELECT name FROM sqlite_master "
                        "WHERE type = 'index'"
                    )
                )
            ).all()
        }
    assert "ix_users_delete_due" in indexes
    assert "ix_stored_files_delete_due" in indexes


@pytest.mark.asyncio
async def test_v5_credentials_migrate_without_plaintext_or_fk_loss(
    v4_database: Path,
) -> None:
    legacy_rpc_secret = "legacy-rpc-physical-marker-4fb4472f"
    legacy_token = "legacy-token-physical-marker-39a421db"
    async with get_engine().begin() as conn:
        await conn.run_sync(metadata.create_all)
        await conn.execute(text("DROP TABLE api_tokens"))
        await conn.execute(text("DROP TABLE users"))
        await conn.execute(
            text(
                "CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR(50) NOT NULL UNIQUE, "
                "password_hash TEXT NOT NULL, is_admin INTEGER NOT NULL DEFAULT 0, quota_bytes INTEGER NOT NULL, "
                "rpc_secret VARCHAR(128) UNIQUE, rpc_secret_created_at_ms INTEGER, "
                "is_initial_password INTEGER NOT NULL DEFAULT 0, created_at_ms INTEGER NOT NULL, "
                "updated_at_ms INTEGER NOT NULL, pending_delete INTEGER NOT NULL DEFAULT 0, "
                "delete_attempts INTEGER NOT NULL DEFAULT 0, delete_next_retry_at_ms INTEGER, "
                "delete_lease_token VARCHAR(64), delete_lease_expires_at_ms INTEGER, delete_error TEXT, "
                "CONSTRAINT ck_users_is_admin_bool CHECK (is_admin IN (0, 1)), "
                "CONSTRAINT ck_users_initial_password_bool CHECK (is_initial_password IN (0, 1)), "
                "CONSTRAINT ck_users_pending_delete_bool CHECK (pending_delete IN (0, 1)), "
                "CONSTRAINT ck_users_delete_attempts_non_negative CHECK (delete_attempts >= 0))"
            )
        )
        await conn.execute(
            text(
                "CREATE TABLE api_tokens (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL "
                "REFERENCES users(id) ON DELETE CASCADE, token VARCHAR(128) NOT NULL UNIQUE, "
                "name VARCHAR(200), created_at_ms INTEGER NOT NULL, last_used_at_ms INTEGER)"
            )
        )
        await conn.execute(text("CREATE INDEX ix_users_delete_due ON users (pending_delete, delete_next_retry_at_ms, delete_lease_expires_at_ms, id)"))
        await conn.execute(text("CREATE INDEX ix_api_tokens_user_created ON api_tokens (user_id, created_at_ms)"))
        await conn.execute(text("INSERT INTO schema_meta VALUES (1, 5, 1)"))
        await conn.execute(
            text(
                "INSERT INTO users VALUES (7, 'legacy', 'password', 0, 100, :rpc_secret, "
                "1000, 0, 1, 2, 0, 0, NULL, NULL, NULL, NULL)"
            ),
            {"rpc_secret": legacy_rpc_secret},
        )
        await conn.execute(
            text("INSERT INTO api_tokens VALUES (9, 7, :token, 'legacy', 3, NULL)"),
            {"token": legacy_token},
        )

    await bootstrap_database()
    rpc_user = await get_user_by_rpc_secret(legacy_rpc_secret)
    token_user = await auth_repo.use_api_token_digest(
        credential_digest("api-token", legacy_token)
    )
    assert rpc_user is not None and rpc_user["id"] == 7
    assert token_user is not None and token_user["id"] == 7

    async with get_engine().connect() as conn:
        user_columns = {row[1] for row in (await conn.execute(text("PRAGMA table_info(users)"))).all()}
        token_columns = {row[1] for row in (await conn.execute(text("PRAGMA table_info(api_tokens)"))).all()}
        token = (await conn.execute(text("SELECT token_digest, token_prefix, last_used_at_ms FROM api_tokens"))).one()
        token_fk = (await conn.execute(text("PRAGMA foreign_key_list(api_tokens)"))).mappings().one()
        indexes = {row[1] for row in (await conn.execute(text("PRAGMA index_list(api_tokens)"))).all()}
        violations = (await conn.execute(text("PRAGMA foreign_key_check"))).all()
    assert "rpc_secret" not in user_columns and "token" not in token_columns
    assert token[0] == credential_digest("api-token", legacy_token)
    assert token[1] == legacy_token[:16] and token[2] is not None
    assert token_fk["table"] == "users" and token_fk["on_delete"] == "CASCADE"
    assert "ix_api_tokens_user_created" in indexes and not violations
    for candidate in (v4_database, Path(str(v4_database) + "-wal")):
        if candidate.exists():
            contents = candidate.read_bytes()
            assert legacy_rpc_secret.encode() not in contents
            assert legacy_token.encode() not in contents
    assert not credential_scrub_marker_path().exists()

    await bootstrap_database()
