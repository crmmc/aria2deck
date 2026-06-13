import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from starlette.responses import StreamingResponse

from app.core.download_limiter import (
    DownloadRejectReason,
    download_config,
    download_limiter,
)
from app.http.file_response import tracked_response


@pytest.fixture
def download_limits_snapshot():
    snapshot = {
        "total_connections": download_config.total_connections,
        "authenticated_reserved_connections": download_config.authenticated_reserved_connections,
        "authenticated_per_user_connections": download_config.authenticated_per_user_connections,
        "authenticated_per_file_connections": download_config.authenticated_per_file_connections,
        "anonymous_base_connections": download_config.anonymous_base_connections,
        "anonymous_borrow_connections": download_config.anonymous_borrow_connections,
        "anonymous_per_ip_connections": download_config.anonymous_per_ip_connections,
        "anonymous_per_file_connections": download_config.anonymous_per_file_connections,
    }
    yield snapshot
    for key, value in snapshot.items():
        setattr(download_config, key, value)


def _http_scope() -> dict[str, Any]:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 12345),
        "server": ("test", 80),
    }


async def _run_response_until_first_body_disconnect(
    response: Callable[..., Awaitable[None]],
) -> None:
    disconnect = asyncio.Event()

    async def receive() -> dict[str, str]:
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.body" and message.get("more_body"):
            disconnect.set()
            # 让 starlette 的 disconnect 监听在下一个 body chunk 前运行
            await asyncio.sleep(0)

    await response(_http_scope(), receive, send)


async def _run_response_to_completion(
    response: Callable[..., Awaitable[None]],
) -> None:
    never_disconnect = asyncio.Event()

    async def receive() -> dict[str, str]:
        await never_disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(_message: dict[str, Any]) -> None:
        return None

    await response(_http_scope(), receive, send)


@pytest.mark.asyncio
async def test_authenticated_per_user_limit(download_limits_snapshot):
    download_config.total_connections = 10
    download_config.authenticated_reserved_connections = 6
    download_config.authenticated_per_user_connections = 2
    download_config.authenticated_per_file_connections = 4
    download_config.anonymous_base_connections = 2
    download_config.anonymous_borrow_connections = 2
    download_config.anonymous_per_ip_connections = 2
    download_config.anonymous_per_file_connections = 1

    first = await download_limiter.acquire_authenticated(1, "file-a")
    second = await download_limiter.acquire_authenticated(1, "file-b")
    blocked = await download_limiter.acquire_authenticated(1, "file-c")

    assert first.allowed is True
    assert second.allowed is True
    assert blocked.allowed is False
    assert blocked.reason == DownloadRejectReason.AUTHENTICATED_PER_USER

    await first.lease.release()
    await second.lease.release()


@pytest.mark.asyncio
async def test_anonymous_limit_preserves_authenticated_reserved_capacity(download_limits_snapshot):
    download_config.total_connections = 10
    download_config.authenticated_reserved_connections = 6
    download_config.authenticated_per_user_connections = 3
    download_config.authenticated_per_file_connections = 3
    download_config.anonymous_base_connections = 2
    download_config.anonymous_borrow_connections = 2
    download_config.anonymous_per_ip_connections = 4
    download_config.anonymous_per_file_connections = 4

    anonymous_leases = []
    for idx in range(4):
        result = await download_limiter.acquire_anonymous(f"10.0.0.{idx}", f"file-{idx}")
        assert result.allowed is True
        anonymous_leases.append(result.lease)

    blocked_anonymous = await download_limiter.acquire_anonymous("10.0.0.99", "file-99")
    assert blocked_anonymous.allowed is False
    assert blocked_anonymous.reason == DownloadRejectReason.ANONYMOUS_POOL

    authenticated_leases = []
    for idx in range(6):
        result = await download_limiter.acquire_authenticated(idx + 1, f"auth-{idx}")
        assert result.allowed is True
        authenticated_leases.append(result.lease)

    blocked_authenticated = await download_limiter.acquire_authenticated(99, "auth-overflow")
    assert blocked_authenticated.allowed is False
    assert blocked_authenticated.reason == DownloadRejectReason.SYSTEM_TOTAL

    for lease in anonymous_leases + authenticated_leases:
        await lease.release()


@pytest.mark.asyncio
async def test_anonymous_per_ip_and_file_limits(download_limits_snapshot):
    download_config.total_connections = 10
    download_config.authenticated_reserved_connections = 6
    download_config.authenticated_per_user_connections = 3
    download_config.authenticated_per_file_connections = 3
    download_config.anonymous_base_connections = 2
    download_config.anonymous_borrow_connections = 2
    download_config.anonymous_per_ip_connections = 2
    download_config.anonymous_per_file_connections = 1

    first = await download_limiter.acquire_anonymous("10.0.0.1", "shared-file")
    same_file = await download_limiter.acquire_anonymous("10.0.0.1", "shared-file")
    second_file = await download_limiter.acquire_anonymous("10.0.0.1", "other-file")
    third_file = await download_limiter.acquire_anonymous("10.0.0.1", "third-file")

    assert first.allowed is True
    assert same_file.allowed is False
    assert same_file.reason == DownloadRejectReason.ANONYMOUS_PER_FILE
    assert second_file.allowed is True
    assert third_file.allowed is False
    assert third_file.reason == DownloadRejectReason.ANONYMOUS_PER_IP

    await first.lease.release()
    await second_file.lease.release()


@pytest.mark.asyncio
async def test_lease_released_when_streaming_client_disconnects(
    download_limits_snapshot,
):
    """客户端在 Range 流式下载中途断开，lease 必须释放，否则 per-file 计数泄漏阻塞后续下载。"""
    await download_limiter.clear_all()
    download_config.total_connections = 0
    download_config.authenticated_per_user_connections = 0
    download_config.authenticated_per_file_connections = 1

    try:
        acquired = await download_limiter.acquire_authenticated(1, "stream-file")
        assert acquired.allowed is True
        assert acquired.lease is not None

        async def body():
            for _ in range(50):
                yield b"x" * 65536
                await asyncio.sleep(0)

        response = tracked_response(
            StreamingResponse(body(), media_type="application/octet-stream"),
            acquired.lease,
        )

        await _run_response_until_first_body_disconnect(response)

        reacquired = await download_limiter.acquire_authenticated(1, "stream-file")
        assert reacquired.allowed is True, "lease 未释放，per-file 计数泄漏"
        assert reacquired.lease is not None
        await reacquired.lease.release()
    finally:
        await download_limiter.clear_all()


@pytest.mark.asyncio
async def test_lease_released_when_streaming_response_completes(
    download_limits_snapshot,
):
    """流式响应正常结束时，lease 必须释放。"""
    await download_limiter.clear_all()
    download_config.total_connections = 0
    download_config.authenticated_per_user_connections = 0
    download_config.authenticated_per_file_connections = 1

    try:
        acquired = await download_limiter.acquire_authenticated(1, "stream-file")
        assert acquired.allowed is True
        assert acquired.lease is not None

        async def body():
            yield b"first"
            await asyncio.sleep(0)
            yield b"second"

        response = tracked_response(
            StreamingResponse(body(), media_type="application/octet-stream"),
            acquired.lease,
        )

        await _run_response_to_completion(response)

        reacquired = await download_limiter.acquire_authenticated(1, "stream-file")
        assert reacquired.allowed is True
        assert reacquired.lease is not None
        await reacquired.lease.release()
    finally:
        await download_limiter.clear_all()
