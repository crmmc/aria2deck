from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from aria2deck.domain.errors import DomainError, ErrorCode
from aria2deck.domain.packs import PackSourceSnapshot, PackStatus, PackStep, PreparedOutput
from aria2deck.domain.quota import PhysicalPack, Quota
from aria2deck.infrastructure.sqlite.engine import SQLiteDatabase
from aria2deck.infrastructure.sqlite.repositories.accounts import AccountRepository
from aria2deck.infrastructure.sqlite.repositories.content import ContentRepository
from aria2deck.infrastructure.sqlite.repositories.packs import PackRepository
from aria2deck.infrastructure.sqlite.repositories.quota import QuotaRepository
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


async def _seed_user(database, username: str = "owner") -> int:
    async with SQLiteUnitOfWork(database) as uow:
        return await AccountRepository(uow.connection).create_user(
            username=username, password_hash="ab" * 32, is_admin=True, quota_bytes=10_000, now_ms=1
        )


async def _seed_file(database, user_id: int, index: int, *, size: int = 10) -> tuple[int, int]:
    async with SQLiteUnitOfWork(database) as uow:
        repo = ContentRepository(uow.connection)
        content_id = await repo.create_object(
            kind="file", digest=f"{index:064x}", size=size,
            storage_path=f"objects/file-{index}", now_ms=1,
        )
        file_id = await repo.add_user_file(
            user_id=user_id, content_id=content_id, display_name=f"src-{index}", now_ms=5
        )
        await QuotaRepository(uow.connection).rebuild(user_id)
    return content_id, file_id


async def test_admission_sources_resolve_ownership_and_snapshot_fields(database) -> None:
    owner = await _seed_user(database)
    other = await _seed_user(database, "other")
    _, own_file = await _seed_file(database, owner, 1)
    _, foreign_file = await _seed_file(database, other, 2)
    async with SQLiteUnitOfWork(database) as uow:
        repo = PackRepository(uow.connection)
        rows = repo.list_admission_sources(user_id=owner, user_file_ids=[own_file, foreign_file, 999])
        rows = await rows
        empty = await repo.list_admission_sources(user_id=owner, user_file_ids=[])
    assert [row.user_file_id for row in rows] == [own_file]
    row = rows[0]
    assert (row.content_key, row.display_name, row.created_at_ms, row.size) == ("file:" + f"{1:064x}", "src-1", 5, 10)
    assert row.snapshot() == PackSourceSnapshot(own_file, row.content_id, row.content_key, "src-1", 5, 10)
    assert empty == []


async def test_claim_next_pending_is_globally_single_and_due_gated(database) -> None:
    owner = await _seed_user(database)
    async with SQLiteUnitOfWork(database) as uow:
        repo = PackRepository(uow.connection)
        first = await repo.create_job(owner_id=owner, output_name="a.zip", pack_format="zip", source_size=1, reserved_bytes=1, physical_reserved_bytes=1, delete_source=False, now_ms=1)
        second = await repo.create_job(owner_id=owner, output_name="b.zip", pack_format="zip", source_size=1, reserved_bytes=1, physical_reserved_bytes=1, delete_source=False, now_ms=1)
        assert await repo.claim_next_pending(now_ms=10) == first
        job = await repo.get_job(first)
        assert (job.status, job.step, job.step_progress, job.step_started_at_ms) == (
            PackStatus.PACKING, PackStep.VALIDATING, 0, 10
        )
        assert await repo.claim_next_pending(now_ms=11) is None  # 已有 packing：不再领取。
        assert await repo.requeue_transient(first, delay_ms=100, now_ms=20)
        assert await repo.claim_next_pending(now_ms=50) == second  # first 未到期，second 可领。
        assert await repo.claim_next_pending(now_ms=51) is None  # second 正在 packing。
        assert await repo.fail_job(second, error_code="boom", now_ms=60)
        failed = await repo.get_job(second)
        assert (failed.status, failed.error_code, failed.reserved_bytes, failed.finished_at_ms) == (
            PackStatus.FAILED, "boom", 0, 60
        )
        assert await repo.claim_next_pending(now_ms=110) is None  # first 仍未到期。
        assert await repo.claim_next_pending(now_ms=120) == first


