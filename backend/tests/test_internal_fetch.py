from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.services.internal_fetch import (
    CAPABILITY_HEADER,
    GatewayDownloadNotFound,
    GatewaySizeExceeded,
    GatewayTargetError,
    GatewayUpstreamError,
    InvalidCapabilityError,
    SourceRequestOptions,
    authorize_gateway_request,
    create_capability,
    http_resource_identity,
    open_gateway_stream,
    source_request_options,
    verify_capability,
)
from tests.helpers_v0 import create_global_download_v0


def _response(
    status: int,
    *,
    headers: dict[str, str] | None = None,
    chunks: tuple[bytes, ...] = (),
) -> MagicMock:
    response = MagicMock()
    response.status = status
    response.headers = headers or {}
    response.close = MagicMock()

    async def iter_chunks(_size: int) -> AsyncIterator[bytes]:
        for chunk in chunks:
            yield chunk

    response.content.iter_chunked = MagicMock(side_effect=iter_chunks)
    return response


def _session(*responses: MagicMock) -> MagicMock:
    session = MagicMock()
    session.get = AsyncMock(side_effect=responses)
    session.close = AsyncMock()
    return session


async def _consume(stream) -> bytes:
    return b"".join([chunk async for chunk in stream.iter_bytes()])


def test_http_resource_identity_is_secret_safe_and_canonical(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.internal_fetch.settings.secret_key",
        "identity-test-secret",
    )
    base_key = "url-hash"
    first = source_request_options(
        {
            "header": ["X-Api-Key: source-secret", "Referer: https://app/"],
            "http-user": "alice",
            "http-passwd": "password",
        },
        mirrors=["https://mirror.example/file"],
    )
    reordered = source_request_options(
        {
            "header": ["referer: https://app/", "x-api-key: source-secret"],
            "http-user": "alice",
            "http-passwd": "password",
        },
        mirrors=["https://mirror.example/file"],
    )

    identity = http_resource_identity(base_key, first)

    assert http_resource_identity(base_key, SourceRequestOptions()) == base_key
    assert identity == http_resource_identity(base_key, reordered)
    assert identity != http_resource_identity(base_key, SourceRequestOptions())
    assert len(identity) == 64
    assert "source-secret" not in identity
    assert "alice" not in identity


def test_capability_binds_download_uri_and_source_options(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.internal_fetch.settings.secret_key",
        "capability-test-secret",
    )
    options = source_request_options(
        {
            "header": ["X-Api-Key: secret", "Authorization: Bearer source"],
            "http-user": "alice",
            "http-passwd": "password",
        }
    )
    capability = create_capability(7, "https://example.com/file", options)

    verified = verify_capability(
        capability,
        7,
        "https://example.com/file",
    )

    assert verified == options
    with pytest.raises(InvalidCapabilityError, match="下载凭证无效"):
        verify_capability(capability, 8, "https://example.com/file")
    with pytest.raises(InvalidCapabilityError, match="下载凭证无效"):
        verify_capability(capability, 7, "https://example.com/other")


@pytest.mark.parametrize(
    "header",
    [
        "Host: attacker.test",
        "Range: bytes=0-1",
        f"{CAPABILITY_HEADER}: forged",
        "X-Test: ok\r\nInjected: value",
    ],
)
def test_source_headers_cannot_override_gateway_controls(header: str) -> None:
    with pytest.raises(ValueError):
        source_request_options({"header": header})


@pytest.mark.asyncio
async def test_gateway_route_rejects_missing_and_forged_capability(
    client: TestClient,
    temp_db: str,
) -> None:
    download = await create_global_download_v0(
        resource_key="gateway:auth",
        resource_kind="http",
        source_uri="https://example.com/file.bin",
        status="active",
    )

    missing = client.get(f"/_internal/fetch/{download['id']}/0")
    forged = client.get(
        f"/_internal/fetch/{download['id']}/0",
        headers={CAPABILITY_HEADER: "invalid.invalid"},
    )

    assert missing.status_code == 401
    assert forged.status_code == 403


