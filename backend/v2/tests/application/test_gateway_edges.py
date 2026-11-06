from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import update

from aria2deck.application.accounts.models import Principal
from aria2deck.application.downloads.models import UriDownloadInput
from aria2deck.bootstrap import create_app
from aria2deck.domain.accounts import CredentialKind
from aria2deck.domain.errors import DomainError, ErrorCode
from aria2deck.infrastructure.config import Settings
from aria2deck.infrastructure.security import CredentialManager
from aria2deck.infrastructure.sqlite.repositories.downloads import DownloadRepository
from aria2deck.infrastructure.sqlite.schema import download_sources
from tests.api.test_internal_gateway import FakeUpstream, response
from tests.download_fakes import FakeSubmissionBackend


def capability(attempt_id: int) -> str:
    return CredentialManager(pepper=None, debug=True).sign_download_fetch(attempt_id=attempt_id, source_index=0)


async def _gateway_rejects_detached_non_http_and_corrupt_sources(tmp_path: Path) -> None:
    settings = Settings(tmp_path / "v2.db", tmp_path / "downloads", tmp_path / "static", True)
    app = create_app(settings=settings)
    async with app.router.lifespan_context(app):
        await app.state.account_service.create_user(None, username="admin", password_hash="ab" * 32, is_admin=True, is_initial_password=False, quota_bytes=None)
        backend = FakeSubmissionBackend()
        app.state.download_commands.backend = backend
        gateway = app.state.download_gateway
        http = (await app.state.download_commands.create_uris(user_id=1, items=[UriDownloadInput("https://example.com/a")]))[0]
        magnet = (await app.state.download_commands.create_uris(user_id=1, items=[UriDownloadInput("magnet:?xt=urn:btih:" + "ab" * 20)]))[0]
        with pytest.raises(DomainError) as caught:
            await gateway.open(attempt_id=magnet.attempt_id, source_index=0, capability=capability(magnet.attempt_id), range_header=None)
        assert caught.value.code is ErrorCode.PERMISSION_DENIED

        with pytest.raises(DomainError) as caught:
            await gateway.open(attempt_id=http.attempt_id, source_index=1, capability=capability(http.attempt_id), range_header=None)
        assert caught.value.code is ErrorCode.NOT_FOUND

        async with app.state.download_commands.uow_factory() as uow:
            repository = DownloadRepository(uow.connection)
            attempt = await repository.get_attempt(http.attempt_id)
            await uow.connection.execute(update(download_sources).where(download_sources.c.id == attempt.source_id).values(detached_at_ms=1))
        with pytest.raises(DomainError) as caught:
            await gateway.open(attempt_id=http.attempt_id, source_index=0, capability=capability(http.attempt_id), range_header=None)
        assert caught.value.code is ErrorCode.DOWNLOAD_UNAVAILABLE

        async def corrupt(payload: dict[str, object], options: str, expected: ErrorCode) -> None:
            created = (await app.state.download_commands.create_uris(user_id=1, items=[UriDownloadInput("https://example.com/" + str(payload) + str(len(options)))]))[0]
            async with app.state.download_commands.uow_factory() as uow:
                attempt = await DownloadRepository(uow.connection).get_attempt(created.attempt_id)
                await uow.connection.execute(update(download_sources).where(download_sources.c.id == attempt.source_id).values(payload_json="{}", options_json=options))
            with pytest.raises(DomainError) as caught:
                await gateway.open(attempt_id=created.attempt_id, source_index=0, capability=capability(created.attempt_id), range_header=None)
            assert caught.value.code is expected

        await corrupt({}, '{"headers":{}}', ErrorCode.INVALID_STATE)
        await corrupt({"uris": ["https://example.com"]}, '{"headers":[]}', ErrorCode.INVALID_STATE)


