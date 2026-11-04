from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import delete, select, text, update

from aria2deck.domain.downloads import AttemptStatus, SubscriptionStatus
from aria2deck.domain.errors import DomainError, ErrorCode
from aria2deck.infrastructure.sqlite.engine import SQLiteDatabase
from aria2deck.infrastructure.sqlite.repositories.content import ContentRepository
from aria2deck.infrastructure.sqlite.repositories.downloads import DownloadRepository
from aria2deck.infrastructure.sqlite.repositories.quota import QuotaRepository
from aria2deck.infrastructure.sqlite.schema import (
    content_objects,
    download_attempts,
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
