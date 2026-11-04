from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from aria2deck.domain.errors import DomainError, ErrorCode
from aria2deck.domain.packs import PackSourceSnapshot, PackStatus, PackStep, PreparedOutput
from aria2deck.infrastructure.sqlite.engine import SQLiteDatabase
from aria2deck.infrastructure.sqlite.repositories.content import ContentRepository
from aria2deck.infrastructure.sqlite.repositories.packs import PackRepository
from aria2deck.infrastructure.sqlite.unit_of_work import SQLiteUnitOfWork

from .test_accounts_quota import HASH, create_user

SNAPSHOT = PackSourceSnapshot(1, 1, "file:" + HASH, "source", 1, 100)


async def pack_id(database: SQLiteDatabase, owner_id: int) -> int:
    async with SQLiteUnitOfWork(database) as uow:
        return await PackRepository(uow.connection).create_job(owner_id=owner_id, output_name="output.zip", pack_format="zip", source_size=100, reserved_bytes=120, physical_reserved_bytes=120, delete_source=True, now_ms=1)


async def test_pack_job_and_source_snapshot_survive_reopen(database: SQLiteDatabase) -> None:
    owner = await create_user(database, quota=1000)
    job_id = await pack_id(database, owner)
    async with SQLiteUnitOfWork(database) as uow:
        packs = PackRepository(uow.connection)
        source_id = await packs.add_source(job_id, SNAPSHOT)
        prepared = PreparedOutput("prepared/output.zip", 120, "cd" * 32)
        await uow.connection.execute(
            text("UPDATE pack_jobs SET status = 'packing', step = 'verifying' WHERE id = :id"),
            {"id": job_id},
        )
        await packs.set_prepared(job_id, prepared, now_ms=2)
        assert source_id > 0
    await database.close()

    reopened = SQLiteDatabase(database.path)
    async with SQLiteUnitOfWork(reopened) as uow:
        packs = PackRepository(uow.connection)
        job = await packs.get_job(job_id)
        source = await packs.get_source(source_id)
        assert job.status is PackStatus.PACKING
        assert job.prepared == prepared
        assert source.snapshot == SNAPSHOT
    await reopened.close()


async def test_prepared_fields_are_all_or_none_at_database_boundary(database: SQLiteDatabase) -> None:
    owner = await create_user(database)
    job_id = await pack_id(database, owner)
    async with SQLiteUnitOfWork(database) as uow:
        with pytest.raises(IntegrityError):
            await uow.connection.execute(text("UPDATE pack_jobs SET prepared_path = 'prepared/a.zip' WHERE id = :id"), {"id": job_id})
    async with SQLiteUnitOfWork(database) as uow:
        with pytest.raises(DomainError) as caught:
            await PackRepository(uow.connection).set_prepared(job_id, PreparedOutput("../bad", 1, HASH), now_ms=1)
        assert caught.value.code is ErrorCode.INVALID_INPUT


async def test_pack_source_snapshot_is_unique_and_does_not_follow_current_file(database: SQLiteDatabase) -> None:
    owner = await create_user(database)
    job_id = await pack_id(database, owner)
    async with SQLiteUnitOfWork(database) as uow:
        packs = PackRepository(uow.connection)
        await packs.add_source(job_id, SNAPSHOT)
        with pytest.raises(DomainError):
            await packs.add_source(job_id, SNAPSHOT)


async def test_pack_job_rejects_invalid_format_and_owner(database: SQLiteDatabase) -> None:
    async with SQLiteUnitOfWork(database) as uow:
        with pytest.raises(IntegrityError):
            await PackRepository(uow.connection).create_job(owner_id=999, output_name="bad.zip", pack_format="zip", source_size=1, reserved_bytes=1, physical_reserved_bytes=1, delete_source=False, now_ms=1)
    owner = await create_user(database)
    async with SQLiteUnitOfWork(database) as uow:
        with pytest.raises(IntegrityError):
            await PackRepository(uow.connection).create_job(owner_id=owner, output_name="bad.7z", pack_format="7z", source_size=1, reserved_bytes=1, physical_reserved_bytes=1, delete_source=False, now_ms=1)


async def test_user_file_identity_is_immutable(database: SQLiteDatabase) -> None:
    owner = await create_user(database)
    other = await create_user(database, "other", admin=False)
    async with SQLiteUnitOfWork(database) as uow:
        content = ContentRepository(uow.connection)
        content_id = await content.create_object(kind="file", digest=HASH, size=1, storage_path="objects/a", now_ms=1)
        file_id = await content.add_user_file(user_id=owner, content_id=content_id, display_name="a", now_ms=1)
        with pytest.raises(IntegrityError):
            await uow.connection.execute(text("UPDATE user_files SET user_id = :other WHERE id = :id"), {"other": other, "id": file_id})


async def test_missing_pack_and_source_are_stable_errors(database: SQLiteDatabase) -> None:
    async with SQLiteUnitOfWork(database) as uow:
        packs = PackRepository(uow.connection)
        with pytest.raises(DomainError) as caught:
            await packs.get_job(1)
        assert caught.value.code is ErrorCode.NOT_FOUND
        with pytest.raises(DomainError):
            await packs.get_source(1)


