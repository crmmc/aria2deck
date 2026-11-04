from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from aria2deck.application.accounts.models import Principal
from aria2deck.application.downloads.commands import (
    DownloadCommandService,
    _from_source,
)
from aria2deck.application.downloads.models import (
    BackendSubmissionResult,
    DownloadWriteResult,
    SourceRecord,
    UriDownloadInput,
)
from aria2deck.bootstrap import create_app
from aria2deck.domain.accounts import CredentialKind
from aria2deck.domain.downloads import RegistrationOutcome
from aria2deck.domain.errors import DomainError, ErrorCode
from aria2deck.infrastructure.config import Settings
from tests.download_fakes import FakeSubmissionBackend, make_torrent


@asynccontextmanager
async def runtime(tmp_path: Path) -> AsyncIterator[tuple[DownloadCommandService, int, Principal, FakeSubmissionBackend]]:
    app = create_app(
        settings=Settings(
            database_path=tmp_path / "v2.db",
            download_directory=tmp_path / "downloads",
            static_directory=tmp_path / "static",
            debug=True,
        )
    )
    async with app.router.lifespan_context(app):
        view = await app.state.account_service.create_user(
            None,
            username="admin",
            password_hash="ab" * 32,
            is_admin=True,
            is_initial_password=False,
            quota_bytes=None,
        )
        principal = Principal(view.user, CredentialKind.SESSION, "session")
        backend = FakeSubmissionBackend()
        app.state.download_commands.backend = backend
        yield app.state.download_commands, view.user.id, principal, backend


async def test_create_batch_validates_size_and_all_business_failures_skip_network(tmp_path: Path) -> None:
    async with runtime(tmp_path) as (service, user_id, _principal, backend):
        for items in ([], [UriDownloadInput("https://example.com")] * 31):
            with pytest.raises(DomainError) as caught:
                await service.create_uris(user_id=user_id, items=items)
            assert caught.value.code is ErrorCode.INVALID_INPUT
        results = await service.create_uris(
            user_id=user_id, items=[UriDownloadInput("ftp://example.com/file")]
        )
        assert results[0].accepted is False
        assert backend.submit_calls == []
        with pytest.raises(DomainError):
            await service.cancel(user_id=user_id, task_ids=[])


async def _create_batch_charges_once_for_duplicate_and_reports_rate_limit(tmp_path: Path) -> None:
    async with runtime(tmp_path) as (service, user_id, _principal, backend):
        from aria2deck.domain.settings import replace

        current = service.rate_limiter.settings()
        limited = replace(current, create_task_rate_limit=1)
        service.rate_limiter.settings = lambda: limited
        results = await service.create_uris(
            user_id=user_id,
            items=[
                UriDownloadInput("https://example.com/a"),
                UriDownloadInput(" https://example.com/a "),
                UriDownloadInput("https://example.com/b"),
            ],
        )
        assert [result.accepted for result in results] == [True, True, False]
        assert results[1].outcome is RegistrationOutcome.DUPLICATE
        assert results[2].error_code == ErrorCode.RATE_LIMITED
        assert len(backend.submit_calls) == 1


@pytest.mark.parametrize("shape", ["count", "gid", "confirm"])
async def _submission_shape_and_confirmation_failures_are_uncertain(
    tmp_path: Path, shape: str
) -> None:
    async with runtime(tmp_path) as (service, user_id, _principal, backend):
        if shape == "count":
            backend.replies = []
        elif shape == "gid":
            backend.replies = [BackendSubmissionResult(True, gid="wrong")]
        else:
            backend.confirm_default = False
        with pytest.raises(DomainError) as caught:
            await service.create_uris(
                user_id=user_id, items=[UriDownloadInput("https://example.com/a")]
            )
        assert caught.value.code is ErrorCode.SUBMISSION_UNCERTAIN


async def _non_uncertain_tracker_failure_is_not_mislabeled(tmp_path: Path) -> None:
    async with runtime(tmp_path) as (service, user_id, _principal, _backend):
        class BrokenTrackers:
            async def current(self) -> tuple[str, ...]:
                raise DomainError(ErrorCode.INVALID_STATE, "bad cache")

        service.trackers = BrokenTrackers()
        with pytest.raises(DomainError) as caught:
            await service.create_uris(
                user_id=user_id, items=[UriDownloadInput("https://example.com/a")]
            )
        assert caught.value.code is ErrorCode.INVALID_STATE