async def test_concurrent_claims_allow_exactly_one_winner(database) -> None:
    owner = await _seed_user(database)

    async def claim() -> int | None:
        async with SQLiteUnitOfWork(database) as uow:
            return await PackRepository(uow.connection).claim_next_pending(now_ms=1)

    async with SQLiteUnitOfWork(database) as uow:
        await PackRepository(uow.connection).create_job(
            owner_id=owner, output_name="a.zip", pack_format="zip", source_size=1,
            reserved_bytes=1, physical_reserved_bytes=1, delete_source=False, now_ms=1,
        )
    results = await asyncio.gather(claim(), claim())
    assert sorted(result is not None for result in results) == [False, True]


async def test_step_updates_are_conditional_and_progress_validated(database) -> None:
    owner = await _seed_user(database)
    async with SQLiteUnitOfWork(database) as uow:
        repo = PackRepository(uow.connection)
        pack_id = await repo.create_job(owner_id=owner, output_name="a.zip", pack_format="zip", source_size=1, reserved_bytes=1, physical_reserved_bytes=1, delete_source=False, now_ms=1)
        await repo.claim_next_pending(now_ms=10)
        assert not await repo.update_step(pack_id, from_step=PackStep.COMPRESSING, to_step=PackStep.VERIFYING, now_ms=11)
        assert await repo.update_step(pack_id, from_step=PackStep.VALIDATING, to_step=PackStep.COMPRESSING, now_ms=12)
        assert await repo.update_step_progress(pack_id, step=PackStep.COMPRESSING, progress=40, now_ms=13)
        job = await repo.get_job(pack_id)
        assert (job.step, job.step_progress, job.step_started_at_ms) == (PackStep.COMPRESSING, 40, 12)
        assert not await repo.update_step_progress(pack_id, step=PackStep.VALIDATING, progress=10, now_ms=14)
        with pytest.raises(DomainError):
            await repo.update_step_progress(pack_id, step=PackStep.COMPRESSING, progress=101, now_ms=15)
        assert await repo.update_step(pack_id, from_step=PackStep.COMPRESSING, to_step=PackStep.VERIFYING, now_ms=16)
        with pytest.raises(DomainError):
            await repo.set_prepared(pack_id, PreparedOutput("packs/prepared-1.part", 4, "bad"), now_ms=17)
        await repo.set_prepared(pack_id, PreparedOutput("packs/prepared-1.part", 4, f"{5:064x}"), now_ms=17)
        await repo.set_prepared(pack_id, PreparedOutput("packs/prepared-1.part", 4, f"{5:064x}"), now_ms=18)  # 幂等
        with pytest.raises(DomainError):
            await repo.set_prepared(pack_id, PreparedOutput("packs/prepared-1.part", 5, f"{5:064x}"), now_ms=19)


async def test_complete_job_converts_reservation_and_installs_output(database) -> None:
    owner = await _seed_user(database)
    _, source_file = await _seed_file(database, owner, 1, size=10)
    digest = f"{7:064x}"
    async with SQLiteUnitOfWork(database) as uow:
        repo = PackRepository(uow.connection)
        pack_id = await repo.create_job(owner_id=owner, output_name="out.zip", pack_format="zip", source_size=10, reserved_bytes=10, physical_reserved_bytes=10, delete_source=False, now_ms=1)
        await QuotaRepository(uow.connection).reserve(owner, 10)
        await repo.add_source(pack_id, PackSourceSnapshot(source_file, 1, "file:x", "src-1", 5, 10))
        await repo.claim_next_pending(now_ms=2)
        await repo.update_step(pack_id, from_step=PackStep.VALIDATING, to_step=PackStep.COMPRESSING, now_ms=3)
        await repo.update_step(pack_id, from_step=PackStep.COMPRESSING, to_step=PackStep.VERIFYING, now_ms=4)
        await repo.set_prepared(pack_id, PreparedOutput("packs/prepared-1.part", 10, digest), now_ms=5)

        content_id = await repo.complete_job(pack_id, now_ms=6)
        job = await repo.get_job(pack_id)
        quota = await QuotaRepository(uow.connection).get(owner)
        content = ContentRepository(uow.connection)
        output_file = await content.find_user_file(user_id=owner, content_id=content_id)
        record = await content.get_object(content_id)
        rebuilt = await QuotaRepository(uow.connection).rebuild(owner)
    assert job.status is PackStatus.DONE
    assert (job.reserved_bytes, job.physical_reserved_bytes, job.finished_at_ms) == (0, 0, 6)
    assert job.output_content_id == content_id
    assert job.output_content_key == f"file:{digest}"
    assert quota == Quota(10_000, 20, 0)  # 来源 10 + 输出 10，预留已转 used。
    assert output_file is not None
    assert (record.storage_path, record.size) == (f"objects/file-{digest}", 10)
    assert rebuilt == quota  # 与权威 rebuild 口径一致。

    # 已完成或无 prepared 的任务不能再次完成。
    async with SQLiteUnitOfWork(database) as uow:
        with pytest.raises(DomainError):
            await PackRepository(uow.connection).complete_job(pack_id, now_ms=7)


