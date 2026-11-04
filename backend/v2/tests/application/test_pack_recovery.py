from __future__ import annotations

import zipfile
from pathlib import Path

from sqlalchemy import text

from aria2deck.domain.content import ContentDeletionState
from aria2deck.domain.packs import PackStatus, PackStep, PreparedOutput
from aria2deck.domain.quota import Quota
from aria2deck.infrastructure.archive_writer import ArchiveWriter
from aria2deck.infrastructure.content_store import DownloadContentStore
from aria2deck.infrastructure.sqlite.engine import SQLiteDatabase
from aria2deck.infrastructure.sqlite.repositories.content import ContentRepository
from aria2deck.infrastructure.sqlite.repositories.packs import PackRepository
from aria2deck.infrastructure.sqlite.repositories.quota import QuotaRepository
from aria2deck.infrastructure.sqlite.repositories.shares import ShareRepository
from aria2deck.infrastructure.sqlite.unit_of_work import SQLiteUnitOfWork

from .test_library_services import FakeClock
from .test_pack_admission import create_pack_user, seed_pack_file, setup_pack_world
from .test_pack_cleanup import make_cleanup_worker, make_control
from .test_pack_dispatcher import RETRY_DELAYS, create_pack, get_job, make_dispatcher


async def _prepare_real_output(
    database: SQLiteDatabase,
    clock: FakeClock,
    store: DownloadContentStore,
    user_id: int,
    pack_id: int,
    file_id: int,
) -> PreparedOutput:
    """Manually drive the job to the prepared state with a real archive on disk."""
    from aria2deck.application.packs.models import ArchiveSource
    from aria2deck.domain.content import ContentKind

    async with SQLiteUnitOfWork(database) as uow:
        repository = PackRepository(uow.connection)
        await repository.claim_next_pending(now_ms=clock())
        await repository.update_step(pack_id, from_step=PackStep.VALIDATING, to_step=PackStep.COMPRESSING, now_ms=clock())
        await repository.update_step(pack_id, from_step=PackStep.COMPRESSING, to_step=PackStep.VERIFYING, now_ms=clock())
        current = await repository.list_admission_sources(user_id=user_id, user_file_ids=[file_id])
    part = f"packs/prepared-{pack_id}.part"
    await ArchiveWriter().write_archive(
        store.physical_path(part),
        sources=[
            ArchiveSource(
                display_name=current[0].display_name,
                root=store.physical_path(current[0].storage_path),
                kind=ContentKind.FILE,
            )
        ],
        pack_format="zip",
        compression_level=3,
    )
    size, digest = await store.verify_prepared(part)
    async with SQLiteUnitOfWork(database) as uow:
        await PackRepository(uow.connection).set_prepared(
            pack_id, PreparedOutput(part, size, digest), now_ms=clock()
        )
    return PreparedOutput(part, size, digest)


async def _assert_converged(
    database: SQLiteDatabase,
    root: Path,
    user_id: int,
    pack_id: int,
    *,
    sources_expected: int,
    share_id: int | None = None,
) -> None:
    """Unified convergence contract: terminal job, terminal sources, complete reference
    removal, consistent quota, on-disk verifiable output, and no part remnants."""
    job = await get_job(database, pack_id)
    assert job.status is PackStatus.DONE
    assert job.step is None and job.step_progress == 100
    assert job.output_content_id is not None

    async with SQLiteUnitOfWork(database) as uow:
        repository = PackRepository(uow.connection)
        sources = await repository.list_sources(pack_id)
        quota = await QuotaRepository(uow.connection).get(user_id)
        rebuilt = await QuotaRepository(uow.connection).rebuild(user_id)
        content = ContentRepository(uow.connection)
        output_file = await content.find_user_file(
            user_id=user_id, content_id=job.output_content_id
        )
        output_object = await content.get_object(job.output_content_id)
        if share_id is not None:
            share = await ShareRepository(uow.connection).get_share(share_id)
        else:
            share = None
        pending_sources = (
            await uow.connection.execute(
                text(
                    "SELECT COUNT(*) FROM pack_sources WHERE pack_id = :id "
                    "AND cleanup_status = 'pending'"
                ),
                {"id": pack_id},
            )
        ).scalar_one()

    assert len(sources) == sources_expected
    assert pending_sources == 0, "来源必须全部终态"
    assert job.cleanup_pending is False
    assert quota == rebuilt, "配额口径必须与权威 rebuild 一致"
    assert quota.reserved_bytes == 0
    assert output_file is not None, "输出引用必须建立"
    assert (root / output_object.storage_path).is_file(), "输出文件必须在盘"
    assert zipfile.is_zipfile(root / output_object.storage_path), "输出必须可被标准库校验"
    if share is not None:
        assert share.revoked_at_ms is not None, "来源分享必须撤销"
    assert not (root / "packs" / f"prepared-{pack_id}.part").exists(), "不得残留半成品 part"


