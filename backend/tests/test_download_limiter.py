import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from fastapi import Request
from starlette.responses import FileResponse, StreamingResponse
from starlette.types import Scope

from app.auth import AuthUser
from app.core.download_limiter import (
    DownloadRejectReason,
    download_config,
    download_limiter,
)
from app.http.file_response import range_file_response, tracked_response
from app.services.storage_locks import (
    acquire_content_read_lease_locked,
    get_content_hash_lock,
    wait_for_content_readers_locked,
)
import app.routers.files as files_router
import app.routers.shares as shares_router


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


def _http_scope() -> Scope:
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


def _auth_user() -> AuthUser:
    return AuthUser(
        id=1,
        username="test",
        password_hash="hash",
        is_admin=False,
        quota=1024,
        quota_bytes=1024,
        is_initial_password=False,
    )


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
async def test_file_route_cancellation_before_tracking_releases_lease(
    download_limits_snapshot,
    monkeypatch,
):
    await download_limiter.clear_all()
    download_config.total_connections = 0
    download_config.authenticated_per_user_connections = 0
    download_config.authenticated_per_file_connections = 1
    resolve_started = asyncio.Event()
    blocker = asyncio.Event()

    async def blocking_resolve(*_args, **_kwargs):
        resolve_started.set()
        await blocker.wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(
        files_router.file_service,
        "resolve_download_target",
        blocking_resolve,
    )
    route_task = asyncio.create_task(
        files_router.download_file(
            "route-file",
            Request(_http_scope()),
            user=_auth_user(),
        )
    )
    try:
        await resolve_started.wait()
        route_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await route_task

        reacquired = await download_limiter.acquire_authenticated(1, "route-file")
        assert reacquired.allowed is True
        assert reacquired.lease is not None
        await reacquired.lease.release()
    finally:
        blocker.set()
        if not route_task.done():
            route_task.cancel()
            await asyncio.gather(route_task, return_exceptions=True)
        await download_limiter.clear_all()


@pytest.mark.asyncio
async def test_share_route_cancellation_before_tracking_releases_lease(
    download_limits_snapshot,
    monkeypatch,
):
    await download_limiter.clear_all()
    download_config.total_connections = 10
    download_config.anonymous_base_connections = 10
    download_config.anonymous_borrow_connections = 0
    download_config.anonymous_per_ip_connections = 10
    download_config.anonymous_per_file_connections = 1
    resolve_started = asyncio.Event()
    blocker = asyncio.Event()

    async def check_access(_code, _token):
        return {"id": 1, "content_hash": "shared-route-file"}

    async def blocking_resolve(*_args, **_kwargs):
        resolve_started.set()
        await blocker.wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(shares_router.share_service, "check_share_access", check_access)
    monkeypatch.setattr(
        shares_router.share_service,
        "resolve_shared_download_target",
        blocking_resolve,
    )
    route_task = asyncio.create_task(
        shares_router.download_shared_file(
            "code",
            Request(_http_scope()),
            subpath=None,
        )
    )
    try:
        await resolve_started.wait()
        route_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await route_task

        reacquired = await download_limiter.acquire_anonymous(
            "127.0.0.1", "shared-route-file"
        )
        assert reacquired.allowed is True
        assert reacquired.lease is not None
        await reacquired.lease.release()
    finally:
        blocker.set()
        if not route_task.done():
            route_task.cancel()
            await asyncio.gather(route_task, return_exceptions=True)
        await download_limiter.clear_all()


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
async def test_lease_released_when_file_response_send_is_cancelled(
    download_limits_snapshot,
    tmp_path,
):
    await download_limiter.clear_all()
    download_config.total_connections = 0
    download_config.authenticated_per_user_connections = 0
    download_config.authenticated_per_file_connections = 1
    file_path = tmp_path / "download.bin"
    file_path.write_bytes(b"content")

    try:
        acquired = await download_limiter.acquire_authenticated(1, "file-response")
        assert acquired.lease is not None
        response = tracked_response(FileResponse(file_path), acquired.lease)

        async def receive() -> dict[str, str]:
            await asyncio.Event().wait()
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.body":
                raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await response(_http_scope(), receive, send)

        reacquired = await download_limiter.acquire_authenticated(1, "file-response")
        assert reacquired.allowed is True
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


@pytest.mark.asyncio
async def test_lease_release_can_retry_after_manager_failure(
    download_limits_snapshot,
    monkeypatch,
):
    await download_limiter.clear_all()
    download_config.total_connections = 0
    download_config.authenticated_per_user_connections = 0
    download_config.authenticated_per_file_connections = 1
    original_release = download_limiter.release
    calls = 0

    async def flaky_release(lease):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("release failed")
        await original_release(lease)

    monkeypatch.setattr(download_limiter, "release", flaky_release)
    try:
        acquired = await download_limiter.acquire_authenticated(1, "retry-file")
        assert acquired.lease is not None

        with pytest.raises(RuntimeError, match="release failed"):
            await acquired.lease.release()
        await acquired.lease.release()

        assert calls == 2
        reacquired = await download_limiter.acquire_authenticated(1, "retry-file")
        assert reacquired.allowed is True
        assert reacquired.lease is not None
        await reacquired.lease.release()
    finally:
        await download_limiter.clear_all()


