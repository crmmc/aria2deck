from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import delete, text
from sqlalchemy.exc import IntegrityError

from aria2deck.domain.errors import DomainError, ErrorCode
from aria2deck.domain.shares import ShareStatus, plan_share_download
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
        await shares.increment_download_count(share_id, expected_count=0, now_ms=1)
        await shares.increment_download_count(share_id, expected_count=1, now_ms=1)
        exhausted = await shares.get_share(share_id)
        with pytest.raises(DomainError) as caught:
            plan_share_download(exhausted.as_domain(), now_ms=1, size=100, range_header=None)
        assert caught.value.code is ErrorCode.SHARE_EXHAUSTED
        with pytest.raises(DomainError):
            await shares.increment_download_count(share_id, expected_count=1, now_ms=1)


@pytest.mark.parametrize(
    "state,expected_code",
    [
        ("revoked", ErrorCode.SHARE_REVOKED),
        ("expired", ErrorCode.SHARE_EXPIRED),
        ("exhausted", ErrorCode.SHARE_EXHAUSTED),
        ("file_deleted", ErrorCode.SHARE_UNAVAILABLE),
        ("content_pending_delete", ErrorCode.SHARE_UNAVAILABLE),
    ],
)
async def test_increment_download_count_rejects_invalid_share_atomically(
    database: SQLiteDatabase, state: str, expected_code: ErrorCode
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
            expires_at_ms=2 if state == "expired" else None,
            max_downloads=1,
            now_ms=1,
        )
        if state == "revoked":
            await uow.connection.execute(
                text("UPDATE share_links SET revoked_at_ms = 2 WHERE id = :id"),
                {"id": share_id},
            )
        elif state == "exhausted":
            await uow.connection.execute(
                text("UPDATE share_links SET download_count = 1 WHERE id = :id"),
                {"id": share_id},
            )
        elif state == "file_deleted":
            await uow.connection.execute(
                text("DELETE FROM user_files WHERE id = :id"), {"id": owned_file}
            )
        elif state == "content_pending_delete":
            await uow.connection.execute(
                text(
                    "UPDATE content_objects SET deletion_state = 'pending_delete' "
                    "WHERE id = (SELECT content_id FROM user_files WHERE id = :id)"
                ),
                {"id": owned_file},
            )

        with pytest.raises(DomainError) as caught:
            await shares.increment_download_count(share_id, expected_count=0, now_ms=2)
        assert caught.value.code is expected_code
        current = await shares.get_share(share_id)
    assert current.download_count == (1 if state == "exhausted" else 0)


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
                    await shares.increment_download_count(
                        share_id, expected_count=share.download_count, now_ms=1
                    )
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
            await shares.increment_download_count(share_id, expected_count=1, now_ms=1)
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


async def test_share_statuses_map_through_share_status_only(database: SQLiteDatabase) -> None:
    owner = await create_user(database)
    target = await file_id(database, owner)
    async with SQLiteUnitOfWork(database) as uow:
        shares = ShareRepository(uow.connection)
        await shares.create_share(user_id=owner, user_file_id=target, code="live", password_hash=None, expires_at_ms=5000, max_downloads=None, now_ms=1)
        await shares.create_share(user_id=owner, user_file_id=target, code="gone", password_hash=None, expires_at_ms=None, max_downloads=None, now_ms=1)
        await shares.create_share(user_id=owner, user_file_id=target, code="spent", password_hash=None, expires_at_ms=None, max_downloads=1, now_ms=1)
        await shares.create_share(user_id=owner, user_file_id=target, code="dead", password_hash=None, expires_at_ms=None, max_downloads=None, now_ms=1)
        await uow.connection.execute(text("UPDATE share_links SET download_count = 1 WHERE code = 'spent'"))
        await uow.connection.execute(text("UPDATE share_links SET revoked_at_ms = 9 WHERE code = 'dead'"))
        statuses = await shares.get_share_statuses(user_file_id=target, now_ms=100)
    assert sorted(status.value for status in statuses) == ["active", "active", "exhausted", "revoked"]
    late = None
    async with SQLiteUnitOfWork(database) as uow:
        late = await ShareRepository(uow.connection).get_share_statuses(user_file_id=target, now_ms=6000)
    assert sorted(status.value for status in late) == ["active", "exhausted", "expired", "revoked"]