async def _torrent_duplicate_shared_join_cancel_and_retry_create_new_attempt(tmp_path: Path) -> None:
    async with runtime(tmp_path) as (service, user_id, _principal, backend):
        from aria2deck.infrastructure.sqlite.repositories.accounts import AccountRepository

        async with service.uow_factory() as uow:
            other_id = await AccountRepository(uow.connection).create_user(
                username="other",
                password_hash="cd" * 32,
                is_admin=False,
                is_initial_password=False,
                quota_bytes=None,
                now_ms=1,
            )
        payload = make_torrent([("a", 3), ("b", 5)])
        first = await service.create_torrent(
            user_id=user_id, payload_base64=payload, selected_file_indexes=[2]
        )
        duplicate = await service.create_torrent(
            user_id=user_id, payload_base64=payload, selected_file_indexes=[2]
        )
        joined = await service.create_torrent(
            user_id=other_id, payload_base64=payload, selected_file_indexes=[2]
        )
        assert first.outcome is RegistrationOutcome.CREATED
        assert duplicate.outcome is RegistrationOutcome.DUPLICATE
        assert joined.outcome is RegistrationOutcome.JOINED_LIVE
        assert joined.task_id != first.task_id
        assert len(backend.submit_calls) == 1

        await service.cancel(user_id=other_id, task_ids=[joined.task_id])
        assert backend.stop_calls == []
        rejoined = await service.retry(user_id=other_id, task_id=joined.task_id)
        assert rejoined.outcome is RegistrationOutcome.JOINED_LIVE
        await service.cancel(user_id=user_id, task_ids=[first.task_id])
        assert backend.stop_calls == []
        await service.cancel(user_id=other_id, task_ids=[rejoined.task_id])
        assert len(backend.stop_calls) == 1
        repeated = await service.cancel(user_id=other_id, task_ids=[rejoined.task_id])
        assert repeated[0].accepted is False
        retried = await service.retry(user_id=other_id, task_id=rejoined.task_id)
        assert retried.outcome is RegistrationOutcome.CREATED
        assert retried.attempt_id != first.attempt_id

        # An already submitted attempt is not submitted again by the confirmation pass.
        await service._submit({retried.attempt_id: DownloadWriteResult(True, attempt_id=retried.attempt_id)})


def test_source_retry_rejects_detached_or_corrupt_payloads() -> None:
    base = SourceRecord(
        id=1,
        kind="http",
        resource_key="http:key",
        payload={"uris": []},
        options={},
        selected_file_indexes=None,
        display_name="file",
        display_uri=None,
        detached_at_ms=1,
    )
    with pytest.raises(DomainError):
        _from_source(base)
    from dataclasses import replace

    with pytest.raises(DomainError):
        _from_source(replace(base, detached_at_ms=None, kind="torrent", payload={}))
    with pytest.raises(DomainError):
        _from_source(replace(base, detached_at_ms=None, payload={}))
    with pytest.raises(DomainError):
        _from_source(replace(base, detached_at_ms=None, kind="unknown"))


async def test_definite_rejection_uses_stable_fallback_code(tmp_path: Path) -> None:
    async with runtime(tmp_path) as (service, user_id, _principal, backend):
        backend.replies = [BackendSubmissionResult(False)]
        results = await service.create_uris(
            user_id=user_id, items=[UriDownloadInput("https://example.com/a")]
        )
        assert results[0].accepted is False
        assert results[0].error_code == "aria2_rejected"


async def test_rejection_does_not_stop_confirmation_of_later_batch_items(tmp_path: Path) -> None:
    async with runtime(tmp_path) as (service, user_id, _principal, backend):
        backend.reject_indexes = {0}
        results = await service.create_uris(
            user_id=user_id,
            items=[UriDownloadInput("https://example.com/a"), UriDownloadInput("https://example.com/b")],
        )
        assert [result.accepted for result in results] == [False, True]
        assert len(backend.confirm_calls) == 1


async def test_stop_failure_and_confirmation_state_are_visible(tmp_path: Path) -> None:
    async with runtime(tmp_path) as (service, user_id, _principal, backend):
        created = await service.create_uris(
            user_id=user_id, items=[UriDownloadInput("https://example.com/a")]
        )
        backend.stop_error = DomainError(ErrorCode.SUBMISSION_UNCERTAIN, "offline")
        result = (await service.cancel(user_id=user_id, task_ids=[created[0].task_id]))[0]
        assert result.accepted is True
        assert result.stop_confirmed is False


