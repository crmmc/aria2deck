"""真实 aiohttp 上游服务下的网关转发音字节回归测试。

背景缺陷：上游返回 Content-Encoding: gzip 且 Content-Length 为压缩后长度时，
客户端若自动解压，流出的解压字节数会超过声明的 body_bytes，网关守卫把连接
中途杀掉（UPSTREAM_FAILURE「上游响应超过声明大小」）。
"""

from __future__ import annotations

import gzip
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

import aria2deck.infrastructure.http as http_module
from aria2deck.application.downloads.gateway import DownloadGatewayService
from aria2deck.application.downloads.models import AttemptRecord, GatewayResponse, SourceRecord
from aria2deck.domain.downloads import AttemptStatus
from aria2deck.domain.errors import DomainError, ErrorCode
from aria2deck.domain.settings import SystemSettings
from aria2deck.infrastructure.http import SafeHttpClient
from tests.application.test_history_edges import UoW

RAW = b"aria2deck gzip regression payload " * 32
COMPRESSED = gzip.compress(RAW, mtime=0)
assert len(COMPRESSED) < len(RAW)

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


@pytest.fixture(autouse=True)
def allow_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    # 线级行为测试不关心 SSRF 策略（已有独立单测），只放开回环地址。
    monkeypatch.setattr(http_module, "require_public_address", lambda value: None)


@dataclass
class Recorder:
    accept_encoding: list[str] = field(default_factory=list)
    ranges: list[str] = field(default_factory=list)

    def record(self, request: web.Request) -> None:
        self.accept_encoding.append(request.headers.get("Accept-Encoding", ""))
        self.ranges.append(request.headers.get("Range", ""))


@pytest.fixture
async def run_upstream() -> AsyncIterator[Callable[[Handler], Awaitable[TestServer]]]:
    started: list[TestServer] = []

    async def start(handler: Handler) -> TestServer:
        app = web.Application()
        app.router.add_get("/file", handler)
        server = TestServer(app)
        await server.start_server()
        started.append(server)
        return server

    yield start
    for server in started:
        await server.close()


class RecordingRepository:
    def __init__(self, url: str) -> None:
        self.url = url
        self.admitted: list[tuple[int, bool]] = []

    async def get_attempt(self, attempt_id: int) -> AttemptRecord:
        return AttemptRecord(1, 1, "http:key", "gid", AttemptStatus.QUEUED, None, "submitted", None, 0, 0)

    async def get_source_for_attempt(self, attempt_id: int) -> SourceRecord:
        return SourceRecord(
            1, "http", "http:key", {"uris": [self.url]}, {"headers": {}}, None, "file", None, None
        )

    async def admit_size(
        self,
        *,
        attempt_id: int,
        observed_bytes: int,
        known_total: bool,
        disk_free_bytes: int,
        min_free_bytes: int,
        now_ms: int,
    ) -> DomainError | None:
        self.admitted.append((observed_bytes, known_total))
        return None


class MachineSpace:
    def machine_free_bytes(self) -> int:
        return 1_000_000_000


def build_gateway(
    url: str, *, max_task_size: int
) -> tuple[DownloadGatewayService, RecordingRepository]:
    repository = RecordingRepository(url)
    gateway = DownloadGatewayService(
        uow_factory=lambda: UoW(),
        repository_factory=lambda _uow: repository,
        upstream=SafeHttpClient(),
        machine_space=MachineSpace(),
        settings=lambda: SystemSettings(max_task_size=max_task_size),
        clock_ms=lambda: 1,
        verify_capability=lambda _attempt_id, _index, _capability: True,
    )
    return gateway, repository


async def collect(
    gateway: DownloadGatewayService, *, range_header: str | None = None
) -> tuple[GatewayResponse, bytes]:
    opened = await gateway.open(
        attempt_id=1, source_index=0, capability="valid", range_header=range_header
    )
    chunks = [chunk async for chunk in opened.body]
    return opened, b"".join(chunks)


async def test_gzip_negotiating_upstream_streams_declared_bytes(
    run_upstream: Callable[[Handler], Awaitable[TestServer]],
) -> None:
    recorder = Recorder()

    async def handler(request: web.Request) -> web.Response:
        recorder.record(request)
        if "gzip" in request.headers.get("Accept-Encoding", ""):
            return web.Response(body=COMPRESSED, headers={"Content-Encoding": "gzip"})
        return web.Response(body=RAW)

    server = await run_upstream(handler)
    gateway, repository = build_gateway(str(server.make_url("/file")), max_task_size=len(RAW) + 1024)
    opened, body = await collect(gateway)
    assert opened.status == 200
    assert body == RAW
    assert opened.headers["Content-Length"] == str(len(RAW))
    assert recorder.accept_encoding == ["identity"]
    assert repository.admitted == [(len(RAW), True)]


async def test_forced_gzip_upstream_is_forwarded_verbatim(
    run_upstream: Callable[[Handler], Awaitable[TestServer]],
) -> None:
    async def handler(request: web.Request) -> web.Response:
        return web.Response(body=COMPRESSED, headers={"Content-Encoding": "gzip"})

    server = await run_upstream(handler)
    gateway, _ = build_gateway(str(server.make_url("/file")), max_task_size=len(RAW) + 1024)
    opened, body = await collect(gateway)
    assert opened.status == 200
    assert body == COMPRESSED
    assert opened.headers["Content-Length"] == str(len(COMPRESSED))


async def test_declared_oversize_is_rejected_before_streaming(
    run_upstream: Callable[[Handler], Awaitable[TestServer]],
) -> None:
    async def handler(request: web.Request) -> web.Response:
        return web.Response(body=RAW)

    server = await run_upstream(handler)
    gateway, _ = build_gateway(str(server.make_url("/file")), max_task_size=len(RAW) - 1)
    with pytest.raises(DomainError) as caught:
        await gateway.open(attempt_id=1, source_index=0, capability="valid", range_header=None)
    assert caught.value.code is ErrorCode.TASK_TOO_LARGE


