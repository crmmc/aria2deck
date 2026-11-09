from __future__ import annotations

import socket

import aiohttp
import pytest

from aria2deck.domain.errors import DomainError, ErrorCode
from aria2deck.infrastructure.http import (
    PublicResolver,
    SafeHttpClient,
    require_public_address,
    validate_public_url,
)


@pytest.mark.parametrize("url", ["https://example.com/a?x=1", "http://8.8.8.8/a"])
def test_public_url_accepts_safe_http_targets(url: str) -> None:
    validate_public_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1",
        "http://10.0.0.1",
        "http://169.254.1.1",
        "http://[::1]",
        "http://localhost",
        "ftp://example.com",
        "https://user@example.com",
        "https://example.com/#fragment",
        "https://example.com:bad",
    ],
)
def test_public_url_rejects_non_public_or_ambiguous_targets(url: str) -> None:
    with pytest.raises(DomainError):
        validate_public_url(url)


def test_public_address_rejects_malformed_ip() -> None:
    with pytest.raises(DomainError):
        require_public_address("not-an-ip")


async def test_resolver_validates_real_dns_results_and_deduplicates(monkeypatch: pytest.MonkeyPatch) -> None:
    def addresses(host: str, port: int, *, family: int, type: int) -> list[tuple[object, ...]]:
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", addresses)
    resolver = PublicResolver()
    assert len(await resolver.resolve("example.com", 443)) == 1
    await resolver.close()


async def test_resolver_rejects_private_dns_and_handles_empty_or_failed_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def private(host: str, port: int, *, family: int, type: int) -> list[tuple[object, ...]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

    monkeypatch.setattr(socket, "getaddrinfo", private)
    with pytest.raises(DomainError) as caught:
        await PublicResolver().resolve("rebind.example", 80)
    assert caught.value.code is ErrorCode.PERMISSION_DENIED

    def failed(host: str, port: int, *, family: int, type: int) -> list[tuple[object, ...]]:
        raise socket.gaierror("offline")

    monkeypatch.setattr(socket, "getaddrinfo", failed)
    with pytest.raises(DomainError) as caught:
        await PublicResolver().resolve("missing.example", 80)
    assert caught.value.code is ErrorCode.UPSTREAM_FAILURE

    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [])
    with pytest.raises(DomainError):
        await PublicResolver().resolve("empty.example", 80)


class Content:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    async def iter_chunked(self, size: int):
        for chunk in self.chunks:
            yield chunk


class Response:
    def __init__(self, status: int = 200, headers: dict[str, str] | None = None, chunks: list[bytes] | None = None) -> None:
        self.status = status
        self.headers = headers or {}
        self.content = Content(chunks or [])
        self.closed = False
        self.released = False

    def release(self) -> None:
        self.released = True

    def close(self) -> None:
        self.closed = True


class Session:
    def __init__(self, responses: list[Response | Exception]) -> None:
        self.responses = responses
        self.requests: list[tuple[str, dict[str, str]]] = []
        self.closed = False
        self.last_response: Response | None = None

    async def get(self, url: str, *, headers: dict[str, str], allow_redirects: bool) -> Response:
        self.requests.append((url, headers))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        self.last_response = response
        return response

    async def close(self) -> None:
        self.closed = True


def install_session(monkeypatch: pytest.MonkeyPatch, session: Session) -> None:
    monkeypatch.setattr("aria2deck.infrastructure.http.aiohttp.ClientSession", lambda **kwargs: session)
    monkeypatch.setattr("aria2deck.infrastructure.http.aiohttp.TCPConnector", lambda **kwargs: object())


async def collect(response: object) -> bytes:
    chunks = []
    async for chunk in response.body:
        chunks.append(chunk)
    return b"".join(chunks)


