from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import delete, insert, select, text, update

from aria2deck.application.downloads.models import ManifestEntry
from aria2deck.domain.downloads import (
    LIVE_ATTEMPT_STATUSES,
    AttemptStatus,
    SubscriptionStatus,
)
from aria2deck.domain.errors import DomainError, ErrorCode
from aria2deck.domain.quota import PhysicalDownload
from aria2deck.infrastructure.sqlite.bootstrap import initialize_database
from aria2deck.infrastructure.sqlite.engine import SQLiteDatabase
from aria2deck.infrastructure.sqlite.repositories.content import ContentRepository
from aria2deck.infrastructure.sqlite.repositories.downloads import DownloadRepository
from aria2deck.infrastructure.sqlite.repositories.quota import QuotaRepository
from aria2deck.infrastructure.sqlite.schema import (
    attempt_manifests,
    content_objects,
    download_attempts,
    quota_accounts,
    task_subscriptions,
)
from aria2deck.infrastructure.sqlite.unit_of_work import SQLiteUnitOfWork

from .test_accounts_quota import create_user

GID = "0123456789abcdef"


async def source(database: SQLiteDatabase, key: str = "http:key") -> int:
    async with SQLiteUnitOfWork(database) as uow:
        return await DownloadRepository(uow.connection).create_source(
            kind="http", resource_key=key, payload_json='{"uri":"https://example.com/private"}',
            options_json="{}", selected_file_indexes_json=None, display_name="file", display_uri="https://example.com/file", now_ms=1,
        )


async def test_download_source_payload_is_separate_from_attempt_and_subscription(database: SQLiteDatabase) -> None:
    user_id = await create_user(database)
    source_id = await source(database)
    async with SQLiteUnitOfWork(database) as uow:
        repo = DownloadRepository(uow.connection)
        attempt_id = await repo.create_attempt(source_id=source_id, resource_key="http:key", gid=GID, now_ms=1)
        subscription_id = await repo.create_subscription(attempt_id=attempt_id, user_id=user_id, reserved_bytes=10, display_name="file", display_uri=None, now_ms=1)
        attempt = await repo.get_attempt(attempt_id)
    assert attempt.resource_key == "http:key"
    assert attempt.status is AttemptStatus.QUEUED
    assert subscription_id > 0
    attempt_columns = set(download_attempts.c.keys())
    subscription_columns = set(task_subscriptions.c.keys())
    assert "uri" not in attempt_columns | subscription_columns
    assert "payload_json" not in attempt_columns | subscription_columns


async def test_live_resource_is_unique_and_terminal_retry_gets_a_new_attempt(database: SQLiteDatabase) -> None:
    source_id = await source(database)
    async with SQLiteUnitOfWork(database) as uow:
        repo = DownloadRepository(uow.connection)
        first_id = await repo.create_attempt(source_id=source_id, resource_key="http:key", gid=GID, now_ms=1)
        with pytest.raises(DomainError):
            await repo.create_attempt(source_id=source_id, resource_key="http:key", gid="fedcba0987654321", now_ms=2)
        assert (await repo.find_live_attempt("http:key")).id == first_id
        await uow.connection.execute(update(download_attempts).where(download_attempts.c.id == first_id).values(status=AttemptStatus.FAILED))
        second_id = await repo.create_attempt(source_id=source_id, resource_key="http:key", gid="fedcba0987654321", now_ms=3)
        assert second_id != first_id
        assert (await repo.find_live_attempt("http:key")).id == second_id


async def test_gid_and_subscription_identities_are_unique(database: SQLiteDatabase) -> None:
    first_user = await create_user(database)
    second_user = await create_user(database, "second", admin=False)
    first_source = await source(database)
    second_source = await source(database, "http:other")
    async with SQLiteUnitOfWork(database) as uow:
        repo = DownloadRepository(uow.connection)
        first = await repo.create_attempt(source_id=first_source, resource_key="http:key", gid=GID, now_ms=1)
        with pytest.raises(DomainError):
            await repo.create_attempt(source_id=second_source, resource_key="http:other", gid=GID, now_ms=1)
        await repo.create_subscription(attempt_id=first, user_id=first_user, reserved_bytes=1, display_name="a", display_uri=None, now_ms=1)
        await repo.create_subscription(attempt_id=first, user_id=second_user, reserved_bytes=1, display_name="a", display_uri=None, now_ms=1)
        assert await repo.count_live_subscriptions(first) == 2
        with pytest.raises(DomainError):
            await repo.create_subscription(attempt_id=first, user_id=first_user, reserved_bytes=1, display_name="a", display_uri=None, now_ms=1)


async def test_attempt_source_identity_is_protected_by_composite_foreign_key(database: SQLiteDatabase) -> None:
    source_id = await source(database)
    async with SQLiteUnitOfWork(database) as uow:
        with pytest.raises(DomainError):
            await DownloadRepository(uow.connection).create_attempt(source_id=source_id, resource_key="http:wrong", gid=GID, now_ms=1)


async def test_concurrent_attempt_creation_has_exactly_one_live_owner(database: SQLiteDatabase) -> None:
    source_id = await source(database)

    async def create(number: int) -> bool:
        try:
            async with SQLiteUnitOfWork(database) as uow:
                await DownloadRepository(uow.connection).create_attempt(source_id=source_id, resource_key="http:key", gid=f"{number:016x}", now_ms=number)
            return True
        except DomainError:
            return False

    results = await asyncio.gather(*(create(number) for number in range(12)))
    assert results.count(True) == 1


