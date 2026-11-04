from __future__ import annotations

import pytest
from sqlalchemy import text

from aria2deck.domain.errors import DomainError, ErrorCode
from aria2deck.infrastructure.sqlite.engine import SQLiteDatabase
from aria2deck.infrastructure.sqlite.repositories.content import ContentRepository
from aria2deck.infrastructure.sqlite.repositories.packs import PackRepository
from aria2deck.infrastructure.sqlite.unit_of_work import SQLiteUnitOfWork

from .test_accounts_quota import HASH, create_user


async def object_id(database: SQLiteDatabase, digest: str = HASH) -> int:
    async with SQLiteUnitOfWork(database) as uow:
        return await ContentRepository(uow.connection).create_object(kind="file", digest=digest, size=10, storage_path="objects/" + digest[:4], now_ms=1)


async def test_content_key_and_canonical_path_are_unique(database: SQLiteDatabase) -> None:
    await object_id(database)
    async with SQLiteUnitOfWork(database) as uow:
        repo = ContentRepository(uow.connection)
        with pytest.raises(DomainError) as caught:
            await repo.create_object(kind="file", digest=HASH, size=10, storage_path="objects/other", now_ms=1)
        assert caught.value.code is ErrorCode.DUPLICATE_RESOURCE
        with pytest.raises(DomainError):
            await repo.create_object(kind="directory", digest=HASH, size=10, storage_path="objects/abab", now_ms=1)
        directory_id = await repo.create_object(kind="directory", digest=HASH, size=10, storage_path="objects/directory", now_ms=1)
        assert (await repo.get_object(directory_id)).kind.value == "directory"


async def test_content_path_validation_runs_before_persistence(database: SQLiteDatabase) -> None:
    async with SQLiteUnitOfWork(database) as uow:
        repo = ContentRepository(uow.connection)
        with pytest.raises(DomainError):
            await repo.create_object(kind="file", digest="bad", size=1, storage_path="objects/a", now_ms=1)
        with pytest.raises(DomainError):
            await repo.create_object(kind="file", digest=HASH, size=1, storage_path="/absolute", now_ms=1)


async def test_user_file_references_have_independent_ownership_and_unique_names(database: SQLiteDatabase) -> None:
    owner = await create_user(database)
    other = await create_user(database, "other", admin=False)
    content_id = await object_id(database)
    async with SQLiteUnitOfWork(database) as uow:
        repo = ContentRepository(uow.connection)
        first = await repo.add_user_file(user_id=owner, content_id=content_id, display_name="same", now_ms=1)
        second = await repo.add_user_file(user_id=other, content_id=content_id, display_name="same", now_ms=1)
        assert first != second
        with pytest.raises(DomainError):
            await repo.add_user_file(user_id=owner, content_id=content_id, display_name="another", now_ms=1)
        other_content = await repo.create_object(kind="file", digest="cd" * 32, size=5, storage_path="objects/cd", now_ms=1)
        with pytest.raises(DomainError):
            await repo.add_user_file(user_id=owner, content_id=other_content, display_name="same", now_ms=1)


async def test_directory_entries_are_unique_by_full_path(database: SQLiteDatabase) -> None:
    content_id = await object_id(database)
    async with SQLiteUnitOfWork(database) as uow:
        repo = ContentRepository(uow.connection)
        entry_id = await repo.add_entry(content_id=content_id, parent_path="", name="a", entry_kind="file", size=10, now_ms=1)
        assert entry_id > 0
        with pytest.raises(DomainError):
            await repo.add_entry(content_id=content_id, parent_path="", name="a", entry_kind="file", size=10, now_ms=1)
        with pytest.raises(DomainError):
            await repo.add_entry(content_id=content_id, parent_path="../", name="a", entry_kind="file", size=10, now_ms=1)


async def test_pending_delete_content_rejects_new_references(database: SQLiteDatabase) -> None:
    user_id = await create_user(database)
    content_id = await object_id(database)
    async with SQLiteUnitOfWork(database) as uow:
        repo = ContentRepository(uow.connection)
        assert await repo.claim_orphan_delete(
            content_id, now_ms=1, lease_owner="content-deletion", lease_ttl_ms=1000
        )
        assert (await repo.get_object(content_id)).deletion_state.value == "pending_delete"
        with pytest.raises(DomainError) as caught:
            await repo.add_user_file(user_id=user_id, content_id=content_id, display_name="a", now_ms=1)
        assert caught.value.code is ErrorCode.CONTENT_PENDING_DELETE
        assert not await repo.claim_orphan_delete(
            content_id, now_ms=2, lease_owner="content-deletion", lease_ttl_ms=1000
        )


async def test_existing_reference_and_pending_pack_source_block_physical_delete_claim(database: SQLiteDatabase) -> None:
    user_id = await create_user(database)
    content_id = await object_id(database)
    async with SQLiteUnitOfWork(database) as uow:
        content = ContentRepository(uow.connection)
        file_id = await content.add_user_file(user_id=user_id, content_id=content_id, display_name="a", now_ms=1)
        assert not await content.claim_orphan_delete(
            content_id, now_ms=1, lease_owner="content-deletion", lease_ttl_ms=1000
        )
        pack_id = await PackRepository(uow.connection).create_job(owner_id=user_id, output_name="a.zip", pack_format="zip", source_size=10, reserved_bytes=10, physical_reserved_bytes=10, delete_source=True, now_ms=1)
        from aria2deck.domain.packs import PackSourceSnapshot
        await PackRepository(uow.connection).add_source(pack_id, PackSourceSnapshot(file_id, content_id, "file:" + HASH, "a", 1, 10))
        await uow.connection.execute(text("DELETE FROM user_files WHERE id = :id"), {"id": file_id})
        assert not await content.claim_orphan_delete(
            content_id, now_ms=2, lease_owner="content-deletion", lease_ttl_ms=1000
        )
        await uow.connection.execute(text("UPDATE pack_sources SET cleanup_status = 'kept'"))
        assert await content.claim_orphan_delete(
            content_id, now_ms=3, lease_owner="content-deletion", lease_ttl_ms=1000
        )


async def test_pack_sources_do_not_have_current_file_or_content_foreign_keys(database: SQLiteDatabase) -> None:
    async with database.connection() as connection:
        rows = (await connection.execute(text("SELECT \"table\" FROM pragma_foreign_key_list('pack_sources')"))).scalars().all()
    assert set(rows) == {"pack_jobs"}


async def test_content_repository_rejects_invalid_kind_size_and_missing_records(database: SQLiteDatabase) -> None:
    async with SQLiteUnitOfWork(database) as uow:
        repo = ContentRepository(uow.connection)
        with pytest.raises(DomainError):
            await repo.create_object(kind="link", digest=HASH, size=1, storage_path="objects/a", now_ms=1)
        with pytest.raises(DomainError):
            await repo.create_object(kind="file", digest=HASH, size=-1, storage_path="objects/a", now_ms=1)
        with pytest.raises(DomainError) as caught:
            await repo.get_object(999)
        assert caught.value.code is ErrorCode.NOT_FOUND
        content_id = await repo.create_object(kind="file", digest=HASH, size=1, storage_path="objects/a", now_ms=1)
        with pytest.raises(DomainError):
            await repo.add_entry(content_id=content_id, parent_path="", name="a", entry_kind="link", size=1, now_ms=1)
        with pytest.raises(DomainError):
            await repo.add_entry(content_id=content_id, parent_path="", name="a", entry_kind="file", size=-1, now_ms=1)
