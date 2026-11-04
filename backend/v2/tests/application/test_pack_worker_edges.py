from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from aria2deck.application.packs.control import PackControlService
from aria2deck.application.packs.progress import PackProgressService
from aria2deck.domain.errors import DomainError, ErrorCode
from aria2deck.domain.packs import PackStatus
from aria2deck.infrastructure.content_store import DownloadContentStore
from aria2deck.infrastructure.pack_writer import PackWriterControl
from aria2deck.infrastructure.sqlite.repositories.content import ContentRepository
from aria2deck.infrastructure.sqlite.repositories.packs import PackRepository
from aria2deck.infrastructure.sqlite.repositories.quota import QuotaRepository
from aria2deck.infrastructure.sqlite.unit_of_work import SQLiteUnitOfWork

from .test_pack_admission import create_pack_user, seed_pack_file, setup_pack_world
from .test_pack_cleanup import make_cleanup_worker, make_control
from .test_pack_dispatcher import create_pack, get_job, make_dispatcher
from .test_pack_recovery import _prepare_real_output


async def test_worker_loops_until_cancelled(tmp_path: Path) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    store = DownloadContentStore(root)
    dispatcher = make_dispatcher(database, clock, store)
    cleanup = make_cleanup_worker(database, clock)
    steps = 0

    async def cancel_after_first(seconds: float) -> None:
        raise asyncio.CancelledError

    original_step = dispatcher.step_once

    async def counting_step() -> None:
        nonlocal steps
        steps += 1
        await original_step()

    dispatcher.step_once = counting_step  # type: ignore[method-assign]
    dispatcher.sleep = cancel_after_first  # type: ignore[assignment]
    cleanup.sleep = cancel_after_first  # type: ignore[assignment]
    with pytest.raises(asyncio.CancelledError):
        await dispatcher.run_worker()
    with pytest.raises(asyncio.CancelledError):
        await cleanup.run_worker()
    assert steps == 1


async def test_execute_ignores_non_packing_job(tmp_path: Path) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    _, first = await seed_pack_file(database, root, user_id, 1)
    store = DownloadContentStore(root)
    pack_id = await create_pack(database, clock, store, user_id, [first])
    service = make_control(database, clock, store)
    await service.cancel(user_id, pack_id)
    dispatcher = make_dispatcher(database, clock, store)
    await dispatcher._execute(pack_id)  # 已取消：静默跳过。
    assert (await get_job(database, pack_id)).status is PackStatus.CANCELLED


async def test_resume_requeues_when_canonical_mismatches(tmp_path: Path) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    _, first = await seed_pack_file(database, root, user_id, 1)
    store = DownloadContentStore(root)
    pack_id = await create_pack(database, clock, store, user_id, [first])
    prepared = await _prepare_real_output(database, clock, store, user_id, pack_id, first)
    await store.install_prepared(prepared.path, prepared.digest)
    # 规范位内容被篡改：与登记的 prepared 不一致 → 暂态重排。
    (root / "objects" / f"file-{prepared.digest}").write_bytes(b"tampered canonical")
    dispatcher = make_dispatcher(database, clock, store)
    await dispatcher.step_once()
    job = await get_job(database, pack_id)
    assert job.status is PackStatus.PENDING
    assert job.retry_count == 1
    clock.advance(250)
    await dispatcher.step_once()
    assert (await get_job(database, pack_id)).status is PackStatus.DONE


async def test_install_rejects_missing_or_tampered_prepared(tmp_path: Path) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    _, first = await seed_pack_file(database, root, user_id, 1)
    store = DownloadContentStore(root)
    pack_id = await create_pack(database, clock, store, user_id, [first])
    dispatcher = make_dispatcher(database, clock, store)
    # 直接驱动安装步骤：防御性校验必须拒绝缺失/篡改的 prepared（执行流中由失败分流兜底）。
    with pytest.raises(DomainError) as caught:
        await dispatcher._install(pack_id)
    assert caught.value.code is ErrorCode.INVALID_STATE
    await make_control(database, clock, store).cancel(user_id, pack_id)  # 释放单活跃槽位。

    other = await create_pack(database, clock, store, user_id, [first], output_name="two.zip")
    await _prepare_real_output(database, clock, store, user_id, other, first)
    (root / "packs" / f"prepared-{other}.part").write_bytes(b"tampered")
    with pytest.raises(DomainError) as caught:
        await dispatcher._install(other)
    assert caught.value.code is ErrorCode.INVALID_STATE