async def test_undeclared_oversize_stops_midstream(
    run_upstream: Callable[[Handler], Awaitable[TestServer]],
) -> None:
    async def handler(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse()
        await response.prepare(request)
        await response.write(RAW[:100])
        await response.write(RAW[100:])
        await response.write_eof()
        return response

    server = await run_upstream(handler)
    gateway, _ = build_gateway(str(server.make_url("/file")), max_task_size=150)
    opened = await gateway.open(attempt_id=1, source_index=0, capability="valid", range_header=None)
    with pytest.raises(DomainError) as caught:
        async for _chunk in opened.body:
            pass
    assert caught.value.code is ErrorCode.TASK_TOO_LARGE


async def test_range_request_forwards_only_the_slice(
    run_upstream: Callable[[Handler], Awaitable[TestServer]],
) -> None:
    recorder = Recorder()

    async def handler(request: web.Request) -> web.Response:
        recorder.record(request)
        match = re.fullmatch(r"bytes=(\d+)-(\d+)", request.headers.get("Range", ""))
        if match is None:
            return web.Response(status=400)
        start, end = int(match.group(1)), int(match.group(2))
        return web.Response(
            status=206,
            body=RAW[start : end + 1],
            headers={"Content-Range": f"bytes {start}-{end}/{len(RAW)}"},
        )

    server = await run_upstream(handler)
    gateway, repository = build_gateway(str(server.make_url("/file")), max_task_size=len(RAW) + 1024)
    opened, body = await collect(gateway, range_header="bytes=2-5")
    assert opened.status == 206
    assert body == RAW[2:6]
    assert opened.headers["Content-Range"] == f"bytes 2-5/{len(RAW)}"
    assert opened.headers["Content-Length"] == "4"
    assert recorder.ranges == ["bytes=2-5"]
    assert repository.admitted == [(len(RAW), True)]


@pytest.mark.parametrize(
    ("range_header", "status", "content_range", "body", "valid"),
    [
        ("bytes=2-5", 206, f"bytes 2-5/{len(RAW)}", RAW[2:6], True),
        (
            f"bytes=2-{len(RAW) + 10}",
            206,
            f"bytes 2-{len(RAW) - 1}/{len(RAW)}",
            RAW[2:],
            True,
        ),
        ("bytes=2-", 206, f"bytes 2-{len(RAW) - 1}/{len(RAW)}", RAW[2:], True),
        (
            "bytes=-5",
            206,
            f"bytes {len(RAW) - 5}-{len(RAW) - 1}/{len(RAW)}",
            RAW[-5:],
            True,
        ),
        ("bytes=0-", 200, None, RAW, True),
        ("bytes=2-5", 206, f"bytes 3-6/{len(RAW)}", RAW[3:7], False),
        ("bytes=2-5", 206, f"bytes 2-4/{len(RAW)}", RAW[2:5], False),
        ("bytes=-5", 206, f"bytes 0-4/{len(RAW)}", RAW[:5], False),
        ("bytes=2-5", 200, None, RAW, False),
    ],
)
async def test_wire_range_response_contract(
    run_upstream: Callable[[Handler], Awaitable[TestServer]],
    range_header: str,
    status: int,
    content_range: str | None,
    body: bytes,
    valid: bool,
) -> None:
    async def handler(request: web.Request) -> web.Response:
        headers = {} if content_range is None else {"Content-Range": content_range}
        return web.Response(status=status, body=body, headers=headers)

    server = await run_upstream(handler)
    gateway, _ = build_gateway(str(server.make_url("/file")), max_task_size=len(RAW) + 1024)
    if valid:
        opened, received = await collect(gateway, range_header=range_header)
        assert opened.status == status
        assert received == body
    else:
        with pytest.raises(DomainError) as caught:
            await collect(gateway, range_header=range_header)
        assert caught.value.code is ErrorCode.UPSTREAM_FAILURE


async def test_wire_short_range_body_fails_at_eof(
    run_upstream: Callable[[Handler], Awaitable[TestServer]],
) -> None:
    async def handler(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(
            status=206, headers={"Content-Range": f"bytes 0-9/{len(RAW)}"}
        )
        await response.prepare(request)
        await response.write(RAW[:3])
        await response.write_eof()
        return response

    server = await run_upstream(handler)
    gateway, _ = build_gateway(str(server.make_url("/file")), max_task_size=len(RAW) + 1024)
    opened = await gateway.open(
        attempt_id=1,
        source_index=0,
        capability="valid",
        range_header="bytes=0-9",
    )
    with pytest.raises(DomainError) as caught:
        async for _chunk in opened.body:
            pass
    assert caught.value.code is ErrorCode.UPSTREAM_FAILURE


@pytest.mark.parametrize("name", ["accept-encoding", "Accept-Encoding", "ACCEPT-ENCODING"])
async def test_user_supplied_accept_encoding_is_not_duplicated(
    run_upstream: Callable[[Handler], Awaitable[TestServer]], name: str
) -> None:
    echoed: list[list[str]] = []

    async def handler(request: web.Request) -> web.Response:
        echoed.append(request.headers.getall("Accept-Encoding", []))
        return web.Response(body=RAW)

    server = await run_upstream(handler)
    client = SafeHttpClient()
    response = await client.open(
        url=str(server.make_url("/file")), headers={name: "br"}, range_header=None
    )
    body = b"".join([chunk async for chunk in response.body])
    assert body == RAW
    assert echoed == [["br"]]