async def _completed_attachment_with_existing_file_does_not_charge_twice(tmp_path: Path) -> None:
    async with runtime(tmp_path) as (service, user_id, _principal, backend):
        from aria2deck.infrastructure.sqlite.repositories.accounts import AccountRepository
        from aria2deck.infrastructure.sqlite.repositories.content import ContentRepository
        from aria2deck.infrastructure.sqlite.repositories.downloads import DownloadRepository
        from aria2deck.infrastructure.sqlite.repositories.quota import QuotaRepository

        async with service.uow_factory() as uow:
            other_id = await AccountRepository(uow.connection).create_user(
                username="other", password_hash="cd" * 32, is_admin=False,
                is_initial_password=False, quota_bytes=None, now_ms=1,
            )
        created = await service.create_uris(
            user_id=user_id, items=[UriDownloadInput("https://example.com/file")]
        )
        async with service.uow_factory() as uow:
            downloads = DownloadRepository(uow.connection)
            content_id = await ContentRepository(uow.connection).create_object(
                kind="file", digest="ab" * 32, size=10, storage_path="objects/file", now_ms=2,
            )
            await downloads.complete_attempt(created[0].attempt_id, content_id=content_id, now_ms=2)
        attached = await service.create_uris(
            user_id=other_id, items=[UriDownloadInput("https://example.com/file")]
        )
        assert attached[0].outcome is RegistrationOutcome.ATTACHED_COMPLETED
        async with service.uow_factory() as uow:
            await DownloadRepository(uow.connection).delete_history(
                user_id=other_id, task_id=attached[0].task_id, now_ms=3,
            )
        reattached = await service.create_uris(
            user_id=other_id, items=[UriDownloadInput("https://example.com/file")]
        )
        assert reattached[0].outcome is RegistrationOutcome.ATTACHED_COMPLETED
        async with service.uow_factory() as uow:
            assert (await QuotaRepository(uow.connection).get(other_id)).used_bytes == 10


async def test_unknown_size_live_join_has_zero_initial_reservation(tmp_path: Path) -> None:
    async with runtime(tmp_path) as (service, user_id, _principal, backend):
        from aria2deck.infrastructure.sqlite.repositories.accounts import AccountRepository

        async with service.uow_factory() as uow:
            other_id = await AccountRepository(uow.connection).create_user(
                username="other", password_hash="cd" * 32, is_admin=False,
                is_initial_password=False, quota_bytes=None, now_ms=1,
            )
        await service.create_uris(user_id=user_id, items=[UriDownloadInput("https://example.com/shared")])
        joined = await service.create_uris(
            user_id=other_id, items=[UriDownloadInput("https://example.com/shared")]
        )
        assert joined[0].outcome is RegistrationOutcome.JOINED_LIVE
        assert len(backend.submit_calls) == 1
        async with service.uow_factory() as uow:
            from aria2deck.infrastructure.sqlite.repositories.downloads import DownloadRepository

            task = await DownloadRepository(uow.connection).get_task(
                user_id=other_id, task_id=joined[0].task_id,
            )
            assert task.reserved_bytes == 0


@pytest.mark.parametrize("shape", ["count", "gid", "confirm"])
def test_submission_shape_and_confirmation_failures_are_uncertain(tmp_path: Path, shape: str) -> None:
    asyncio.run(_submission_shape_and_confirmation_failures_are_uncertain(tmp_path, shape))


def test_non_uncertain_tracker_failure_is_not_mislabeled(tmp_path: Path) -> None:
    asyncio.run(_non_uncertain_tracker_failure_is_not_mislabeled(tmp_path))


def test_torrent_duplicate_shared_join_cancel_and_retry_create_new_attempt(tmp_path: Path) -> None:
    asyncio.run(_torrent_duplicate_shared_join_cancel_and_retry_create_new_attempt(tmp_path))


def test_create_batch_charges_once_for_duplicate_and_reports_rate_limit(tmp_path: Path) -> None:
    asyncio.run(_create_batch_charges_once_for_duplicate_and_reports_rate_limit(tmp_path))


def test_completed_attachment_with_existing_file_does_not_charge_twice(tmp_path: Path) -> None:
    asyncio.run(_completed_attachment_with_existing_file_does_not_charge_twice(tmp_path))


async def test_torrent_recreate_rebuilds_detached_selection(tmp_path: Path) -> None:
    async with runtime(tmp_path) as (service, user_id, _principal, backend):
        from aria2deck.infrastructure.sqlite.repositories.downloads import DownloadRepository

        payload = make_torrent([("a", 3), ("b", 5)])
        first = await service.create_torrent(
            user_id=user_id, payload_base64=payload, selected_file_indexes=[2]
        )
        await service.cancel(user_id=user_id, task_ids=[first.task_id])
        async with service.uow_factory() as uow:
            expired = await DownloadRepository(uow.connection).expire_history(
                cutoff_ms=2**63 - 1, now_ms=2
            )
            assert expired.detached_sources == 1

        recreated = await service.create_torrent(
            user_id=user_id, payload_base64=payload, selected_file_indexes=[2]
        )
        assert recreated.attempt_id != first.attempt_id
        submission = backend.submit_calls[-1][0]
        assert submission.attempt_id == recreated.attempt_id
        assert submission.source.payload != {}
        assert submission.source.selected_file_indexes == (2,)
        assert submission.source.detached_at_ms is None
