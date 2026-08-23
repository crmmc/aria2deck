"""Coverage tests for app/services/gateway.py (pure helpers + fake-session stream)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest
from sqlalchemy import insert

from app.db.engine import transaction
from app.db.schema import global_downloads
from app.http.safe_client import UnsafeTargetError
from app.services import gateway
from app.services.gateway import (
    GatewayDownloadNotFound,
    GatewayDownloadUnavailable,
    GatewaySizeExceeded,
    GatewayStream,
    GatewayTargetError,
    GatewayUpstreamError,
    InvalidCapabilityError,
    InvalidRangeError,
    SourceRequestOptions,
    authorize_gateway_request,
    build_gateway_submission,
    create_capability,
    http_resource_identity,
    open_gateway_stream,
    parse_range_header,
    source_request_options,
    verify_capability,
)
from tests.helpers_v0 import now_ms


# ---------------------------------------------------------------------------
# source_request_options / headers
# ---------------------------------------------------------------------------


def test_source_header_option_type_invalid() -> None:
    with pytest.raises(ValueError, match="字符串"):
        source_request_options({"header": 123})


def test_source_header_missing_colon() -> None:
    with pytest.raises(ValueError, match="格式无效"):
        source_request_options({"header": "nocolon"})


def test_source_header_invalid_name() -> None:
    with pytest.raises(ValueError, match="不允许的请求头"):
        source_request_options({"header": "bad name: v"})


def test_source_header_blocked_header() -> None:
    with pytest.raises(ValueError, match="不允许的请求头"):
        source_request_options({"header": "Host: example.com"})


def test_source_header_newline_rejected() -> None:
    with pytest.raises(ValueError, match="不允许的请求头"):
        source_request_options({"header": "X-A: v\r\nX-B: w"})


def test_source_header_too_large() -> None:
    with pytest.raises(ValueError, match="过大"):
        source_request_options({"header": [f"X-H{i}: {'a' * 3000}" for i in range(4)]})


def test_source_auth_option_invalid() -> None:
    with pytest.raises(ValueError, match="HTTP 认证选项无效"):
        source_request_options({"http-user": "u" * 5000})
    with pytest.raises(ValueError, match="HTTP 认证选项无效"):
        source_request_options({"http-passwd": "a\nb"})


def test_mirror_limit_rejected() -> None:
    with pytest.raises(ValueError, match="镜像地址过多"):
        source_request_options({}, mirrors=[f"https://m{i}.example.com" for i in range(16)])


def test_mirror_unsafe_rejected_and_deduplicated() -> None:
    with pytest.raises(ValueError, match="不允许"):
        source_request_options({}, mirrors=["http://127.0.0.1:8080/x"])
    options = source_request_options(
        {}, mirrors=["https://a.example.com/f#frag", "https://a.example.com/f"]
    )
    assert options.mirrors == ("https://a.example.com/f",)


# ---------------------------------------------------------------------------
# capability / payload
# ---------------------------------------------------------------------------


def _capability_payload(options: SourceRequestOptions) -> str:
    payload = json.dumps(
        gateway._payload(options), separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return gateway._encode(payload)


def test_create_capability_payload_too_large() -> None:
    with pytest.raises(ValueError, match="过大"):
        create_capability(
            1,
            "https://example.com/f",
            SourceRequestOptions(
                headers=tuple(
                    (f"x-h{i}", "v" * 2000) for i in range(10)
                )
            ),
        )


def test_verify_capability_invalid_signature_blob() -> None:
    with pytest.raises(InvalidCapabilityError):
        verify_capability("", 1, "https://example.com/f")
    with pytest.raises(InvalidCapabilityError):
        verify_capability("no-dot-at-all", 1, "https://example.com/f")
    with pytest.raises(InvalidCapabilityError):
        verify_capability("x.y.z", 1, "https://example.com/f")


def test_verify_capability_bad_base64() -> None:
    with pytest.raises(InvalidCapabilityError):
        verify_capability("x.!!not-base64!!", 1, "https://example.com/f")


def test_verify_capability_signature_mismatch() -> None:
    capability = create_capability(1, "https://example.com/f", SourceRequestOptions())
    wrong = capability.rsplit(".", 1)[0] + "." + gateway._encode(b"\x00" * 32)
    with pytest.raises(InvalidCapabilityError):
        verify_capability(wrong, 1, "https://example.com/f")
    with pytest.raises(InvalidCapabilityError):
        verify_capability(capability, 2, "https://example.com/f")


def test_verify_capability_payload_not_json() -> None:
    payload = gateway._encode(b"not-json")
    signature = gateway._encode(
        __import__("hmac").new(
            gateway._capability_key(),
            f"1\nhttps://example.com/f\n{payload}".encode(),
            __import__("hashlib").sha256,
        ).digest()
    )
    with pytest.raises(InvalidCapabilityError):
        verify_capability(f"{payload}.{signature}", 1, "https://example.com/f")


@pytest.mark.parametrize(
    "payload",
    [
        {"v": 2},
        {"v": 1, "x": 1},
        {"v": 1, "h": "bad"},
        {"v": 1, "h": [["a", "b", "c"]]},
        {"v": 1, "h": [["a", 1]]},
        {"v": 1, "u": 5},
        {"v": 1, "p": 5},
        {"v": 1, "m": "https://a.example.com"},
        {"v": 1, "m": [1]},
    ],
)
def test_options_from_payload_invalid(payload: dict) -> None:
    with pytest.raises(InvalidCapabilityError):
        gateway._options_from_payload(payload)


def test_options_from_payload_rejects_blocked_header() -> None:
    with pytest.raises(InvalidCapabilityError):
        gateway._options_from_payload({"v": 1, "h": [["host", "example.com"]]})


def test_options_from_payload_accepts_valid_options() -> None:
    options = gateway._options_from_payload(
        {"v": 1, "h": [["x-a", "b"]], "u": "user", "p": "pass", "m": ["https://m.example.com"]}
    )
    assert options.headers == (("x-a", "b"),)
    assert (options.username, options.password) == ("user", "pass")
    assert options.mirrors == ("https://m.example.com",)


def test_http_resource_identity_passthrough_and_context() -> None:
    plain = "http:abc"
    assert http_resource_identity(plain, SourceRequestOptions()) == plain
    with_options = http_resource_identity(
        plain, SourceRequestOptions(headers=(("x-a", "b"),))
    )
    assert with_options != plain


# ---------------------------------------------------------------------------
# authorize_gateway_request
# ---------------------------------------------------------------------------


_download_counter = iter(range(1, 100000))


async def _insert_download(**overrides: object) -> int:
    seq = next(_download_counter)
    timestamp = now_ms()
    values: dict = {
        "resource_key": f"http:gwcov{seq}",
        "resource_kind": "http",
        "source_uri": "https://example.com/file.bin",
        "display_name": "file.bin",
        "aria2_gid": f"gwcov_gid_{seq}",
        "status": "active",
        "total_bytes": 10,
        "completed_bytes": 0,
        "created_at_ms": timestamp,
        "updated_at_ms": timestamp,
    }
    values.update(overrides)
    async with transaction() as conn:
        row = (
            await conn.execute(
                insert(global_downloads).values(**values).returning(global_downloads.c.id)
            )
        ).one()
    return int(row[0])


def test_authorize_download_not_found(temp_db: str) -> None:
    with pytest.raises(GatewayDownloadNotFound):
        asyncio.run(authorize_gateway_request(999999, 0, "cap"))


def test_authorize_rejects_non_http_and_terminal(temp_db: str) -> None:
    torrent_id = asyncio.run(
        _insert_download(
            resource_key="torrent:gwcov-t",
            resource_kind="torrent",
            source_uri="[torrent]",
            status="active",
        )
    )
    with pytest.raises(GatewayDownloadUnavailable) as exc_info:
        asyncio.run(
            authorize_gateway_request(
                torrent_id, 0, create_capability(torrent_id, "[torrent]", SourceRequestOptions())
            )
        )
    assert exc_info.value.terminal is False

    http_id = asyncio.run(_insert_download())
    asyncio.run(_expire_status(http_id, "completed"))
    capability = create_capability(
        http_id, "https://example.com/file.bin", SourceRequestOptions()
    )
    with pytest.raises(GatewayDownloadUnavailable) as exc_info:
        asyncio.run(authorize_gateway_request(http_id, 0, capability))
    assert exc_info.value.terminal is True


async def _expire_status(download_id: int, status: str) -> None:
    from sqlalchemy import update

    async with transaction() as conn:
        await conn.execute(
            update(global_downloads)
            .where(global_downloads.c.id == download_id)
            .values(status=status)
        )


def test_authorize_source_index_out_of_range(temp_db: str) -> None:
    download_id = asyncio.run(_insert_download())
    capability = create_capability(download_id, "https://example.com/file.bin", SourceRequestOptions())
    with pytest.raises(GatewayDownloadNotFound):
        asyncio.run(authorize_gateway_request(download_id, 5, capability))
    with pytest.raises(GatewayDownloadNotFound):
        asyncio.run(authorize_gateway_request(download_id, -1, capability))


def test_authorize_returns_source_and_mirrors(temp_db: str) -> None:
    download_id = asyncio.run(_insert_download())
    options = SourceRequestOptions(mirrors=("https://m.example.com/f",))
    capability = create_capability(download_id, "https://example.com/file.bin", options)
    uri, resolved = asyncio.run(authorize_gateway_request(download_id, 1, capability))
    assert uri == "https://m.example.com/f"
    assert resolved.mirrors == options.mirrors


# ---------------------------------------------------------------------------
# build_gateway_submission / parse_range_header
# ---------------------------------------------------------------------------


def test_build_gateway_submission() -> None:
    uris, options = build_gateway_submission(
        download_id=7,
        source_uri="https://example.com/f",
        options={"header": "X-A: b", "http-user": "u", "http-passwd": "p"},
        source_uris=["https://example.com/f", "https://m.example.com/f"],
    )
    assert len(uris) == 2
    assert uris[0].endswith("/_internal/fetch/7/0")
    assert uris[1].endswith("/_internal/fetch/7/1")
    assert options["out"] == "payload"
    assert options["header"][0].startswith("X-Aria2Deck-Fetch-Capability: ")


@pytest.mark.parametrize(
    ("value", "start", "end"),
    [
        (None, None, None),
        ("bytes=0-", 0, None),
        ("bytes=5-9", 5, 9),
        ("  bytes=5-9  ", 5, 9),
    ],
)
def test_parse_range_header_valid(value: str | None, start: int | None, end: int | None) -> None:
    result = parse_range_header(value)
    if start is None:
        assert result is None
    else:
        assert (result.start, result.end) == (start, end)


@pytest.mark.parametrize(
    "value",
    [
        "x" * 200,
        "bytes=-5",
        "bytes=5-9;x",
        "bytes=9-5",
        f"bytes=0-{'9' * 21}",
    ],
)
def test_parse_range_header_invalid(value: str) -> None:
    with pytest.raises(InvalidRangeError):
        parse_range_header(value)


# ---------------------------------------------------------------------------
# upstream header / size helpers
# ---------------------------------------------------------------------------


def test_upstream_headers_authorization_from_credentials() -> None:
    options = SourceRequestOptions(username="u", password="p")
    headers = dict(gateway._upstream_headers(options, include_source_headers=True, range_header="bytes=0-1"))
    assert headers["Authorization"].startswith("Basic ")
    assert headers["Range"] == "bytes=0-1"
    assert headers["Accept-Encoding"] == "identity"


def test_upstream_headers_existing_authorization_wins() -> None:
    options = SourceRequestOptions(
        headers=(("authorization", "Bearer t"),), username="u"
    )
    headers = dict(
        (name.lower(), value)
        for name, value in gateway._upstream_headers(
            options, include_source_headers=True, range_header=None
        )
    )
    assert headers["authorization"] == "Bearer t"


def test_upstream_headers_without_source_headers() -> None:
    options = SourceRequestOptions(headers=(("x-a", "b"),), username="u")
    headers = gateway._upstream_headers(options, include_source_headers=False, range_header=None)
    assert ("x-a", "b") not in headers
    assert not any(name.lower() == "authorization" for name, _ in headers)


def _response(status: int, headers: dict) -> SimpleNamespace:
    return SimpleNamespace(status=status, headers=headers)


def test_parse_content_length_invalid() -> None:
    for raw in ("x" * 200, "12abc", "-5"):
        with pytest.raises(GatewayUpstreamError):
            gateway._parse_content_length(_response(200, {"Content-Length": raw}))


def test_content_range_invalid() -> None:
    for raw in ("x" * 200, "bytes 1", "bytes 9-5/10", "bytes 0-10/10", "bytes 0-1/" + "9" * 21):
        with pytest.raises(GatewayUpstreamError):
            gateway._content_range(_response(206, {"Content-Range": raw}))


def test_content_range_valid_star_total() -> None:
    assert gateway._content_range(_response(206, {"Content-Range": "bytes 0-1/*"})) == (0, 1, None)


def test_response_headers_allowlist() -> None:
    response = _response(
        200,
        {"Content-Type": "application/octet-stream", "X-Drop": "1", "ETag": '"e"'},
    )
    headers = gateway._response_headers(response)
    assert headers == {"Content-Type": "application/octet-stream", "ETag": '"e"'}


@pytest.mark.parametrize(
    ("status", "headers", "requested", "max_size", "budget"),
    [
        # non-body status has zero budget
        (404, {}, None, 100, 0),
        # plain 200 within budget
        (200, {"Content-Length": "10"}, None, 100, 100),
        # 206 matching range, clamped by requested end
        (
            206,
            {"Content-Range": "bytes 5-9/50", "Content-Length": "5"},
            gateway.RangeRequest(start=5, end=9),
            100,
            5,
        ),
    ],
)
def test_stream_budget_valid(status, headers, requested, max_size, budget) -> None:
    assert gateway._stream_budget(_response(status, headers), requested, max_size) == budget


@pytest.mark.parametrize(
    ("status", "headers", "requested", "match"),
    [
        (206, {"Content-Range": "bytes 5-9/50"}, None, "未请求的 Range"),
        (206, {"Content-Range": "bytes 0-9/50"}, gateway.RangeRequest(start=5, end=9), "起点不匹配"),
        (
            206,
            {"Content-Range": "bytes 0-9/50"},
            gateway.RangeRequest(start=0, end=4),
            "终点不匹配",
        ),
        (
            206,
            {"Content-Range": "bytes 0-9/50", "Content-Length": "3"},
            gateway.RangeRequest(start=0, end=9),
            "长度不匹配",
        ),
        (206, {"Content-Range": "bytes 0-9/500"}, gateway.RangeRequest(start=0, end=9), "大小限制"),
        (200, {}, gateway.RangeRequest(start=1, end=None), "未响应 Range"),
        (
            206,
            {"Content-Range": "bytes 200-209/*", "Content-Length": "10"},
            gateway.RangeRequest(start=200, end=209),
            "大小限制",
        ),
        (200, {"Content-Length": "500"}, None, "大小限制"),
    ],
)
def test_stream_budget_errors(status, headers, requested, match) -> None:
    with pytest.raises((GatewayUpstreamError, GatewaySizeExceeded), match=match):
        gateway._stream_budget(_response(status, headers), requested, 100)


# ---------------------------------------------------------------------------
# GatewayStream.iter_bytes
# ---------------------------------------------------------------------------


class _FakeContent:
    def __init__(self, chunks: list[bytes] | BaseException) -> None:
        self._chunks = chunks

    async def iter_chunked(self, _size: int):
        if isinstance(self._chunks, BaseException):
            raise self._chunks
        for chunk in self._chunks:
            yield chunk


def _stream(chunks: list[bytes] | BaseException, budget: int, stream_body: bool = True) -> GatewayStream:
    response = SimpleNamespace(content=_FakeContent(chunks), close=lambda: None)
    session = SimpleNamespace(close=AsyncMock())
    stream = GatewayStream(
        session=session,
        response=response,
        status_code=200,
        headers={},
        budget=budget,
        stream_body=stream_body,
    )
    return stream


def test_iter_bytes_empty_chunks_skipped_and_budget_enforced() -> None:
    result = asyncio.run(_collect(_stream([b"", b"abc"], budget=10)))
    assert result == [b"abc"]

    with pytest.raises(GatewaySizeExceeded):
        asyncio.run(_collect(_stream([b"abcdefgh"], budget=3)))


async def _collect(stream: GatewayStream) -> list[bytes]:
    return [chunk async for chunk in stream.iter_bytes()]


def test_iter_bytes_no_body_returns_empty() -> None:
    assert asyncio.run(_collect(_stream([b"x"], budget=10, stream_body=False))) == []


def test_iter_bytes_upstream_error() -> None:
    with pytest.raises(GatewayUpstreamError, match="上游下载中断"):
        asyncio.run(_collect(_stream(aiohttp.ClientError("boom"), budget=10)))


def test_gateway_stream_close_is_idempotent() -> None:
    stream = _stream([], budget=1)
    asyncio.run(stream.close())
    asyncio.run(stream.close())
    assert stream.closed


# ---------------------------------------------------------------------------
# open_gateway_stream with a fake aiohttp session
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status: int, headers: dict) -> None:
        self.status = status
        self.headers = headers
        self.closed = False
        self.content = _FakeContent([b"data"])

    def close(self) -> None:
        self.closed = True


class _FakeSession:
    queue: list = []
    exception: BaseException | None = None
    responses: list = []
    closed = False

    def __init__(self, *args, **kwargs) -> None:
        pass

    @classmethod
    def enqueue(cls, *responses: _FakeResponse) -> None:
        cls.queue = list(responses)

    async def get(self, url: str, headers=None, allow_redirects: bool = False) -> _FakeResponse:
        _FakeSession.responses.append((url, headers))
        if _FakeSession.exception is not None:
            raise _FakeSession.exception
        return _FakeSession.queue.pop(0)

    async def close(self) -> None:
        _FakeSession.closed = True


@pytest.fixture
def fake_session(monkeypatch: pytest.MonkeyPatch):
    _FakeSession.queue = []
    _FakeSession.exception = None
    _FakeSession.responses = []
    _FakeSession.closed = False
    monkeypatch.setattr(aiohttp, "ClientSession", _FakeSession)
    yield _FakeSession
    _FakeSession.queue = []
    _FakeSession.exception = None


def _open(source_uri: str, fake: _FakeSession, **kwargs) -> GatewayStream:
    return asyncio.run(
        open_gateway_stream(
            source_uri=source_uri,
            options=kwargs.pop("options", SourceRequestOptions()),
            range_header=kwargs.pop("range_header", None),
            **kwargs,
        )
    )


def test_open_stream_rejects_unsafe_target(fake_session: _FakeSession, temp_db: str) -> None:
    with pytest.raises(GatewayTargetError):
        _open("http://127.0.0.1/x", fake_session)


def test_open_stream_connect_error(fake_session: _FakeSession, temp_db: str) -> None:
    fake_session.exception = aiohttp.ClientConnectionError("refused")
    with pytest.raises(GatewayUpstreamError, match="无法连接上游"):
        _open("https://example.com/f", fake_session)
    assert fake_session.closed


def test_open_stream_connect_error_from_unsafe_resolver(fake_session: _FakeSession, temp_db: str) -> None:
    error = aiohttp.ClientOSError("resolve failed")
    error.__cause__ = UnsafeTargetError("域名解析到非公网地址")
    fake_session.exception = error
    with pytest.raises(GatewayTargetError, match="非公网"):
        _open("https://example.com/f", fake_session)


def test_open_stream_redirect_without_location(fake_session: _FakeSession, temp_db: str) -> None:
    fake_session.enqueue(_FakeResponse(302, {}))
    with pytest.raises(GatewayUpstreamError, match="缺少 Location"):
        _open("https://example.com/f", fake_session)


def test_open_stream_redirect_loop_exhausted(fake_session: _FakeSession, temp_db: str) -> None:
    fake_session.enqueue(
        _FakeResponse(302, {"Location": "https://example.com/next"})
    )
    with pytest.raises(GatewayUpstreamError, match="重定向次数"):
        _open("https://example.com/f", fake_session, max_redirects=0)


def test_open_stream_redirect_to_unsafe_location(fake_session: _FakeSession, temp_db: str) -> None:
    fake_session.enqueue(_FakeResponse(302, {"Location": "http://10.0.0.1/x"}))
    with pytest.raises(GatewayTargetError):
        _open("https://example.com/f", fake_session)


def test_open_stream_cross_origin_redirect_drops_source_headers(
    fake_session: _FakeSession, temp_db: str
) -> None:
    fake_session.enqueue(
        _FakeResponse(302, {"Location": "https://other.example.com/f"}),
        _FakeResponse(200, {}),
    )
    stream = _open(
        "https://example.com/f",
        fake_session,
        options=SourceRequestOptions(headers=(("x-a", "b"),)),
    )
    assert stream.status_code == 200
    first_headers = dict(fake_session.responses[0][1])
    second_headers = dict(fake_session.responses[1][1])
    assert first_headers.get("x-a") == "b"
    assert "x-a" not in second_headers
    asyncio.run(stream.close())


def test_open_stream_non_body_status_drops_content_length(
    fake_session: _FakeSession, temp_db: str
) -> None:
    fake_session.enqueue(_FakeResponse(404, {"Content-Length": "17"}))
    stream = _open("https://example.com/f", fake_session)
    assert stream.stream_body is False
    assert stream.budget == 0
    assert "Content-Length" not in stream.headers
    asyncio.run(stream.close())


def test_open_stream_rejects_unsupported_responses(fake_session: _FakeSession, temp_db: str) -> None:
    class _RedirectResponse(_FakeResponse):
        def __init__(self) -> None:
            super().__init__(304, {})

    fake_session.enqueue(_FakeResponse(100, {}))
    with pytest.raises(GatewayUpstreamError, match="协议切换"):
        _open("https://example.com/f", fake_session)

    fake_session.enqueue(_RedirectResponse())
    with pytest.raises(GatewayUpstreamError, match="重定向"):
        _open("https://example.com/f", fake_session)


def test_open_stream_rejects_content_encoding(fake_session: _FakeSession, temp_db: str) -> None:
    fake_session.enqueue(_FakeResponse(200, {"Content-Encoding": "gzip"}))
    with pytest.raises(GatewayUpstreamError, match="内容编码"):
        _open("https://example.com/f", fake_session)


def test_open_stream_success(fake_session: _FakeSession, temp_db: str) -> None:
    fake_session.enqueue(
        _FakeResponse(
            200,
            {
                "Content-Length": "4",
                "Content-Type": "application/octet-stream",
                "X-Drop": "1",
            },
        )
    )
    stream = _open("https://example.com/f", fake_session, range_header="bytes=0-")
    assert stream.status_code == 200
    assert stream.headers["Content-Type"] == "application/octet-stream"
    assert stream.budget == 10 * 1024 * 1024 * 1024
    asyncio.run(stream.close())