async def test_source_duplicate_and_missing_attempt_are_stable(database: SQLiteDatabase) -> None:
    await source(database)
    with pytest.raises(DomainError):
        await source(database)
    async with SQLiteUnitOfWork(database) as uow:
        repo = DownloadRepository(uow.connection)
        assert await repo.find_live_attempt("missing") is None
        with pytest.raises(DomainError) as caught:
            await repo.get_attempt(999)
    assert caught.value.code is ErrorCode.NOT_FOUND



async def test_completed_content_supports_instant_attachment_after_reopen(
    database: SQLiteDatabase,
) -> None:
    user_id = await create_user(database)
    source_id = await source(database)
    async with SQLiteUnitOfWork(database) as uow:
        downloads = DownloadRepository(uow.connection)
        content = ContentRepository(uow.connection)
        content_id = await content.create_object(
            kind="file", digest="ab" * 32, size=10, storage_path="objects/done", now_ms=1
        )
        attempt_id = await downloads.create_attempt(
            source_id=source_id, resource_key="http:key", gid=GID, now_ms=1
        )
        subscription_id = await downloads.create_subscription(
            attempt_id=attempt_id,
            user_id=user_id,
            reserved_bytes=10,
            display_name="file",
            display_uri=None,
            now_ms=1,
            status=SubscriptionStatus.WAITING,
        )
    await database.close()

    reopened = SQLiteDatabase(database.path)
    async with SQLiteUnitOfWork(reopened) as uow:
        downloads = DownloadRepository(uow.connection)
        assert (await downloads.get_attempt(attempt_id)).status is AttemptStatus.QUEUED
        await downloads.complete_attempt(attempt_id, content_id=content_id, now_ms=2)
        assert (await downloads.get_attempt(attempt_id)).completed_content_id == content_id
        task = await downloads.get_task(user_id=user_id, task_id=subscription_id)
        assert task.completed_content_id == content_id
        completed_content_id = await downloads.find_completed_content_id("http:key")
        assert completed_content_id == content_id
        file_id = await ContentRepository(uow.connection).add_user_file(
            user_id=user_id,
            content_id=completed_content_id,
            display_name="instant",
            now_ms=3,
        )
        assert file_id > 0
        status = (
            await uow.connection.execute(
                select(task_subscriptions.c.status).where(
                    task_subscriptions.c.id == subscription_id
                )
            )
        ).scalar_one()
        assert status == SubscriptionStatus.COMPLETED
    await reopened.close()


async def test_terminal_attempts_keep_independent_completed_content_links(
    database: SQLiteDatabase,
) -> None:
    source_id = await source(database)
    async with SQLiteUnitOfWork(database) as uow:
        downloads = DownloadRepository(uow.connection)
        content = ContentRepository(uow.connection)
        first_content = await content.create_object(
            kind="file", digest="ab" * 32, size=10, storage_path="objects/first", now_ms=1
        )
        second_content = await content.create_object(
            kind="file", digest="cd" * 32, size=20, storage_path="objects/second", now_ms=1
        )
        first = await downloads.create_attempt(
            source_id=source_id, resource_key="http:key", gid=GID, now_ms=1
        )
        await downloads.complete_attempt(first, content_id=first_content, now_ms=2)
        second = await downloads.create_attempt(
            source_id=source_id,
            resource_key="http:key",
            gid="fedcba0987654321",
            now_ms=3,
        )
        await downloads.complete_attempt(second, content_id=second_content, now_ms=4)

        assert (await downloads.get_attempt(first)).completed_content_id == first_content
        assert (await downloads.get_attempt(second)).completed_content_id == second_content
        assert await downloads.find_completed_content_id("http:key") == second_content


async def test_physical_content_delete_preserves_history_but_disables_instant_attachment(
    database: SQLiteDatabase,
) -> None:
    source_id = await source(database)
    async with SQLiteUnitOfWork(database) as uow:
        downloads = DownloadRepository(uow.connection)
        content_id = await ContentRepository(uow.connection).create_object(
            kind="file", digest="ab" * 32, size=10, storage_path="objects/done", now_ms=1
        )
        attempt_id = await downloads.create_attempt(
            source_id=source_id, resource_key="http:key", gid=GID, now_ms=1
        )
        await downloads.complete_attempt(attempt_id, content_id=content_id, now_ms=2)
        await uow.connection.execute(
            delete(content_objects).where(content_objects.c.id == content_id)
        )

        attempt = await downloads.get_attempt(attempt_id)
        assert attempt.status is AttemptStatus.COMPLETED
        assert attempt.completed_content_id is None
        assert await downloads.find_completed_content_id("http:key") is None


async def test_completion_reaches_shared_waiting_and_paused_subscriptions(
    database: SQLiteDatabase,
) -> None:
    first_user = await create_user(database)
    second_user = await create_user(database, "second", admin=False)
    source_id = await source(database)
    async with SQLiteUnitOfWork(database) as uow:
        downloads = DownloadRepository(uow.connection)
        content_id = await ContentRepository(uow.connection).create_object(
            kind="file", digest="ab" * 32, size=10, storage_path="objects/done", now_ms=1
        )
        attempt_id = await downloads.create_attempt(
            source_id=source_id,
            resource_key="http:key",
            gid=GID,
            now_ms=1,
            status=AttemptStatus.PAUSED,
        )
        for user_id, status in (
            (first_user, SubscriptionStatus.WAITING),
            (second_user, SubscriptionStatus.PAUSED),
        ):
            await downloads.create_subscription(
                attempt_id=attempt_id,
                user_id=user_id,
                reserved_bytes=10,
                display_name="file",
                display_uri=None,
                now_ms=1,
                status=status,
            )
        await downloads.complete_attempt(attempt_id, content_id=content_id, now_ms=2)

        statuses = (
            (await uow.connection.execute(
                select(task_subscriptions.c.status)
                .where(task_subscriptions.c.attempt_id == attempt_id)
                .order_by(task_subscriptions.c.id)
            ))
            .scalars()
            .all()
        )
        assert statuses == [SubscriptionStatus.COMPLETED, SubscriptionStatus.COMPLETED]
        assert await downloads.count_live_subscriptions(attempt_id) == 0



