from __future__ import annotations

import pytest
from sqlalchemy import text, update

from aria2deck.domain.downloads import AttemptStatus, SubscriptionStatus
from aria2deck.domain.errors import DomainError, ErrorCode
from aria2deck.infrastructure.sqlite.engine import SQLiteDatabase
from aria2deck.infrastructure.sqlite.repositories.downloads import DownloadRepository
from aria2deck.infrastructure.sqlite.repositories.quota import QuotaRepository
from aria2deck.infrastructure.sqlite.schema import (
    download_attempts,
    download_sources,
    task_subscriptions,
)
from aria2deck.infrastructure.sqlite.unit_of_work import SQLiteUnitOfWork
from tests.infrastructure.sqlite.test_accounts_quota import create_user
from tests.infrastructure.sqlite.test_sqlite_downloads import GID, source


async def make_attempt(database: SQLiteDatabase, *, user_id: int | None = None, reserved: int = 0, status: AttemptStatus = AttemptStatus.QUEUED) -> int:
    source_id = await source(database)
    async with SQLiteUnitOfWork(database) as uow:
        repository = DownloadRepository(uow.connection)
        attempt_id = await repository.create_attempt(
            source_id=source_id, resource_key="http:key", gid=GID, status=status, total_length=reserved, physical_reserved_bytes=reserved, now_ms=1,
        )
        if user_id is not None:
            subscription_status = SubscriptionStatus.FAILED if status is AttemptStatus.FAILED else SubscriptionStatus.QUEUED
            await repository.create_subscription(
                attempt_id=attempt_id, user_id=user_id, reserved_bytes=reserved, display_name="file", display_uri=None, status=subscription_status, now_ms=1,
            )
            if reserved:
                await QuotaRepository(uow.connection).reserve(user_id, reserved)
        return attempt_id


@pytest.mark.parametrize(
    "column,value",
    [
        ("payload_json", "not-json"),
        ("payload_json", "[]"),
        ("options_json", "[]"),
        ("selected_file_indexes_json", "not-json"),
        ("selected_file_indexes_json", "[true]"),
    ],
)
async def test_source_json_corruption_is_rejected(database: SQLiteDatabase, column: str, value: str) -> None:
    source_id = await source(database)
    async with SQLiteUnitOfWork(database) as uow:
        await uow.connection.execute(update(download_sources).where(download_sources.c.id == source_id).values(**{column: value}))
        with pytest.raises(DomainError) as caught:
            await DownloadRepository(uow.connection).get_source(source_id)
        assert caught.value.code is ErrorCode.INVALID_STATE


async def test_missing_source_and_invalid_gid_confirmation_are_stable(database: SQLiteDatabase) -> None:
    async with SQLiteUnitOfWork(database) as uow:
        repository = DownloadRepository(uow.connection)
        with pytest.raises(DomainError) as caught:
            await repository.get_source(999)
        assert caught.value.code is ErrorCode.NOT_FOUND
        with pytest.raises(DomainError):
            await repository.confirm_stopped_gid("missing")


async def test_planned_gid_requires_a_planned_attempt(database: SQLiteDatabase) -> None:
    attempt_id = await make_attempt(database)
    async with SQLiteUnitOfWork(database) as uow:
        repository = DownloadRepository(uow.connection)
        await repository.mark_submitted(attempt_id)
        with pytest.raises(DomainError) as caught:
            await repository.set_planned_gid(attempt_id, "fedcba0987654321")
        assert caught.value.code is ErrorCode.INVALID_STATE


async def test_failed_attempt_confirmation_is_idempotent(database: SQLiteDatabase) -> None:
    attempt_id = await make_attempt(database, status=AttemptStatus.FAILED)
    async with SQLiteUnitOfWork(database) as uow:
        repository = DownloadRepository(uow.connection)
        await repository.fail_attempt(attempt_id, error_code="failed", now_ms=2)
        assert (await repository.get_attempt(attempt_id)).status is AttemptStatus.FAILED


async def test_cancel_subscription_cas_failure_rolls_back(database: SQLiteDatabase) -> None:
    user_id = await create_user(database)
    await make_attempt(database, user_id=user_id)
    async with SQLiteUnitOfWork(database) as uow:
        await uow.connection.execute(text("CREATE TRIGGER ignore_task_cancel BEFORE UPDATE OF status ON task_subscriptions WHEN NEW.status = 'cancelled' BEGIN SELECT RAISE(IGNORE); END"))
        with pytest.raises(DomainError) as caught:
            await DownloadRepository(uow.connection).cancel_subscription(user_id=user_id, task_id=1, now_ms=2)
        assert caught.value.code is ErrorCode.INVALID_STATE


