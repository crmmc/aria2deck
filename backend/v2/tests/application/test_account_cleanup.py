from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest
from fastapi import FastAPI
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncConnection

from aria2deck.application.accounts.models import CleanupReport, Principal
from aria2deck.application.accounts.service import AccountService
from aria2deck.application.downloads.cleanup import DownloadControlPort, DownloadUserCleanup
from aria2deck.application.packs.cleanup import PackControlPort, PackUserCleanup
from aria2deck.bootstrap import create_app
from aria2deck.domain.accounts import CredentialKind
from aria2deck.domain.content import ContentDeletionState
from aria2deck.domain.downloads import AttemptStatus
from aria2deck.domain.errors import DomainError, ErrorCode
from aria2deck.domain.packs import PackSourceSnapshot, PackStatus
from aria2deck.domain.settings import SystemSettings
from aria2deck.infrastructure.archive_writer import ArchiveWriter
from aria2deck.infrastructure.config import Settings
from aria2deck.infrastructure.content_store import DownloadContentStore
from aria2deck.infrastructure.pack_writer import PackWriterControl
from aria2deck.infrastructure.sqlite.bootstrap import initialize_database
from aria2deck.infrastructure.sqlite.engine import SQLiteDatabase
from aria2deck.infrastructure.sqlite.repositories.accounts import AccountRepository
from aria2deck.infrastructure.sqlite.repositories.content import ContentRepository
from aria2deck.infrastructure.sqlite.repositories.downloads import DownloadRepository
from aria2deck.infrastructure.sqlite.repositories.packs import PackRepository
from aria2deck.infrastructure.sqlite.repositories.quota import QuotaRepository
from aria2deck.infrastructure.sqlite.schema import (
    content_objects,
    download_attempts,
    pack_jobs,
    task_subscriptions,
    user_files,
    users,
)
from aria2deck.infrastructure.sqlite.unit_of_work import SQLiteUnitOfWork

from .test_library_services import FakeClock
from .test_pack_admission import make_admission, seed_pack_file
from .test_pack_dispatcher import get_job, make_dispatcher

PASSWORD = "ab" * 32
OTHER_PASSWORD = "cd" * 32
THIRD_PASSWORD = "ef" * 32


class RecordingDownloadControl:
    def __init__(self, *, failures: int = 0) -> None:
        self.gids: list[str] = []
        self.failures = failures

    async def stop(self, *, gid: str) -> None:
        self.gids.append(gid)
        if self.failures:
            self.failures -= 1
            raise DomainError(ErrorCode.INVALID_STATE, "模拟下载停止失败")


class RecordingPackControl:
    """记录账户清理的停止与清理调用顺序，并允许按次注入失败。"""

    def __init__(
        self,
        events: list[str] | None = None,
        *,
        stop_failures: int = 0,
        clear_failures: int = 0,
    ) -> None:
        self.events = events if events is not None else []
        self.stop_failures = stop_failures
        self.clear_failures = clear_failures
        self.stop_attempts: list[int] = []
        self.cleared: list[int] = []

    async def stop_and_wait(self, pack_id: int) -> None:
        self.events.append(f"stop:{pack_id}")
        self.stop_attempts.append(pack_id)
        if self.stop_failures:
            self.stop_failures -= 1
            raise DomainError(ErrorCode.INVALID_STATE, "模拟打包停止失败")

    def clear(self, pack_id: int) -> None:
        self.events.append(f"clear:{pack_id}")
        if self.clear_failures:
            self.clear_failures -= 1
            raise DomainError(ErrorCode.INVALID_STATE, "模拟控制状态清理失败")
        self.cleared.append(pack_id)


class RecordingPreparedStore:
    """记录 prepared 删除顺序；root 非空时真实删除文件，failures 用于注入首次失败。"""

    def __init__(
        self,
        root: Path | None = None,
        events: list[str] | None = None,
        *,
        failures: int = 0,
    ) -> None:
        self.root = root
        self.events = events if events is not None else []
        self.failures = failures
        self.removed: list[str] = []

    async def remove_prepared(self, storage_path: str) -> None:
        self.events.append(f"remove:{storage_path}")
        self.removed.append(storage_path)
        if self.failures:
            self.failures -= 1
            raise OSError("模拟 prepared 清理失败")
        if self.root is not None:
            (self.root / storage_path).unlink(missing_ok=True)


async def make_service(
    tmp_path: Path,
    *,
    download_control: DownloadControlPort | None = None,
    pack_control: PackControlPort | None = None,
    pack_prepared_store: RecordingPreparedStore | None = None,
) -> tuple[FastAPI, AccountService]:
    settings = Settings(
        database_path=tmp_path / "data" / "v2.db",
        download_directory=tmp_path / "downloads",
        static_directory=tmp_path / "static",
        debug=True,
    )
    database = SQLiteDatabase(settings.database_path)
    await initialize_database(database)
    await database.close()
    app = create_app(settings=settings)
    service = app.state.account_service
    download_cleanup = service.cleanup_ports[0]
    pack_cleanup = service.cleanup_ports[1]
    assert isinstance(download_cleanup, DownloadUserCleanup)
    assert isinstance(pack_cleanup, PackUserCleanup)
    download_cleanup.control = download_control or RecordingDownloadControl()
    pack_cleanup.control = pack_control or RecordingPackControl()
    pack_cleanup.prepared_store = pack_prepared_store or RecordingPreparedStore(
        tmp_path / "downloads"
    )
    return app, service


async def create_admin(service: AccountService) -> Principal:
    view = await service.create_user(
        None,
        username="admin",
        password_hash=PASSWORD,
        is_admin=True,
        is_initial_password=False,
        quota_bytes=None,
    )
    return Principal(view.user, CredentialKind.SESSION, "admin-session")


