from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text

from aria2deck.domain.errors import DomainError, ErrorCode
from aria2deck.domain.packs import PackSourceSnapshot
from aria2deck.domain.quota import Quota
from aria2deck.infrastructure.sqlite.engine import SQLiteDatabase
from aria2deck.infrastructure.sqlite.repositories.accounts import AccountRepository
from aria2deck.infrastructure.sqlite.repositories.content import ContentRepository
from aria2deck.infrastructure.sqlite.repositories.downloads import DownloadRepository
from aria2deck.infrastructure.sqlite.repositories.packs import PackRepository
from aria2deck.infrastructure.sqlite.repositories.quota import QuotaRepository
from aria2deck.infrastructure.sqlite.unit_of_work import SQLiteUnitOfWork

HASH = "ab" * 32


async def create_user(database: SQLiteDatabase, username: str = "admin", quota: int | None = 100, admin: bool = True) -> int:
    async with SQLiteUnitOfWork(database) as uow:
        return await AccountRepository(uow.connection).create_user(
            username=username,
            password_hash=HASH,
            is_admin=admin,
            quota_bytes=quota,
            now_ms=1,
        )


async def test_first_user_and_quota_account_are_created_atomically(database: SQLiteDatabase) -> None:
    user_id = await create_user(database)
    async with SQLiteUnitOfWork(database) as uow:
        account = await QuotaRepository(uow.connection).get(user_id)
        user = await AccountRepository(uow.connection).get_user(user_id)
    assert account == Quota(100, 0, 0)
    assert user.username == "admin"
    assert user.is_admin


async def test_first_non_admin_and_duplicate_username_leave_no_partial_account(database: SQLiteDatabase) -> None:
    with pytest.raises(DomainError):
        await create_user(database, admin=False)
    await create_user(database)
    with pytest.raises(DomainError) as caught:
        await create_user(database, username="ADMIN", admin=False)
    assert caught.value.code is ErrorCode.DUPLICATE_RESOURCE
    async with database.connection() as connection:
        assert (await connection.execute(text("SELECT COUNT(*) FROM users"))).scalar_one() == 1
        assert (await connection.execute(text("SELECT COUNT(*) FROM quota_accounts"))).scalar_one() == 1


async def test_missing_user_is_a_stable_not_found_error(database: SQLiteDatabase) -> None:
    async with SQLiteUnitOfWork(database) as uow:
        with pytest.raises(DomainError) as caught:
            await QuotaRepository(uow.connection).get(999)
    assert caught.value.code is ErrorCode.NOT_FOUND


async def test_quota_reserve_release_and_complete_are_conditional(database: SQLiteDatabase) -> None:
    user_id = await create_user(database)
    async with SQLiteUnitOfWork(database) as uow:
        quota = QuotaRepository(uow.connection)
        assert await quota.reserve(user_id, 60) == Quota(100, 0, 60)
        assert await quota.release(user_id, 20) == Quota(100, 0, 40)
        assert await quota.complete_download(user_id, 40, content_size=30, already_owned=False) == Quota(100, 30, 0)
    async with SQLiteUnitOfWork(database) as uow:
        quota = QuotaRepository(uow.connection)
        await quota.reserve(user_id, 20)
        assert await quota.complete_download(user_id, 20, content_size=30, already_owned=True) == Quota(100, 30, 0)


@pytest.mark.parametrize("operation", ["over_reserve", "over_release", "over_complete", "negative"])
async def test_quota_rejects_invalid_or_excessive_atomic_operations(database: SQLiteDatabase, operation: str) -> None:
    user_id = await create_user(database)
    async with SQLiteUnitOfWork(database) as uow:
        quota = QuotaRepository(uow.connection)
        if operation == "over_reserve":
            with pytest.raises(DomainError) as caught:
                await quota.reserve(user_id, 101)
            assert caught.value.code is ErrorCode.QUOTA_EXCEEDED
        elif operation == "over_release":
            with pytest.raises(DomainError):
                await quota.release(user_id, 1)
        elif operation == "over_complete":
            with pytest.raises(DomainError):
                await quota.complete_download(user_id, 1, content_size=1, already_owned=False)
        else:
            with pytest.raises(DomainError):
                await quota.reserve(user_id, -1)


async def test_concurrent_reservations_never_exceed_quota(database: SQLiteDatabase) -> None:
    user_id = await create_user(database)

    async def reserve_one() -> bool:
        try:
            async with SQLiteUnitOfWork(database) as uow:
                await QuotaRepository(uow.connection).reserve(user_id, 10)
            return True
        except DomainError as error:
            assert error.code is ErrorCode.QUOTA_EXCEEDED
            return False

    results = await asyncio.gather(*(reserve_one() for _ in range(25)))
    assert results.count(True) == 10
    async with SQLiteUnitOfWork(database) as uow:
        assert await QuotaRepository(uow.connection).get(user_id) == Quota(100, 0, 100)


