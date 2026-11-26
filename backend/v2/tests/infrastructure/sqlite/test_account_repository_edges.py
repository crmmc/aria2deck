from __future__ import annotations

import pytest
from sqlalchemy import text

from aria2deck.domain.accounts import AccountStatus
from aria2deck.domain.errors import DomainError, ErrorCode
from aria2deck.infrastructure.sqlite.engine import SQLiteDatabase
from aria2deck.infrastructure.sqlite.repositories.accounts import AccountRepository
from aria2deck.infrastructure.sqlite.unit_of_work import SQLiteUnitOfWork

HASH = "ab" * 32
LOOKUP_VALUE = "test-lookup-value"


async def create_user(database: SQLiteDatabase) -> int:
    async with SQLiteUnitOfWork(database) as uow:
        return await AccountRepository(uow.connection).create_user(
            username="admin",
            password_hash=HASH,
            is_admin=True,
            quota_bytes=100,
            now_ms=1,
        )


async def test_account_repository_missing_rows_and_conditional_writes(
    database: SQLiteDatabase,
) -> None:
    user_id = await create_user(database)
    async with SQLiteUnitOfWork(database) as uow:
        repository = AccountRepository(uow.connection)
        assert await repository.find_user_by_username("missing") is None
        assert len(await repository.list_users(offset=0, limit=20)) == 1
        with pytest.raises(DomainError) as caught:
            await repository.get_quota(999)
        assert caught.value.code is ErrorCode.NOT_FOUND
        with pytest.raises(DomainError) as caught:
            await repository.update_user(
                user_id=999,
                username="missing",
                username_key="missing",
                password_hash=HASH,
                is_admin=False,
                quota_bytes=1,
                is_initial_password=False,
                status=AccountStatus.ACTIVE,
                now_ms=2,
            )
        assert caught.value.code is ErrorCode.NOT_FOUND
        assert not await repository.change_password(
            user_id=user_id,
            expected_generation=999,
            password_hash=HASH,
            is_initial_password=False,
            status=AccountStatus.ACTIVE,
            now_ms=2,
        )
        with pytest.raises(DomainError):
            await repository.mark_pending_delete(user_id=999, now_ms=2)
        assert await repository.get_session_user("missing") is None
        assert await repository.get_api_token_user("missing") is None
        with pytest.raises(DomainError) as caught:
            await repository.set_rpc_secret(
                user_id=999, ciphertext=None, prefix=None, now_ms=2
            )
        assert caught.value.code is ErrorCode.NOT_FOUND
        assert not await repository.hard_delete_user(user_id)


async def test_token_repository_round_trip_and_missing_delete(
    database: SQLiteDatabase,
) -> None:
    user_id = await create_user(database)
    async with SQLiteUnitOfWork(database) as uow:
        repository = AccountRepository(uow.connection)
        token_id = await repository.create_api_token(
            user_id=user_id,
            name="CLI",
            token_digest=LOOKUP_VALUE,
            prefix="prefix",
            now_ms=1,
        )
        await repository.touch_api_token(token_id=token_id, now_ms=2)
        result = await repository.get_api_token_user(LOOKUP_VALUE)
        assert result is not None
        token, owner = result
        assert token.id == token_id
        assert token.last_used_at_ms == 2
        assert owner.id == user_id
        assert await repository.count_api_tokens(user_id) == 1
        assert len(await repository.list_api_tokens(user_id=user_id, offset=0, limit=20)) == 1
        await repository.delete_api_token(token_id=token_id, user_id=user_id)
        with pytest.raises(DomainError) as caught:
            await repository.delete_api_token(token_id=token_id, user_id=user_id)
        assert caught.value.code is ErrorCode.NOT_FOUND

    async with database.connection() as connection:
        assert (await connection.execute(text("SELECT COUNT(*) FROM api_tokens"))).scalar_one() == 0


async def test_status_round_trip_and_active_admin_counting(database: SQLiteDatabase) -> None:
    admin_id = await create_user(database)
    async with SQLiteUnitOfWork(database) as uow:
        repository = AccountRepository(uow.connection)
        assert (await repository.get_user(admin_id)).status is AccountStatus.ACTIVE
        assert await repository.count_active_admins() == 1

        await repository.update_user(
            user_id=admin_id,
            username="admin",
            username_key="admin",
            password_hash=HASH,
            is_admin=True,
            quota_bytes=100,
            is_initial_password=False,
            status=AccountStatus.LOCKED,
            now_ms=3,
        )
        assert (await repository.get_user(admin_id)).status is AccountStatus.LOCKED
        assert await repository.count_active_admins() == 0

        assert await repository.change_password(
            user_id=admin_id,
            expected_generation=1,
            password_hash=HASH,
            is_initial_password=True,
            status=AccountStatus.ACTIVE,
            now_ms=4,
        )
        assert (await repository.get_user(admin_id)).status is AccountStatus.ACTIVE
        assert await repository.count_active_admins() == 1

        second = await repository.create_user(
            username="second",
            password_hash=HASH,
            is_admin=True,
            quota_bytes=100,
            now_ms=5,
        )
        assert await repository.count_admins() == 2
        assert await repository.count_active_admins() == 2
        await repository.mark_pending_delete(user_id=second, now_ms=6)
        assert await repository.count_admins() == 2
        assert await repository.count_active_admins() == 1