async def test_complete_job_reuses_existing_output_identity(database) -> None:
    owner = await _seed_user(database)
    digest = f"{9:064x}"
    async with SQLiteUnitOfWork(database) as uow:
        content = ContentRepository(uow.connection)
        existing_id = await content.create_object(
            kind="file", digest=digest, size=8, storage_path=f"objects/file-{digest}", now_ms=1
        )
        repo = PackRepository(uow.connection)
        pack_id = await repo.create_job(owner_id=owner, output_name="out.zip", pack_format="zip", source_size=8, reserved_bytes=8, physical_reserved_bytes=8, delete_source=False, now_ms=1)
        await QuotaRepository(uow.connection).reserve(owner, 8)
        await repo.claim_next_pending(now_ms=2)
        await repo.update_step(pack_id, from_step=PackStep.VALIDATING, to_step=PackStep.COMPRESSING, now_ms=3)
        await repo.update_step(pack_id, from_step=PackStep.COMPRESSING, to_step=PackStep.VERIFYING, now_ms=4)
        await repo.set_prepared(pack_id, PreparedOutput("packs/prepared-1.part", 8, digest), now_ms=5)
        content_id = await repo.complete_job(pack_id, now_ms=6)
        quota = await QuotaRepository(uow.connection).get(owner)
    assert content_id == existing_id  # 秒传：复用既有内容对象。
    assert quota == Quota(10_000, 8, 0)  # 仅输出引用（无既有文件）。


async def test_mark_cancelled_clears_reservation_and_prepared(database) -> None:
    owner = await _seed_user(database)
    digest = f"{3:064x}"
    async with SQLiteUnitOfWork(database) as uow:
        repo = PackRepository(uow.connection)
        pack_id = await repo.create_job(owner_id=owner, output_name="a.zip", pack_format="zip", source_size=4, reserved_bytes=4, physical_reserved_bytes=4, delete_source=False, now_ms=1)
        await repo.claim_next_pending(now_ms=2)
        await repo.update_step(pack_id, from_step=PackStep.VALIDATING, to_step=PackStep.COMPRESSING, now_ms=3)
        await repo.update_step(pack_id, from_step=PackStep.COMPRESSING, to_step=PackStep.VERIFYING, now_ms=4)
        await repo.set_prepared(pack_id, PreparedOutput("packs/prepared-1.part", 4, digest), now_ms=5)
        assert await repo.mark_cancelled(pack_id, now_ms=6)
        job = await repo.get_job(pack_id)
        assert not await repo.mark_cancelled(pack_id, now_ms=7)
    assert (job.status, job.prepared, job.reserved_bytes, job.finished_at_ms) == (
        PackStatus.CANCELLED, None, 0, 6
    )