async def test_pack_repository_rejects_negative_sizes_and_missing_prepared_update(database: SQLiteDatabase) -> None:
    owner = await create_user(database)
    async with SQLiteUnitOfWork(database) as uow:
        packs = PackRepository(uow.connection)
        with pytest.raises(DomainError):
            await packs.create_job(owner_id=owner, output_name="a.zip", pack_format="zip", source_size=-1, reserved_bytes=1, physical_reserved_bytes=1, delete_source=False, now_ms=1)
        with pytest.raises(DomainError) as caught:
            await packs.set_prepared(999, PreparedOutput("prepared/a.zip", 1, HASH), now_ms=1)
        assert caught.value.code is ErrorCode.NOT_FOUND



@pytest.mark.parametrize("status", [PackStatus.PENDING, PackStatus.DONE, PackStatus.FAILED])
async def test_prepared_write_rejects_non_packing_jobs(
    database: SQLiteDatabase, status: PackStatus
) -> None:
    owner = await create_user(database)
    job_id = await pack_id(database, owner)
    async with SQLiteUnitOfWork(database) as uow:
        content_id = await ContentRepository(uow.connection).create_object(
            kind="file", digest=HASH, size=1, storage_path="objects/output", now_ms=1
        )
        await uow.connection.execute(
            text(
                "UPDATE pack_jobs SET status = :status, step = 'verifying', "
                "output_content_id = :output_id WHERE id = :id"
            ),
            {
                "status": status.value,
                "output_id": content_id if status is PackStatus.DONE else None,
                "id": job_id,
            },
        )
        with pytest.raises(DomainError) as caught:
            await PackRepository(uow.connection).set_prepared(
                job_id, PreparedOutput("prepared/a.zip", 1, HASH), now_ms=2
            )
        assert caught.value.code is ErrorCode.INVALID_STATE


async def test_prepared_write_recovers_same_output_and_rejects_a_different_one(
    database: SQLiteDatabase,
) -> None:
    owner = await create_user(database)
    job_id = await pack_id(database, owner)
    prepared = PreparedOutput("prepared/a.zip", 1, HASH)
    async with SQLiteUnitOfWork(database) as uow:
        packs = PackRepository(uow.connection)
        await uow.connection.execute(
            text("UPDATE pack_jobs SET status = 'packing', step = 'verifying' WHERE id = :id"),
            {"id": job_id},
        )
        await packs.set_prepared(job_id, prepared, now_ms=2)
        await packs.set_prepared(job_id, prepared, now_ms=3)
        assert (await packs.get_job(job_id)).prepared == prepared
        with pytest.raises(DomainError) as caught:
            await packs.set_prepared(
                job_id, PreparedOutput("prepared/other.zip", 1, HASH), now_ms=4
            )
        assert caught.value.code is ErrorCode.INVALID_STATE



async def test_prepared_write_requires_the_verifying_step(database: SQLiteDatabase) -> None:
    owner = await create_user(database)
    job_id = await pack_id(database, owner)
    async with SQLiteUnitOfWork(database) as uow:
        await uow.connection.execute(
            text("UPDATE pack_jobs SET status = 'packing', step = :step WHERE id = :id"),
            {"step": PackStep.COMPRESSING.value, "id": job_id},
        )
        with pytest.raises(DomainError) as caught:
            await PackRepository(uow.connection).set_prepared(
                job_id, PreparedOutput("prepared/a.zip", 1, HASH), now_ms=2
            )
        assert caught.value.code is ErrorCode.INVALID_STATE



async def test_prepared_write_does_not_hide_an_ignored_database_update(
    database: SQLiteDatabase,
) -> None:
    owner = await create_user(database)
    job_id = await pack_id(database, owner)
    async with SQLiteUnitOfWork(database) as uow:
        await uow.connection.execute(
            text("UPDATE pack_jobs SET status = 'packing', step = 'verifying' WHERE id = :id"),
            {"id": job_id},
        )
        await uow.connection.execute(
            text(
                "CREATE TRIGGER test_ignore_prepared_write "
                "BEFORE UPDATE OF prepared_path ON pack_jobs BEGIN SELECT RAISE(IGNORE); END"
            )
        )
        with pytest.raises(DomainError) as caught:
            await PackRepository(uow.connection).set_prepared(
                job_id, PreparedOutput("prepared/a.zip", 1, HASH), now_ms=2
            )
        assert caught.value.code is ErrorCode.INVALID_STATE



async def test_user_cleanup_fails_if_pack_cancellation_is_not_persisted(
    database: SQLiteDatabase,
) -> None:
    owner = await create_user(database)
    job_id = await pack_id(database, owner)
    async with SQLiteUnitOfWork(database) as uow:
        packs = PackRepository(uow.connection)
        await uow.connection.execute(
            text(
                "CREATE TRIGGER test_ignore_pack_cancellation "
                "BEFORE UPDATE OF status ON pack_jobs "
                "WHEN NEW.status = 'cancelled' BEGIN SELECT RAISE(IGNORE); END"
            )
        )
        with pytest.raises(DomainError) as caught:
            await packs.cleanup_user(user_id=owner, now_ms=2)
        assert caught.value.code is ErrorCode.INVALID_STATE
        assert (await packs.get_job(job_id)).status is PackStatus.PENDING