async def test_completion_is_idempotent_and_rejects_missing_or_changed_content(
    database: SQLiteDatabase,
) -> None:
    source_id = await source(database)
    async with SQLiteUnitOfWork(database) as uow:
        downloads = DownloadRepository(uow.connection)
        content = ContentRepository(uow.connection)
        first_content = await content.create_object(
            kind="file", digest="ab" * 32, size=10, storage_path="objects/first", now_ms=1
        )
        second_content = await content.create_object(
            kind="file", digest="cd" * 32, size=10, storage_path="objects/second", now_ms=1
        )
        attempt_id = await downloads.create_attempt(
            source_id=source_id, resource_key="http:key", gid=GID, now_ms=1
        )
        with pytest.raises(DomainError) as caught:
            await downloads.complete_attempt(attempt_id, content_id=999, now_ms=2)
        assert caught.value.code is ErrorCode.NOT_FOUND
        assert (await downloads.get_attempt(attempt_id)).status is AttemptStatus.QUEUED

        await downloads.complete_attempt(attempt_id, content_id=first_content, now_ms=3)
        await downloads.complete_attempt(attempt_id, content_id=first_content, now_ms=4)
        with pytest.raises(DomainError) as caught:
            await downloads.complete_attempt(attempt_id, content_id=second_content, now_ms=5)
        assert caught.value.code is ErrorCode.INVALID_STATE
        assert (await downloads.get_attempt(attempt_id)).completed_content_id == first_content



async def test_user_cleanup_fails_if_attempt_cancellation_is_not_persisted(
    database: SQLiteDatabase,
) -> None:
    user_id = await create_user(database)
    source_id = await source(database)
    async with SQLiteUnitOfWork(database) as uow:
        downloads = DownloadRepository(uow.connection)
        attempt_id = await downloads.create_attempt(
            source_id=source_id, resource_key="http:key", gid=GID, now_ms=1
        )
        await downloads.create_subscription(
            attempt_id=attempt_id,
            user_id=user_id,
            reserved_bytes=10,
            display_name="file",
            display_uri=None,
            now_ms=1,
        )
        await QuotaRepository(uow.connection).reserve(user_id, 10)
        await uow.connection.execute(
            text(
                "CREATE TRIGGER test_ignore_attempt_cancellation "
                "BEFORE UPDATE OF status ON download_attempts "
                "WHEN NEW.status = 'cancelled' BEGIN SELECT RAISE(IGNORE); END"
            )
        )
        with pytest.raises(DomainError) as caught:
            await downloads.cleanup_user(user_id=user_id, now_ms=2)
        assert caught.value.code is ErrorCode.INVALID_STATE



async def test_user_cleanup_deletes_terminal_history_without_releasing_again(
    database: SQLiteDatabase,
) -> None:
    user_id = await create_user(database)
    source_id = await source(database)
    async with SQLiteUnitOfWork(database) as uow:
        downloads = DownloadRepository(uow.connection)
        attempt_id = await downloads.create_attempt(
            source_id=source_id,
            resource_key="http:key",
            gid=GID,
            status=AttemptStatus.FAILED,
            now_ms=1,
        )
        await downloads.create_subscription(
            attempt_id=attempt_id,
            user_id=user_id,
            reserved_bytes=0,
            display_name="file",
            display_uri=None,
            status=SubscriptionStatus.FAILED,
            now_ms=1,
        )
        await downloads.cleanup_user(user_id=user_id, now_ms=2)

        assert (
            await uow.connection.execute(
                select(task_subscriptions.c.id).where(task_subscriptions.c.user_id == user_id)
            )
        ).first() is None
        assert (await downloads.get_attempt(attempt_id)).status is AttemptStatus.FAILED
        assert (await QuotaRepository(uow.connection).get(user_id)).reserved_bytes == 0


