from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from aria2deck.bootstrap import create_app
from aria2deck.domain.content import ContentDeletionState
from aria2deck.domain.errors import DomainError, ErrorCode
from aria2deck.infrastructure.config import Settings
from aria2deck.infrastructure.sqlite.engine import SQLiteDatabase
from aria2deck.infrastructure.sqlite.repositories.content import (
    DELETE_LEASE_OWNER,
    DELETE_LEASE_TTL_MS,
    ContentRepository,
)
from aria2deck.infrastructure.sqlite.repositories.quota import QuotaRepository
from aria2deck.infrastructure.sqlite.unit_of_work import SQLiteUnitOfWork

from .test_library_services import (
    FakeClock,
    FakeStore,
    _create_user,
    _make_services,
    _seed_file,
    _setup,
    _write_store_file,
)


def _worker(database: SQLiteDatabase, clock: FakeClock, store: FakeStore):
    from aria2deck.application.library.deletion import ContentDeletionWorker

    return ContentDeletionWorker(
        uow_factory=lambda: SQLiteUnitOfWork(database),
        repository_factory=lambda uow: ContentRepository(uow.connection),
        content_store=store,
        clock_ms=clock,
        owner=DELETE_LEASE_OWNER,
        lease_ttl_ms=DELETE_LEASE_TTL_MS,
    )


async def _sql(database: SQLiteDatabase, statement: str, params: dict[str, object]) -> None:
    async with SQLiteUnitOfWork(database) as uow:
        await uow.connection.execute(text(statement), params)


async def _state(database: SQLiteDatabase, content_id: int) -> ContentDeletionState:
    async with SQLiteUnitOfWork(database) as uow:
        return (await ContentRepository(uow.connection).get_object(content_id)).deletion_state


async def _gone(database: SQLiteDatabase, content_id: int) -> bool:
    async with SQLiteUnitOfWork(database) as uow:
        with pytest.raises(DomainError) as caught:
            await ContentRepository(uow.connection).get_object(content_id)
    return caught.value.code is ErrorCode.NOT_FOUND


def _physical(store: FakeStore) -> bool:
    return store.physical_path("objects/file-1").exists()


async def _assert_converged(
    database: SQLiteDatabase, store: FakeStore, user_id: int, content_id: int, physical_name: str
) -> None:
    assert await _gone(database, content_id), "对象行必须删除"
    assert not store.physical_path(physical_name).exists(), "物理文件必须删除"
    async with SQLiteUnitOfWork(database) as uow:
        references = (
            await uow.connection.execute(
                text("SELECT COUNT(*) FROM user_files WHERE content_id = :id"),
                {"id": content_id},
            )
        ).scalar_one()
        entries = (
            await uow.connection.execute(
                text("SELECT COUNT(*) FROM content_entries WHERE content_id = :id"),
                {"id": content_id},
            )
        ).scalar_one()
        quota = await QuotaRepository(uow.connection).get(user_id)
    assert references == 0
    assert entries == 0, "条目必须随对象行级联清理，无半成品"
    assert quota.used_bytes == 0


async def _seed_pending_scenario(tmp_path: Path) -> tuple:
    database, clock = await _setup(tmp_path)
    user_id = await _create_user(database, "admin")
    content_id, file_id = await _seed_file(database, user_id, 1, payload=b"payload")
    root = tmp_path / "downloads"
    _write_store_file(root, "objects/file-1", b"payload")
    store = FakeStore(root)
    _, commands, _, _ = _make_services(database, clock, store)
    async with SQLiteUnitOfWork(database) as uow:
        await QuotaRepository(uow.connection).rebuild(user_id)
    return database, clock, store, user_id, content_id, file_id, commands


async def test_recovery_after_reference_removal_without_lease(tmp_path: Path) -> None:
    """a. 删引用后中断：pending_delete 但租约为空（写入方中断的残留形态）。"""
    database, clock, store, user_id, content_id, file_id, commands = (
        await _seed_pending_scenario(tmp_path)
    )
    await commands.delete_user_file(user_id=user_id, file_id=file_id)
    await _sql(
        database,
        "UPDATE content_objects SET delete_lease_owner = NULL, delete_lease_expires_at_ms = NULL "
        "WHERE id = :id",
        {"id": content_id},
    )
    assert await _state(database, content_id) is ContentDeletionState.PENDING_DELETE
    worker = _worker(database, clock, store)
    await worker.step_once()
    await _assert_converged(database, store, user_id, content_id, "objects/file-1")
    await worker.step_once()  # 无候选，重复执行稳定。