async def test_find_share_by_code_and_pending_delete_derivation(database: SQLiteDatabase) -> None:
    owner = await create_user(database)
    target = await file_id(database, owner)
    async with SQLiteUnitOfWork(database) as uow:
        shares = ShareRepository(uow.connection)
        share_id = await shares.create_share(user_id=owner, user_file_id=target, code="by-code", password_hash=None, expires_at_ms=None, max_downloads=None, now_ms=1)
        record = await shares.find_share_by_code("by-code")
        assert record is not None and record.id == share_id
        assert await shares.find_share_by_code("missing") is None
        content_id = (
            await ContentRepository(uow.connection).find_object_by_key(record.content_key)
        ).id
    async with SQLiteUnitOfWork(database) as uow:
        await uow.connection.execute(
            text("UPDATE content_objects SET deletion_state = 'pending_delete' WHERE id = :id"),
            {"id": content_id},
        )
    async with SQLiteUnitOfWork(database) as uow:
        pending = await ShareRepository(uow.connection).find_share_by_code("by-code")
    assert pending is not None
    assert pending.file_pending_delete is True


async def test_revoke_delete_and_revoke_all_semantics(database: SQLiteDatabase) -> None:
    owner = await create_user(database)
    other = await create_user(database, "other", admin=False)
    own = await file_id(database, owner)
    async with SQLiteUnitOfWork(database) as uow:
        content = ContentRepository(uow.connection)
        foreign_content = await content.create_object(
            kind="file", digest="cd" * 32, size=5, storage_path="objects/foreign", now_ms=1
        )
        foreign_file = await content.add_user_file(
            user_id=other, content_id=foreign_content, display_name="foreign", now_ms=1
        )
    async with SQLiteUnitOfWork(database) as uow:
        shares = ShareRepository(uow.connection)
        first = await shares.create_share(user_id=owner, user_file_id=own, code="r1", password_hash=None, expires_at_ms=None, max_downloads=None, now_ms=1)
        second = await shares.create_share(user_id=owner, user_file_id=own, code="r2", password_hash=None, expires_at_ms=None, max_downloads=None, now_ms=1)
        foreign = await shares.create_share(user_id=other, user_file_id=foreign_file, code="r3", password_hash=None, expires_at_ms=None, max_downloads=None, now_ms=1)
        assert await shares.revoke_share(first, owner_id=owner, now_ms=10)
        assert not await shares.revoke_share(first, owner_id=owner, now_ms=11)  # 重复撤销。
        assert not await shares.revoke_share(second, owner_id=other, now_ms=12)  # 他人不可撤销。
        assert (await shares.get_share(first)).revoked_at_ms == 10
        assert await shares.revoke_all_shares(owner, now_ms=20) == 1  # first 已撤销，仅 second。
        assert (await shares.get_share(second)).revoked_at_ms == 20
        assert (await shares.get_share(foreign)).revoked_at_ms is None

        assert await shares.delete_share(first, owner_id=owner)
        assert not await shares.delete_share(first, owner_id=owner)
        assert not await shares.delete_share(foreign, owner_id=owner)
        assert await shares.count_shares(owner) == 1


async def test_page_shares_filters_status_and_paginates(database: SQLiteDatabase) -> None:
    owner = await create_user(database)
    target = await file_id(database, owner)
    async with SQLiteUnitOfWork(database) as uow:
        shares = ShareRepository(uow.connection)
        await shares.create_share(user_id=owner, user_file_id=target, code="p1", password_hash=None, expires_at_ms=None, max_downloads=None, now_ms=1)
        await shares.create_share(user_id=owner, user_file_id=target, code="p2", password_hash=None, expires_at_ms=None, max_downloads=None, now_ms=1)
        await shares.create_share(user_id=owner, user_file_id=target, code="p3", password_hash=None, expires_at_ms=None, max_downloads=None, now_ms=1)
        await uow.connection.execute(text("UPDATE share_links SET revoked_at_ms = 5 WHERE code IN ('p2', 'p3')"))
        active, active_total = await shares.page_shares(owner, now_ms=10, status=ShareStatus.ACTIVE)
        revoked, revoked_total = await shares.page_shares(owner, now_ms=10, status=ShareStatus.REVOKED, offset=1, limit=1)
        everything, everything_total = await shares.page_shares(owner, now_ms=10)
        beyond, beyond_total = await shares.page_shares(owner, now_ms=10, offset=9, limit=5)
    assert [record.code for record in active] == ["p1"]
    assert active_total == 1
    assert [record.code for record in revoked] == ["p3"]
    assert revoked_total == 2
    assert everything_total == 3
    assert [record.code for record in everything] == ["p1", "p2", "p3"]
    assert beyond == []
    assert beyond_total == 3