async def test_download_reads_map_nullable_fields_and_enforce_user_scope(
    database: SQLiteDatabase,
) -> None:
    first_user = await create_user(database)
    second_user = await create_user(database, "second", admin=False)
    async with SQLiteUnitOfWork(database) as uow:
        repository = DownloadRepository(uow.connection)
        first_source = await repository.create_source(
            kind="http",
            resource_key="http:first",
            payload_json="{}",
            options_json="{}",
            selected_file_indexes_json=None,
            display_name="first",
            display_uri=None,
            now_ms=1,
        )
        first_attempt = await repository.create_attempt(
            source_id=first_source,
            resource_key="http:first",
            gid=GID,
            now_ms=1,
        )
        first_task = await repository.create_subscription(
            attempt_id=first_attempt,
            user_id=first_user,
            reserved_bytes=0,
            display_name="first",
            display_uri=None,
            now_ms=1,
        )
        second_source = await repository.create_source(
            kind="http",
            resource_key="http:second",
            payload_json="{}",
            options_json="{}",
            selected_file_indexes_json=None,
            display_name="second",
            display_uri="https://example.com/second",
            now_ms=2,
        )
        second_attempt = await repository.create_attempt(
            source_id=second_source,
            resource_key="http:second",
            gid="fedcba0987654321",
            total_length=10,
            now_ms=2,
        )
        second_task = await repository.create_subscription(
            attempt_id=second_attempt,
            user_id=first_user,
            reserved_bytes=5,
            display_name="second",
            display_uri="https://example.com/second",
            now_ms=2,
        )

        records = await repository.list_tasks(user_id=first_user)
        detail = await repository.get_task(user_id=first_user, task_id=first_task)

        assert [item.id for item in records] == [second_task, first_task]
        assert detail.display_uri is None
        assert detail.total_bytes is None
        assert records[0].total_bytes == 10
        assert records[0].display_uri == "https://example.com/second"
        with pytest.raises(DomainError) as caught:
            await repository.get_task(user_id=second_user, task_id=first_task)
        assert caught.value.code is ErrorCode.NOT_FOUND
        with pytest.raises(DomainError):
            await repository.get_task(user_id=first_user, task_id=999)


async def test_ingestion_start_is_persistent_idempotent_and_a_recovery_candidate(
    database: SQLiteDatabase,
) -> None:
    source_id = await source(database)
    async with SQLiteUnitOfWork(database) as uow:
        repository = DownloadRepository(uow.connection)
        attempt_id = await repository.create_attempt(
            source_id=source_id, resource_key="http:key", gid=GID, now_ms=7
        )
        await repository.start_ingestion(attempt_id, expected_gid=GID, now_ms=11)
        await repository.start_ingestion(attempt_id, expected_gid=GID, now_ms=19)

        attempt = await repository.get_attempt(attempt_id)
        assert attempt.created_at_ms == 7
        assert attempt.ingestion_started_at_ms == 11
        assert [item.id for item in await repository.list_completion_intents()] == [attempt_id]
        assert (await repository.get_submission(attempt_id)).created_at_ms == 7


async def test_manifest_round_trips_and_the_first_write_wins(
    database: SQLiteDatabase,
) -> None:
    source_id = await source(database)
    async with SQLiteUnitOfWork(database) as uow:
        repository = DownloadRepository(uow.connection)
        attempt_id = await repository.create_attempt(
            source_id=source_id, resource_key="http:key", gid=GID, now_ms=1
        )
        assert await repository.get_manifest(attempt_id) is None
        await repository.record_manifest(
            attempt_id,
            entries=(ManifestEntry("first.bin", 3), ManifestEntry("子目录/second.bin", 5)),
            now_ms=2,
        )
    # 换事务重读：读到的是落库的数据，不是调用方的内存对象
    async with SQLiteUnitOfWork(database) as uow:
        repository = DownloadRepository(uow.connection)
        expected = (ManifestEntry("first.bin", 3), ManifestEntry("子目录/second.bin", 5))
        assert await repository.get_manifest(attempt_id) == expected
        await repository.record_manifest(
            attempt_id, entries=(ManifestEntry("late.bin", 9),), now_ms=3
        )
        # 后到的观测不覆盖已有清单
        assert await repository.get_manifest(attempt_id) == expected


async def test_manifest_distinguishes_absent_from_explicitly_empty(
    database: SQLiteDatabase,
) -> None:
    """没有清单（不知道有哪些文件）与空清单（明确知道没有文件）必须是两个值。"""
    source_id = await source(database)
    async with SQLiteUnitOfWork(database) as uow:
        repository = DownloadRepository(uow.connection)
        attempt_id = await repository.create_attempt(
            source_id=source_id, resource_key="http:key", gid=GID, now_ms=1
        )
        assert await repository.get_manifest(attempt_id) is None
        await uow.connection.execute(
            insert(attempt_manifests).values(
                attempt_id=attempt_id, files_json="[]", created_at_ms=2
            )
        )
        assert await repository.get_manifest(attempt_id) == ()


@pytest.mark.parametrize(
    "files_json",
    [
        "not-json",
        '{"path": "a.bin", "size": 1}',
        "[1]",
        '[{"path": 1, "size": 2}]',
        '[{"path": "a.bin", "size": true}]',
    ],
)
async def test_manifest_rejects_corrupt_payloads(
    database: SQLiteDatabase, files_json: str
) -> None:
    source_id = await source(database)
    async with SQLiteUnitOfWork(database) as uow:
        repository = DownloadRepository(uow.connection)
        attempt_id = await repository.create_attempt(
            source_id=source_id, resource_key="http:key", gid=GID, now_ms=1
        )
        await uow.connection.execute(
            insert(attempt_manifests).values(
                attempt_id=attempt_id, files_json=files_json, created_at_ms=2
            )
        )
        with pytest.raises(DomainError) as caught:
            await repository.get_manifest(attempt_id)
        assert caught.value.code is ErrorCode.INVALID_STATE