async def test_last_attempt_cancellation_cas_failure_rolls_back(database: SQLiteDatabase) -> None:
    user_id = await create_user(database)
    await make_attempt(database, user_id=user_id)
    async with SQLiteUnitOfWork(database) as uow:
        await uow.connection.execute(text("CREATE TRIGGER ignore_attempt_cancel BEFORE UPDATE OF status ON download_attempts WHEN NEW.status = 'cancelled' BEGIN SELECT RAISE(IGNORE); END"))
        with pytest.raises(DomainError) as caught:
            await DownloadRepository(uow.connection).cancel_subscription(user_id=user_id, task_id=1, now_ms=2)
        assert caught.value.code is ErrorCode.INVALID_STATE


async def test_history_delete_cas_failure_is_not_reported(database: SQLiteDatabase) -> None:
    user_id = await create_user(database)
    await make_attempt(database, user_id=user_id, status=AttemptStatus.FAILED)
    async with SQLiteUnitOfWork(database) as uow:
        await uow.connection.execute(text("CREATE TRIGGER ignore_history_delete BEFORE DELETE ON task_subscriptions BEGIN SELECT RAISE(IGNORE); END"))
        with pytest.raises(DomainError) as caught:
            await DownloadRepository(uow.connection).delete_history(user_id=user_id, task_id=1, now_ms=2)
        assert caught.value.code is ErrorCode.INVALID_STATE


async def test_admission_rejects_negative_terminal_and_subscriptionless_attempts(database: SQLiteDatabase) -> None:
    attempt_id = await make_attempt(database)
    async with SQLiteUnitOfWork(database) as uow:
        repository = DownloadRepository(uow.connection)
        with pytest.raises(DomainError):
            await repository.admit_size(attempt_id=attempt_id, observed_bytes=-1, known_total=True, disk_free_bytes=1_000_000, min_free_bytes=0, now_ms=1)
        with pytest.raises(DomainError) as caught:
            await repository.admit_size(attempt_id=attempt_id, observed_bytes=1, known_total=True, disk_free_bytes=1_000_000, min_free_bytes=0, now_ms=1)
        assert caught.value.code is ErrorCode.DOWNLOAD_UNAVAILABLE
    async with SQLiteUnitOfWork(database) as uow:
        repository = DownloadRepository(uow.connection)
        await uow.connection.execute(update(task_subscriptions).where(task_subscriptions.c.attempt_id == attempt_id).values(status=SubscriptionStatus.CANCELLED))
        await uow.connection.execute(update(download_sources).where(download_sources.c.id == 1).values(detached_at_ms=1))
        # Marking the source detached does not change the attempt; use the real terminal transition next.
        await uow.connection.execute(text("UPDATE download_attempts SET status = 'cancelled' WHERE id = :id"), {"id": attempt_id})
        with pytest.raises(DomainError) as caught:
            await repository.admit_size(attempt_id=attempt_id, observed_bytes=1, known_total=True, disk_free_bytes=1_000_000, min_free_bytes=0, now_ms=2)
        assert caught.value.code is ErrorCode.DOWNLOAD_UNAVAILABLE


async def test_admission_releases_existing_reservation_when_growth_exceeds_quota(database: SQLiteDatabase) -> None:
    user_id = await create_user(database, quota=2)
    attempt_id = await make_attempt(database, user_id=user_id, reserved=2)
    async with SQLiteUnitOfWork(database) as uow:
        repository = DownloadRepository(uow.connection)
        failure = await repository.admit_size(attempt_id=attempt_id, observed_bytes=3, known_total=True, disk_free_bytes=1_000_000, min_free_bytes=0, now_ms=2)
        assert failure is not None and failure.code is ErrorCode.QUOTA_EXCEEDED
        assert (await QuotaRepository(uow.connection).get(user_id)).reserved_bytes == 0
        assert (await repository.get_attempt(attempt_id)).status is AttemptStatus.FAILED


