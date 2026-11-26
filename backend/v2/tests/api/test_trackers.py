from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from aria2deck.application.accounts.models import Principal
from aria2deck.bootstrap import create_app
from aria2deck.domain.accounts import CredentialKind
from aria2deck.domain.errors import DomainError, ErrorCode
from aria2deck.infrastructure.config import Settings
from aria2deck.infrastructure.sqlite.repositories.trackers import TrackerRepository
from aria2deck.infrastructure.sqlite.schema import tracker_cache
from tests.api.conftest import OTHER_PASSWORD, create_admin, create_user, login

FIRST = "https://one.example/trackers"
SECOND = "https://two.example/trackers"


class FakeFetcher:
    def __init__(self) -> None:
        self.results: dict[str, str | Exception] = {}
        self.calls: list[str] = []

    async def fetch_text(self, url: str) -> str:
        self.calls.append(url)
        result = self.results[url]
        if isinstance(result, Exception):
            raise result
        return result


def configure(client: TestClient) -> FakeFetcher:
    create_admin(client)
    login(client)
    create_user(client)
    response = client.patch(
        "/api/v2/settings",
        json={"tracker_fixed_list": ["udp://fixed.example:80"], "tracker_remote_urls": [FIRST, SECOND]},
    )
    assert response.status_code == 200
    fetcher = FakeFetcher()
    client.app.state.tracker_service.fetcher = fetcher
    return fetcher


def test_tracker_refresh_merges_success_and_preserves_partial_failures(client: TestClient) -> None:
    fetcher = configure(client)
    fetcher.results = {
        FIRST: "udp://one.example:80\nudp://fixed.example:80\ninvalid",
        SECOND: DomainError(ErrorCode.UPSTREAM_FAILURE, "offline"),
    }
    response = client.post("/api/v2/trackers/refresh")
    assert response.status_code == 200
    body = response.json()
    assert body["refresh_status"] == "partial"
    assert body["items"] == ["udp://fixed.example:80", "udp://one.example:80"]
    assert body["failed_urls"] == [SECOND]
    assert body["entry_count"] == 2
    assert client.get("/api/v2/trackers").json() == body


def test_tracker_all_failed_preserves_last_good_remote_and_empty_success_is_partial(
    client: TestClient,
) -> None:
    fetcher = configure(client)
    fetcher.results = {FIRST: "udp://one.example:80", SECOND: "udp://two.example:80"}
    assert client.post("/api/v2/trackers/refresh").json()["refresh_status"] == "success"

    fetcher.results = {FIRST: DomainError(ErrorCode.UPSTREAM_FAILURE, "offline"), SECOND: DomainError(ErrorCode.UPSTREAM_FAILURE, "offline")}
    failed = client.post("/api/v2/trackers/refresh").json()
    assert failed["refresh_status"] == "failed"
    assert failed["failed_urls"] == [FIRST, SECOND]

    fetcher.results = {FIRST: "# valid empty list", SECOND: DomainError(ErrorCode.UPSTREAM_FAILURE, "offline")}
    partial = client.post("/api/v2/trackers/refresh").json()
    assert partial["refresh_status"] == "partial"


def test_tracker_permissions_and_empty_configuration(client: TestClient) -> None:
    fetcher = configure(client)
    login(client, "user", OTHER_PASSWORD)
    assert client.get("/api/v2/trackers").status_code == 403
    assert client.post("/api/v2/trackers/refresh").status_code == 403
    login(client)
    assert client.patch("/api/v2/settings", json={"tracker_remote_urls": [], "tracker_fixed_list": []}).status_code == 200
    refreshed = client.post("/api/v2/trackers/refresh")
    assert refreshed.json()["refresh_status"] == "success"
    assert refreshed.json()["items"] == []
    assert fetcher.calls == []


@pytest.mark.parametrize("bad_json", ["not-json", "{}", "[1]"])
async def test_tracker_repository_round_trip_and_corruption(settings: Settings, bad_json: str) -> None:
    app = create_app(settings=settings)
    async with app.router.lifespan_context(app):
        async with app.state.download_commands.uow_factory() as uow:
            repository = TrackerRepository(uow.connection)
            cache = await repository.get()
            assert cache.refresh_status == "never"
            await repository.save(cache)
            await uow.connection.execute(tracker_cache.update().values(remote_json=bad_json))
            with pytest.raises(DomainError):
                await repository.get()
        async with app.state.download_commands.uow_factory() as uow:
            await uow.connection.execute(delete(tracker_cache))
            with pytest.raises(DomainError):
                await TrackerRepository(uow.connection).get()
            with pytest.raises(DomainError):
                await TrackerRepository(uow.connection).save(cache)


async def test_tracker_concurrent_refreshes_are_serialized(settings: Settings) -> None:
    app = create_app(settings=settings)
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
        await app.state.settings_service.update(principal, {"tracker_remote_urls": [FIRST]})

        class SlowFetcher:
            def __init__(self) -> None:
                self.active = 0
                self.maximum = 0

            async def fetch_text(self, url: str) -> str:
                self.active += 1
                self.maximum = max(self.maximum, self.active)
                await asyncio.sleep(0)
                self.active -= 1
                return "udp://one.example:80"

        fetcher = SlowFetcher()
        app.state.tracker_service.fetcher = fetcher
        await asyncio.gather(app.state.tracker_service.refresh(), app.state.tracker_service.refresh())
        assert fetcher.maximum == 1


@pytest.mark.parametrize("minutes", [0, 5])
async def test_tracker_worker_observes_manual_and_periodic_modes(
    settings: Settings, minutes: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app(settings=settings)
    async with app.router.lifespan_context(app):
        view = await app.state.account_service.create_user(
            None, username="admin", password_hash="ab" * 32, is_admin=True,
            is_initial_password=False, quota_bytes=None,
        )
        principal = Principal(view.user, CredentialKind.SESSION, "session")
        if minutes:
            await app.state.settings_service.update(principal, {"tracker_refresh_interval_minutes": minutes, "tracker_remote_urls": [FIRST]})
        fetcher = FakeFetcher()
        fetcher.results[FIRST] = "udp://one.example:80"
        app.state.tracker_service.fetcher = fetcher
        assert await app.state.tracker_service.current() == ()
        assert (await app.state.tracker_service.status(principal)).refresh_status == "never"
        await app.state.supervisor.stop()
        delays: list[float] = []

        async def stop_after_second(delay: float) -> None:
            delays.append(delay)
            if len(delays) == 2:
                raise RuntimeError("stop")

        monkeypatch.setattr("aria2deck.application.settings.trackers.asyncio.sleep", stop_after_second)
        with pytest.raises(RuntimeError, match="stop"):
            await app.state.tracker_service.run_worker()
        assert delays == [minutes * 60 if minutes else 60] * 2
        assert fetcher.calls == ([FIRST] if minutes else [])


async def test_tracker_does_not_swallow_cancellation(settings: Settings) -> None:
    app = create_app(settings=settings)
    async with app.router.lifespan_context(app):
        view = await app.state.account_service.create_user(
            None, username="admin", password_hash="ab" * 32, is_admin=True,
            is_initial_password=False, quota_bytes=None,
        )
        principal = Principal(view.user, CredentialKind.SESSION, "session")
        await app.state.settings_service.update(principal, {"tracker_remote_urls": [FIRST]})

        class CancelledFetcher:
            async def fetch_text(self, url: str) -> str:
                raise asyncio.CancelledError

        app.state.tracker_service.fetcher = CancelledFetcher()
        with pytest.raises(asyncio.CancelledError):
            await app.state.tracker_service.refresh()