async def test_manifest_survives_database_restart(tmp_path: Path) -> None:
    path = tmp_path / "v2.db"
    database = SQLiteDatabase(path)
    await initialize_database(database)
    source_id = await source(database)
    async with SQLiteUnitOfWork(database) as uow:
        repository = DownloadRepository(uow.connection)
        attempt_id = await repository.create_attempt(
            source_id=source_id, resource_key="http:key", gid=GID, now_ms=1
        )
        await repository.record_manifest(
            attempt_id, entries=(ManifestEntry("a/b.bin", 7),), now_ms=2
        )
    await database.close()

    reopened = SQLiteDatabase(path)
    try:
        async with SQLiteUnitOfWork(reopened) as uow:
            assert await DownloadRepository(uow.connection).get_manifest(attempt_id) == (
                ManifestEntry("a/b.bin", 7),
            )
    finally:
        await reopened.close()


@pytest.mark.parametrize("status", [AttemptStatus.FAILED, AttemptStatus.CANCELLED])
async def test_failed_and_cancelled_uncertain_stops_can_both_be_confirmed(
    database: SQLiteDatabase, status: AttemptStatus
) -> None:
    source_id = await source(database)
    async with SQLiteUnitOfWork(database) as uow:
        repository = DownloadRepository(uow.connection)
        attempt_id = await repository.create_attempt(
            source_id=source_id,
            resource_key="http:key",
            gid=GID,
            status=status,
            now_ms=1,
        )
        await repository.mark_stop_uncertain(attempt_id)
        await repository.confirm_stopped_gid(GID)
        assert (await repository.get_attempt(attempt_id)).submission_state == "submitted"


async def test_ingestion_start_rejects_terminal_and_concurrently_changed_attempts(
    database: SQLiteDatabase,
) -> None:
    source_id = await source(database)
    async with SQLiteUnitOfWork(database) as uow:
        repository = DownloadRepository(uow.connection)
        attempt_id = await repository.create_attempt(
            source_id=source_id, resource_key="http:key", gid=GID, now_ms=1
        )
        await uow.connection.execute(
            text(
                "CREATE TRIGGER test_ignore_ingestion_start "
                "BEFORE UPDATE OF ingestion_started_at_ms ON download_attempts "
                "BEGIN SELECT RAISE(IGNORE); END"
            )
        )
        with pytest.raises(DomainError) as caught:
            await repository.start_ingestion(attempt_id, expected_gid=GID, now_ms=2)
        assert caught.value.code is ErrorCode.STALE_EVENT
        await uow.connection.execute(text("DROP TRIGGER test_ignore_ingestion_start"))
        await repository.fail_attempt(attempt_id, error_code="failed", now_ms=3)
        with pytest.raises(DomainError) as caught:
            await repository.start_ingestion(attempt_id, expected_gid=GID, now_ms=4)
        assert caught.value.code is ErrorCode.STALE_EVENT


@pytest.mark.parametrize(
    "status,total,completed,expected",
    [
        (AttemptStatus.QUEUED, 100, 20, 80),
        (AttemptStatus.ACTIVE, 100, 20, 80),
        (AttemptStatus.WAITING, 100, 20, 0),
        (AttemptStatus.PAUSED, 100, 20, 0),
        (AttemptStatus.COMPLETED, 100, 20, 0),
        (AttemptStatus.CANCELLED, 100, 20, 0),
        (AttemptStatus.FAILED, 100, 20, 0),
        (AttemptStatus.ACTIVE, None, 20, 80),
    ],
)
async def test_list_active_physical_maps_attempt_status_and_remaining_bytes(
    database: SQLiteDatabase,
    status: AttemptStatus,
    total: int | None,
    completed: int,
    expected: int,
) -> None:
    source_id = await source(database)
    async with SQLiteUnitOfWork(database) as uow:
        repository = DownloadRepository(uow.connection)
        attempt_id = await repository.create_attempt(
            source_id=source_id,
            resource_key="http:key",
            gid=GID,
            status=status,
            total_length=total,
            physical_reserved_bytes=100,
            now_ms=1,
        )
        await uow.connection.execute(
            update(download_attempts)
            .where(download_attempts.c.id == attempt_id)
            .values(completed_length=completed)
        )

        physical = await repository.list_active_physical()

    expected_items = (
        [PhysicalDownload(status, total, completed, 100)]
        if status in LIVE_ATTEMPT_STATUSES
        else []
    )
    assert physical == expected_items
    assert sum(item.reserved_bytes for item in physical) == expected


async def test_list_active_physical_stays_empty_after_completion_and_history_delete(
    database: SQLiteDatabase,
) -> None:
    user_id = await create_user(database)
    source_id = await source(database)
    async with SQLiteUnitOfWork(database) as uow:
        repository = DownloadRepository(uow.connection)
        content_id = await ContentRepository(uow.connection).create_object(
            kind="file",
            digest="ab" * 32,
            size=100,
            storage_path="objects/done",
            now_ms=1,
        )
        attempt_id = await repository.create_attempt(
            source_id=source_id,
            resource_key="http:key",
            gid=GID,
            status=AttemptStatus.ACTIVE,
            total_length=100,
            physical_reserved_bytes=100,
            now_ms=1,
        )
        task_id = await repository.create_subscription(
            attempt_id=attempt_id,
            user_id=user_id,
            reserved_bytes=100,
            display_name="file",
            display_uri=None,
            now_ms=1,
        )
        assert [item.reserved_bytes for item in await repository.list_active_physical()] == [
            100
        ]

        await repository.complete_attempt(attempt_id, content_id=content_id, now_ms=2)
        assert await repository.list_active_physical() == []
        assert await repository.delete_history(user_id=user_id, task_id=task_id, now_ms=3)
        assert await repository.list_active_physical() == []