async def test_admission_does_not_mask_unexpected_quota_failure(database: SQLiteDatabase, monkeypatch: pytest.MonkeyPatch) -> None:
    user_id = await create_user(database)
    attempt_id = await make_attempt(database, user_id=user_id)

    async def fail(self: QuotaRepository, user_id: int, amount: int) -> None:
        raise DomainError(ErrorCode.NOT_FOUND, "missing account")

    monkeypatch.setattr(QuotaRepository, "reserve", fail)
    async with SQLiteUnitOfWork(database) as uow:
        with pytest.raises(DomainError) as caught:
            await DownloadRepository(uow.connection).admit_size(attempt_id=attempt_id, observed_bytes=1, known_total=True, disk_free_bytes=1_000_000, min_free_bytes=0, now_ms=2)
        assert caught.value.code is ErrorCode.NOT_FOUND


async def test_uncertain_stop_can_be_listed_and_confirmed(database: SQLiteDatabase) -> None:
    attempt_id = await make_attempt(database, status=AttemptStatus.CANCELLED)
    async with SQLiteUnitOfWork(database) as uow:
        repository = DownloadRepository(uow.connection)
        await uow.connection.execute(text("UPDATE download_attempts SET submission_state = 'uncertain' WHERE id = :id"), {"id": attempt_id})
        assert await repository.list_uncertain_stops() == [GID]
        await repository.confirm_stopped_gid(GID)
        assert await repository.list_uncertain_stops() == []


async def test_real_history_repository_rejects_live_and_deletes_terminal(database: SQLiteDatabase) -> None:
    user_id = await create_user(database)
    attempt_id = await make_attempt(database, user_id=user_id)
    async with SQLiteUnitOfWork(database) as uow:
        repository = DownloadRepository(uow.connection)
        with pytest.raises(DomainError) as caught:
            await repository.delete_history(user_id=user_id, task_id=1, now_ms=2)
        assert caught.value.code is ErrorCode.INVALID_STATE
        await uow.connection.execute(update(task_subscriptions).where(task_subscriptions.c.id == 1).values(status=SubscriptionStatus.FAILED))
        await uow.connection.execute(text("UPDATE download_attempts SET status = 'failed' WHERE id = :id"), {"id": attempt_id})
        assert await repository.delete_history(user_id=user_id, task_id=1, now_ms=3) is True
        with pytest.raises(DomainError):
            await repository.get_task(user_id=user_id, task_id=1)


async def test_admission_is_monotonic_for_repeated_and_unknown_sizes(database: SQLiteDatabase) -> None:
    user_id = await create_user(database)
    attempt_id = await make_attempt(database, user_id=user_id)
    async with SQLiteUnitOfWork(database) as uow:
        repository = DownloadRepository(uow.connection)
        assert await repository.admit_size(attempt_id=attempt_id, observed_bytes=2, known_total=False, disk_free_bytes=1_000_000, min_free_bytes=0, now_ms=2) is None
        assert await repository.admit_size(attempt_id=attempt_id, observed_bytes=2, known_total=True, disk_free_bytes=1_000_000, min_free_bytes=0, now_ms=3) is None
        assert (await repository.get_attempt(attempt_id)).total_length == 2
        assert (await QuotaRepository(uow.connection).get(user_id)).reserved_bytes == 2


async def test_zero_reservation_failure_is_persistent(database: SQLiteDatabase) -> None:
    user_id = await create_user(database, quota=0)
    attempt_id = await make_attempt(database, user_id=user_id)
    async with SQLiteUnitOfWork(database) as uow:
        failure = await DownloadRepository(uow.connection).admit_size(
            attempt_id=attempt_id, observed_bytes=1, known_total=True, disk_free_bytes=1_000_000, min_free_bytes=0, now_ms=2,
        )
        assert failure is not None and failure.code is ErrorCode.QUOTA_EXCEEDED


