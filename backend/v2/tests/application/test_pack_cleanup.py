from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from aria2deck.application.packs.control import PackControlService
from aria2deck.application.packs.progress import PackProgressService
from aria2deck.application.packs.source_cleanup import SourceCleanupWorker
from aria2deck.domain.content import ContentDeletionState
from aria2deck.domain.errors import DomainError, ErrorCode
from aria2deck.domain.packs import PackStatus, SourceCleanupDecision
from aria2deck.domain.quota import Quota
from aria2deck.domain.shares import ShareStatus
from aria2deck.infrastructure.content_store import DownloadContentStore
from aria2deck.infrastructure.pack_writer import PackWriterControl
from aria2deck.infrastructure.sqlite.engine import SQLiteDatabase
from aria2deck.infrastructure.sqlite.repositories.content import ContentRepository
from aria2deck.infrastructure.sqlite.repositories.packs import PackRepository
from aria2deck.infrastructure.sqlite.repositories.quota import QuotaRepository
from aria2deck.infrastructure.sqlite.repositories.shares import ShareRepository
from aria2deck.infrastructure.sqlite.unit_of_work import SQLiteUnitOfWork

from .test_library_services import FakeClock, _digest
from .test_pack_admission import (
    create_pack_user,
    seed_pack_file,
    setup_pack_world,
)
from .test_pack_dispatcher import create_pack, get_job, make_dispatcher

OWNER_LEASE = ("content-deletion", 300_000)


def make_cleanup_worker(
    database: SQLiteDatabase, clock: FakeClock
) -> SourceCleanupWorker:
    return SourceCleanupWorker(
        uow_factory=lambda: SQLiteUnitOfWork(database),
        repository_factory=lambda uow: PackRepository(uow.connection),
        content_repository_factory=lambda uow: ContentRepository(uow.connection),
        quota_repository_factory=lambda uow: QuotaRepository(uow.connection),
        clock_ms=clock,
        lease_owner=OWNER_LEASE[0],
        lease_ttl_ms=OWNER_LEASE[1],
    )


def make_control(
    database: SQLiteDatabase, clock: FakeClock, store: DownloadContentStore
) -> PackControlService:
    return PackControlService(
        uow_factory=lambda: SQLiteUnitOfWork(database),
        repository_factory=lambda uow: PackRepository(uow.connection),
        content_repository_factory=lambda uow: ContentRepository(uow.connection),
        quota_repository_factory=lambda uow: QuotaRepository(uow.connection),
        stop_writer=PackWriterControl().stop_and_wait,
        prepared_store=store,
        clock_ms=clock,
        lease_owner=OWNER_LEASE[0],
        lease_ttl_ms=OWNER_LEASE[1],
    )


async def _run_to_done(
    database: SQLiteDatabase,
    clock: FakeClock,
    store: DownloadContentStore,
    pack_id: int,
) -> None:
    dispatcher = make_dispatcher(database, clock, store)
    await dispatcher.step_once()
    job = await get_job(database, pack_id)
    while job.status is PackStatus.PENDING:  # 暂态退避后重试。
        clock.advance(15_000)
        await dispatcher.step_once()
        job = await get_job(database, pack_id)
    assert job.status is PackStatus.DONE