@pytest.mark.asyncio
async def test_gateway_route_rejects_nonexistent_terminal_and_non_http(
    client: TestClient,
    temp_db: str,
) -> None:
    terminal = await create_global_download_v0(
        resource_key="gateway:terminal",
        resource_kind="http",
        source_uri="https://example.com/terminal.bin",
        status="failed",
    )
    magnet = await create_global_download_v0(
        resource_key="gateway:magnet",
        resource_kind="magnet",
        source_uri="magnet:?xt=urn:btih:gateway",
        status="active",
    )
    terminal_capability = create_capability(
        int(terminal["id"]),
        str(terminal["source_uri"]),
        SourceRequestOptions(),
    )
    magnet_capability = create_capability(
        int(magnet["id"]),
        str(magnet["source_uri"]),
        SourceRequestOptions(),
    )

    nonexistent = client.get(
        "/_internal/fetch/999999/0",
        headers={CAPABILITY_HEADER: "invalid.invalid"},
    )
    terminal_response = client.get(
        f"/_internal/fetch/{terminal['id']}/0",
        headers={CAPABILITY_HEADER: terminal_capability},
    )
    magnet_response = client.get(
        f"/_internal/fetch/{magnet['id']}/0",
        headers={CAPABILITY_HEADER: magnet_capability},
    )

    assert nonexistent.status_code == 404
    assert terminal_response.status_code == 410
    assert magnet_response.status_code == 403


@pytest.mark.asyncio
async def test_capability_authorizes_only_its_signed_mirror_indexes(
    temp_db: str,
) -> None:
    download = await create_global_download_v0(
        resource_key="gateway:mirrors",
        resource_kind="http",
        source_uri="https://origin.example/file",
        status="active",
    )
    options = SourceRequestOptions(
        mirrors=("https://mirror.example/file",)
    )
    capability = create_capability(
        int(download["id"]),
        str(download["source_uri"]),
        options,
    )

    primary, _ = await authorize_gateway_request(
        int(download["id"]), 0, capability
    )
    mirror, _ = await authorize_gateway_request(
        int(download["id"]), 1, capability
    )

    assert primary == "https://origin.example/file"
    assert mirror == "https://mirror.example/file"
    with pytest.raises(GatewayDownloadNotFound):
        await authorize_gateway_request(int(download["id"]), 2, capability)


@pytest.mark.asyncio
async def test_gateway_follows_safe_redirect_without_exposing_it_to_aria2(
    monkeypatch,
) -> None:
    redirect = _response(
        302,
        headers={"Location": "https://cdn.example.com/file.bin"},
    )
    final = _response(
        200,
        headers={
            "Content-Length": "4",
            "Content-Type": "application/octet-stream",
            "Location": "https://ignored.example/",
            "Set-Cookie": "session=secret",
        },
        chunks=(b"safe",),
    )
    session = _session(redirect, final)
    monkeypatch.setattr(
        "app.services.internal_fetch.get_max_task_size",
        lambda: 10,
    )
    with patch(
        "app.services.internal_fetch.create_public_connector",
        return_value=MagicMock(),
    ), patch(
        "app.services.internal_fetch.aiohttp.ClientSession",
        return_value=session,
    ):
        stream = await open_gateway_stream(
            source_uri="https://origin.example/file",
            options=SourceRequestOptions(),
            range_header=None,
        )
        payload = await _consume(stream)

    assert payload == b"safe"
    assert session.get.await_count == 2
    assert [call.args[0] for call in session.get.await_args_list] == [
        "https://origin.example/file",
        "https://cdn.example.com/file.bin",
    ]
    assert "Location" not in stream.headers
    assert "Set-Cookie" not in stream.headers


@pytest.mark.asyncio
async def test_gateway_rejects_private_redirect_before_next_transport() -> None:
    redirect = _response(
        302,
        headers={"Location": "http://127.0.0.1/private"},
    )
    session = _session(redirect)
    with patch(
        "app.services.internal_fetch.create_public_connector",
        return_value=MagicMock(),
    ), patch(
        "app.services.internal_fetch.aiohttp.ClientSession",
        return_value=session,
    ):
        with pytest.raises(GatewayTargetError):
            await open_gateway_stream(
                source_uri="https://origin.example/file",
                options=SourceRequestOptions(),
                range_header=None,
            )

    session.get.assert_awaited_once()
    assert session.get.await_args.args[0] == "https://origin.example/file"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [300, 304, 305])
