from __future__ import annotations

import asyncio
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from aria2deck.bootstrap import create_app
from aria2deck.domain.downloads import AttemptStatus, SubscriptionStatus
from aria2deck.domain.errors import DomainError, ErrorCode
from aria2deck.infrastructure.config import Settings
from aria2deck.infrastructure.sqlite.engine import SQLiteDatabase
from aria2deck.infrastructure.sqlite.repositories.downloads import DownloadRepository
from aria2deck.infrastructure.sqlite.repositories.quota import QuotaRepository
from aria2deck.infrastructure.sqlite.schema import download_attempts
from aria2deck.infrastructure.sqlite.unit_of_work import SQLiteUnitOfWork
from tests.api.conftest import create_admin, login
from tests.api.test_download_writes import install_backend
from tests.download_fakes import FakeSubmissionBackend

PLANNED_GID = "0000000000000001"
EXISTING_GID = "0000000000000002"
STOP_GID = "0000000000000003"


def prepare_database(settings: Settings, *, mode: str) -> int:
    app = create_app(settings=settings)
    with TestClient(app) as client:
        user_id = int(create_admin(client)["id"])

    async def seed() -> None:
        now_ms = time.time_ns() // 1_000_000
        database = SQLiteDatabase(settings.database_path)
        try:
            async with SQLiteUnitOfWork(database) as uow:
                downloads = DownloadRepository(uow.connection)
                if mode in {"mixed", "failure"}:
                    source_id = await downloads.create_source(
                        kind="http", resource_key="http:planned", payload_json='{"uris":["https://example.com/a"]}', options_json='{"headers":{},"mirrors":[]}', selected_file_indexes_json=None, display_name="a", display_uri="https://example.com/a", now_ms=now_ms,
                    )
                    attempt_id = await downloads.create_attempt(
                        source_id=source_id, resource_key="http:planned", gid=PLANNED_GID, total_length=7, physical_reserved_bytes=7, now_ms=now_ms,
                    )
                    await downloads.create_subscription(
                        attempt_id=attempt_id, user_id=user_id, reserved_bytes=7, display_name="a", display_uri="https://example.com/a", now_ms=now_ms,
                    )
                    await QuotaRepository(uow.connection).reserve(user_id, 7)
                if mode == "mixed":
                    source_id = await downloads.create_source(
                        kind="magnet", resource_key="magnet:existing", payload_json='{"uri":"magnet:?xt=urn:btih:' + 'ab' * 20 + '"}', options_json="{}", selected_file_indexes_json=None, display_name="existing", display_uri=None, now_ms=now_ms,
                    )
                    attempt_id = await downloads.create_attempt(
                        source_id=source_id, resource_key="magnet:existing", gid=EXISTING_GID, now_ms=now_ms,
                    )
                    await downloads.create_subscription(
                        attempt_id=attempt_id, user_id=user_id, reserved_bytes=0, display_name="existing", display_uri=None, now_ms=now_ms,
                    )
                    await uow.connection.execute(update(download_attempts).where(download_attempts.c.id == attempt_id).values(submission_state="uncertain"))
                    source_id = await downloads.create_source(
                        kind="http", resource_key="http:stop", payload_json='{"uris":["https://example.com/stop"]}', options_json="{}", selected_file_indexes_json=None, display_name="stop", display_uri=None, now_ms=now_ms,
                    )
                    attempt_id = await downloads.create_attempt(
                        source_id=source_id, resource_key="http:stop", gid=STOP_GID, status=AttemptStatus.CANCELLED, now_ms=now_ms,
                    )
                    await downloads.create_subscription(
                        attempt_id=attempt_id, user_id=user_id, reserved_bytes=0, display_name="stop", display_uri=None, status=SubscriptionStatus.CANCELLED, now_ms=now_ms,
                    )
                    await uow.connection.execute(update(download_attempts).where(download_attempts.c.id == attempt_id).values(submission_state="uncertain"))
        finally:
            await database.close()

    asyncio.run(seed())
    return user_id


def test_restart_recovers_planned_existing_and_uncertain_stop(settings: Settings) -> None:
    prepare_database(settings, mode="mixed")
    app = create_app(settings=settings)
    backend = FakeSubmissionBackend()
    backend.confirm_results[PLANNED_GID] = False
    backend.mark_confirmed_on_submit = True
    app.state.download_recovery.backend = backend

    with TestClient(app) as client:
        assert client.get("/api/health/ready").status_code == 200

    assert [call[0].gid for call in backend.submit_calls] == [PLANNED_GID]
    assert backend.stop_calls == [STOP_GID]

    async def check() -> None:
        database = SQLiteDatabase(settings.database_path)
        try:
            async with database.connection() as connection:
                rows = (await connection.execute(select(download_attempts).order_by(download_attempts.c.id))).all()
                assert [row.submission_state for row in rows] == ["submitted"] * 3
        finally:
            await database.close()

    asyncio.run(check())