async def test_source_cleanup_deletes_via_reference_removal_semantics(
    tmp_path: Path,
) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    other = await create_pack_user(database, "other")
    content_id, first = await seed_pack_file(database, root, user_id, 1, payload=b"a" * 10)
    _, second = await seed_pack_file(database, root, user_id, 2, payload=b"b" * 5)
    async with SQLiteUnitOfWork(database) as uow:
        await ContentRepository(uow.connection).add_user_file(
            user_id=other, content_id=content_id, display_name="shared", now_ms=1
        )
        await ShareRepository(uow.connection).create_share(
            user_id=user_id, user_file_id=first, code="bye", password_hash=None,
            expires_at_ms=None, max_downloads=None, now_ms=10,
        )
        await QuotaRepository(uow.connection).rebuild(user_id)
    store = DownloadContentStore(root)
    pack_id = await create_pack(
        database, clock, store, user_id, [first, second], output_name="mix.zip", delete_source=True
    )
    await _run_to_done(database, clock, store, pack_id)
    job = await get_job(database, pack_id)
    assert job.cleanup_pending is True

    worker = make_cleanup_worker(database, clock)
    outcomes = await worker.step_once()
    assert {(o.decision, o.cleanup_status) for o in outcomes} == {
        (SourceCleanupDecision.DELETE, "deleted"),
        (SourceCleanupDecision.DELETE, "deleted"),
    }
    refreshed = await get_job(database, pack_id)
    assert refreshed.cleanup_pending is False
    async with SQLiteUnitOfWork(database) as uow:
        content = ContentRepository(uow.connection)
        remaining = await content.count_references(content_id)
        state = (await content.get_object(content_id)).deletion_state
        quota = await QuotaRepository(uow.connection).get(user_id)
        sources = await PackRepository(uow.connection).list_sources(pack_id)
        share = await ShareRepository(uow.connection).get_share(1)
        output_size = (await content.get_object(job.output_content_id)).size
    assert remaining == 1  # other 用户仍引用：不误删共享内容。
    assert state is ContentDeletionState.ACTIVE
    assert share.revoked_at_ms is not None
    assert [source.cleanup_status for source in sources] == ["deleted", "deleted"]
    # 本人来源已删：used 只剩输出归档本身，预留清零。
    assert quota == Quota(100 * 1024 * 1024, output_size, 0)


async def test_source_cleanup_identity_matrix(tmp_path: Path) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    _, matching = await seed_pack_file(database, root, user_id, 1, payload=b"a" * 10)
    _, renamed = await seed_pack_file(database, root, user_id, 2, payload=b"b" * 6)
    _, replaced = await seed_pack_file(database, root, user_id, 3, payload=b"c" * 7)
    _, reused = await seed_pack_file(database, root, user_id, 4, payload=b"d" * 8)
    _, missing = await seed_pack_file(database, root, user_id, 5, payload=b"e" * 9)
    async with SQLiteUnitOfWork(database) as uow:
        replacement_content = await ContentRepository(uow.connection).create_object(
            kind="file", digest=_digest(7), size=4, storage_path="objects/file-7", now_ms=1
        )
    store = DownloadContentStore(root)
    pack_id = await create_pack(
        database, clock, store, user_id,
        [matching, renamed, replaced, reused, missing],
        output_name="matrix.zip", delete_source=True,
    )
    await _run_to_done(database, clock, store, pack_id)

    # 改名：display_name 与快照不一致。
    async with SQLiteUnitOfWork(database) as uow:
        await ContentRepository(uow.connection).rename_user_file(
            user_id=user_id, file_id=renamed, new_name="renamed.bin"
        )
    # 替换：原引用行删除后以同名新内容重建（user_files 身份不可变触发器的真实路径）。
    async with SQLiteUnitOfWork(database) as uow:
        content = ContentRepository(uow.connection)
        await content.remove_user_file(user_id=user_id, file_id=replaced)
        replacement_file = await content.add_user_file(
            user_id=user_id, content_id=replacement_content, display_name="src-3", now_ms=1
        )
    # 复用输出：输出内容对象与来源内容对象同一（安装时按 digest 去重的可达状态）。
    async with SQLiteUnitOfWork(database) as uow:
        source_content = (
            await uow.connection.execute(
                text("SELECT content_id FROM user_files WHERE id = :id"), {"id": reused}
            )
        ).scalar_one()
        await uow.connection.execute(
            text("UPDATE pack_jobs SET output_content_id = :out WHERE id = :pack"),
            {"out": source_content, "pack": pack_id},
        )
    # 已缺失：直接删除引用行。
    async with SQLiteUnitOfWork(database) as uow:
        await ContentRepository(uow.connection).remove_user_file(user_id=user_id, file_id=missing)

    worker = make_cleanup_worker(database, clock)
    outcomes = await worker.step_once()
    assert len(outcomes) == 5
    async with SQLiteUnitOfWork(database) as uow:
        sources = await PackRepository(uow.connection).list_sources(pack_id)
    statuses = {source.snapshot.user_file_id: source for source in sources}
    assert statuses[matching].cleanup_status == "deleted"
    assert statuses[matching].cleanup_error_code is None
    assert statuses[renamed].cleanup_status == "kept"
    assert statuses[replaced].cleanup_status == "deleted"  # 原引用已不在：按已缺失收敛。
    assert statuses[replaced].cleanup_error_code == "already_missing"
    assert statuses[reused].cleanup_status == "kept"  # 输出与来源同内容：保留。
    assert statuses[missing].cleanup_status == "deleted"
    assert statuses[missing].cleanup_error_code == "already_missing"
    assert (await get_job(database, pack_id)).cleanup_pending is False
    async with SQLiteUnitOfWork(database) as uow:
        still_there = await ContentRepository(uow.connection).get_user_file(
            user_id=user_id, file_id=replacement_file
        )
    assert still_there.display_name == "src-3", "替换后的新文件必须保留"