# --- 回归（D2）：终态 attempt 的 begin_completion 必须抛 STALE_EVENT，禁止静默返回 ---
async def test_begin_completion_rejects_terminal_attempt_with_stale_event(
    database: SQLiteDatabase,
) -> None:
    source_id = await source(database)
    async with SQLiteUnitOfWork(database) as uow:
        downloads = DownloadRepository(uow.connection)
        content_id = await ContentRepository(uow.connection).create_object(
            kind="file", digest="ab" * 32, size=3, storage_path="objects/d2", now_ms=1
        )
        attempt_id = await downloads.create_attempt(
            source_id=source_id, resource_key="http:key", gid=GID, now_ms=1
        )
        await downloads.fail_attempt(attempt_id, error_code="cancelled", now_ms=2)
        with pytest.raises(DomainError) as caught:
            await downloads.begin_completion(
                attempt_id, expected_gid=GID, content_id=content_id, now_ms=3
            )
        assert caught.value.code is ErrorCode.STALE_EVENT
        assert (await downloads.get_attempt(attempt_id)).completed_content_id is None


# --- 回归（D4）：终态 attempt 必须有原子 result_consumed + 最终认领入口 ---
async def test_finish_terminal_cleanup_consumes_result_and_claims_orphan_content(
    database: SQLiteDatabase,
) -> None:
    from aria2deck.domain.content import ContentDeletionState
    from aria2deck.infrastructure.sqlite.repositories.content import DELETE_LEASE_OWNER

    source_id = await source(database)
    async with SQLiteUnitOfWork(database) as uow:
        downloads = DownloadRepository(uow.connection)
        content_id = await ContentRepository(uow.connection).create_object(
            kind="file", digest="ab" * 32, size=3, storage_path="objects/d4", now_ms=1
        )
        attempt_id = await downloads.create_attempt(
            source_id=source_id, resource_key="http:key", gid=GID, now_ms=1
        )
        await downloads.begin_completion(
            attempt_id, expected_gid=GID, content_id=content_id, now_ms=2
        )
        await downloads.fail_attempt(attempt_id, error_code="failed", now_ms=3)
        await downloads.finish_terminal_cleanup(attempt_id, expected_gid=GID, now_ms=4)

        attempt = await downloads.get_attempt(attempt_id)
        assert attempt.status is AttemptStatus.FAILED
        assert attempt.result_consumed_at_ms == 4
        content = await ContentRepository(uow.connection).get_object(content_id)
        assert content.deletion_state is ContentDeletionState.PENDING_DELETE
        assert content.delete_lease_owner == DELETE_LEASE_OWNER


# --- 回归（D4）：已有用户引用的内容认领被阻挡，但 consumed 写入仍要提交 ---
async def test_finish_terminal_cleanup_keeps_referenced_content_but_consumes(
    database: SQLiteDatabase,
) -> None:
    from aria2deck.domain.content import ContentDeletionState

    user_id = await create_user(database)
    source_id = await source(database)
    async with SQLiteUnitOfWork(database) as uow:
        downloads = DownloadRepository(uow.connection)
        content = ContentRepository(uow.connection)
        content_id = await content.create_object(
            kind="file", digest="ab" * 32, size=3, storage_path="objects/d4b", now_ms=1
        )
        await content.add_user_file(
            user_id=user_id, content_id=content_id, display_name="keep.bin", now_ms=1
        )
        attempt_id = await downloads.create_attempt(
            source_id=source_id, resource_key="http:key", gid=GID, now_ms=1
        )
        await downloads.begin_completion(
            attempt_id, expected_gid=GID, content_id=content_id, now_ms=2
        )
        await downloads.fail_attempt(attempt_id, error_code="failed", now_ms=3)
        await downloads.finish_terminal_cleanup(attempt_id, expected_gid=GID, now_ms=4)

        assert (await downloads.get_attempt(attempt_id)).result_consumed_at_ms == 4
        assert (await content.get_object(content_id)).deletion_state is ContentDeletionState.ACTIVE


# --- 回归：finish_terminal_cleanup 重复调用保持幂等（consumed 时间戳不覆盖、不重复认领） ---
async def test_finish_terminal_cleanup_is_idempotent_on_repeat_call(database: SQLiteDatabase) -> None:
    from aria2deck.domain.content import ContentDeletionState
    from aria2deck.infrastructure.sqlite.repositories.content import DELETE_LEASE_OWNER

    source_id = await source(database)
    async with SQLiteUnitOfWork(database) as uow:
        downloads = DownloadRepository(uow.connection)
        content_id = await ContentRepository(uow.connection).create_object(
            kind="file", digest="ab" * 32, size=3, storage_path="objects/d4-idem", now_ms=1
        )
        attempt_id = await downloads.create_attempt(
            source_id=source_id, resource_key="http:key", gid=GID, now_ms=1
        )
        await downloads.begin_completion(
            attempt_id, expected_gid=GID, content_id=content_id, now_ms=2
        )
        await downloads.fail_attempt(attempt_id, error_code="failed", now_ms=3)
        await downloads.finish_terminal_cleanup(attempt_id, expected_gid=GID, now_ms=4)
        # 重复调用：不抛错，consumed 保持第一时间戳。
        await downloads.finish_terminal_cleanup(attempt_id, expected_gid=GID, now_ms=9)

        attempt = await downloads.get_attempt(attempt_id)
        assert attempt.result_consumed_at_ms == 4
        content = await ContentRepository(uow.connection).get_object(content_id)
        assert content.deletion_state is ContentDeletionState.PENDING_DELETE
        assert content.delete_lease_owner == DELETE_LEASE_OWNER