def test_restart_definite_rejection_fails_attempt_and_releases_quota(settings: Settings) -> None:
    user_id = prepare_database(settings, mode="failure")
    app = create_app(settings=settings)
    backend = FakeSubmissionBackend()
    backend.confirm_default = False
    backend.reject_indexes = {0}
    app.state.download_recovery.backend = backend

    with TestClient(app):
        pass

    async def check() -> None:
        database = SQLiteDatabase(settings.database_path)
        try:
            async with SQLiteUnitOfWork(database) as uow:
                attempt = (await uow.connection.execute(select(download_attempts))).one()
                assert attempt.status == AttemptStatus.FAILED
                assert (await QuotaRepository(uow.connection).get(user_id)).reserved_bytes == 0
        finally:
            await database.close()

    asyncio.run(check())


def test_restart_transport_failure_leaves_uncertain_without_destructive_cleanup(settings: Settings) -> None:
    prepare_database(settings, mode="failure")
    app = create_app(settings=settings)
    backend = FakeSubmissionBackend()
    backend.confirm_error = DomainError(ErrorCode.SUBMISSION_UNCERTAIN, "offline")
    app.state.download_recovery.backend = backend

    with TestClient(app) as client:
        assert client.get("/api/health/live").json() == {"ok": True}

    async def check() -> None:
        database = SQLiteDatabase(settings.database_path)
        try:
            async with database.connection() as connection:
                attempt = (await connection.execute(select(download_attempts))).one()
                assert attempt.status == AttemptStatus.QUEUED
                assert attempt.submission_state == "uncertain"
        finally:
            await database.close()

    asyncio.run(check())


def test_cancel_commit_before_process_exit_is_stopped_on_restart(settings: Settings) -> None:
    app = create_app(settings=settings)
    with TestClient(app) as client:
        create_admin(client)
        login(client)
        backend = install_backend(client)
        created = client.post(
            "/api/v2/tasks",
            json={
                "tasks": [
                    {"uri": "https://example.com/cancel-after-commit"},
                    {"uri": "https://example.com/must-keep-running"},
                ]
            },
        ).json()["items"]
        target_task_id = created[0]["task"]["id"]

        async def read_attempts() -> list[tuple[str, str, str]]:
            database = SQLiteDatabase(settings.database_path)
            try:
                async with database.connection() as connection:
                    rows = (
                        await connection.execute(
                            select(download_attempts).order_by(download_attempts.c.id)
                        )
                    ).all()
                    return [(row.gid, row.status, row.submission_state) for row in rows]
            finally:
                await database.close()

        attempts = asyncio.run(read_attempts())
        target_gid = attempts[0][0]
        other_gid = attempts[1][0]

        async def exit_before_stop(*, gid: str) -> None:
            raise RuntimeError("simulated process exit")

        backend.stop = exit_before_stop
        with pytest.raises(RuntimeError, match="simulated process exit"):
            client.post("/api/v2/tasks/cancel", json={"task_ids": [target_task_id]})

    after_exit = {gid: (status, submission) for gid, status, submission in asyncio.run(read_attempts())}
    assert after_exit[target_gid] == (AttemptStatus.CANCELLED, "uncertain")
    assert after_exit[other_gid] == (AttemptStatus.QUEUED, "submitted")

    restarted = create_app(settings=settings)
    recovery_backend = FakeSubmissionBackend()
    restarted.state.download_recovery.backend = recovery_backend
    with TestClient(restarted):
        pass

    assert recovery_backend.stop_calls == [target_gid]
    after_recovery = {
        gid: (status, submission) for gid, status, submission in asyncio.run(read_attempts())
    }
    assert after_recovery[target_gid] == (AttemptStatus.CANCELLED, "submitted")
    assert after_recovery[other_gid] == (AttemptStatus.QUEUED, "submitted")


def test_pending_recovery_worker_shares_the_command_submission_lock(settings: Settings) -> None:
    app = create_app(settings=settings)

    assert app.state.download_commands.submission_lock is app.state.download_recovery.submission_lock
    assert "download-recovery-pending" in {
        worker.name for worker in app.state.supervisor.snapshot()
    }


def test_submission_timeout_releases_quota_then_confirms_failed_stop(settings: Settings) -> None:
    user_id = prepare_database(settings, mode="failure")
    app = create_app(settings=settings)
    backend = FakeSubmissionBackend()
    backend.confirm_error = DomainError(ErrorCode.SUBMISSION_UNCERTAIN, "offline")
    app.state.download_recovery.backend = backend
    app.state.download_recovery.clock_ms = lambda: time.time_ns() // 1_000_000 + 60_000

    async def recover_and_check() -> None:
        first = await app.state.download_recovery.recover_pending()
        assert first.failed == 1
        async with app.state.download_recovery.uow_factory() as uow:
            attempt = await DownloadRepository(uow.connection).get_attempt(1)
            assert attempt.status is AttemptStatus.FAILED
            assert attempt.error_code == "submission_timeout"
            assert attempt.submission_state == "uncertain"
            assert (await QuotaRepository(uow.connection).get(user_id)).reserved_bytes == 0

        second = await app.state.download_recovery.recover_pending()
        assert second.stopped == 1
        assert backend.stop_calls == [PLANNED_GID]
        async with app.state.download_recovery.uow_factory() as uow:
            attempt = await DownloadRepository(uow.connection).get_attempt(1)
            assert attempt.submission_state == "submitted"

    asyncio.run(recover_and_check())
