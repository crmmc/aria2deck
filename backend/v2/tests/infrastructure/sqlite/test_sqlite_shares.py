from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import delete, text
from sqlalchemy.exc import IntegrityError

from aria2deck.domain.errors import DomainError, ErrorCode
from aria2deck.domain.shares import plan_share_download
from aria2deck.infrastructure.sqlite.engine import SQLiteDatabase
from aria2deck.infrastructure.sqlite.repositories.content import ContentRepository
from aria2deck.infrastructure.sqlite.repositories.shares import ShareRepository
from aria2deck.infrastructure.sqlite.schema import share_links, user_files, users
from aria2deck.infrastructure.sqlite.unit_of_work import SQLiteUnitOfWork

from .test_accounts_quota import HASH, create_user


async def file_id(database: SQLiteDatabase, user_id: int) -> int:
    async with SQLiteUnitOfWork(database) as uow:
        content = ContentRepository(uow.connection)
        content_id = await content.create_object(kind="file", digest=HASH, size=100, storage_path="objects/share", now_ms=1)
        return await content.add_user_file(user_id=user_id, content_id=content_id, display_name="shared", now_ms=1)


async def test_share_owner_is_explicit_and_file_ownership_is_validated(
    database: SQLiteDatabase,
) -> None:
    owner = await create_user(database)
    other = await create_user(database, "other", admin=False)
    owned_file = await file_id(database, owner)
    async with SQLiteUnitOfWork(database) as uow:
        shares = ShareRepository(uow.connection)
        share_id = await shares.create_share(user_id=owner, user_file_id=owned_file, code="code", password_hash=None, expires_at_ms=None, max_downloads=2, now_ms=1)
        share = await shares.get_share(share_id)
        assert share.owner_id == owner
        assert share.user_file_id == owned_file
        assert share.file_name == "shared"
        assert share.file_size == 100
        assert share.file_kind == "file"
        assert share.content_key == "file:" + HASH
        assert share.as_domain().download_count == 0
        assert await shares.list_shares(owner) == [share]
        assert await shares.list_shares(other) == []
        with pytest.raises(DomainError) as caught:
            await shares.create_share(user_id=other, user_file_id=owned_file, code="other", password_hash=None, expires_at_ms=None, max_downloads=1, now_ms=1)
        assert caught.value.code is ErrorCode.NOT_FOUND
    assert "owner_id" in share_links.c


async def test_only_ten_currently_active_shares_count_toward_capacity(database: SQLiteDatabase) -> None:
    owner = await create_user(database)
    owned_file = await file_id(database, owner)
    async with SQLiteUnitOfWork(database) as uow:
        shares = ShareRepository(uow.connection)
        for index in range(10):
            await shares.create_share(user_id=owner, user_file_id=owned_file, code=f"code-{index}", password_hash=None, expires_at_ms=1000, max_downloads=1, now_ms=1)
        with pytest.raises(DomainError) as caught:
            await shares.create_share(user_id=owner, user_file_id=owned_file, code="overflow", password_hash=None, expires_at_ms=1000, max_downloads=1, now_ms=1)
        assert caught.value.code is ErrorCode.SHARE_LIMIT_EXCEEDED
        await uow.connection.execute(text("UPDATE share_links SET revoked_at_ms = 2 WHERE code = 'code-0'"))
        await uow.connection.execute(text("UPDATE share_links SET expires_at_ms = 1 WHERE code = 'code-1'"))
        await uow.connection.execute(text("UPDATE share_links SET download_count = 1 WHERE code = 'code-2'"))
        replacement = await shares.create_share(user_id=owner, user_file_id=owned_file, code="replacement", password_hash=None, expires_at_ms=1000, max_downloads=1, now_ms=2)
        assert replacement > 0


async def test_full_and_partial_downloads_use_atomic_counting(database: SQLiteDatabase) -> None:
    owner = await create_user(database)
    owned_file = await file_id(database, owner)
    async with SQLiteUnitOfWork(database) as uow:
        shares = ShareRepository(uow.connection)
        share_id = await shares.create_share(user_id=owner, user_file_id=owned_file, code="code", password_hash=None, expires_at_ms=None, max_downloads=2, now_ms=1)
        first = await shares.get_share(share_id)
        partial = plan_share_download(first.as_domain(), now_ms=1, size=100, range_header="bytes=0-49")
        assert not partial.count_download
        full = plan_share_download(first.as_domain(), now_ms=1, size=100, range_header="bytes=0-99")
        assert full.count_download
        await shares.increment_download_count(share_id, expected_count=0)
        await shares.increment_download_count(share_id, expected_count=1)
        exhausted = await shares.get_share(share_id)
        with pytest.raises(DomainError) as caught:
            plan_share_download(exhausted.as_domain(), now_ms=1, size=100, range_header=None)
        assert caught.value.code is ErrorCode.SHARE_EXHAUSTED
        with pytest.raises(DomainError):
            await shares.increment_download_count(share_id, expected_count=1)