async def _seed_page_task(
    database: SQLiteDatabase,
    user_id: int,
    index: int,
    *,
    subscription_status: SubscriptionStatus,
    attempt_status: AttemptStatus,
    name: str | None = None,
    uri: str | None = None,
) -> int:
    async with SQLiteUnitOfWork(database) as uow:
        repo = DownloadRepository(uow.connection)
        key = f"http:page:{index}"
        source_id = await repo.create_source(
            kind="http", resource_key=key, payload_json="{}", options_json="{}",
            selected_file_indexes_json=None,
            display_name=name or f"task-{index:02d}", display_uri=uri, now_ms=index,
        )
        attempt_id = await repo.create_attempt(
            source_id=source_id, resource_key=key, gid=f"{index:016x}", now_ms=index,
        )
        await uow.connection.execute(
            update(download_attempts).where(download_attempts.c.id == attempt_id).values(
                status=attempt_status.value
            )
        )
        return await repo.create_subscription(
            attempt_id=attempt_id, user_id=user_id, reserved_bytes=0,
            display_name=name or f"task-{index:02d}", display_uri=uri, now_ms=index,
            status=subscription_status,
        )


async def test_page_tasks_matches_effective_status_semantics(database: SQLiteDatabase) -> None:
    """SQL CASE 与 effective_task_status 纯函数逐情形一致（含订阅终态优先）。"""
    user_id = await create_user(database)
    expired = await _seed_page_task(
        database, user_id, 1,
        subscription_status=SubscriptionStatus.EXPIRED, attempt_status=AttemptStatus.COMPLETED,
    )
    attempt_failed = await _seed_page_task(
        database, user_id, 2,
        subscription_status=SubscriptionStatus.ACTIVE, attempt_status=AttemptStatus.FAILED,
    )
    completed = await _seed_page_task(
        database, user_id, 3,
        subscription_status=SubscriptionStatus.COMPLETED, attempt_status=AttemptStatus.ACTIVE,
    )
    live = await _seed_page_task(
        database, user_id, 4,
        subscription_status=SubscriptionStatus.ACTIVE, attempt_status=AttemptStatus.ACTIVE,
    )
    async with SQLiteUnitOfWork(database) as uow:
        repo = DownloadRepository(uow.connection)
        failed_page, failed_total = await repo.page_tasks(
            user_id=user_id, statuses=(SubscriptionStatus.FAILED,),
            sort="created_at", order="desc", page=1, page_size=10,
        )
        assert failed_total == 1
        assert [r.id for r in failed_page] == [attempt_failed]
        expired_page, expired_total = await repo.page_tasks(
            user_id=user_id, statuses=(SubscriptionStatus.EXPIRED,),
            sort="created_at", order="desc", page=1, page_size=10,
        )
        assert expired_total == 1
        assert [r.id for r in expired_page] == [expired]
        multi, multi_total = await repo.page_tasks(
            user_id=user_id,
            statuses=(SubscriptionStatus.COMPLETED, SubscriptionStatus.FAILED, SubscriptionStatus.EXPIRED),
            sort="created_at", order="desc", page=1, page_size=10,
        )
        assert multi_total == 3
        assert [r.id for r in multi] == [completed, attempt_failed, expired]
        everything, everything_total = await repo.page_tasks(
            user_id=user_id, statuses=None,
            sort="created_at", order="asc", page=1, page_size=10,
        )
        assert everything_total == 4
        assert [r.id for r in everything] == [expired, attempt_failed, completed, live]


async def test_page_tasks_sort_ties_and_page_boundaries(database: SQLiteDatabase) -> None:
    user_id = await create_user(database)
    for index in range(1, 6):
        await _seed_page_task(
            database, user_id, index,
            subscription_status=SubscriptionStatus.ACTIVE, attempt_status=AttemptStatus.ACTIVE,
            name="same-name",
        )
    async with SQLiteUnitOfWork(database) as uow:
        repo = DownloadRepository(uow.connection)
        first, total = await repo.page_tasks(
            user_id=user_id, statuses=None,
            sort="created_at", order="desc", page=1, page_size=2,
        )
        assert total == 5
        assert [r.id for r in first] == [5, 4]
        second, _ = await repo.page_tasks(
            user_id=user_id, statuses=None,
            sort="created_at", order="desc", page=2, page_size=2,
        )
        assert [r.id for r in second] == [3, 2]
        last, _ = await repo.page_tasks(
            user_id=user_id, statuses=None,
            sort="created_at", order="desc", page=3, page_size=2,
        )
        assert [r.id for r in last] == [1]
        beyond, beyond_total = await repo.page_tasks(
            user_id=user_id, statuses=None,
            sort="created_at", order="desc", page=4, page_size=2,
        )
        assert beyond == [] and beyond_total == 5
        # 同名排序 ties 以 id 同向收尾；asc 时整体升序。
        tied, _ = await repo.page_tasks(
            user_id=user_id, statuses=None,
            sort="name", order="asc", page=1, page_size=10,
        )
        assert [r.id for r in tied] == [1, 2, 3, 4, 5]
        tied_desc, _ = await repo.page_tasks(
            user_id=user_id, statuses=None,
            sort="name", order="desc", page=1, page_size=10,
        )
        assert [r.id for r in tied_desc] == [5, 4, 3, 2, 1]