async def test_pending_user_without_references_is_deleted_and_old_session_is_rejected(
    tmp_path: Path,
) -> None:
    _app, service = await make_service(tmp_path)
    admin = await create_admin(service)
    user = await service.create_user(
        admin,
        username="user",
        password_hash=OTHER_PASSWORD,
        is_admin=False,
        is_initial_password=False,
        quota_bytes=100,
    )

    await service.delete_user(admin, user_id=user.user.id)
    report = await service.cleanup_pending_users()

    assert report == CleanupReport(deleted=1, busy=0, failed=0)
    async with service.uow_factory() as uow:
        with pytest.raises(DomainError) as caught:
            await service.repository_factory(uow).get_user(user.user.id)
    assert caught.value.code is ErrorCode.NOT_FOUND


async def test_cleanup_removes_real_releases_and_preserves_other_users(
    tmp_path: Path,
) -> None:
    download_control = RecordingDownloadControl()
    pack_control = RecordingPackControl()
    _app, service = await make_service(
        tmp_path, download_control=download_control, pack_control=pack_control
    )
    admin = await create_admin(service)
    user = await service.create_user(
        admin,
        username="user",
        password_hash=OTHER_PASSWORD,
        is_admin=False,
        is_initial_password=False,
        quota_bytes=1000,
    )
    other = await service.create_user(
        admin,
        username="other",
        password_hash=THIRD_PASSWORD,
        is_admin=False,
        is_initial_password=False,
        quota_bytes=1000,
    )

    async with service.uow_factory() as uow:
        content = ContentRepository(uow.connection)
        downloads = DownloadRepository(uow.connection)
        packs = PackRepository(uow.connection)
        quota = QuotaRepository(uow.connection)
        shared_content = await content.create_object(
            kind="file", digest="ab" * 32, size=10, storage_path="objects/shared", now_ms=1
        )
        exclusive_content = await content.create_object(
            kind="file", digest="cd" * 32, size=20, storage_path="objects/exclusive", now_ms=1
        )
        shared_file = await content.add_user_file(
            user_id=user.user.id, content_id=shared_content, display_name="shared", now_ms=1
        )
        exclusive_file = await content.add_user_file(
            user_id=user.user.id, content_id=exclusive_content, display_name="exclusive", now_ms=1
        )
        await content.add_user_file(
            user_id=other.user.id, content_id=shared_content, display_name="shared", now_ms=1
        )

        shared_source = await downloads.create_source(
            kind="http",
            resource_key="http:shared",
            payload_json="{}",
            options_json="{}",
            selected_file_indexes_json=None,
            display_name="shared-task",
            display_uri=None,
            now_ms=1,
        )
        shared_attempt = await downloads.create_attempt(
            source_id=shared_source,
            resource_key="http:shared",
            gid="0123456789abcdef",
            total_length=100,
            physical_reserved_bytes=100,
            now_ms=1,
        )
        await downloads.create_subscription(
            attempt_id=shared_attempt,
            user_id=user.user.id,
            reserved_bytes=40,
            display_name="shared-task",
            display_uri=None,
            now_ms=1,
        )
        await downloads.create_subscription(
            attempt_id=shared_attempt,
            user_id=other.user.id,
            reserved_bytes=20,
            display_name="shared-task",
            display_uri=None,
            now_ms=1,
        )
        exclusive_source = await downloads.create_source(
            kind="http",
            resource_key="http:exclusive",
            payload_json="{}",
            options_json="{}",
            selected_file_indexes_json=None,
            display_name="exclusive-task",
            display_uri=None,
            now_ms=1,
        )
        exclusive_attempt = await downloads.create_attempt(
            source_id=exclusive_source,
            resource_key="http:exclusive",
            gid="fedcba0987654321",
            status=AttemptStatus.ACTIVE,
            total_length=50,
            physical_reserved_bytes=50,
            now_ms=1,
        )
        await downloads.create_subscription(
            attempt_id=exclusive_attempt,
            user_id=user.user.id,
            reserved_bytes=10,
            display_name="exclusive-task",
            display_uri=None,
            now_ms=1,
        )

        active_pack = await packs.create_job(
            owner_id=user.user.id,
            output_name="active.zip",
            pack_format="zip",
            source_size=20,
            reserved_bytes=30,
            physical_reserved_bytes=30,
            delete_source=True,
            now_ms=1,
        )
        await packs.add_source(
            active_pack,
            PackSourceSnapshot(
                exclusive_file,
                exclusive_content,
                "file:" + "cd" * 32,
                "exclusive",
                1,
                20,
            ),
        )
        failed_pack = await packs.create_job(
            owner_id=user.user.id,
            output_name="failed.zip",
            pack_format="zip",
            source_size=10,
            reserved_bytes=10,
            physical_reserved_bytes=10,
            delete_source=False,
            now_ms=1,
        )
        await uow.connection.execute(
            text("UPDATE pack_jobs SET status = 'failed' WHERE id = :id"), {"id": failed_pack}
        )
        assert (await quota.rebuild(user.user.id)).used_bytes == 30
        assert (await quota.rebuild(user.user.id)).reserved_bytes == 80
        assert (await quota.rebuild(other.user.id)).used_bytes == 10
        assert (await quota.rebuild(other.user.id)).reserved_bytes == 20
        assert shared_file > 0

    await service.delete_user(admin, user_id=user.user.id)
    report = await service.cleanup_pending_users()

    assert report == CleanupReport(deleted=1, busy=0, failed=0)
    assert download_control.gids == ["fedcba0987654321"]
    assert pack_control.stop_attempts == [active_pack, failed_pack]
    async with service.uow_factory() as uow:
        assert (
            await uow.connection.execute(
                select(users.c.id).where(users.c.id == user.user.id)
            )
        ).first() is None
        assert (
            await uow.connection.execute(
                select(task_subscriptions.c.id).where(
                    task_subscriptions.c.user_id == user.user.id
                )
            )
        ).all() == []
        assert (
            await uow.connection.execute(
                select(user_files.c.id).where(user_files.c.user_id == user.user.id)
            )
        ).all() == []
        assert (
            await uow.connection.execute(
                select(pack_jobs.c.id).where(pack_jobs.c.owner_id == user.user.id)
            )
        ).all() == []

        shared_attempt_row = (
            await uow.connection.execute(
                select(download_attempts).where(download_attempts.c.id == shared_attempt)
            )
        ).one()
        assert shared_attempt_row.status == AttemptStatus.QUEUED
        assert shared_attempt_row.physical_reserved_bytes == 100
        exclusive_attempt_row = (
            await uow.connection.execute(
                select(download_attempts).where(download_attempts.c.id == exclusive_attempt)
            )
        ).one()
        assert exclusive_attempt_row.status == AttemptStatus.CANCELLED
        assert exclusive_attempt_row.physical_reserved_bytes == 0
        assert (
            await uow.connection.execute(
                select(content_objects.c.deletion_state).where(
                    content_objects.c.id == shared_content
                )
            )
        ).scalar_one() == ContentDeletionState.ACTIVE
        assert (
            await uow.connection.execute(
                select(content_objects.c.deletion_state).where(
                    content_objects.c.id == exclusive_content
                )
            )
        ).scalar_one() == ContentDeletionState.PENDING_DELETE
        other_quota = await QuotaRepository(uow.connection).get(other.user.id)
        assert (other_quota.used_bytes, other_quota.reserved_bytes) == (10, 20)