async def test_source_cleanup_busy_conflict_retries_then_converges(tmp_path: Path) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    content_id, first = await seed_pack_file(database, root, user_id, 1, payload=b"a" * 10)
    store = DownloadContentStore(root)
    pack_id = await create_pack(
        database, clock, store, user_id, [first], output_name="busy.zip", delete_source=True
    )
    await _run_to_done(database, clock, store, pack_id)
    # 另一个打包任务仍以 pending 来源占用同一内容（模拟进行中的打包）。
    blocker = await create_pack(database, clock, store, user_id, [first], output_name="block.zip")

    worker = make_cleanup_worker(database, clock)
    outcomes = await worker.step_once()
    assert outcomes[0].decision is None
    assert outcomes[0].cleanup_status == "pending"
    assert outcomes[0].error_code == "content_busy"
    assert (await get_job(database, pack_id)).cleanup_pending is True
    assert (await get_job(database, blocker)).status is PackStatus.PENDING

    async with SQLiteUnitOfWork(database) as uow:
        repository = PackRepository(uow.connection)
        for source in await repository.list_sources(blocker):
            await repository.set_source_cleanup(source.id, status="kept")
    outcomes = await worker.step_once()
    assert outcomes[0].decision is SourceCleanupDecision.DELETE
    refreshed = await get_job(database, pack_id)
    assert refreshed.cleanup_pending is False
    async with SQLiteUnitOfWork(database) as uow:
        state = (await ContentRepository(uow.connection).get_object(content_id)).deletion_state
    assert state is ContentDeletionState.PENDING_DELETE  # 最后引用已认领物理删除。


async def test_control_delete_and_clear_record_semantics(tmp_path: Path) -> None:
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    _, first = await seed_pack_file(database, root, user_id, 1, payload=b"a" * 10)
    _, second = await seed_pack_file(database, root, user_id, 2, payload=b"b" * 6)
    _, third = await seed_pack_file(database, root, user_id, 3, payload=b"c" * 7)
    store = DownloadContentStore(root)
    pack_id = await create_pack(
        database, clock, store, user_id, [first], output_name="ctl.zip", delete_source=True
    )
    await _run_to_done(database, clock, store, pack_id)
    job = await get_job(database, pack_id)
    control = make_control(database, clock, store)
    progress = PackProgressService(
        uow_factory=lambda: SQLiteUnitOfWork(database),
        repository_factory=lambda uow: PackRepository(uow.connection),
        content_repository_factory=lambda uow: ContentRepository(uow.connection),
    )
    view = await progress.get_pack(user_id, pack_id)
    assert view.status is PackStatus.DONE
    assert view.output is not None and view.output.display_name == "ctl.zip"
    with pytest.raises(DomainError) as caught:  # 来源清理未完成禁止删除。
        await control.delete_pack(user_id, pack_id)
    assert caught.value.code is ErrorCode.INVALID_STATE

    await make_cleanup_worker(database, clock).step_once()
    await control.clear_record(user_id, pack_id)  # 清记录：输出引用保留。
    with pytest.raises(DomainError):
        await get_job(database, pack_id)
    async with SQLiteUnitOfWork(database) as uow:
        content = ContentRepository(uow.connection)
        output_file = await content.find_user_file(
            user_id=user_id, content_id=job.output_content_id
        )
        quota = await QuotaRepository(uow.connection).get(user_id)
    assert output_file is not None
    assert quota.used_bytes > 0

    # 再造一个已完成任务做整体删除：输出引用一并删除。
    pack_two = await create_pack(
        database, clock, store, user_id, [second], output_name="gone.zip", delete_source=False
    )
    await _run_to_done(database, clock, store, pack_two)
    job_two = await get_job(database, pack_two)
    await control.delete_pack(user_id, pack_two)
    with pytest.raises(DomainError):
        await get_job(database, pack_two)
    async with SQLiteUnitOfWork(database) as uow:
        content = ContentRepository(uow.connection)
        output_file = await content.find_user_file(
            user_id=user_id, content_id=job_two.output_content_id
        )
        state = (await content.get_object(job_two.output_content_id)).deletion_state
        quota = await QuotaRepository(uow.connection).get(user_id)
        rebuilt = await QuotaRepository(uow.connection).rebuild(user_id)
    assert output_file is None
    assert state is ContentDeletionState.PENDING_DELETE
    # 剩余 used = ctl.zip 输出（清记录保留）+ 未动过的 third 来源；与权威 rebuild 一致。
    assert quota == rebuilt and quota.used_bytes > 0
    with pytest.raises(DomainError) as caught:
        await progress.get_pack(user_id, pack_two)
    assert caught.value.code is ErrorCode.NOT_FOUND
    foreign = await create_pack_user(database, "foreign")
    pack_three = await create_pack(database, clock, store, user_id, [third], output_name="f.zip")
    with pytest.raises(DomainError) as caught:
        await control.delete_pack(foreign, pack_three)
    assert caught.value.code is ErrorCode.NOT_FOUND
    with pytest.raises(DomainError) as caught:  # 活动任务不可删除。
        await control.delete_pack(user_id, pack_three)
    assert caught.value.code is ErrorCode.INVALID_STATE
    assert ShareStatus.ACTIVE.value == "active"