async def test_recovery_from_validating_interruption(tmp_path: Path) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    _, first = await seed_pack_file(database, root, user_id, 1)
    store = DownloadContentStore(root)
    pack_id = await create_pack(database, clock, store, user_id, [first], delete_source=True)
    async with SQLiteUnitOfWork(database) as uow:
        await ShareRepository(uow.connection).create_share(
            user_id=user_id, user_file_id=first, code="r1", password_hash=None,
            expires_at_ms=None, max_downloads=None, now_ms=10,
        )
    async with SQLiteUnitOfWork(database) as uow:
        await PackRepository(uow.connection).claim_next_pending(now_ms=clock())  # 崩溃于 validating。
    job = await get_job(database, pack_id)
    assert job.status is PackStatus.PACKING and job.step is PackStep.VALIDATING

    dispatcher = make_dispatcher(database, clock, store)  # 新实例模拟重启。
    await dispatcher.step_once()
    assert (await get_job(database, pack_id)).status is PackStatus.PENDING  # 重排退避。
    clock.advance(RETRY_DELAYS[0])
    await dispatcher.step_once()
    await make_cleanup_worker(database, clock).step_once()
    await _assert_converged(database, root, user_id, pack_id, sources_expected=1, share_id=1)


async def test_recovery_from_partial_part_file(tmp_path: Path) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    _, first = await seed_pack_file(database, root, user_id, 1)
    store = DownloadContentStore(root)
    pack_id = await create_pack(database, clock, store, user_id, [first], delete_source=True)
    async with SQLiteUnitOfWork(database) as uow:
        repository = PackRepository(uow.connection)
        await repository.claim_next_pending(now_ms=clock())
        await repository.update_step(pack_id, from_step=PackStep.VALIDATING, to_step=PackStep.COMPRESSING, now_ms=clock())
    part = root / "packs" / f"prepared-{pack_id}.part"
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(b"half-written junk")

    dispatcher = make_dispatcher(database, clock, store)
    await dispatcher.step_once()
    assert not part.exists(), "半成品 part 文件必须清理"
    assert (await get_job(database, pack_id)).status is PackStatus.PENDING
    clock.advance(RETRY_DELAYS[0])
    await dispatcher.step_once()
    await make_cleanup_worker(database, clock).step_once()
    await _assert_converged(database, root, user_id, pack_id, sources_expected=1)


async def test_recovery_from_prepared_interruption_installs_without_recompress(
    tmp_path: Path,
) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    _, first = await seed_pack_file(database, root, user_id, 1)
    store = DownloadContentStore(root)
    pack_id = await create_pack(database, clock, store, user_id, [first], delete_source=True)
    prepared = await _prepare_real_output(database, clock, store, user_id, pack_id, first)
    original_size = (root / "packs" / f"prepared-{pack_id}.part").stat().st_size
    assert (root / "packs" / f"prepared-{pack_id}.part").exists()

    dispatcher = make_dispatcher(database, clock, store)
    await dispatcher.step_once()  # 重启：重验直接安装，不重压缩。
    job = await get_job(database, pack_id)
    assert job.prepared == prepared
    canonical = root / "objects" / f"file-{prepared.digest}"
    assert canonical.stat().st_size == original_size
    await make_cleanup_worker(database, clock).step_once()
    await _assert_converged(database, root, user_id, pack_id, sources_expected=1)