async def test_recovery_after_claim_with_lease_and_reference_recheck(tmp_path: Path) -> None:
    """b. 认领后中断：带租约 pending_delete；新引用出现时安全保留，引用消失后收敛。"""
    database, clock, store, user_id, content_id, file_id, commands = (
        await _seed_pending_scenario(tmp_path)
    )
    await commands.delete_user_file(user_id=user_id, file_id=file_id)
    assert await _state(database, content_id) is ContentDeletionState.PENDING_DELETE
    worker = _worker(database, clock, store)

    # 认领后出现新引用（如秒传重建竞态）：worker 必须跳过且不推进状态机。
    await _sql(
        database,
        "INSERT INTO user_files (user_id, content_id, display_name, created_at_ms) "
        "VALUES (:user_id, :content_id, 'ghost', 1)",
        {"user_id": user_id, "content_id": content_id},
    )
    await worker.step_once()
    assert await _state(database, content_id) is ContentDeletionState.PENDING_DELETE
    assert _physical(store), "仍有引用的内容不得物理删除"
    await _sql(
        database, "DELETE FROM user_files WHERE content_id = :id", {"id": content_id}
    )

    await worker.step_once()
    await _assert_converged(database, store, user_id, content_id, "objects/file-1")


async def test_recovery_from_tombstone_before_physical_delete(tmp_path: Path) -> None:
    """c. tombstone 后、物理删除前中断：文件仍在盘，重启后收敛。"""
    database, clock, store, user_id, content_id, file_id, commands = (
        await _seed_pending_scenario(tmp_path)
    )
    await commands.delete_user_file(user_id=user_id, file_id=file_id)
    async with SQLiteUnitOfWork(database) as uow:
        assert await ContentRepository(uow.connection).write_tombstone(
            content_id, owner=DELETE_LEASE_OWNER, now_ms=clock()
        )
    assert _physical(store)
    assert await _state(database, content_id) is ContentDeletionState.TOMBSTONED

    worker = _worker(database, clock, store)  # 新实例模拟重启
    await worker.step_once()
    await _assert_converged(database, store, user_id, content_id, "objects/file-1")


async def test_recovery_from_missing_file_before_finalize(tmp_path: Path) -> None:
    """d. 物理删除后、删行前中断：幂等收敛。"""
    database, clock, store, user_id, content_id, file_id, commands = (
        await _seed_pending_scenario(tmp_path)
    )
    await commands.delete_user_file(user_id=user_id, file_id=file_id)
    async with SQLiteUnitOfWork(database) as uow:
        assert await ContentRepository(uow.connection).write_tombstone(
            content_id, owner=DELETE_LEASE_OWNER, now_ms=clock()
        )
    await store.physical_delete("objects/file-1")
    assert await _state(database, content_id) is ContentDeletionState.TOMBSTONED

    worker = _worker(database, clock, store)
    await worker.step_once()
    await _assert_converged(database, store, user_id, content_id, "objects/file-1")


async def test_recovery_blocked_by_foreign_lease_until_expiry(tmp_path: Path) -> None:
    """e. 租约被他人持有且未过期：跳过；过期后收敛。"""
    database, clock, store, user_id, content_id, file_id, commands = (
        await _seed_pending_scenario(tmp_path)
    )
    await commands.delete_user_file(user_id=user_id, file_id=file_id)
    await _sql(
        database,
        "UPDATE content_objects SET delete_lease_owner = 'other-worker', "
        "delete_lease_expires_at_ms = :expiry WHERE id = :id",
        {"expiry": clock() + 600_000, "id": content_id},
    )
    worker = _worker(database, clock, store)
    await worker.step_once()
    assert await _state(database, content_id) is ContentDeletionState.PENDING_DELETE
    assert _physical(store), "他人租约未过期时文件必须保留"

    clock.advance(600_001)
    await worker.step_once()
    await _assert_converged(database, store, user_id, content_id, "objects/file-1")