async def test_clear_records_removes_only_terminal_with_finished_cleanup(
    tmp_path: Path,
) -> None:
    """A3：清空全部终态打包记录（保留输出）；清理未完成/进行中的记录保留。"""
    database, clock, root = await setup_pack_world(tmp_path)
    user_id = await create_pack_user(database)
    other = await create_pack_user(database, "other")
    _, first = await seed_pack_file(database, root, user_id, 1, payload=b"a" * 10)
    _, second = await seed_pack_file(database, root, user_id, 2, payload=b"b" * 6)
    _, third = await seed_pack_file(database, root, user_id, 3, payload=b"c" * 7)
    _, other_source = await seed_pack_file(database, root, other, 4, payload=b"d" * 5)
    store = DownloadContentStore(root)

    # 1) done + delete_source=True：清理未完成 → 记录保留。
    pending_cleanup = await create_pack(
        database, clock, store, user_id, [first], output_name="wait.zip", delete_source=True
    )
    await _run_to_done(database, clock, store, pending_cleanup)
    # 2) done + delete_source=False：来源标记保留 → 立即可清。
    done = await create_pack(
        database, clock, store, user_id, [second], output_name="done.zip", delete_source=False
    )
    await _run_to_done(database, clock, store, done)
    # 3) 他人终态记录：不受本人清理影响。
    other_done = await create_pack(
        database, clock, store, other, [other_source], output_name="other.zip", delete_source=False
    )
    await _run_to_done(database, clock, store, other_done)
    # 4) 进行中（pending）记录：保留。
    pending = await create_pack(
        database, clock, store, user_id, [third], output_name="live.zip", delete_source=False
    )

    control = make_control(database, clock, store)
    cleared = await control.clear_records(user_id)

    assert cleared == 1
    with pytest.raises(DomainError):
        await get_job(database, done)
    for kept in (pending_cleanup, pending, other_done):
        assert (await get_job(database, kept)).status in {PackStatus.DONE, PackStatus.PENDING}
    # 输出引用保留：done.zip 归档仍在本人文件里（记录删除但输出未动）。
    async with SQLiteUnitOfWork(database) as uow:
        rows = (
            await uow.connection.execute(
                text("SELECT display_name FROM user_files WHERE user_id = :u"),
                {"u": user_id},
            )
        ).fetchall()
    names = {row[0] for row in rows}
    assert "done.zip" in names