@pytest.mark.asyncio
async def test_response_waits_for_release_when_finally_is_cancelled(
    download_limits_snapshot,
    monkeypatch,
):
    await download_limiter.clear_all()
    download_config.total_connections = 0
    download_config.authenticated_per_user_connections = 0
    download_config.authenticated_per_file_connections = 1
    original_release = download_limiter.release
    release_started = asyncio.Event()
    allow_release = asyncio.Event()

    async def delayed_release(lease):
        release_started.set()
        await allow_release.wait()
        await original_release(lease)

    monkeypatch.setattr(download_limiter, "release", delayed_release)
    response_task = None
    try:
        acquired = await download_limiter.acquire_authenticated(1, "cancel-file")
        assert acquired.lease is not None

        async def body():
            yield b"content"

        response = tracked_response(StreamingResponse(body()), acquired.lease)
        response_task = asyncio.create_task(_run_response_to_completion(response))
        await release_started.wait()
        response_task.cancel()
        await asyncio.sleep(0)
        assert response_task.done() is False

        allow_release.set()
        with pytest.raises(asyncio.CancelledError):
            await response_task

        monkeypatch.setattr(download_limiter, "release", original_release)
        reacquired = await download_limiter.acquire_authenticated(1, "cancel-file")
        assert reacquired.allowed is True
        assert reacquired.lease is not None
        await reacquired.lease.release()
    finally:
        allow_release.set()
        if response_task is not None and not response_task.done():
            await asyncio.gather(response_task, return_exceptions=True)
        await download_limiter.clear_all()


@pytest.mark.asyncio
async def test_shared_range_response_holds_content_read_lease(
    download_limits_snapshot,
    monkeypatch,
    tmp_path,
):
    await download_limiter.clear_all()
    download_config.total_connections = 10
    download_config.anonymous_base_connections = 10
    download_config.anonymous_borrow_connections = 0
    download_config.anonymous_per_ip_connections = 10
    download_config.anonymous_per_file_connections = 10
    content_hash = "shared-range-read-lease"
    file_path = tmp_path / "shared.bin"
    file_path.write_bytes(b"content")
    share: dict[str, object] = {"id": 1, "content_hash": content_hash}

    async def check_access(_code: str, _token: str | None) -> dict[str, object]:
        return share

    async def resolve_target(*_args, **_kwargs):
        return file_path, file_path.name

    count_decisions: list[bool] = []

    async def record_share(
        _share: dict[str, object], *, should_count_download: bool
    ) -> None:
        count_decisions.append(should_count_download)

    monkeypatch.setattr(shares_router.share_service, "check_share_access", check_access)
    monkeypatch.setattr(
        shares_router.share_service, "resolve_shared_download_target", resolve_target
    )
    monkeypatch.setattr(
        shares_router.share_service, "record_shared_download", record_share
    )
    scope = {**_http_scope(), "headers": [(b"range", b"bytes=0-3")]}
    response = await shares_router.download_shared_file(
        "code", Request(scope), subpath=None
    )
    assert count_decisions == [False]

    async def wait_for_readers() -> None:
        lock = await get_content_hash_lock(content_hash)
        async with lock:
            await wait_for_content_readers_locked(content_hash)

    waiter = asyncio.create_task(wait_for_readers())
    await asyncio.sleep(0)
    assert not waiter.done()
    await _run_response_to_completion(response)
    await asyncio.wait_for(waiter, timeout=1)


@pytest.mark.asyncio
async def test_range_disconnect_releases_content_read_lease(tmp_path):
    content_hash = "range-disconnect-read-lease"
    file_path = tmp_path / "range.bin"
    file_path.write_bytes(b"content")
    lock = await get_content_hash_lock(content_hash)
    async with lock:
        read_lease = acquire_content_read_lease_locked(content_hash)
    scope = {**_http_scope(), "headers": [(b"range", b"bytes=0-3")]}
    response = tracked_response(
        range_file_response(Request(scope), file_path, file_path.name),
        None,
        read_lease,
    )
    disconnected = asyncio.Event()

    async def receive() -> dict[str, str]:
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.body" and message.get("more_body"):
            disconnected.set()
            await asyncio.sleep(0)

    await response(scope, receive, send)
    async with lock:
        await asyncio.wait_for(wait_for_content_readers_locked(content_hash), timeout=1)


@pytest.mark.asyncio
async def test_file_route_releases_read_lease_when_response_creation_fails(
    download_limits_snapshot,
    monkeypatch,
    tmp_path,
):
    await download_limiter.clear_all()
    download_config.total_connections = 0
    download_config.authenticated_per_user_connections = 0
    download_config.authenticated_per_file_connections = 1
    content_hash = "response-construction-read-lease"
    lock = await get_content_hash_lock(content_hash)
    async with lock:
        read_lease = acquire_content_read_lease_locked(content_hash)

    async def resolve_target(*_args, **_kwargs):
        return tmp_path / "missing.bin", "missing.bin", read_lease

    def fail_response(*_args, **_kwargs):
        raise RuntimeError("response construction failed")

    monkeypatch.setattr(
        files_router.file_service, "resolve_download_target_with_read_lease", resolve_target
    )
    monkeypatch.setattr(files_router, "range_file_response", fail_response)
    with pytest.raises(RuntimeError, match="response construction failed"):
        await files_router.download_file(
            content_hash, Request(_http_scope()), user=_auth_user()
        )
    async with lock:
        await asyncio.wait_for(wait_for_content_readers_locked(content_hash), timeout=1)
    await download_limiter.clear_all()