async def _gateway_direct_admission_success_and_failures(tmp_path: Path) -> None:
    settings = Settings(tmp_path / "v2.db", tmp_path / "downloads", tmp_path / "static", True)
    app = create_app(settings=settings)
    async with app.router.lifespan_context(app):
        view = await app.state.account_service.create_user(None, username="admin", password_hash="ab" * 32, is_admin=True, is_initial_password=False, quota_bytes=None)
        principal = Principal(view.user, CredentialKind.SESSION, "session")
        backend = FakeSubmissionBackend()
        app.state.download_commands.backend = backend
        created = (await app.state.download_commands.create_uris(user_id=view.user.id, items=[UriDownloadInput("https://example.com/a")]))[0]
        gateway = app.state.download_gateway
        upstream, closed = response([b"abc"], total=3, body_bytes=3)
        gateway.upstream = FakeUpstream(upstream)
        opened = await gateway.open(attempt_id=created.attempt_id, source_index=0, capability=capability(created.attempt_id), range_header=None)
        chunks = [chunk async for chunk in opened.body]
        assert chunks == [b"abc"]

        await app.state.account_service.update_user(principal, user_id=view.user.id, changes={"quota_bytes": 4})
        other = (await app.state.download_commands.create_uris(user_id=view.user.id, items=[UriDownloadInput("https://example.com/b")]))[0]
        upstream, closed = response([b"abcde"], total=5, body_bytes=5)
        gateway.upstream = FakeUpstream(upstream)
        with pytest.raises(DomainError) as caught:
            await gateway.open(attempt_id=other.attempt_id, source_index=0, capability=capability(other.attempt_id), range_header=None)
        assert caught.value.code is ErrorCode.QUOTA_EXCEEDED
        assert closed == [True]

        await app.state.settings_service.update(principal, {"max_task_size": 2})
        ranged = (await app.state.download_commands.create_uris(user_id=view.user.id, items=[UriDownloadInput("https://example.com/c")]))[0]
        upstream, closed = response([b"x"], total=None, body_bytes=1, offset=5)
        gateway.upstream = FakeUpstream(upstream)
        with pytest.raises(DomainError) as caught:
            await gateway.open(attempt_id=ranged.attempt_id, source_index=0, capability=capability(ranged.attempt_id), range_header=None)
        assert caught.value.code is ErrorCode.TASK_TOO_LARGE
        assert closed == [True]


def test_gateway_rejects_detached_non_http_and_corrupt_sources(tmp_path: Path) -> None:
    asyncio.run(_gateway_rejects_detached_non_http_and_corrupt_sources(tmp_path))


def test_gateway_direct_admission_success_and_failures(tmp_path: Path) -> None:
    asyncio.run(_gateway_direct_admission_success_and_failures(tmp_path))


def test_gateway_rejects_non_mapping_header_data() -> None:
    async def exercise() -> None:
        from aria2deck.application.downloads.gateway import DownloadGatewayService
        from aria2deck.application.downloads.models import AttemptRecord, SourceRecord
        from aria2deck.domain.downloads import AttemptStatus
        from aria2deck.domain.settings import SystemSettings
        from tests.application.test_history_edges import UoW

        class Repository:
            async def get_attempt(self, attempt_id: int) -> AttemptRecord:
                return AttemptRecord(1, 1, "http:key", "gid", AttemptStatus.QUEUED, None, "submitted", None, 0, 0)

            async def get_source_for_attempt(self, attempt_id: int) -> SourceRecord:
                return SourceRecord(1, "http", "http:key", {"uris": ["https://example.com"]}, {"headers": []}, None, "file", None, None)

        gateway = DownloadGatewayService(
            uow_factory=lambda: UoW(), repository_factory=lambda _uow: Repository(),
            upstream=FakeUpstream(response([], total=None, body_bytes=None)[0]),
            settings=SystemSettings, clock_ms=lambda: 1, verify_capability=lambda *args: True,
        )
        with pytest.raises(DomainError) as caught:
            await gateway.open(attempt_id=1, source_index=0, capability="valid", range_header=None)
        assert caught.value.code is ErrorCode.INVALID_STATE

    asyncio.run(exercise())


