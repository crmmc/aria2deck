from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping

import pytest
from fastapi.testclient import TestClient

from aria2deck.application.downloads.models import UpstreamResponse
from aria2deck.domain.errors import DomainError, ErrorCode
from aria2deck.infrastructure.config import Settings
from aria2deck.infrastructure.security import CredentialManager
from aria2deck.infrastructure.sqlite.engine import SQLiteDatabase
from aria2deck.infrastructure.sqlite.repositories.downloads import DownloadRepository
from aria2deck.infrastructure.sqlite.repositories.quota import QuotaRepository
from aria2deck.infrastructure.sqlite.unit_of_work import SQLiteUnitOfWork
from tests.api.conftest import OTHER_PASSWORD, create_admin, create_user, login
from tests.api.test_download_writes import install_backend


class FakeUpstream:
    def __init__(self, response: UpstreamResponse) -> None:
        self.response = response
        self.calls: list[tuple[str, Mapping[str, str], str | None]] = []

    async def open(
        self, *, url: str, headers: Mapping[str, str], range_header: str | None
    ) -> UpstreamResponse:
        self.calls.append((url, headers, range_header))
        return self.response


def response(
    chunks: list[bytes],
    *,
    total: int | None,
    body_bytes: int | None,
    status: int = 200,
    headers: dict[str, str] | None = None,
    offset: int = 0,
) -> tuple[UpstreamResponse, list[bool]]:
    closed: list[bool] = []

    async def body() -> AsyncIterator[bytes]:
        for chunk in chunks:
            yield chunk

    async def close() -> None:
        closed.append(True)

    return UpstreamResponse(
        status=status,
        headers=headers or {},
        body=body(),
        total_bytes=total,
        body_bytes=body_bytes,
        close=close,
        content_offset=offset,
    ), closed


def create_task(client: TestClient, settings: Settings, *, uri: str = "https://example.com/file", headers: dict[str, str] | None = None) -> tuple[int, int]:
    create_admin(client)
    login(client)
    install_backend(client)
    task_id = client.post(
        "/api/v2/tasks",
        json={"tasks": [{"uri": uri, "options": {"headers": headers or {}}}]},
    ).json()["items"][0]["task"]["id"]

    async def lookup() -> int:
        database = SQLiteDatabase(settings.database_path)
        try:
            async with SQLiteUnitOfWork(database) as uow:
                return (await DownloadRepository(uow.connection).get_task(user_id=1, task_id=task_id)).attempt_id
        finally:
            await database.close()

    return task_id, asyncio.run(lookup())


def capability(attempt_id: int) -> str:
    return CredentialManager(pepper=None, debug=True).sign_download_fetch(attempt_id=attempt_id, source_index=0)


def test_internal_gateway_streams_only_signed_sources_and_preserves_upstream_auth(
    client: TestClient, settings: Settings
) -> None:
    _, attempt_id = create_task(client, settings, headers={"Authorization": "private"})
    upstream_response, closed = response(
        [b"", b"ab", b"c"], total=3, body_bytes=3, headers={"Content-Length": "3"}
    )
    upstream = FakeUpstream(upstream_response)
    client.app.state.download_gateway.upstream = upstream
    url = f"/api/v2/internal/fetch/{attempt_id}/0"

    assert client.get(url).status_code == 401
    assert client.get(url, headers={"X-Mjiz-Fetch-Capability": "wrong"}).status_code == 403
    result = client.get(
        url,
        headers={"X-Mjiz-Fetch-Capability": capability(attempt_id), "Range": "bytes=0-2"},
    )
    assert result.status_code == 200
    assert result.content == b"abc"
    assert result.headers["Accept-Ranges"] == "bytes"
    assert upstream.calls == [("https://example.com/file", {"authorization": "private"}, "bytes=0-2")]
    assert closed == [True]

    async def quota() -> int:
        database = SQLiteDatabase(settings.database_path)
        try:
            async with SQLiteUnitOfWork(database) as uow:
                return (await QuotaRepository(uow.connection).get(1)).reserved_bytes
        finally:
            await database.close()

    assert asyncio.run(quota()) == 3
    assert client.get(f"/api/v2/internal/fetch/{attempt_id}/1", headers={"X-Mjiz-Fetch-Capability": capability(attempt_id)}).status_code == 404


def test_terminal_and_non_http_sources_cannot_use_gateway(client: TestClient, settings: Settings) -> None:
    task_id, attempt_id = create_task(client, settings)
    client.post("/api/v2/tasks/cancel", json={"task_ids": [task_id]})
    headers = {"X-Mjiz-Fetch-Capability": capability(attempt_id)}
    assert client.get(f"/api/v2/internal/fetch/{attempt_id}/0", headers=headers).status_code == 410


def test_single_user_over_quota_fails_subscription_before_streaming(
    client: TestClient, settings: Settings
) -> None:
    _, attempt_id = create_task(client, settings)
    assert client.patch("/api/v2/users/1", json={"quota_bytes": 2}).status_code == 200
    upstream_response, closed = response([b"abc"], total=3, body_bytes=3)
    client.app.state.download_gateway.upstream = FakeUpstream(upstream_response)

    result = client.get(
        f"/api/v2/internal/fetch/{attempt_id}/0",
        headers={"X-Mjiz-Fetch-Capability": capability(attempt_id)},
    )
    assert result.status_code == 409
    assert result.json()["error"]["code"] == "quota_exceeded"
    assert closed == [True]
    assert client.get(f"/api/v2/tasks/{attempt_id}").json()["status"] == "failed"