async def test_handle_failure_skips_terminal_job(tmp_path: Path) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    _, first = await seed_pack_file(database, root, user_id, 1)
    store = DownloadContentStore(root)
    pack_id = await create_pack(database, clock, store, user_id, [first])
    await make_control(database, clock, store).cancel(user_id, pack_id)
    dispatcher = make_dispatcher(database, clock, store)
    await dispatcher._handle_failure(pack_id, OSError("late failure"))
    assert (await get_job(database, pack_id)).status is PackStatus.CANCELLED


async def test_cancel_race_terminal_between_stop_and_write(tmp_path: Path) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    _, first = await seed_pack_file(database, root, user_id, 1)
    store = DownloadContentStore(root)
    pack_id = await create_pack(database, clock, store, user_id, [first])

    async def racing_stop(pack_id_arg: int) -> None:
        # 模拟竞态：写入者停止期间任务被其他路径置为终态。
        async with SQLiteUnitOfWork(database) as uow:
            await PackRepository(uow.connection).mark_cancelled(pack_id_arg, now_ms=clock())

    service = PackControlService(
        uow_factory=lambda: SQLiteUnitOfWork(database),
        repository_factory=lambda uow: PackRepository(uow.connection),
        content_repository_factory=lambda uow: ContentRepository(uow.connection),
        quota_repository_factory=lambda uow: QuotaRepository(uow.connection),
        stop_writer=racing_stop,
        prepared_store=store,
        clock_ms=clock,
        lease_owner="content-deletion",
        lease_ttl_ms=300_000,
    )
    with pytest.raises(DomainError) as caught:
        await service.cancel(user_id, pack_id)
    assert caught.value.code is ErrorCode.INVALID_STATE


async def test_delete_pack_without_owner_reference_skips_removal(tmp_path: Path) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    _, first = await seed_pack_file(database, root, user_id, 1)
    store = DownloadContentStore(root)
    pack_id = await create_pack(database, clock, store, user_id, [first], output_name="skip.zip")
    dispatcher = make_dispatcher(database, clock, store)
    await dispatcher.step_once()
    job = await get_job(database, pack_id)
    # 输出引用已被独立删除（如用户先行删除输出文件）。
    async with SQLiteUnitOfWork(database) as uow:
        await ContentRepository(uow.connection).remove_user_file(
            user_id=user_id,
            file_id=await ContentRepository(uow.connection).find_user_file(
                user_id=user_id, content_id=job.output_content_id
            ),
        )
        await QuotaRepository(uow.connection).rebuild(user_id)
    await make_control(database, clock, store).delete_pack(user_id, pack_id)
    with pytest.raises(DomainError):
        await get_job(database, pack_id)


async def test_progress_view_edges(tmp_path: Path) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    _, first = await seed_pack_file(database, root, user_id, 1)
    store = DownloadContentStore(root)
    pack_id = await create_pack(database, clock, store, user_id, [first])
    progress = PackProgressService(
        uow_factory=lambda: SQLiteUnitOfWork(database),
        repository_factory=lambda uow: PackRepository(uow.connection),
        content_repository_factory=lambda uow: ContentRepository(uow.connection),
    )
    pending_view = await progress.get_pack(user_id, pack_id)
    assert pending_view.output is None and pending_view.status is PackStatus.PENDING

    foreign = await create_pack_user(database, "foreign")
    with pytest.raises(DomainError) as caught:
        await progress.get_pack(foreign, pack_id)
    assert caught.value.code is ErrorCode.NOT_FOUND

    await make_dispatcher(database, clock, store).step_once()
    job = await get_job(database, pack_id)
    async with SQLiteUnitOfWork(database) as uow:
        await ContentRepository(uow.connection).remove_user_file(
            user_id=user_id,
            file_id=await ContentRepository(uow.connection).find_user_file(
                user_id=user_id, content_id=job.output_content_id
            ),
        )
    view = await progress.get_pack(user_id, pack_id)
    assert view.status is PackStatus.DONE and view.output is None