async def test_page_tasks_huge_page_returns_empty_with_total(database: SQLiteDatabase) -> None:
    """回归：超大页码的 OFFSET 曾超 SQLite int64 直接 500；钳到 total 后返回空页。"""
    user_id = await create_user(database)
    for index in range(1, 4):
        await _seed_page_task(
            database, user_id, index,
            subscription_status=SubscriptionStatus.ACTIVE, attempt_status=AttemptStatus.ACTIVE,
        )
    async with SQLiteUnitOfWork(database) as uow:
        repo = DownloadRepository(uow.connection)
        page, total = await repo.page_tasks(
            user_id=user_id, statuses=None,
            sort="created_at", order="desc", page=2**62, page_size=100,
        )
        assert page == []
        assert total == 3


# --- R4/R5：完成结算按实测大小对账，超额在终态迁移前判失败 ---
async def _prepare_completion(
    database: SQLiteDatabase,
    *,
    quota: int,
    used: int,
    reserved: int,
    content_size: int,
    already_owned: bool = False,
) -> tuple[int, int, int]:
    user_id = await create_user(database, quota=quota)
    source_id = await source(database)
    async with SQLiteUnitOfWork(database) as uow:
        repository = DownloadRepository(uow.connection)
        content = ContentRepository(uow.connection)
        content_id = await content.create_object(
            kind="file",
            digest="ef" * 32,
            size=content_size,
            storage_path="objects/actual",
            now_ms=1,
        )
        if already_owned:
            await content.add_user_file(
                user_id=user_id, content_id=content_id, display_name="owned.bin", now_ms=1
            )
        if used:
            await uow.connection.execute(
                update(quota_accounts).where(quota_accounts.c.user_id == user_id).values(used_bytes=used)
            )
        if reserved:
            await QuotaRepository(uow.connection).reserve(user_id, reserved)
        attempt_id = await repository.create_attempt(
            source_id=source_id,
            resource_key="http:key",
            gid=GID,
            total_length=reserved,
            physical_reserved_bytes=reserved,
            now_ms=1,
        )
        await repository.create_subscription(
            attempt_id=attempt_id,
            user_id=user_id,
            reserved_bytes=reserved,
            display_name="file.bin",
            display_uri=None,
            now_ms=1,
        )
        await repository.begin_completion(
            attempt_id, expected_gid=GID, content_id=content_id, now_ms=2
        )
    return user_id, attempt_id, content_id


async def test_finalize_completion_settles_by_actual_content_size(
    database: SQLiteDatabase,
) -> None:
    """声明 10 字节、实际 15 字节：按 15 记账，attempt 的 total_length 落实测值。"""
    user_id, attempt_id, _ = await _prepare_completion(
        database, quota=100, used=0, reserved=10, content_size=15
    )
    async with SQLiteUnitOfWork(database) as uow:
        assert await DownloadRepository(uow.connection).finalize_completion(
            attempt_id, expected_gid=GID, now_ms=3
        )
        attempt = await DownloadRepository(uow.connection).get_attempt(attempt_id)
        quota = await QuotaRepository(uow.connection).get(user_id)
    assert attempt.status is AttemptStatus.COMPLETED
    assert attempt.total_length == 15
    assert quota.used_bytes == 15
    assert quota.reserved_bytes == 0


async def test_finalize_completion_allows_already_owned_content_over_free_bytes(
    database: SQLiteDatabase,
) -> None:
    """已拥有同内容的用户不再占配额，即使实测大小超过当前剩余也放行。"""
    user_id, attempt_id, content_id = await _prepare_completion(
        database, quota=20, used=10, reserved=10, content_size=15, already_owned=True
    )
    async with SQLiteUnitOfWork(database) as uow:
        assert await DownloadRepository(uow.connection).finalize_completion(
            attempt_id, expected_gid=GID, now_ms=3
        )
        attempt = await DownloadRepository(uow.connection).get_attempt(attempt_id)
        quota = await QuotaRepository(uow.connection).get(user_id)
        owned = await ContentRepository(uow.connection).find_user_file(
            user_id=user_id, content_id=content_id
        )
    assert attempt.status is AttemptStatus.COMPLETED
    assert quota.used_bytes == 10
    assert quota.reserved_bytes == 0
    assert isinstance(owned, int)


async def test_finalize_completion_rejects_over_quota_without_partial_writes(
    database: SQLiteDatabase,
) -> None:
    """实测超出剩余配额：抛 quota_exceeded，且不留下用户文件、账目与终态的半截写入。"""
    user_id, attempt_id, content_id = await _prepare_completion(
        database, quota=20, used=10, reserved=10, content_size=15
    )
    async with SQLiteUnitOfWork(database) as uow:
        with pytest.raises(DomainError) as caught:
            await DownloadRepository(uow.connection).finalize_completion(
                attempt_id, expected_gid=GID, now_ms=3
            )
        assert caught.value.code is ErrorCode.QUOTA_EXCEEDED
    async with SQLiteUnitOfWork(database) as uow:
        attempt = await DownloadRepository(uow.connection).get_attempt(attempt_id)
        quota = await QuotaRepository(uow.connection).get(user_id)
        owned = await ContentRepository(uow.connection).find_user_file(
            user_id=user_id, content_id=content_id
        )
    # attempt 保持 live，交给调用方 _fail(cleanup=True) 走失败清理
    assert attempt.status is AttemptStatus.QUEUED
    assert attempt.completed_content_id == content_id
    assert quota.used_bytes == 10
    assert quota.reserved_bytes == 10
    assert owned is None
