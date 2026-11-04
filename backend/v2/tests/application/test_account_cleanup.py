from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi import FastAPI
from sqlalchemy import select, text

from aria2deck.application.accounts.models import CleanupReport, Principal
from aria2deck.application.accounts.service import AccountService
from aria2deck.application.downloads.cleanup import DownloadControlPort, DownloadUserCleanup
from aria2deck.application.packs.cleanup import PackControlPort, PackUserCleanup
from aria2deck.bootstrap import create_app
from aria2deck.domain.accounts import CredentialKind
from aria2deck.domain.content import ContentDeletionState
from aria2deck.domain.downloads import AttemptStatus
from aria2deck.domain.errors import DomainError, ErrorCode
from aria2deck.domain.packs import PackSourceSnapshot
from aria2deck.infrastructure.config import Settings
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
    def __init__(self, *, failures: int = 0) -> None:
        self.pack_ids: list[int] = []
        self.failures = failures

    async def stop_and_wait(self, pack_id: int) -> None:
        self.pack_ids.append(pack_id)
        if self.failures:
            self.failures -= 1
            raise DomainError(ErrorCode.INVALID_STATE, "模拟打包停止失败")


async def make_service(
    tmp_path: Path,
    *,
    download_control: DownloadControlPort | None = None,
    pack_control: PackControlPort | None = None,
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
    assert pack_control.pack_ids == [active_pack]
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



@pytest.mark.parametrize("repository_type", [DownloadRepository, PackRepository, ContentRepository])
async def test_domain_cleanup_treats_a_missing_user_as_already_finished(
    tmp_path: Path, repository_type: type[DownloadRepository] | type[PackRepository] | type[ContentRepository]
) -> None:
    _app, service = await make_service(tmp_path)
    async with service.uow_factory() as uow:
        await repository_type(uow.connection).cleanup_user(user_id=999, now_ms=1)


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
    control = RecordingPackControl(failures=1)
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
    assert control.pack_ids == [pack_id, pack_id]