async def test_startup_resets_read_leases_and_unblocks_deletion(tmp_path: Path) -> None:
    """重启复位：进程重启后虚假租约归零，worker 不再被阻塞。"""
    settings = Settings(
        database_path=tmp_path / "data" / "v2.db",
        download_directory=tmp_path / "downloads",
        static_directory=tmp_path / "static",
        debug=True,
    )
    settings.download_directory.mkdir(parents=True, exist_ok=True)
    database = SQLiteDatabase(settings.database_path)
    from aria2deck.infrastructure.sqlite.bootstrap import initialize_database

    await initialize_database(database)
    async with SQLiteUnitOfWork(database) as uow:
        from aria2deck.infrastructure.sqlite.repositories.accounts import AccountRepository

        from .test_library_services import _digest

        user_id = await AccountRepository(uow.connection).create_user(
            username="admin", password_hash=_digest(1), is_admin=True, quota_bytes=1000, now_ms=1
        )
        repository = ContentRepository(uow.connection)
        content_id = await repository.create_object(
            kind="file", digest=_digest(2), size=7, storage_path="objects/file-9", now_ms=1
        )
        await repository.remove_user_file(
            user_id=user_id,
            file_id=await repository.add_user_file(
                user_id=user_id, content_id=content_id, display_name="stale", now_ms=1
            ),
        )
        await repository.claim_orphan_delete(
            content_id, now_ms=1, lease_owner=DELETE_LEASE_OWNER, lease_ttl_ms=DELETE_LEASE_TTL_MS
        )
        await uow.connection.execute(
            text("UPDATE content_objects SET read_leases = 3 WHERE id = :id"),
            {"id": content_id},
        )
    await database.close()
    _write_store_file(settings.download_directory, "objects/file-9", b"stale!!")

    from fastapi.testclient import TestClient

    app = create_app(settings=settings)
    with TestClient(app):  # lifespan：恢复阶段先复位读租约，再启动 worker。
        # 复位在 worker 启动前顺序执行；若后台 worker 抢先收敛（行已删）同样证明未被租约阻塞。
        database = SQLiteDatabase(settings.database_path)
        async with SQLiteUnitOfWork(database) as uow:
            leases = (
                await uow.connection.execute(
                    text("SELECT read_leases FROM content_objects WHERE id = :id"),
                    {"id": content_id},
                )
            ).scalar_one_or_none()
        await database.close()
        if leases is not None:
            assert leases == 0, "启动必须把跨进程不存在的读租约归零"
        worker = app.state.library_deletion
        await worker.step_once()
        database = SQLiteDatabase(settings.database_path)
        async with SQLiteUnitOfWork(database) as uow:
            with pytest.raises(DomainError):
                await ContentRepository(uow.connection).get_object(content_id)
        await database.close()
    assert not (settings.download_directory / "objects" / "file-9").exists()


async def test_concurrent_download_survives_reference_deletion(tmp_path: Path) -> None:
    """并发下载 vs 删除：下载迭代器读完剩余字节，租约排空后 worker 收敛。"""
    database, clock = await _setup(tmp_path)
    user_id = await _create_user(database, "admin")
    payload = bytes(range(256)) * 8
    content_id, file_id = await _seed_file(database, user_id, 1, payload=payload)
    root = tmp_path / "downloads"
    _write_store_file(root, "objects/file-1", payload)
    store = FakeStore(root)
    _, commands, leases, download = _make_services(database, clock, store)
    download.chunk_size = 512

    target = await download.open_download(user_id=user_id, file_id=file_id)
    assert leases.in_flight(content_id) == 1

    # 下载进行中删除引用：物理删除必须等待读租约排空。
    await commands.delete_user_file(user_id=user_id, file_id=file_id)
    worker = _worker(database, clock, store)
    await worker.step_once()
    assert await _state(database, content_id) is ContentDeletionState.PENDING_DELETE
    assert store.physical_path("objects/file-1").exists(), "读租约未排空不得删除文件"

    received = bytearray()
    async for chunk in download.iter_chunks(target):
        received.extend(chunk)
    assert bytes(received) == payload, "下载必须完整读完且不抛错"
    assert target.released is True
    assert leases.in_flight(content_id) == 0

    await worker.step_once()
    await _assert_converged(database, store, user_id, content_id, "objects/file-1")