def test_shared_gateway_only_rejects_the_over_quota_subscriber(
    client: TestClient, settings: Settings
) -> None:
    admin = create_admin(client)
    login(client)
    create_user(client)
    install_backend(client)
    first_id = client.post(
        "/api/v2/tasks", json={"tasks": [{"uri": "https://example.com/shared"}]}
    ).json()["items"][0]["task"]["id"]
    login(client, "user", OTHER_PASSWORD)
    second_id = client.post(
        "/api/v2/tasks", json={"tasks": [{"uri": "https://example.com/shared"}]}
    ).json()["items"][0]["task"]["id"]
    login(client)
    assert client.patch(f"/api/v2/users/{admin['id']}", json={"quota_bytes": 2}).status_code == 200

    async def attempt() -> int:
        database = SQLiteDatabase(settings.database_path)
        try:
            async with SQLiteUnitOfWork(database) as uow:
                return (await DownloadRepository(uow.connection).get_task(user_id=1, task_id=first_id)).attempt_id
        finally:
            await database.close()

    attempt_id = asyncio.run(attempt())
    upstream_response, _ = response([b"abc"], total=3, body_bytes=3)
    client.app.state.download_gateway.upstream = FakeUpstream(upstream_response)
    result = client.get(
        f"/api/v2/internal/fetch/{attempt_id}/0",
        headers={"X-Mjiz-Fetch-Capability": capability(attempt_id)},
    )
    assert result.status_code == 200
    assert result.content == b"abc"
    assert client.get(f"/api/v2/tasks/{first_id}").json()["status"] == "failed"

    async def quotas() -> tuple[int, int, int]:
        database = SQLiteDatabase(settings.database_path)
        try:
            async with SQLiteUnitOfWork(database) as uow:
                quota = QuotaRepository(uow.connection)
                first = await quota.get(1)
                second = await quota.get(2)
                return first.reserved_bytes, second.reserved_bytes, second_id
        finally:
            await database.close()

    assert asyncio.run(quotas()) == (0, 3, second_id)


def test_gateway_enforces_configured_size_before_quota(client: TestClient, settings: Settings) -> None:
    _, attempt_id = create_task(client, settings)
    assert client.patch("/api/v2/settings", json={"max_task_size": 2}).status_code == 200
    upstream_response, closed = response([b"abc"], total=3, body_bytes=3)
    client.app.state.download_gateway.upstream = FakeUpstream(upstream_response)
    result = client.get(
        f"/api/v2/internal/fetch/{attempt_id}/0",
        headers={"X-Mjiz-Fetch-Capability": capability(attempt_id)},
    )
    assert result.status_code == 413
    assert result.json()["error"]["code"] == "task_too_large"
    assert closed == [True]


def test_unknown_length_grows_atomic_reservation_from_actual_bytes(
    client: TestClient, settings: Settings
) -> None:
    _, attempt_id = create_task(client, settings)
    upstream_response, _ = response([b"ab", b"cd"], total=None, body_bytes=None)
    client.app.state.download_gateway.upstream = FakeUpstream(upstream_response)
    result = client.get(
        f"/api/v2/internal/fetch/{attempt_id}/0",
        headers={"X-Mjiz-Fetch-Capability": capability(attempt_id)},
    )
    assert result.content == b"abcd"

    async def quota() -> int:
        database = SQLiteDatabase(settings.database_path)
        try:
            async with SQLiteUnitOfWork(database) as uow:
                return (await QuotaRepository(uow.connection).get(1)).reserved_bytes
        finally:
            await database.close()

    assert asyncio.run(quota()) == 4


@pytest.mark.parametrize("overflow", ["limit", "declared"])
def test_streaming_rejects_actual_bytes_beyond_limit_or_declaration(
    client: TestClient, settings: Settings, overflow: str
) -> None:
    _, attempt_id = create_task(client, settings)
    if overflow == "limit":
        assert client.patch("/api/v2/settings", json={"max_task_size": 2}).status_code == 200
        upstream_response, closed = response([b"abc"], total=None, body_bytes=None)
        expected = ErrorCode.TASK_TOO_LARGE
    else:
        upstream_response, closed = response([b"abc"], total=None, body_bytes=2)
        expected = ErrorCode.UPSTREAM_FAILURE
    client.app.state.download_gateway.upstream = FakeUpstream(upstream_response)

    async def collect() -> None:
        opened = await client.app.state.download_gateway.open(
            attempt_id=attempt_id,
            source_index=0,
            capability=capability(attempt_id),
            range_header=None,
        )
        with pytest.raises(DomainError) as caught:
            async for _ in opened.body:
                pass
        assert caught.value.code is expected

    asyncio.run(collect())
    assert closed == [True]


def test_range_metadata_is_forwarded_and_full_entity_is_reserved(
    client: TestClient, settings: Settings
) -> None:
    _, attempt_id = create_task(client, settings)
    upstream_response, _ = response(
        [b"ok"], total=20, body_bytes=2, status=206, headers={"Content-Length": "2", "Content-Range": "bytes 5-6/20"}, offset=5
    )
    client.app.state.download_gateway.upstream = FakeUpstream(upstream_response)
    result = client.get(
        f"/api/v2/internal/fetch/{attempt_id}/0",
        headers={"X-Mjiz-Fetch-Capability": capability(attempt_id)},
    )
    assert result.status_code == 206
    assert result.headers["Content-Range"] == "bytes 5-6/20"