async def test_concurrent_full_downloads_never_cross_the_limit(database: SQLiteDatabase) -> None:
    owner = await create_user(database)
    owned_file = await file_id(database, owner)
    async with SQLiteUnitOfWork(database) as uow:
        share_id = await ShareRepository(uow.connection).create_share(user_id=owner, user_file_id=owned_file, code="code", password_hash=None, expires_at_ms=None, max_downloads=5, now_ms=1)

    async def download() -> bool:
        try:
            async with SQLiteUnitOfWork(database) as uow:
                shares = ShareRepository(uow.connection)
                share = await shares.get_share(share_id)
                decision = plan_share_download(share.as_domain(), now_ms=1, size=100, range_header=None)
                if decision.count_download:
                    await shares.increment_download_count(share_id, expected_count=share.download_count)
            return True
        except DomainError:
            return False

    results = await asyncio.gather(*(download() for _ in range(20)))
    assert results.count(True) == 5
    async with SQLiteUnitOfWork(database) as uow:
        assert (await ShareRepository(uow.connection).get_share(share_id)).download_count == 5


async def test_share_count_trigger_rejects_decrease_and_over_limit(database: SQLiteDatabase) -> None:
    owner = await create_user(database)
    owned_file = await file_id(database, owner)
    async with SQLiteUnitOfWork(database) as uow:
        share_id = await ShareRepository(uow.connection).create_share(user_id=owner, user_file_id=owned_file, code="code", password_hash=None, expires_at_ms=None, max_downloads=2, now_ms=1)
        await uow.connection.execute(text("UPDATE share_links SET download_count = 1 WHERE id = :id"), {"id": share_id})
        with pytest.raises(IntegrityError):
            await uow.connection.execute(text("UPDATE share_links SET download_count = 0 WHERE id = :id"), {"id": share_id})


async def test_missing_share_is_not_found(database: SQLiteDatabase) -> None:
    async with SQLiteUnitOfWork(database) as uow:
        with pytest.raises(DomainError) as caught:
            await ShareRepository(uow.connection).get_share(999)
    assert caught.value.code is ErrorCode.NOT_FOUND


async def test_duplicate_share_code_and_stale_count_are_stable(database: SQLiteDatabase) -> None:
    owner = await create_user(database)
    owned_file = await file_id(database, owner)
    async with SQLiteUnitOfWork(database) as uow:
        shares = ShareRepository(uow.connection)
        share_id = await shares.create_share(user_id=owner, user_file_id=owned_file, code="code", password_hash=None, expires_at_ms=None, max_downloads=2, now_ms=1)
        with pytest.raises(DomainError) as caught:
            await shares.create_share(user_id=owner, user_file_id=owned_file, code="code", password_hash=None, expires_at_ms=None, max_downloads=2, now_ms=1)
        assert caught.value.code is ErrorCode.DUPLICATE_RESOURCE
        with pytest.raises(DomainError) as caught:
            await shares.increment_download_count(share_id, expected_count=1)
        assert caught.value.code is ErrorCode.INVALID_STATE


async def test_concurrent_share_creation_never_exceeds_active_limit(database: SQLiteDatabase) -> None:
    owner = await create_user(database)
    owned_file = await file_id(database, owner)

    async def create(index: int) -> bool:
        try:
            async with SQLiteUnitOfWork(database) as uow:
                await ShareRepository(uow.connection).create_share(user_id=owner, user_file_id=owned_file, code=f"concurrent-{index}", password_hash=None, expires_at_ms=None, max_downloads=1, now_ms=1)
            return True
        except DomainError as error:
            assert error.code is ErrorCode.SHARE_LIMIT_EXCEEDED
            return False

    results = await asyncio.gather(*(create(index) for index in range(20)))
    assert results.count(True) == 10



async def test_deleted_user_file_leaves_a_visible_snapshot_but_blocks_public_access(
    database: SQLiteDatabase,
) -> None:
    owner = await create_user(database)
    owned_file = await file_id(database, owner)
    async with SQLiteUnitOfWork(database) as uow:
        shares = ShareRepository(uow.connection)
        share_id = await shares.create_share(
            user_id=owner,
            user_file_id=owned_file,
            code="code",
            password_hash=None,
            expires_at_ms=None,
            max_downloads=2,
            now_ms=1,
        )
        await uow.connection.execute(
            text("DELETE FROM user_files WHERE id = :id"), {"id": owned_file}
        )

        share = await shares.get_share(share_id)
        assert share.user_file_id is None
        assert share.owner_id == owner
        assert (share.file_name, share.file_size, share.file_kind) == ("shared", 100, "file")
        assert share.content_key == "file:" + HASH
        assert await shares.list_shares(owner) == [share]
        with pytest.raises(DomainError) as caught:
            plan_share_download(share.as_domain(), now_ms=2, size=100, range_header=None)
        assert caught.value.code is ErrorCode.SHARE_UNAVAILABLE


async def test_user_delete_cascades_share_shells_after_file_cleanup(
    database: SQLiteDatabase,
) -> None:
    owner = await create_user(database)
    owned_file = await file_id(database, owner)
    async with SQLiteUnitOfWork(database) as uow:
        shares = ShareRepository(uow.connection)
        share_id = await shares.create_share(
            user_id=owner,
            user_file_id=owned_file,
            code="code",
            password_hash=None,
            expires_at_ms=None,
            max_downloads=2,
            now_ms=1,
        )
        await uow.connection.execute(delete(user_files).where(user_files.c.id == owned_file))
        assert (await shares.get_share(share_id)).user_file_id is None
        await uow.connection.execute(delete(users).where(users.c.id == owner))

        with pytest.raises(DomainError) as caught:
            await shares.get_share(share_id)
        assert caught.value.code is ErrorCode.NOT_FOUND
        assert await shares.list_shares(owner) == []