@pytest.mark.parametrize("failure", ["domain", "busy", "unexpected"])
async def test_cleanup_isolates_failure_and_retries_the_pending_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    _app, service = await make_service(tmp_path)
    admin = await create_admin(service)
    first = await service.create_user(
        admin,
        username="first",
        password_hash=OTHER_PASSWORD,
        is_admin=False,
        is_initial_password=False,
        quota_bytes=100,
    )
    second = await service.create_user(
        admin,
        username="second",
        password_hash=THIRD_PASSWORD,
        is_admin=False,
        is_initial_password=False,
        quota_bytes=100,
    )
    await service.delete_user(admin, user_id=first.user.id)
    await service.delete_user(admin, user_id=second.user.id)
    original = AccountRepository.hard_delete_user

    async def fail_first(self: AccountRepository, user_id: int) -> bool:
        if user_id == first.user.id:
            if failure == "domain":
                raise DomainError(ErrorCode.INVALID_STATE, "模拟清理冲突")
            if failure == "busy":
                raise DomainError(ErrorCode.CONTENT_BUSY, "模拟内容占用")
            raise RuntimeError("simulated repository failure")
        return await original(self, user_id)

    monkeypatch.setattr(AccountRepository, "hard_delete_user", fail_first)
    failed = await service.cleanup_pending_users()
    expected = (
        CleanupReport(deleted=1, busy=1, failed=0)
        if failure == "busy"
        else CleanupReport(deleted=1, busy=0, failed=1)
    )
    assert failed == expected
    async with service.uow_factory() as uow:
        assert await service.repository_factory(uow).get_user(first.user.id)

    monkeypatch.setattr(AccountRepository, "hard_delete_user", original)
    retried = await service.cleanup_pending_users()
    assert retried == CleanupReport(deleted=1, busy=0, failed=0)


async def test_deletion_worker_lets_failure_reach_supervisor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _app, service = await make_service(tmp_path)

    async def stop_after_first_round(_delay: float) -> None:
        raise RuntimeError("stop test worker")

    monkeypatch.setattr(asyncio, "sleep", stop_after_first_round)
    with pytest.raises(RuntimeError, match="stop test worker"):
        await service.run_deletion_worker()


async def test_service_guards_invalid_pagination_and_missing_session_id(tmp_path: Path) -> None:
    _app, service = await make_service(tmp_path)
    admin = await create_admin(service)
    for page, page_size in ((0, 20), (1, 101)):
        with pytest.raises(DomainError) as caught:
            await service.list_users(admin, page=page, page_size=page_size)
        assert caught.value.code is ErrorCode.INVALID_INPUT

    with pytest.raises(DomainError) as caught:
        await service.logout(Principal(admin.user, CredentialKind.SESSION, None))
    assert caught.value.code is ErrorCode.UNAUTHENTICATED
    view = await service.current_user(admin)
    assert service.available_bytes(view) == view.user.quota_bytes


async def test_concurrent_cleanup_of_the_same_user_is_idempotent(tmp_path: Path) -> None:
    _app, service = await make_service(tmp_path)
    admin = await create_admin(service)
    user = await service.create_user(
        admin,
        username="user",
        password_hash=OTHER_PASSWORD,
        is_admin=False,
        is_initial_password=False,
        quota_bytes=100,
    )
    await service.delete_user(admin, user_id=user.user.id)

    reports = await asyncio.gather(
        service.cleanup_pending_users(), service.cleanup_pending_users()
    )

    assert sum(report.deleted for report in reports) == 1
    assert sum(report.busy for report in reports) == 0
    assert sum(report.failed for report in reports) == 0


@pytest.mark.parametrize("repository_type", [DownloadRepository, ContentRepository])
async def test_domain_cleanup_treats_a_missing_user_as_already_finished(
    tmp_path: Path, repository_type: type[DownloadRepository] | type[ContentRepository]
) -> None:
    _app, service = await make_service(tmp_path)
    async with service.uow_factory() as uow:
        await repository_type(uow.connection).cleanup_user(user_id=999, now_ms=1)


async def test_pack_cleanup_steps_treat_a_missing_user_as_already_finished(
    tmp_path: Path,
) -> None:
    _app, service = await make_service(tmp_path)
    async with service.uow_factory() as uow:
        packs = PackRepository(uow.connection)
        assert await packs.list_user_job_ids(999) == []
        await packs.mark_user_jobs_cancelled(user_id=999, now_ms=1)
        await packs.delete_user_jobs_and_rebuild_quota(user_id=999)