async def test_unknown_growth_and_larger_total_charge_only_machine_delta(
    database: SQLiteDatabase,
) -> None:
    user_id = await create_user(database)
    attempt_id = await make_attempt(
        database, user_id=user_id, status=AttemptStatus.ACTIVE
    )
    async with SQLiteUnitOfWork(database) as uow:
        repository = DownloadRepository(uow.connection)
        await uow.connection.execute(
            update(download_attempts)
            .where(download_attempts.c.id == attempt_id)
            .values(total_length=None)
        )
        assert await repository.admit_size(
            attempt_id=attempt_id,
            observed_bytes=40,
            known_total=False,
            disk_free_bytes=40,
            min_free_bytes=0,
            now_ms=2,
        ) is None
        await uow.connection.execute(
            update(download_attempts)
            .where(download_attempts.c.id == attempt_id)
            .values(completed_length=10)
        )
        assert await repository.admit_size(
            attempt_id=attempt_id,
            observed_bytes=60,
            known_total=False,
            disk_free_bytes=50,
            min_free_bytes=0,
            now_ms=3,
        ) is None
        assert await repository.admit_size(
            attempt_id=attempt_id,
            observed_bytes=100,
            known_total=True,
            disk_free_bytes=100,
            min_free_bytes=0,
            now_ms=4,
        ) is None
        attempt = await repository.get_attempt(attempt_id)
        assert attempt.total_length == 100
        assert attempt.physical_reserved_bytes == 100
        assert (await QuotaRepository(uow.connection).get(user_id)).reserved_bytes == 100


@pytest.mark.parametrize("disk_free,succeeds", [(79, False), (80, True)])
async def test_paused_admission_checks_the_full_target_remaining(
    database: SQLiteDatabase, disk_free: int, succeeds: bool
) -> None:
    user_id = await create_user(database)
    attempt_id = await make_attempt(
        database,
        user_id=user_id,
        reserved=100,
        status=AttemptStatus.PAUSED,
    )
    async with SQLiteUnitOfWork(database) as uow:
        repository = DownloadRepository(uow.connection)
        await uow.connection.execute(
            update(download_attempts)
            .where(download_attempts.c.id == attempt_id)
            .values(completed_length=20)
        )
        failure = await repository.admit_size(
            attempt_id=attempt_id,
            observed_bytes=100,
            known_total=True,
            disk_free_bytes=disk_free,
            min_free_bytes=0,
            now_ms=2,
        )
        attempt = await repository.get_attempt(attempt_id)
        quota = await QuotaRepository(uow.connection).get(user_id)
        if succeeds:
            assert failure is None
            assert attempt.status is AttemptStatus.PAUSED
            assert quota.reserved_bytes == 100
        else:
            assert failure is not None
            assert failure.code is ErrorCode.PHYSICAL_SPACE_EXCEEDED
            assert attempt.status is AttemptStatus.FAILED
            assert attempt.error_code == ErrorCode.PHYSICAL_SPACE_EXCEEDED.value
            assert quota.reserved_bytes == 0


async def test_failed_attempt_with_zero_reservation_skips_release(database: SQLiteDatabase) -> None:
    user_id = await create_user(database)
    attempt_id = await make_attempt(database, user_id=user_id)
    async with SQLiteUnitOfWork(database) as uow:
        repository = DownloadRepository(uow.connection)
        await repository.fail_attempt(attempt_id, error_code="aria2_rejected", now_ms=2)
        assert (await repository.get_attempt(attempt_id)).status is AttemptStatus.FAILED
        assert (await QuotaRepository(uow.connection).get(user_id)).reserved_bytes == 0


async def test_source_rebuild_requires_a_stable_identity(database: SQLiteDatabase) -> None:
    source_id = await source(database)
    async with SQLiteUnitOfWork(database) as uow:
        repository = DownloadRepository(uow.connection)
        await uow.connection.execute(
            update(download_sources)
            .where(download_sources.c.id == source_id)
            .values(payload_json="{}", options_json="{}", display_uri=None, detached_at_ms=1)
        )
        await repository.rebuild_source(
            source_id=source_id,
            resource_key="http:key",
            payload_json='{"uris":["https://example.com/private"]}',
            options_json='{"headers":{},"mirrors":[]}',
            selected_file_indexes_json=None,
            display_name="private",
            display_uri="https://example.com/private",
        )
        rebuilt = await repository.get_source(source_id)
        assert rebuilt.payload == {"uris": ["https://example.com/private"]}
        assert rebuilt.detached_at_ms is None
        with pytest.raises(DomainError) as caught:
            await repository.rebuild_source(
                source_id=source_id,
                resource_key="http:changed",
                payload_json="{}",
                options_json="{}",
                selected_file_indexes_json=None,
                display_name="changed",
                display_uri=None,
            )
        assert caught.value.code is ErrorCode.INVALID_STATE
