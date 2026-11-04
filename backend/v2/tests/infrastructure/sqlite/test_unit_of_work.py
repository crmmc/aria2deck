from __future__ import annotations

import pytest
from sqlalchemy import text

from aria2deck.domain.errors import DomainError, ErrorCode
from aria2deck.infrastructure.sqlite.engine import SQLiteDatabase
from aria2deck.infrastructure.sqlite.repositories.accounts import AccountRepository
from aria2deck.infrastructure.sqlite.unit_of_work import SQLiteUnitOfWork

TEST_HASH = "h"


async def test_unit_of_work_commits_and_rolls_back_explicitly(database: SQLiteDatabase) -> None:
    async with SQLiteUnitOfWork(database) as uow:
        await AccountRepository(uow.connection).create_user(username="a", password_hash=TEST_HASH, is_admin=True, quota_bytes=10, now_ms=1)
    async with database.connection() as connection:
        assert (await connection.execute(text("SELECT COUNT(*) FROM users"))).scalar_one() == 1

    with pytest.raises(DomainError):
        async with SQLiteUnitOfWork(database) as uow:
            await AccountRepository(uow.connection).create_user(username="b", password_hash=TEST_HASH, is_admin=False, quota_bytes=10, now_ms=1)
            raise DomainError(ErrorCode.INVALID_STATE, "测试回滚")
    async with database.connection() as connection:
        assert (await connection.execute(text("SELECT COUNT(*) FROM users"))).scalar_one() == 1


async def test_unit_of_work_enables_foreign_keys_and_immediate_transactions(database: SQLiteDatabase) -> None:
    async with SQLiteUnitOfWork(database) as uow:
        assert (await uow.connection.execute(text("PRAGMA foreign_keys"))).scalar_one() == 1
        with pytest.raises(Exception) as caught:
            await uow.connection.execute(text("INSERT INTO sessions(id, user_id, credential_generation, created_at_ms, expires_at_ms) VALUES ('s', 999, 1, 1, 2)"))
        assert "FOREIGN KEY" in str(caught.value)


async def test_unit_of_work_cannot_be_reentered_or_used_outside_context(database: SQLiteDatabase) -> None:
    uow = SQLiteUnitOfWork(database)
    with pytest.raises(RuntimeError):
        _ = uow.connection
    async with uow:
        with pytest.raises(RuntimeError):
            await uow.__aenter__()
    with pytest.raises(RuntimeError):
        _ = uow.connection


async def test_begin_failure_closes_the_connection(database: SQLiteDatabase, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fail_configure(_connection: object) -> None:
        raise RuntimeError("测试开始事务失败")

    monkeypatch.setattr(database, "configure", fail_configure)
    with pytest.raises(RuntimeError):
        async with SQLiteUnitOfWork(database):
            pass