async def test_download_stop_failure_keeps_pending_state_until_retry(tmp_path: Path) -> None:
    control = RecordingDownloadControl(failures=1)
    _app, service = await make_service(tmp_path, download_control=control)
    admin = await create_admin(service)
    user = await service.create_user(
        admin,
        username="user",
        password_hash=OTHER_PASSWORD,
        is_admin=False,
        is_initial_password=False,
        quota_bytes=1000,
    )
    async with service.uow_factory() as uow:
        downloads = DownloadRepository(uow.connection)
        source = await downloads.create_source(
            kind="http",
            resource_key="http:exclusive",
            payload_json="{}",
            options_json="{}",
            selected_file_indexes_json=None,
            display_name="task",
            display_uri=None,
            now_ms=1,
        )
        attempt = await downloads.create_attempt(
            source_id=source,
            resource_key="http:exclusive",
            gid="0123456789abcdef",
            status=AttemptStatus.ACTIVE,
            total_length=50,
            physical_reserved_bytes=50,
            now_ms=1,
        )
        await downloads.create_subscription(
            attempt_id=attempt,
            user_id=user.user.id,
            reserved_bytes=10,
            display_name="task",
            display_uri=None,
            now_ms=1,
        )
        await QuotaRepository(uow.connection).rebuild(user.user.id)
    await service.delete_user(admin, user_id=user.user.id)

    failed = await service.cleanup_pending_users()

    assert failed == CleanupReport(deleted=0, busy=0, failed=1)
    async with service.uow_factory() as uow:
        assert (await service.repository_factory(uow).get_user(user.user.id)).pending_delete
        assert (await DownloadRepository(uow.connection).get_attempt(attempt)).status is AttemptStatus.ACTIVE
        assert await DownloadRepository(uow.connection).count_live_subscriptions(attempt) == 1

    retried = await service.cleanup_pending_users()
    settled = await service.cleanup_pending_users()

    assert retried == CleanupReport(deleted=1, busy=0, failed=0)
    assert settled == CleanupReport(deleted=0, busy=0, failed=0)
    assert control.gids == ["0123456789abcdef", "0123456789abcdef"]


async def test_pack_stop_failure_keeps_pending_state_until_retry(tmp_path: Path) -> None:
    control = RecordingPackControl(stop_failures=1)
    _app, service = await make_service(tmp_path, pack_control=control)
    admin = await create_admin(service)
    user = await service.create_user(
        admin,
        username="user",
        password_hash=OTHER_PASSWORD,
        is_admin=False,
        is_initial_password=False,
        quota_bytes=1000,
    )
    async with service.uow_factory() as uow:
        pack_id = await PackRepository(uow.connection).create_job(
            owner_id=user.user.id,
            output_name="active.zip",
            pack_format="zip",
            source_size=20,
            reserved_bytes=30,
            physical_reserved_bytes=30,
            delete_source=False,
            now_ms=1,
        )
    await service.delete_user(admin, user_id=user.user.id)

    failed = await service.cleanup_pending_users()

    assert failed == CleanupReport(deleted=0, busy=0, failed=1)
    async with service.uow_factory() as uow:
        assert (await service.repository_factory(uow).get_user(user.user.id)).pending_delete
        assert (await PackRepository(uow.connection).get_job(pack_id)).status.value == "pending"

    retried = await service.cleanup_pending_users()
    settled = await service.cleanup_pending_users()

    assert retried == CleanupReport(deleted=1, busy=0, failed=0)
    assert settled == CleanupReport(deleted=0, busy=0, failed=0)
    assert control.stop_attempts == [pack_id, pack_id]


async def test_pending_delete_owner_cannot_create_a_pack(tmp_path: Path) -> None:
    """删除标记先提交时，同一 UoW 内的打包创建必须失败，不能绕过清理快照。"""
    app, service = await make_service(tmp_path)
    (tmp_path / "downloads").mkdir(parents=True, exist_ok=True)
    admin = await create_admin(service)
    user = await service.create_user(
        admin,
        username="user",
        password_hash=OTHER_PASSWORD,
        is_admin=False,
        is_initial_password=False,
        quota_bytes=100_000,
    )
    async with service.uow_factory() as uow:
        content = ContentRepository(uow.connection)
        content_id = await content.create_object(
            kind="file", digest="cd" * 32, size=10, storage_path="objects/source", now_ms=1
        )
        file_id = await content.add_user_file(
            user_id=user.user.id, content_id=content_id, display_name="source.bin", now_ms=1
        )
        await QuotaRepository(uow.connection).rebuild(user.user.id)
    await service.delete_user(admin, user_id=user.user.id)
    admission = app.state.pack_admission
    admission.settings = lambda: SystemSettings(
        pack_format="zip"
    )  # 未进 lifespan，运行配置未加载。

    with pytest.raises(DomainError) as caught:
        await admission.create(
            user.user.id,
            user_file_ids=[file_id],
            output_name="late.zip",
            delete_source=False,
        )
    assert caught.value.code is ErrorCode.CONTENT_PENDING_DELETE

    async with service.uow_factory() as uow:
        jobs = (await uow.connection.execute(text("SELECT COUNT(*) FROM pack_jobs"))).scalar_one()
        quota = await QuotaRepository(uow.connection).get(user.user.id)
    assert jobs == 0
    assert quota.reserved_bytes == 0


async def _create_cleanup_user(
    service: AccountService, *, quota_bytes: int = 1000
) -> tuple[Principal, int]:
    admin = await create_admin(service)
    user = await service.create_user(
        admin,
        username="user",
        password_hash=OTHER_PASSWORD,
        is_admin=False,
        is_initial_password=False,
        quota_bytes=quota_bytes,
    )
    return admin, user.user.id