async def test_gateway_rejects_unsupported_3xx(status: int) -> None:
    session = _session(_response(status))
    with patch(
        "app.services.internal_fetch.create_public_connector",
        return_value=MagicMock(),
    ), patch(
        "app.services.internal_fetch.aiohttp.ClientSession",
        return_value=session,
    ):
        with pytest.raises(GatewayUpstreamError, match="不支持的重定向"):
            await open_gateway_stream(
                source_uri="https://origin.example/file",
                options=SourceRequestOptions(),
                range_header=None,
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "headers", "message"),
    [
        (101, {}, "协议切换"),
        (200, {"Content-Encoding": "gzip"}, "内容编码"),
    ],
)
async def test_gateway_rejects_protocol_switch_or_encoded_payload(
    status: int,
    headers: dict[str, str],
    message: str,
) -> None:
    session = _session(_response(status, headers=headers))
    with patch(
        "app.services.internal_fetch.create_public_connector",
        return_value=MagicMock(),
    ), patch(
        "app.services.internal_fetch.aiohttp.ClientSession",
        return_value=session,
    ):
        with pytest.raises(GatewayUpstreamError, match=message):
            await open_gateway_stream(
                source_uri="https://origin.example/file",
                options=SourceRequestOptions(),
                range_header=None,
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [{}, {"Content-Length": "2"}])
async def test_gateway_runtime_limit_stops_chunked_or_lying_upstream(
    headers: dict[str, str],
    monkeypatch,
) -> None:
    session = _session(
        _response(200, headers=headers, chunks=(b"1234", b"5678"))
    )
    monkeypatch.setattr(
        "app.services.internal_fetch.get_max_task_size",
        lambda: 5,
    )
    with patch(
        "app.services.internal_fetch.create_public_connector",
        return_value=MagicMock(),
    ), patch(
        "app.services.internal_fetch.aiohttp.ClientSession",
        return_value=session,
    ):
        stream = await open_gateway_stream(
            source_uri="https://origin.example/file",
            options=SourceRequestOptions(),
            range_header=None,
        )
        forwarded = bytearray()
        with pytest.raises(GatewaySizeExceeded):
            async for chunk in stream.iter_bytes():
                forwarded.extend(chunk)

    assert bytes(forwarded) == b"12345"
    assert len(forwarded) == 5
    session.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_gateway_rejects_declared_oversize_before_stream(monkeypatch) -> None:
    response = _response(
        200,
        headers={"Content-Length": "6"},
        chunks=(b"123456",),
    )
    session = _session(response)
    monkeypatch.setattr(
        "app.services.internal_fetch.get_max_task_size",
        lambda: 5,
    )
    with patch(
        "app.services.internal_fetch.create_public_connector",
        return_value=MagicMock(),
    ), patch(
        "app.services.internal_fetch.aiohttp.ClientSession",
        return_value=session,
    ):
        with pytest.raises(GatewaySizeExceeded):
            await open_gateway_stream(
                source_uri="https://origin.example/file",
                options=SourceRequestOptions(),
                range_header=None,
            )

    response.content.iter_chunked.assert_not_called()
    session.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_gateway_range_resume_uses_remaining_budget(monkeypatch) -> None:
    response = _response(
        206,
        headers={
            "Content-Range": "bytes 4-5/*",
        },
        chunks=(b"56",),
    )
    session = _session(response)
    monkeypatch.setattr(
        "app.services.internal_fetch.get_max_task_size",
        lambda: 5,
    )
    with patch(
        "app.services.internal_fetch.create_public_connector",
        return_value=MagicMock(),
    ), patch(
        "app.services.internal_fetch.aiohttp.ClientSession",
        return_value=session,
    ):
        stream = await open_gateway_stream(
            source_uri="https://origin.example/file",
            options=SourceRequestOptions(),
            range_header="bytes=4-",
        )
        forwarded = bytearray()
        with pytest.raises(GatewaySizeExceeded):
            async for chunk in stream.iter_bytes():
                forwarded.extend(chunk)

    assert bytes(forwarded) == b"5"
    sent_headers = dict(session.get.await_args.kwargs["headers"])
    assert sent_headers["Range"] == "bytes=4-"


@pytest.mark.asyncio
async def test_gateway_valid_range_resume_streams_expected_bytes(monkeypatch) -> None:
    response = _response(
        206,
        headers={
            "Content-Length": "3",
            "Content-Range": "bytes 2-4/5",
            "Accept-Ranges": "bytes",
        },
        chunks=(b"345",),
    )
    session = _session(response)
    monkeypatch.setattr(
        "app.services.internal_fetch.get_max_task_size",
        lambda: 5,
    )
    with patch(
        "app.services.internal_fetch.create_public_connector",
        return_value=MagicMock(),
    ), patch(
        "app.services.internal_fetch.aiohttp.ClientSession",
        return_value=session,
    ):
        stream = await open_gateway_stream(
            source_uri="https://origin.example/file",
            options=SourceRequestOptions(),
            range_header="bytes=2-",
        )
        assert await _consume(stream) == b"345"