async def test_recovery_after_install_move_before_db(tmp_path: Path) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    _, first = await seed_pack_file(database, root, user_id, 1)
    store = DownloadContentStore(root)
    pack_id = await create_pack(database, clock, store, user_id, [first], delete_source=True)
    prepared = await _prepare_real_output(database, clock, store, user_id, pack_id, first)
    # 崩溃于「移动到规范位之后、DB 完成之前」。
    await store.install_prepared(prepared.path, prepared.digest)

    dispatcher = make_dispatcher(database, clock, store)
    await dispatcher.step_once()
    await make_cleanup_worker(database, clock).step_once()
    await _assert_converged(database, root, user_id, pack_id, sources_expected=1)
    async with SQLiteUnitOfWork(database) as uow:
        count = (
            await uow.connection.execute(text("SELECT COUNT(*) FROM content_objects"))
        ).scalar_one()
    assert count == 2  # 源 + 输出：不重复建对象。


async def test_recovery_with_corrupted_prepared_requeues(tmp_path: Path) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    _, first = await seed_pack_file(database, root, user_id, 1)
    store = DownloadContentStore(root)
    pack_id = await create_pack(database, clock, store, user_id, [first], delete_source=True)
    await _prepare_real_output(database, clock, store, user_id, pack_id, first)
    (root / "packs" / f"prepared-{pack_id}.part").write_bytes(b"tampered")  # 外部篡改。

    dispatcher = make_dispatcher(database, clock, store)
    await dispatcher.step_once()
    job = await get_job(database, pack_id)
    assert job.status is PackStatus.PENDING, "内容不匹配：暂态重排而非失败"
    assert job.retry_count == 1
    clock.advance(RETRY_DELAYS[0])
    await dispatcher.step_once()
    await make_cleanup_worker(database, clock).step_once()
    await _assert_converged(database, root, user_id, pack_id, sources_expected=1)


async def test_source_cleanup_interruption_resumes_only_pending(tmp_path: Path) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    _, first = await seed_pack_file(database, root, user_id, 1)
    _, second = await seed_pack_file(database, root, user_id, 2, payload=b"b" * 6)
    store = DownloadContentStore(root)
    pack_id = await create_pack(
        database, clock, store, user_id, [first, second],
        output_name="resume.zip", delete_source=True,
    )
    dispatcher = make_dispatcher(database, clock, store)
    await dispatcher.step_once()
    assert (await get_job(database, pack_id)).status is PackStatus.DONE

    # 中断模拟：先手工完成第一个来源的删除，第二个保持 pending。
    worker = make_cleanup_worker(database, clock)
    async with SQLiteUnitOfWork(database) as uow:
        repository = PackRepository(uow.connection)
        sources = await repository.list_sources(pack_id)
        first_source = next(s for s in sources if s.snapshot.user_file_id == first)
        await repository.set_source_cleanup(first_source.id, status="deleted")
        # 单事务语义：标记与引用删除需一致，这里补齐引用删除。
        content = ContentRepository(uow.connection)
        await content.revoke_shares_for_user_file(user_file_id=first, now_ms=clock())
        await content.remove_user_file(user_id=user_id, file_id=first)
        await QuotaRepository(uow.connection).rebuild(user_id)
    assert (await get_job(database, pack_id)).cleanup_pending is True

    outcomes = await worker.step_once()  # 重跑：只处理剩余 pending 来源。
    assert len(outcomes) == 1
    await _assert_converged(database, root, user_id, pack_id, sources_expected=2)