async def test_source_cleanup_state_machine_converges(database) -> None:
    owner = await _seed_user(database)
    _, first = await _seed_file(database, owner, 1)
    _, second = await _seed_file(database, owner, 2)
    digest = f"{4:064x}"
    async with SQLiteUnitOfWork(database) as uow:
        repo = PackRepository(uow.connection)
        pack_id = await repo.create_job(owner_id=owner, output_name="a.zip", pack_format="zip", source_size=20, reserved_bytes=0, physical_reserved_bytes=0, delete_source=True, now_ms=1)
        await repo.add_source(pack_id, PackSourceSnapshot(first, 1, "file:a", "src-1", 5, 10))
        await repo.add_source(pack_id, PackSourceSnapshot(second, 2, "file:b", "src-2", 5, 10))
        await repo.claim_next_pending(now_ms=2)
        await repo.update_step(pack_id, from_step=PackStep.VALIDATING, to_step=PackStep.COMPRESSING, now_ms=3)
        await repo.update_step(pack_id, from_step=PackStep.COMPRESSING, to_step=PackStep.VERIFYING, now_ms=4)
        await repo.set_prepared(pack_id, PreparedOutput("packs/prepared-1.part", 20, digest), now_ms=5)
        await repo.complete_job(pack_id, now_ms=6)
        assert (await repo.get_job(pack_id)).cleanup_pending is True
        pending_jobs = await repo.list_cleanup_pending_jobs()
        assert pending_jobs[0].id == pack_id
        assert await repo.count_pending_sources(pack_id) == 2
        with pytest.raises(DomainError):
            await repo.finish_source_cleanup(pack_id, now_ms=7)

        sources = await repo.list_sources(pack_id)
        assert [source.cleanup_status for source in sources] == ["pending", "pending"]
        with pytest.raises(DomainError) as caught:  # 状态机只接受终态写入。
            await repo.set_source_cleanup(sources[0].id, status="pending")
        assert caught.value.code is ErrorCode.INVALID_INPUT
        assert await repo.set_source_cleanup(sources[0].id, status="deleted")
        assert await repo.set_source_cleanup(sources[1].id, status="kept", error_code="renamed")
        with pytest.raises(DomainError):
            await repo.set_source_cleanup(sources[1].id, status="failed")
        assert await repo.finish_source_cleanup(pack_id, now_ms=8)
        assert not await repo.finish_source_cleanup(pack_id, now_ms=9)
        assert await repo.delete_job(pack_id)
        assert not await repo.delete_job(pack_id)
        assert await repo.list_sources(pack_id) == []
    assert sources[1].snapshot.user_file_id == second


async def test_list_active_physical_reports_reservations(database) -> None:
    owner = await _seed_user(database)
    digest = f"{6:064x}"
    async with SQLiteUnitOfWork(database) as uow:
        repo = PackRepository(uow.connection)
        pack_id = await repo.create_job(owner_id=owner, output_name="a.zip", pack_format="zip", source_size=4, reserved_bytes=4, physical_reserved_bytes=4, delete_source=False, now_ms=1)
        assert [item.reserved_bytes for item in await repo.list_active_physical()] == [4]
        await repo.claim_next_pending(now_ms=2)
        await repo.update_step(pack_id, from_step=PackStep.VALIDATING, to_step=PackStep.COMPRESSING, now_ms=3)
        await repo.update_step(pack_id, from_step=PackStep.COMPRESSING, to_step=PackStep.VERIFYING, now_ms=4)
        await repo.set_prepared(pack_id, PreparedOutput("packs/prepared-1.part", 4, digest), now_ms=5)
        active = await repo.list_active_physical()
        assert active == [PhysicalPack(PackStatus.PACKING, 4, 4)]
        await repo.fail_job(pack_id, error_code="x", now_ms=6)
        assert await repo.list_active_physical() == []


async def test_requeue_rejects_negative_delay_and_output_identity_conflict_fails(
    database,
) -> None:
    owner = await _seed_user(database)
    digest = f"{8:064x}"
    async with SQLiteUnitOfWork(database) as uow:
        repo = PackRepository(uow.connection)
        pack_id = await repo.create_job(owner_id=owner, output_name="a.zip", pack_format="zip", source_size=4, reserved_bytes=4, physical_reserved_bytes=4, delete_source=False, now_ms=1)
        await repo.claim_next_pending(now_ms=2)
        with pytest.raises(DomainError) as caught:
            await repo.requeue_transient(pack_id, delay_ms=-1, now_ms=3)
        assert caught.value.code is ErrorCode.INVALID_INPUT

        # 同摘要但大小不一致的既有对象：输出身份冲突必须确定性失败。
        content = ContentRepository(uow.connection)
        await content.create_object(
            kind="file", digest=digest, size=99, storage_path=f"objects/file-{digest}", now_ms=1
        )
        await repo.update_step(pack_id, from_step=PackStep.VALIDATING, to_step=PackStep.COMPRESSING, now_ms=4)
        await repo.update_step(pack_id, from_step=PackStep.COMPRESSING, to_step=PackStep.VERIFYING, now_ms=5)
        await repo.set_prepared(pack_id, PreparedOutput("packs/prepared-1.part", 4, digest), now_ms=6)
        with pytest.raises(DomainError) as caught:
            await repo.complete_job(pack_id, now_ms=7)
        assert caught.value.code is ErrorCode.INVALID_STATE
        assert (await repo.get_job(pack_id)).status is PackStatus.PACKING