@pytest.mark.asyncio
async def test_gateway_strips_auth_on_cross_origin_redirect(monkeypatch) -> None:
    redirect = _response(
        302,
        headers={"Location": "https://cdn.example.com/file"},
    )
    return_redirect = _response(
        302,
        headers={"Location": "https://origin.example/final"},
    )
    final = _response(200, headers={"Content-Length": "2"}, chunks=(b"ok",))
    session = _session(redirect, return_redirect, final)
    monkeypatch.setattr(
        "app.services.internal_fetch.get_max_task_size",
        lambda: 10,
    )
    options = SourceRequestOptions(
        headers=(("Cookie", "source=session"), ("X-Source", "value")),
        username="alice",
        password="password",
    )
    with patch(
        "app.services.internal_fetch.create_public_connector",
        return_value=MagicMock(),
    ), patch(
        "app.services.internal_fetch.aiohttp.ClientSession",
        return_value=session,
    ):
        stream = await open_gateway_stream(
            source_uri="https://origin.example/file",
            options=options,
            range_header=None,
        )
        assert await _consume(stream) == b"ok"

    initial_headers = dict(session.get.await_args_list[0].kwargs["headers"])
    redirected_headers = dict(session.get.await_args_list[1].kwargs["headers"])
    returned_headers = dict(session.get.await_args_list[2].kwargs["headers"])
    assert initial_headers["Authorization"].startswith("Basic ")
    assert initial_headers["Cookie"] == "source=session"
    assert initial_headers["X-Source"] == "value"
    assert "Authorization" not in redirected_headers
    assert "Cookie" not in redirected_headers
    assert "X-Source" not in redirected_headers
    assert redirected_headers == {"Accept-Encoding": "identity"}
    assert returned_headers == {"Accept-Encoding": "identity"}
    assert CAPABILITY_HEADER not in initial_headers
    assert CAPABILITY_HEADER not in redirected_headers
    assert CAPABILITY_HEADER not in returned_headers


@pytest.mark.asyncio
async def test_gateway_route_rejects_huge_range_with_416(
    client: TestClient,
    temp_db: str,
) -> None:
    download = await create_global_download_v0(
        resource_key="gateway:huge-range",
        resource_kind="http",
        source_uri="https://example.com/file.bin",
        status="active",
    )
    capability = create_capability(
        int(download["id"]),
        str(download["source_uri"]),
        SourceRequestOptions(),
    )

    response = client.get(
        f"/_internal/fetch/{download['id']}/0",
        headers={
            CAPABILITY_HEADER: capability,
            "Range": f"bytes={'9' * 1000}-",
        },
    )

    assert response.status_code == 416
    assert "Range" in response.json()["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "headers", "range_header"),
    [
        (200, {"Content-Length": "9" * 100}, None),
        (206, {"Content-Range": f"bytes 0-1/{'9' * 100}"}, "bytes=0-"),
    ],
)
async def test_gateway_route_rejects_huge_upstream_size_fields_with_502(
    client: TestClient,
    temp_db: str,
    status: int,
    headers: dict[str, str],
    range_header: str | None,
) -> None:
    download = await create_global_download_v0(
        resource_key=f"gateway:huge-upstream:{status}",
        resource_kind="http",
        source_uri="https://example.com/file.bin",
        status="active",
    )
    capability = create_capability(
        int(download["id"]),
        str(download["source_uri"]),
        SourceRequestOptions(),
    )
    session = _session(_response(status, headers=headers))
    request_headers = {CAPABILITY_HEADER: capability}
    if range_header is not None:
        request_headers["Range"] = range_header

    with patch(
        "app.services.internal_fetch.create_public_connector",
        return_value=MagicMock(),
    ), patch(
        "app.services.internal_fetch.aiohttp.ClientSession",
        return_value=session,
    ):
        response = client.get(
            f"/_internal/fetch/{download['id']}/0",
            headers=request_headers,
        )

    assert response.status_code == 502
    assert "无效" in response.json()["detail"]
    session.close.assert_awaited_once()