async def _create_pack(service: AccountService, *, user_id: int, name: str) -> int:
    async with service.uow_factory() as uow:
        return await PackRepository(uow.connection).create_job(
            owner_id=user_id,
            output_name=name,
            pack_format="zip",
            source_size=20,
            reserved_bytes=30,
            physical_reserved_bytes=30,
            delete_source=False,
            now_ms=1,
        )


def _write_part(tmp_path: Path, pack_id: int, body: bytes = b"late") -> Path:
    parts = tmp_path / "downloads" / "packs"
    parts.mkdir(parents=True, exist_ok=True)
    part = parts / f"prepared-{pack_id}.part"
    part.write_bytes(body)
    return part


class StatusRecordingPreparedStore(RecordingPreparedStore):
    """删除 part 前用独立 UoW 复核状态：证明 cancelled 门闩已提交。"""

    service: AccountService

    async def remove_prepared(self, storage_path: str) -> None:
        pack_id = int(storage_path.removeprefix("packs/prepared-").removesuffix(".part"))
        async with self.service.uow_factory() as uow:
            job = await PackRepository(uow.connection).get_job(pack_id)
        self.events.append(f"status:{job.status.value}")
        await super().remove_prepared(storage_path)


async def test_user_cleanup_stops_all_packs_latches_before_removing_parts(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    control = RecordingPackControl(events)
    store = StatusRecordingPreparedStore(tmp_path / "downloads", events)
    _app, service = await make_service(tmp_path, pack_control=control, pack_prepared_store=store)
    store.service = service
    admin, user_id = await _create_cleanup_user(service)
    active = await _create_pack(service, user_id=user_id, name="active.zip")
    cancelled = await _create_pack(service, user_id=user_id, name="cancelled.zip")
    async with service.uow_factory() as uow:
        await uow.connection.execute(
            text("UPDATE pack_jobs SET status = 'cancelled' WHERE id = :id"),
            {"id": cancelled},
        )
    active_part = _write_part(tmp_path, active)
    cancelled_part = _write_part(tmp_path, cancelled)
    await service.delete_user(admin, user_id=user_id)

    report = await service.cleanup_pending_users()

    assert report == CleanupReport(deleted=1, busy=0, failed=0)
    # 全部未删除 ID 先停止，cancelled 门闩提交后才删 part，最后清控制状态。
    assert events == [
        f"stop:{active}",
        f"stop:{cancelled}",
        "status:cancelled",
        f"remove:packs/prepared-{active}.part",
        f"clear:{active}",
        "status:cancelled",
        f"remove:packs/prepared-{cancelled}.part",
        f"clear:{cancelled}",
    ]
    assert not active_part.exists() and not cancelled_part.exists()
    async with service.uow_factory() as uow:
        remaining = await uow.connection.execute(
            select(pack_jobs.c.id).where(pack_jobs.c.owner_id == user_id)
        )
    assert remaining.all() == []


async def test_user_cleanup_retries_a_part_removal_failure_after_the_latch(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    control = RecordingPackControl(events)
    store = RecordingPreparedStore(tmp_path / "downloads", events, failures=1)
    _app, service = await make_service(tmp_path, pack_control=control, pack_prepared_store=store)
    admin, user_id = await _create_cleanup_user(service)
    pack_id = await _create_pack(service, user_id=user_id, name="active.zip")
    part = _write_part(tmp_path, pack_id)
    await service.delete_user(admin, user_id=user_id)

    failed = await service.cleanup_pending_users()

    assert failed == CleanupReport(deleted=0, busy=0, failed=1)
    async with service.uow_factory() as uow:
        assert (await PackRepository(uow.connection).get_job(pack_id)).status is (
            PackStatus.CANCELLED
        )
    assert part.exists(), "part 删除失败时文件仍由下一轮接管"
    assert control.cleared == [], "part 未删完不得先清控制状态"

    retried = await service.cleanup_pending_users()

    assert retried == CleanupReport(deleted=1, busy=0, failed=0)
    assert not part.exists()
    assert control.stop_attempts == [pack_id, pack_id]
    assert control.cleared == [pack_id]
    assert events == [
        f"stop:{pack_id}",
        f"remove:packs/prepared-{pack_id}.part",
        f"stop:{pack_id}",
        f"remove:packs/prepared-{pack_id}.part",
        f"clear:{pack_id}",
    ]


async def test_user_cleanup_retries_a_control_clear_failure(tmp_path: Path) -> None:
    events: list[str] = []
    control = RecordingPackControl(events, clear_failures=1)
    store = RecordingPreparedStore(tmp_path / "downloads", events)
    _app, service = await make_service(tmp_path, pack_control=control, pack_prepared_store=store)
    admin, user_id = await _create_cleanup_user(service)
    pack_id = await _create_pack(service, user_id=user_id, name="active.zip")
    part = _write_part(tmp_path, pack_id)
    await service.delete_user(admin, user_id=user_id)

    failed = await service.cleanup_pending_users()

    assert failed == CleanupReport(deleted=0, busy=0, failed=1)
    assert not part.exists(), "控制状态清理失败不影响已完成的 part 清理"
    assert control.cleared == []

    retried = await service.cleanup_pending_users()

    assert retried == CleanupReport(deleted=1, busy=0, failed=0)
    assert control.stop_attempts == [pack_id, pack_id]
    assert control.cleared == [pack_id]
    async with service.uow_factory() as uow:
        assert (
            await uow.connection.execute(
                select(pack_jobs.c.id).where(pack_jobs.c.owner_id == user_id)
            )
        ).all() == []


async def test_user_cleanup_retries_after_the_final_delete_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    control = RecordingPackControl(events)
    store = RecordingPreparedStore(tmp_path / "downloads", events)
    _app, service = await make_service(tmp_path, pack_control=control, pack_prepared_store=store)
    admin, user_id = await _create_cleanup_user(service)
    pack_id = await _create_pack(service, user_id=user_id, name="active.zip")
    part = _write_part(tmp_path, pack_id)
    await service.delete_user(admin, user_id=user_id)

    original = PackRepository.delete_user_jobs_and_rebuild_quota
    failures = [1]

    async def flaky_delete(self: PackRepository, *, user_id: int) -> None:
        if failures[0]:
            failures[0] -= 1
            raise DomainError(ErrorCode.INVALID_STATE, "模拟最终删除失败")
        await original(self, user_id=user_id)

    monkeypatch.setattr(PackRepository, "delete_user_jobs_and_rebuild_quota", flaky_delete)

    failed = await service.cleanup_pending_users()

    assert failed == CleanupReport(deleted=0, busy=0, failed=1)
    async with service.uow_factory() as uow:
        assert (await PackRepository(uow.connection).get_job(pack_id)).status is (
            PackStatus.CANCELLED
        )
    assert not part.exists()

    retried = await service.cleanup_pending_users()

    assert retried == CleanupReport(deleted=1, busy=0, failed=0)
    # 前序步骤幂等重放：同一 ID 再次停止、删除不存在的 part、再次清理控制状态。
    assert control.stop_attempts == [pack_id, pack_id]
    assert control.cleared == [pack_id, pack_id]
    assert events == [
        f"stop:{pack_id}",
        f"remove:packs/prepared-{pack_id}.part",
        f"clear:{pack_id}",
        f"stop:{pack_id}",
        f"remove:packs/prepared-{pack_id}.part",
        f"clear:{pack_id}",
    ]
    async with service.uow_factory() as uow:
        assert (
            await uow.connection.execute(
                select(pack_jobs.c.id).where(pack_jobs.c.owner_id == user_id)
            )
        ).all() == []


async def test_account_cleanup_finishes_after_the_hard_delete_failed_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    control = RecordingPackControl(events)
    store = RecordingPreparedStore(tmp_path / "downloads", events)
    _app, service = await make_service(tmp_path, pack_control=control, pack_prepared_store=store)
    admin, user_id = await _create_cleanup_user(service)
    pack_id = await _create_pack(service, user_id=user_id, name="active.zip")
    part = _write_part(tmp_path, pack_id)
    await service.delete_user(admin, user_id=user_id)

    original = AccountRepository.hard_delete_user
    failures = [1]

    async def flaky_hard_delete(self: AccountRepository, user_id: int) -> bool:
        if failures[0]:
            failures[0] -= 1
            raise RuntimeError("模拟账户硬删除失败")
        return await original(self, user_id=user_id)

    monkeypatch.setattr(AccountRepository, "hard_delete_user", flaky_hard_delete)

    failed = await service.cleanup_pending_users()

    assert failed == CleanupReport(deleted=0, busy=0, failed=1)
    async with service.uow_factory() as uow:
        assert (await service.repository_factory(uow).get_user(user_id)).pending_delete
        assert (
            await uow.connection.execute(
                select(pack_jobs.c.id).where(pack_jobs.c.owner_id == user_id)
            )
        ).all() == []
    assert not part.exists()
    assert control.cleared == [pack_id]
    settled_events = list(events)

    retried = await service.cleanup_pending_users()

    assert retried == CleanupReport(deleted=1, busy=0, failed=0)
    assert events == settled_events, "现场已清完，下一轮只补账户硬删除"


class GatedPreparedStore(RecordingPreparedStore):
    """指定任务的首次 part 删除前暂停，用来构造「旧快照 + 复用 ID」竞态。"""

    def __init__(self, root: Path, events: list[str] | None = None) -> None:
        super().__init__(root, events)
        self.gated_task: asyncio.Task[object] | None = None
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def remove_prepared(self, storage_path: str) -> None:
        if self.gated_task is not None and asyncio.current_task() is self.gated_task:
            self.gated_task = None
            self.entered.set()
            await self.release.wait()
        await super().remove_prepared(storage_path)


async def test_concurrent_cleanup_serializes_rounds_before_id_reuse(tmp_path: Path) -> None:
    """旧行仍在时，并发轮次必须串行；真正同 ID 复用由最终删除屏障测试负责。"""
    events: list[str] = []
    control = RecordingPackControl(events)
    store = GatedPreparedStore(tmp_path / "downloads", events)
    _app, service = await make_service(tmp_path, pack_control=control, pack_prepared_store=store)
    admin, user_id = await _create_cleanup_user(service)
    other = await service.create_user(
        admin,
        username="other",
        password_hash=THIRD_PASSWORD,
        is_admin=False,
        is_initial_password=False,
        quota_bytes=1000,
    )
    old_pack = await _create_pack(service, user_id=user_id, name="old.zip")
    _write_part(tmp_path, old_pack, body=b"old")
    await service.delete_user(admin, user_id=user_id)

    task_b = asyncio.create_task(service.cleanup_pending_users())
    store.gated_task = task_b
    # B 已按旧用户列出全部 ID，并停在首次删除 part 之前。
    await asyncio.wait_for(store.entered.wait(), timeout=5.0)
    task_a = asyncio.create_task(service.cleanup_pending_users())
    # 未串行化时 A 能在 B 暂停期间删完旧记录；串行化后 A 必须等 B 释放，
    # 因此这里只能有上限地观察 A，不能无条件等待。
    await asyncio.wait({task_a}, timeout=2.0)
    new_pack = await _create_pack(service, user_id=other.user.id, name="new.zip")
    assert new_pack != old_pack, "旧行未删除前，新建任务不可能复用同一 ID"
    new_part = _write_part(tmp_path, new_pack, body=b"new")

    store.release.set()
    await task_b
    await task_a

    assert new_part.exists(), "并发清理不得按旧快照删除其他用户的 part"
    assert new_part.read_bytes() == b"new"
    assert new_pack not in control.cleared, "不得清理新任务的控制状态"
    assert f"clear:{new_pack}" not in events
    async with service.uow_factory() as uow:
        assert (await PackRepository(uow.connection).get_job(new_pack)).status is PackStatus.PENDING
        with pytest.raises(DomainError) as caught:
            await service.repository_factory(uow).get_user(user_id)
    assert caught.value.code is ErrorCode.NOT_FOUND


class RecordingPackWriterControl:
    """记录账户清理的控制状态调用，并委托真实控制管理 cancel_event。"""

    def __init__(self, inner: PackWriterControl, events: list[str]) -> None:
        self.inner = inner
        self.events = events

    async def stop_and_wait(self, pack_id: int) -> None:
        self.events.append(f"stop:{pack_id}")
        await self.inner.stop_and_wait(pack_id)

    def clear(self, pack_id: int) -> None:
        self.events.append(f"clear:{pack_id}")
        self.inner.clear(pack_id)


class DeleteBarrier:
    """最终删除提交后、账户清理仍持锁时暂停，用来构造真实 ID 复用窗口。"""

    def __init__(self) -> None:
        self.armed = False
        self.committed = asyncio.Event()
        self.release = asyncio.Event()


class BarrieredUnitOfWork:
    """包装真实 UoW：让指定轮次的最终删除在提交后暂停。"""

    def __init__(self, inner: SQLiteUnitOfWork, barrier: DeleteBarrier) -> None:
        self.inner = inner
        self.barrier = barrier

    @property
    def connection(self) -> AsyncConnection:
        return self.inner.connection

    async def __aenter__(self) -> BarrieredUnitOfWork:
        await self.inner.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.inner.__aexit__(exc_type, exc, tb)
        if exc_type is not None or not self.barrier.armed:
            return
        self.barrier.armed = False
        self.barrier.committed.set()
        await self.barrier.release.wait()


async def test_reused_pack_id_after_final_delete_keeps_new_round_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    inner_control = PackWriterControl()
    control = RecordingPackWriterControl(inner_control, events)
    store = RecordingPreparedStore(tmp_path / "downloads", events)
    _app, service = await make_service(tmp_path, pack_control=control, pack_prepared_store=store)
    pack_cleanup = service.cleanup_ports[1]
    assert isinstance(pack_cleanup, PackUserCleanup)

    barrier = DeleteBarrier()
    original_delete = PackRepository.delete_user_jobs_and_rebuild_quota

    async def delete_then_arm(self: PackRepository, *, user_id: int) -> None:
        await original_delete(self, user_id=user_id)
        barrier.armed = True

    monkeypatch.setattr(PackRepository, "delete_user_jobs_and_rebuild_quota", delete_then_arm)
    inner_uow_factory = pack_cleanup.uow_factory
    pack_cleanup.uow_factory = lambda: BarrieredUnitOfWork(inner_uow_factory(), barrier)

    admin, user_id = await _create_cleanup_user(service)
    other = await service.create_user(
        admin,
        username="other",
        password_hash=THIRD_PASSWORD,
        is_admin=False,
        is_initial_password=False,
        quota_bytes=1000,
    )
    old_pack = await _create_pack(service, user_id=user_id, name="old.zip")
    old_part = _write_part(tmp_path, old_pack, body=b"old")
    await service.delete_user(admin, user_id=user_id)

    task_b = asyncio.create_task(service.cleanup_pending_users())
    await asyncio.wait_for(barrier.committed.wait(), timeout=5.0)
    assert not old_part.exists(), "B 已删除旧 part 并提交删除"

    task_a = asyncio.create_task(service.cleanup_pending_users())
    await asyncio.wait({task_a}, timeout=0.2)
    assert not task_a.done(), "A 必须在 B 仍持锁时等待，不能沿用旧快照"

    # 旧行已删，同一 ID 会被真实复用给另一个用户。
    new_pack = await _create_pack(service, user_id=other.user.id, name="new.zip")
    assert new_pack == old_pack, "删除后必须能观察到真实 ID 复用"
    new_part = _write_part(tmp_path, new_pack, body=b"new")
    new_event = threading.Event()
    writer_task = asyncio.create_task(asyncio.sleep(60))
    assert inner_control.register(new_pack, writer_task, new_event) is True

    barrier.release.set()
    assert await task_b == CleanupReport(deleted=1, busy=0, failed=0)
    assert (await task_a).failed == 0

    assert new_part.exists() and new_part.read_bytes() == b"new"
    assert not new_event.is_set(), "旧轮次不得请求取消新任务"
    assert inner_control.allows_resume(new_pack) is False, "新控制记录不得被旧轮次清理"
    assert events == [
        f"stop:{old_pack}",
        f"remove:packs/prepared-{old_pack}.part",
        f"clear:{old_pack}",
    ]
    async with service.uow_factory() as uow:
        assert (await PackRepository(uow.connection).get_job(new_pack)).status is PackStatus.PENDING
        with pytest.raises(DomainError) as caught:
            await service.repository_factory(uow).get_user(user_id)
    assert caught.value.code is ErrorCode.NOT_FOUND

    writer_task.cancel()
    await asyncio.gather(writer_task, return_exceptions=True)
    inner_control.clear(new_pack)


class GatedFailureDispositionStore(DownloadContentStore):
    """旧任务失败收尾删 part 前暂停，并记录账户清理同路径删除时看到的状态。"""

    def __init__(self, root: Path, database: SQLiteDatabase) -> None:
        super().__init__(root)
        self.database_path = database.path
        self.gated_pack_id: int | None = None
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.gate_passed = False
        self.cleanup_saw: list[tuple[str, int]] = []

    async def remove_prepared(self, storage_path: str) -> None:
        if (
            self.gated_pack_id is not None
            and storage_path == f"packs/prepared-{self.gated_pack_id}.part"
        ):
            if not self.gate_passed:
                self.gate_passed = True
                self.entered.set()
                await self.release.wait()
            else:
                self.cleanup_saw.append(await self._job_state(self.gated_pack_id))
        await super().remove_prepared(storage_path)

    async def _job_state(self, pack_id: int) -> tuple[str, int]:
        database = SQLiteDatabase(self.database_path)
        try:
            async with SQLiteUnitOfWork(database) as uow:
                try:
                    job = await PackRepository(uow.connection).get_job(pack_id)
                except DomainError:
                    return ("missing", 0)
                return (job.status.value, job.reserved_bytes)
        finally:
            await database.close()


class DeterministicFailureThenVisibleWriter:
    """首个任务在压缩阶段确定性失败；后续真实写入并在 part 落盘后暂停。"""

    def __init__(self) -> None:
        self.inner = ArchiveWriter()
        self.calls = 0
        self.part_visible = asyncio.Event()
        self.release = asyncio.Event()

    async def write_archive(
        self,
        destination: Path,
        *,
        sources,
        pack_format,
        compression_level,
        max_output_bytes,
        min_free_bytes,
        cancel_event,
        on_progress=None,
    ):
        self.calls += 1
        if self.calls == 1:
            raise DomainError(ErrorCode.TASK_TOO_LARGE, "模拟确定性超预算")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"in-flight")
        self.part_visible.set()
        await self.release.wait()
        return await self.inner.write_archive(
            destination,
            sources=sources,
            pack_format=pack_format,
            compression_level=compression_level,
            max_output_bytes=max_output_bytes,
            min_free_bytes=min_free_bytes,
            cancel_event=cancel_event,
            on_progress=on_progress,
        )


async def test_account_cleanup_waits_for_failure_disposition_before_id_reuse(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    inner_control = PackWriterControl(stop_timeout_seconds=5.0)
    control = RecordingPackWriterControl(inner_control, events)
    root = tmp_path / "downloads"
    database = SQLiteDatabase(tmp_path / "data" / "v2.db")
    store = GatedFailureDispositionStore(root, database)
    _app, service = await make_service(tmp_path, pack_control=control, pack_prepared_store=store)
    admin, user_id = await _create_cleanup_user(service, quota_bytes=1_000_000)
    other = await service.create_user(
        admin,
        username="other",
        password_hash=THIRD_PASSWORD,
        is_admin=False,
        is_initial_password=False,
        quota_bytes=1_000_000,
    )
    clock = FakeClock(1_000)
    admission = make_admission(database, clock, store)
    _, first = await seed_pack_file(database, root, user_id, 1, payload=b"a" * 10)
    old_pack = await admission.create(
        user_id, user_file_ids=[first], output_name="old.zip", delete_source=False
    )
    frozen = await get_job(database, old_pack)
    await service.delete_user(admin, user_id=user_id)

    store.gated_pack_id = old_pack
    writer = DeterministicFailureThenVisibleWriter()
    dispatcher = make_dispatcher(database, clock, store, writer=writer, control=inner_control)
    stepping = asyncio.create_task(dispatcher.step_once())
    await asyncio.wait_for(store.entered.wait(), timeout=5.0)
    assert (await get_job(database, old_pack)).status is PackStatus.PACKING

    cleanup = asyncio.create_task(service.cleanup_pending_users())
    await asyncio.wait({cleanup}, timeout=0.3)
    assert not cleanup.done(), "账户清理必须等待失败收尾，不能先删行让 ID 被复用"
    async with service.uow_factory() as uow:
        waiting = await PackRepository(uow.connection).get_job(old_pack)
    assert waiting.status is PackStatus.PACKING, "失败收尾未完成前旧行必须仍在"
    assert waiting.reserved_bytes == frozen.reserved_bytes

    store.release.set()
    await asyncio.wait_for(stepping, timeout=5.0)
    assert await asyncio.wait_for(cleanup, timeout=5.0) == CleanupReport(
        deleted=1, busy=0, failed=0
    )
    assert store.cleanup_saw == [("failed", 0)], "清理看到的必须是已收尾的失败任务"

    _, other_file = await seed_pack_file(database, root, other.user.id, 2, payload=b"b" * 7)
    new_pack = await admission.create(
        other.user.id, user_file_ids=[other_file], output_name="new.zip", delete_source=False
    )
    assert new_pack == old_pack, "旧行删除后同一 ID 必须真实复用于另一个用户"
    new_frozen = await get_job(database, new_pack)
    new_part = root / "packs" / f"prepared-{new_pack}.part"

    claiming = asyncio.create_task(dispatcher.step_once())
    await asyncio.wait_for(writer.part_visible.wait(), timeout=5.0)
    assert new_part.read_bytes() == b"in-flight", "新任务的 part 不受旧失败收尾影响"
    listing = await get_job(database, new_pack)
    assert listing.status is PackStatus.PACKING
    assert listing.prepared is None
    assert listing.reserved_bytes == new_frozen.reserved_bytes
    async with service.uow_factory() as uow:
        quota = await QuotaRepository(uow.connection).get(other.user.id)
    assert quota.reserved_bytes == new_frozen.reserved_bytes, "新任务的预留不受旧失败收尾影响"
    assert inner_control.allows_resume(new_pack) is False, "新控制记录不得被旧轮次清理"

    writer.release.set()
    await asyncio.wait_for(claiming, timeout=5.0)
    assert (await get_job(database, new_pack)).status is PackStatus.DONE
    assert not new_part.exists()

    _, third = await seed_pack_file(database, root, other.user.id, 3, payload=b"c" * 5)
    later = await admission.create(
        other.user.id, user_file_ids=[third], output_name="later.zip", delete_source=False
    )
    await asyncio.wait_for(dispatcher.step_once(), timeout=5.0)
    assert (await get_job(database, later)).status is PackStatus.DONE