async def test_source_cleanup_error_keeps_pending_and_continues(tmp_path: Path) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    _, blocked = await seed_pack_file(database, root, user_id, 1)
    _, normal = await seed_pack_file(database, root, user_id, 2, payload=b"b" * 6)
    store = DownloadContentStore(root)
    pack_id = await create_pack(
        database, clock, store, user_id, [blocked, normal],
        output_name="mixed.zip", delete_source=True,
    )
    await make_dispatcher(database, clock, store).step_once()
    # 另一个打包任务仍占用 blocked 来源（删除抛 CONTENT_BUSY）。
    blocker_pack = await create_pack(
        database, clock, store, user_id, [blocked], output_name="blocker.zip", delete_source=False
    )

    outcomes = await make_cleanup_worker(database, clock).step_once()
    assert len(outcomes) == 2
    busy = [o for o in outcomes if o.cleanup_status == "pending"]
    done = [o for o in outcomes if o.cleanup_status == "deleted"]
    assert len(busy) == 1 and busy[0].error_code == "content_busy"
    assert len(done) == 1, "单项失败不阻断其余来源"
    job = await get_job(database, pack_id)
    assert job.cleanup_pending is True

    # 解除占用（标记 kept + 取消占用任务）后重跑收敛。
    async with SQLiteUnitOfWork(database) as uow:
        repository = PackRepository(uow.connection)
        for source in await repository.list_sources(blocker_pack):
            await repository.set_source_cleanup(source.id, status="kept")
    await make_control(database, clock, store).cancel(user_id, blocker_pack)
    await make_cleanup_worker(database, clock).step_once()
    await _assert_converged(database, root, user_id, pack_id, sources_expected=2)


async def test_cleanup_deletion_removes_references_and_claims_orphan(tmp_path: Path) -> None:
    """删除走完整引用删除语义：user_files 行删、分享撤销、配额重建、最后引用认领。"""
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    content_id, first = await seed_pack_file(database, root, user_id, 1)
    store = DownloadContentStore(root)
    pack_id = await create_pack(
        database, clock, store, user_id, [first], output_name="refs.zip", delete_source=True
    )
    async with SQLiteUnitOfWork(database) as uow:
        await ShareRepository(uow.connection).create_share(
            user_id=user_id, user_file_id=first, code="bye", password_hash=None,
            expires_at_ms=None, max_downloads=None, now_ms=10,
        )
    await make_dispatcher(database, clock, store).step_once()
    await make_cleanup_worker(database, clock).step_once()

    await _assert_converged(database, root, user_id, pack_id, sources_expected=1, share_id=1)
    async with SQLiteUnitOfWork(database) as uow:
        content = ContentRepository(uow.connection)
        references = await content.count_references(content_id)
        state = (await content.get_object(content_id)).deletion_state
    assert references == 0
    assert state is ContentDeletionState.PENDING_DELETE, "最后引用认领物理删除"


async def test_multi_source_quota_matches_rebuild_after_cleanup(tmp_path: Path) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    _, first = await seed_pack_file(database, root, user_id, 1, payload=b"a" * 10)
    _, second = await seed_pack_file(database, root, user_id, 2, payload=b"b" * 20)
    store = DownloadContentStore(root)
    pack_id = await create_pack(
        database, clock, store, user_id, [first, second],
        output_name="quota.zip", delete_source=True,
    )
    await make_dispatcher(database, clock, store).step_once()
    await make_cleanup_worker(database, clock).step_once()

    await _assert_converged(database, root, user_id, pack_id, sources_expected=2)
    async with SQLiteUnitOfWork(database) as uow:
        quota = await QuotaRepository(uow.connection).get(user_id)
        job = await PackRepository(uow.connection).get_job(pack_id)
        output = await ContentRepository(uow.connection).get_object(job.output_content_id)
    # 仅剩输出归档计入 used；预留清零。
    assert quota == Quota(100 * 1024 * 1024, output.size, 0)