async def test_usage_rebuild_uses_files_live_subscriptions_and_active_packs(database: SQLiteDatabase) -> None:
    user_id = await create_user(database, quota=1000)
    async with SQLiteUnitOfWork(database) as uow:
        content = ContentRepository(uow.connection)
        downloads = DownloadRepository(uow.connection)
        packs = PackRepository(uow.connection)
        content_id = await content.create_object(kind="file", digest=HASH, size=100, storage_path="objects/a", now_ms=1)
        await content.add_user_file(user_id=user_id, content_id=content_id, display_name="a", now_ms=1)
        source_id = await downloads.create_source(kind="http", resource_key="http:key", payload_json="{}", options_json="{}", selected_file_indexes_json=None, display_name="a", display_uri="https://example.com/a", now_ms=1)
        attempt_id = await downloads.create_attempt(source_id=source_id, resource_key="http:key", gid="0123456789abcdef", now_ms=1)
        await downloads.create_subscription(attempt_id=attempt_id, user_id=user_id, reserved_bytes=40, display_name="a", display_uri=None, now_ms=1)
        pack_id = await packs.create_job(owner_id=user_id, output_name="out.zip", pack_format="zip", source_size=100, reserved_bytes=30, physical_reserved_bytes=30, delete_source=False, now_ms=1)
        await packs.add_source(pack_id, PackSourceSnapshot(1, content_id, "file:" + HASH, "a", 1, 100))
        await uow.connection.execute(text("UPDATE quota_accounts SET used_bytes = 0, reserved_bytes = 0 WHERE user_id = :id"), {"id": user_id})
        rebuilt = await QuotaRepository(uow.connection).rebuild(user_id)
    assert rebuilt == Quota(1000, 100, 70)


async def test_rebuild_ignores_terminal_reservations(database: SQLiteDatabase) -> None:
    user_id = await create_user(database)
    async with SQLiteUnitOfWork(database) as uow:
        downloads = DownloadRepository(uow.connection)
        source_id = await downloads.create_source(kind="http", resource_key="http:key", payload_json="{}", options_json="{}", selected_file_indexes_json=None, display_name="a", display_uri=None, now_ms=1)
        attempt_id = await downloads.create_attempt(source_id=source_id, resource_key="http:key", gid="0123456789abcdef", now_ms=1)
        await downloads.create_subscription(attempt_id=attempt_id, user_id=user_id, reserved_bytes=40, display_name="a", display_uri=None, now_ms=1)
        await uow.connection.execute(text("UPDATE task_subscriptions SET status = 'cancelled'"))
        assert await QuotaRepository(uow.connection).rebuild(user_id) == Quota(100, 0, 0)


async def test_account_lookup_missing_is_stable(database: SQLiteDatabase) -> None:
    async with SQLiteUnitOfWork(database) as uow:
        with pytest.raises(DomainError) as caught:
            await AccountRepository(uow.connection).get_user(999)
    assert caught.value.code is ErrorCode.NOT_FOUND


async def test_rebuild_missing_user_is_stable(database: SQLiteDatabase) -> None:
    async with SQLiteUnitOfWork(database) as uow:
        with pytest.raises(DomainError) as caught:
            await QuotaRepository(uow.connection).rebuild(999)
    assert caught.value.code is ErrorCode.NOT_FOUND


@pytest.mark.parametrize("operation", ["release", "complete"])
async def test_conditional_quota_write_conflict_is_not_silent(database: SQLiteDatabase, operation: str) -> None:
    user_id = await create_user(database)
    async with SQLiteUnitOfWork(database) as uow:
        quota = QuotaRepository(uow.connection)
        await quota.reserve(user_id, 10)
        await uow.connection.execute(text("CREATE TRIGGER test_ignore_quota_write BEFORE UPDATE OF reserved_bytes ON quota_accounts BEGIN SELECT RAISE(IGNORE); END"))
        with pytest.raises(DomainError) as caught:
            if operation == "release":
                await quota.release(user_id, 5)
            else:
                await quota.complete_download(user_id, 5, content_size=5, already_owned=False)
        assert caught.value.code is ErrorCode.INVALID_STATE



async def test_rebuild_recreates_a_missing_account_and_get_reads_it(
    database: SQLiteDatabase,
) -> None:
    user_id = await create_user(database, quota=1000)
    async with SQLiteUnitOfWork(database) as uow:
        content = ContentRepository(uow.connection)
        downloads = DownloadRepository(uow.connection)
        content_id = await content.create_object(
            kind="file", digest=HASH, size=100, storage_path="objects/a", now_ms=1
        )
        await content.add_user_file(
            user_id=user_id, content_id=content_id, display_name="a", now_ms=1
        )
        source_id = await downloads.create_source(
            kind="http",
            resource_key="http:key",
            payload_json="{}",
            options_json="{}",
            selected_file_indexes_json=None,
            display_name="a",
            display_uri=None,
            now_ms=1,
        )
        attempt_id = await downloads.create_attempt(
            source_id=source_id, resource_key="http:key", gid="0123456789abcdef", now_ms=1
        )
        await downloads.create_subscription(
            attempt_id=attempt_id,
            user_id=user_id,
            reserved_bytes=40,
            display_name="a",
            display_uri=None,
            now_ms=1,
        )
        await uow.connection.execute(text("DELETE FROM quota_accounts WHERE user_id = :id"), {"id": user_id})

        quota = QuotaRepository(uow.connection)
        assert await quota.rebuild(user_id) == Quota(1000, 100, 40)
        assert await quota.get(user_id) == Quota(1000, 100, 40)