def test_gateway_closes_an_over_limit_response_before_streaming() -> None:
    async def exercise() -> None:
        from aria2deck.application.downloads.gateway import DownloadGatewayService
        from aria2deck.application.downloads.models import AttemptRecord, SourceRecord
        from aria2deck.domain.downloads import AttemptStatus
        from aria2deck.domain.settings import SystemSettings
        from tests.application.test_history_edges import UoW

        class Repository:
            async def get_attempt(self, attempt_id: int) -> AttemptRecord:
                return AttemptRecord(1, 1, "http:key", "gid", AttemptStatus.QUEUED, None, "submitted", None, 0, 0)

            async def get_source_for_attempt(self, attempt_id: int) -> SourceRecord:
                return SourceRecord(1, "http", "http:key", {"uris": ["https://example.com"]}, {"headers": {}}, None, "file", None, None)

        upstream, closed = response([b"abc"], total=3, body_bytes=3)
        gateway = DownloadGatewayService(
            uow_factory=lambda: UoW(), repository_factory=lambda _uow: Repository(),
            upstream=FakeUpstream(upstream), settings=lambda: SystemSettings(max_task_size=2),
            clock_ms=lambda: 1, verify_capability=lambda *args: True,
        )
        with pytest.raises(DomainError) as caught:
            await gateway.open(attempt_id=1, source_index=0, capability="valid", range_header=None)
        assert caught.value.code is ErrorCode.TASK_TOO_LARGE
        assert closed == [True]

    asyncio.run(exercise())


async def test_gateway_maps_http_credentials_to_basic_authorization(
    tmp_path: Path,
) -> None:
    """A9：http-user/http-passwd → 上游 Basic 认证；显式 Authorization 头优先。"""
    import base64

    settings = Settings(tmp_path / "v2.db", tmp_path / "downloads", tmp_path / "static", True)
    app = create_app(settings=settings)
    async with app.router.lifespan_context(app):
        view = await app.state.account_service.create_user(
            None, username="admin", password_hash="ab" * 32, is_admin=True,
            is_initial_password=False, quota_bytes=None,
        )
        app.state.download_commands.backend = FakeSubmissionBackend()
        gateway = app.state.download_gateway
        fake = FakeUpstream(response([b"x"], total=1, body_bytes=1)[0])
        gateway.upstream = fake

        credentials = (
            await app.state.download_commands.create_uris(
                user_id=view.user.id,
                items=[
                    UriDownloadInput(
                        "https://example.com/authed",
                        {"http-user": "alice", "http-passwd": "secret"},
                    )
                ],
            )
        )[0]
        assert credentials.accepted
        await gateway.open(
            attempt_id=credentials.attempt_id,
            source_index=0,
            capability=capability(credentials.attempt_id),
            range_header=None,
        )
        url, headers, _ = fake.calls[-1]
        expected = base64.b64encode(b"alice:secret").decode()
        assert headers["Authorization"] == f"Basic {expected}"

        explicit = (
            await app.state.download_commands.create_uris(
                user_id=view.user.id,
                items=[
                    UriDownloadInput(
                        "https://example.com/explicit",
                        {
                            "http-user": "alice",
                            "http-passwd": "secret",
                            "headers": {"Authorization": "Bearer token"},
                        },
                    )
                ],
            )
        )[0]
        assert explicit.accepted
        await gateway.open(
            attempt_id=explicit.attempt_id,
            source_index=0,
            capability=capability(explicit.attempt_id),
            range_header=None,
        )
        _, explicit_headers, _ = fake.calls[-1]
        assert explicit_headers["authorization"] == "Bearer token"

        username_only = (
            await app.state.download_commands.create_uris(
                user_id=view.user.id,
                items=[
                    UriDownloadInput("https://example.com/useronly", {"http-user": "bob"})
                ],
            )
        )[0]
        assert username_only.accepted
        await gateway.open(
            attempt_id=username_only.attempt_id,
            source_index=0,
            capability=capability(username_only.attempt_id),
            range_header=None,
        )
        _, no_password_headers, _ = fake.calls[-1]
        assert no_password_headers["Authorization"] == f"Basic {base64.b64encode(b'bob:').decode()}"