async def test_cross_origin_redirect_drops_source_headers_and_range_is_retained(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redirect = Response(302, {"Location": "https://other.example/final"})
    final = Response(200, {"Content-Length": "2"}, [b"ok"])
    session = Session([redirect, final])
    install_session(monkeypatch, session)

    response = await SafeHttpClient().open(
        url="https://origin.example/start",
        headers={"Authorization": "secret", "X-Custom": "private"},
        range_header="bytes=0-1",
    )
    assert await collect(response) == b"ok"
    assert session.requests[0][1]["Authorization"] == "secret"
    assert session.requests[1] == (
        "https://other.example/final",
        {"Range": "bytes=0-1", "Accept-Encoding": "identity"},
    )
    assert redirect.released is True
    assert session.closed is True


async def test_same_origin_redirect_keeps_authentication(monkeypatch: pytest.MonkeyPatch) -> None:
    session = Session([Response(301, {"Location": "/next"}), Response(200, chunks=[b"ok"])])
    install_session(monkeypatch, session)
    response = await SafeHttpClient().open(
        url="https://example.com/start", headers={"Authorization": "secret"}, range_header=None
    )
    await collect(response)
    assert session.requests[1][1]["Authorization"] == "secret"


@pytest.mark.parametrize(
    "responses,expected",
    [
        ([Response(302)], ErrorCode.UPSTREAM_FAILURE),
        ([Response(416)], ErrorCode.RANGE_NOT_SATISFIABLE),
        ([Response(413)], ErrorCode.TASK_TOO_LARGE),
        ([Response(500)], ErrorCode.UPSTREAM_FAILURE),
        ([aiohttp.ClientError("offline")], ErrorCode.UPSTREAM_FAILURE),
    ],
)
async def test_http_boundaries_map_errors_and_close_session(
    monkeypatch: pytest.MonkeyPatch, responses: list[Response | Exception], expected: ErrorCode
) -> None:
    session = Session(responses)
    install_session(monkeypatch, session)
    with pytest.raises(DomainError) as caught:
        await SafeHttpClient().open(url="https://example.com", headers={}, range_header=None)
    assert caught.value.code is expected
    assert session.closed is True


async def test_redirect_limit_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    session = Session([Response(302, {"Location": "/next"}) for _ in range(11)])
    install_session(monkeypatch, session)
    with pytest.raises(DomainError) as caught:
        await SafeHttpClient().open(url="https://example.com", headers={}, range_header=None)
    assert caught.value.code is ErrorCode.UPSTREAM_FAILURE
    assert len(session.requests) == 11


async def test_range_response_uses_entity_total_and_validates_lengths(monkeypatch: pytest.MonkeyPatch) -> None:
    response = Response(206, {"Content-Length": "2", "Content-Range": "bytes 5-6/20"}, [b"ok"])
    session = Session([response])
    install_session(monkeypatch, session)
    opened = await SafeHttpClient().open(url="https://example.com", headers={}, range_header="bytes=5-6")
    assert (opened.total_bytes, opened.body_bytes, opened.content_offset) == (20, 2, 5)
    assert await collect(opened) == b"ok"

    invalid = Response(206, {"Content-Length": "3", "Content-Range": "bytes 5-6/20"})
    session = Session([invalid])
    install_session(monkeypatch, session)
    with pytest.raises(DomainError):
        await SafeHttpClient().open(url="https://example.com", headers={}, range_header=None)
    assert invalid.closed is True
    assert session.closed is True


@pytest.mark.parametrize("content_range", ["invalid", "bytes 6-5/20", "bytes 5-20/20"])
async def test_invalid_content_range_fails_closed(monkeypatch: pytest.MonkeyPatch, content_range: str) -> None:
    session = Session([Response(206, {"Content-Range": content_range})])
    install_session(monkeypatch, session)
    with pytest.raises(DomainError):
        await SafeHttpClient().open(url="https://example.com", headers={}, range_header=None)


@pytest.mark.parametrize(
    "range_header",
    [
        "",
        "bytes=",
        "bytes=-",
        "bytes=-0",
        "bytes=1",
        "bytes=2-1",
        "bytes=1-2,4-5",
        "items=1-2",
    ],
)
async def test_invalid_range_request_fails_before_upstream(
    monkeypatch: pytest.MonkeyPatch, range_header: str
) -> None:
    session = Session([Response(200, {"Content-Length": "1"}, [b"x"])])
    install_session(monkeypatch, session)

    with pytest.raises(DomainError) as caught:
        await SafeHttpClient().open(
            url="https://example.com", headers={}, range_header=range_header
        )

    assert caught.value.code is ErrorCode.RANGE_NOT_SATISFIABLE
    assert session.requests == []


@pytest.mark.parametrize(
    ("range_header", "status", "headers"),
    [
        ("bytes=100-199", 200, {"Content-Length": "1000"}),
        (
            "bytes=100-199",
            206,
            {"Content-Length": "100", "Content-Range": "bytes 0-99/1000"},
        ),
        (
            "bytes=100-199",
            206,
            {"Content-Length": "50", "Content-Range": "bytes 100-149/1000"},
        ),
        (
            "bytes=100-199",
            206,
            {"Content-Length": "100", "Content-Range": "bytes 100-199/150"},
        ),
        (
            "bytes=100-",
            206,
            {"Content-Length": "100", "Content-Range": "bytes 100-199/1000"},
        ),
        (
            "bytes=-100",
            206,
            {"Content-Length": "100", "Content-Range": "bytes 0-99/1000"},
        ),
        (
            "bytes=-100",
            206,
            {"Content-Length": "100", "Content-Range": "bytes 900-999/*"},
        ),
        ("bytes=0-98", 200, {"Content-Length": "100"}),
        ("bytes=-99", 200, {"Content-Length": "100"}),
        ("bytes=0-99", 200, {}),
        ("bytes=-100", 200, {}),
    ],
)
async def test_range_rejects_response_not_matching_request(
    monkeypatch: pytest.MonkeyPatch,
    range_header: str,
    status: int,
    headers: dict[str, str],
) -> None:
    upstream = Response(status, headers)
    session = Session([upstream])
    install_session(monkeypatch, session)

    with pytest.raises(DomainError) as caught:
        await SafeHttpClient().open(
            url="https://example.com", headers={}, range_header=range_header
        )

    assert caught.value.code is ErrorCode.UPSTREAM_FAILURE
    assert upstream.closed is True
    assert session.closed is True


@pytest.mark.parametrize(
    ("range_header", "status", "headers", "expected"),
    [
        (
            "bytes=100-199",
            206,
            {"Content-Length": "100", "Content-Range": "bytes 100-199/1000"},
            (1000, 100, 100),
        ),
        (
            "bytes=100-199",
            206,
            {"Content-Length": "100", "Content-Range": "bytes 100-199/*"},
            (None, 100, 100),
        ),
        (
            "bytes=100-199",
            206,
            {"Content-Length": "50", "Content-Range": "bytes 100-149/150"},
            (150, 50, 100),
        ),
        (
            "bytes=100-",
            206,
            {"Content-Length": "900", "Content-Range": "bytes 100-999/1000"},
            (1000, 900, 100),
        ),
        (
            "bytes=100-",
            206,
            {"Content-Length": "100", "Content-Range": "bytes 100-199/*"},
            (None, 100, 100),
        ),
        (
            "bytes=-100",
            206,
            {"Content-Length": "100", "Content-Range": "bytes 900-999/1000"},
            (1000, 100, 900),
        ),
        (
            "bytes=-1000",
            206,
            {"Content-Length": "1000", "Content-Range": "bytes 0-999/1000"},
            (1000, 1000, 0),
        ),
        ("bytes=0-", 200, {"Content-Length": "1000"}, (1000, 1000, 0)),
        ("bytes=0-", 200, {}, (None, None, 0)),
        ("bytes=0-999", 200, {"Content-Length": "1000"}, (1000, 1000, 0)),
        ("bytes=0-1000", 200, {"Content-Length": "1000"}, (1000, 1000, 0)),
        ("bytes=-1000", 200, {"Content-Length": "1000"}, (1000, 1000, 0)),
    ],
)
async def test_range_accepts_only_the_requested_complete_slice(
    monkeypatch: pytest.MonkeyPatch,
    range_header: str,
    status: int,
    headers: dict[str, str],
    expected: tuple[int | None, int | None, int],
) -> None:
    session = Session([Response(status, headers)])
    install_session(monkeypatch, session)

    opened = await SafeHttpClient().open(
        url="https://example.com", headers={}, range_header=range_header
    )

    assert (opened.total_bytes, opened.body_bytes, opened.content_offset) == expected
    await opened.close()


@pytest.mark.parametrize("length", ["bad", "-1"])
async def test_invalid_content_length_fails_closed(monkeypatch: pytest.MonkeyPatch, length: str) -> None:
    session = Session([Response(200, {"Content-Length": length})])
    install_session(monkeypatch, session)
    with pytest.raises(DomainError):
        await SafeHttpClient().open(url="https://example.com", headers={}, range_header=None)


async def test_tracker_fetch_is_limited_and_utf8(monkeypatch: pytest.MonkeyPatch) -> None:
    session = Session([Response(200, chunks=[b"udp://tracker.example:80"])])
    install_session(monkeypatch, session)
    assert await SafeHttpClient().fetch_text("https://example.com/list") == "udp://tracker.example:80"

    session = Session([Response(200, chunks=[b"x" * (1024 * 1024 + 1)])])
    install_session(monkeypatch, session)
    with pytest.raises(DomainError) as caught:
        await SafeHttpClient().fetch_text("https://example.com/large")
    assert caught.value.code is ErrorCode.UPSTREAM_FAILURE

    session = Session([Response(200, chunks=[b"\xff"])])
    install_session(monkeypatch, session)
    with pytest.raises(DomainError):
        await SafeHttpClient().fetch_text("https://example.com/invalid")