async def test_cleanup_skips_non_pending_sources(tmp_path: Path) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    _, first = await seed_pack_file(database, root, user_id, 1)
    _, second = await seed_pack_file(database, root, user_id, 2, payload=b"b" * 4)
    store = DownloadContentStore(root)
    pack_id = await create_pack(
        database, clock, store, user_id, [first, second], output_name="mix.zip", delete_source=True
    )
    from .test_pack_cleanup import _run_to_done

    await _run_to_done(database, clock, store, pack_id)
    async with SQLiteUnitOfWork(database) as uow:
        for source in await PackRepository(uow.connection).list_sources(pack_id):
            if source.snapshot.user_file_id == first:
                await PackRepository(uow.connection).set_source_cleanup(source.id, status="kept")
    outcomes = await make_cleanup_worker(database, clock).step_once()
    assert len(outcomes) == 1  # 仅 pending 来源被处理。
    refreshed = await get_job(database, pack_id)
    assert refreshed.cleanup_pending is False
    async with SQLiteUnitOfWork(database) as uow:
        still = await ContentRepository(uow.connection).get_user_file(
            user_id=user_id, file_id=first
        )
    assert still.display_name == "src-1", "已标 kept 的来源不得被删除"


async def test_outer_cancellation_propagates_and_stops_writer(tmp_path: Path) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    _, first = await seed_pack_file(database, root, user_id, 1)
    store = DownloadContentStore(root)
    pack_id = await create_pack(database, clock, store, user_id, [first])

    from .test_pack_dispatcher import BlockingWriter

    control = PackWriterControl()
    blocking = BlockingWriter(database)
    dispatcher = make_dispatcher(database, clock, store, writer=blocking, control=control)
    stepping = asyncio.create_task(dispatcher.step_once())
    await asyncio.wait_for(blocking.started.wait(), timeout=5)
    stepping.cancel()  # supervisor 关闭：整棵执行树必须一起取消。
    with pytest.raises(asyncio.CancelledError):
        await stepping
    await asyncio.sleep(0)  # 让 finally 的 stop_and_wait 完成。
    writer = control._writers.get(pack_id)
    assert writer is None or writer.done()


async def test_resume_failure_is_dispositioned_not_fatal(tmp_path: Path) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    _, first = await seed_pack_file(database, root, user_id, 1)
    # 输出重名冲突：恢复阶段 complete_job 抛错必须走确定性失败而不是击穿 worker。
    _, clash = await seed_pack_file(database, root, user_id, 2, payload=b"k" * 3, name="clash.zip")
    store = DownloadContentStore(root)
    pack_id = await create_pack(database, clock, store, user_id, [first], output_name="clash.zip")
    await _prepare_real_output(database, clock, store, user_id, pack_id, first)

    dispatcher = make_dispatcher(database, clock, store)
    await dispatcher.step_once()  # resume → complete 冲突 → 确定性失败。
    job = await get_job(database, pack_id)
    assert job.status is PackStatus.FAILED
    assert job.error_code == "duplicate_resource"
    async with SQLiteUnitOfWork(database) as uow:
        quota = await QuotaRepository(uow.connection).get(user_id)
    assert quota.reserved_bytes == 0
    assert clash > 0


async def test_resume_requeues_when_part_and_canonical_both_missing(tmp_path: Path) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    _, first = await seed_pack_file(database, root, user_id, 1)
    store = DownloadContentStore(root)
    pack_id = await create_pack(database, clock, store, user_id, [first])
    prepared = await _prepare_real_output(database, clock, store, user_id, pack_id, first)
    await store.remove_prepared(prepared.path)  # part 与规范位都不在。

    dispatcher = make_dispatcher(database, clock, store)
    await dispatcher.step_once()
    job = await get_job(database, pack_id)
    assert job.status is PackStatus.PENDING and job.retry_count == 1
    clock.advance(250)
    await dispatcher.step_once()
    assert (await get_job(database, pack_id)).status is PackStatus.DONE


async def test_create_rejects_deleting_source_at_pack_time(tmp_path: Path) -> None:
    from .test_pack_admission import make_admission

    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    _, first = await seed_pack_file(database, root, user_id, 1)
    from sqlalchemy import text

    async with SQLiteUnitOfWork(database) as uow:
        await uow.connection.execute(
            text("UPDATE content_objects SET deletion_state = 'pending_delete' "
                 "WHERE id = (SELECT content_id FROM user_files WHERE id = :id)"),
            {"id": first},
        )
    service = make_admission(database, clock, DownloadContentStore(root))
    with pytest.raises(DomainError) as caught:
        await service.create(user_id, user_file_ids=[first], output_name="x.zip", delete_source=False)
    assert caught.value.code is ErrorCode.CONTENT_PENDING_DELETE
