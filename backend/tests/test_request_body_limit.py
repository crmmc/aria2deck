import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException
from starlette.types import Message, Receive, Scope, Send

from app.http.request_body_limit import (
    MAX_HTTP_REQUEST_BODY_BYTES,
    REQUEST_BODY_TOO_LARGE_DETAIL,
    RequestBodyLimitMiddleware,
)
from app.main import create_app


async def _unexpected(*args: object) -> Message:
    raise AssertionError("unexpected ASGI call")


async def _run_http(
    *, headers: list[tuple[bytes, bytes]], chunks: list[bytes], limit: int = 5
) -> tuple[list[Message], int, bool]:
    sent: list[Message] = []
    receive_calls = 0
    downstream_called = False
    messages = [
        {"type": "http.request", "body": chunk, "more_body": index < len(chunks) - 1}
        for index, chunk in enumerate(chunks)
    ]

    async def receive() -> Message:
        nonlocal receive_calls
        receive_calls += 1
        return messages.pop(0)

    async def send(message: Message) -> None:
        sent.append(message)

    async def downstream(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal downstream_called
        downstream_called = True
        while (await receive()).get("more_body", False):
            pass
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    scope: Scope = {"type": "http", "headers": headers}
    middleware = RequestBodyLimitMiddleware(downstream, max_body_bytes=limit)
    await middleware(scope, receive, send)
    return sent, receive_calls, downstream_called


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        [(b"content-length", b"6")],
        [(b"content-length", b"invalid"), (b"content-length", b"6")],
        [(b"content-length", b"6"), (b"content-length", b"1")],
    ],
)
async def test_declared_oversize_rejected_without_receive_or_downstream(
    headers: list[tuple[bytes, bytes]],
) -> None:
    sent, receive_calls, downstream_called = await _run_http(
        headers=headers, chunks=[b"ignored"]
    )

    assert sent[0]["status"] == 413
    assert receive_calls == 0
    assert downstream_called is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        [],
        [(b"content-length", b"1")],
        [(b"content-length", b"invalid")],
    ],
)
async def test_actual_chunk_total_enforces_limit(headers: list[tuple[bytes, bytes]]) -> None:
    sent, receive_calls, downstream_called = await _run_http(
        headers=headers, chunks=[b"abc", b"def"]
    )

    assert sent[0]["status"] == 413
    assert json.loads(sent[1]["body"])["detail"] == REQUEST_BODY_TOO_LARGE_DETAIL
    assert receive_calls == 2
    assert downstream_called is True


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [2, 5])
async def test_normal_and_exact_limit_bodies_pass(size: int) -> None:
    sent, receive_calls, downstream_called = await _run_http(
        headers=[], chunks=[b"x" * size]
    )

    assert sent[0]["status"] == 204
    assert receive_calls == 1
    assert downstream_called is True


@pytest.mark.asyncio
async def test_websocket_scope_is_unchanged() -> None:
    called = False

    async def downstream(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal called
        called = scope["type"] == "websocket"

    middleware = RequestBodyLimitMiddleware(downstream, max_body_bytes=5)
    await middleware({"type": "websocket"}, _unexpected, _unexpected)
    assert called is True


@pytest.mark.asyncio
async def test_http_disconnect_is_forwarded() -> None:
    received: Message | None = None

    async def downstream(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal received
        received = await receive()

    async def receive() -> Message:
        return {"type": "http.disconnect"}

    middleware = RequestBodyLimitMiddleware(downstream, max_body_bytes=5)
    await middleware({"type": "http", "headers": []}, receive, _unexpected)
    assert received == {"type": "http.disconnect"}


@pytest.mark.asyncio
async def test_unrelated_downstream_exception_propagates() -> None:
    async def downstream(scope: Scope, receive: Receive, send: Send) -> None:
        raise RuntimeError("downstream failed")

    middleware = RequestBodyLimitMiddleware(downstream, max_body_bytes=5)
    with pytest.raises(RuntimeError, match="downstream failed"):
        await middleware(
            {"type": "http", "headers": []}, _unexpected, _unexpected
        )


@pytest.mark.asyncio
async def test_overflow_after_response_start_does_not_send_second_response() -> None:
    sent: list[Message] = []

    async def downstream(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await receive()

    async def receive() -> Message:
        return {"type": "http.request", "body": b"123456", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    middleware = RequestBodyLimitMiddleware(downstream, max_body_bytes=5)
    with pytest.raises(HTTPException) as exc_info:
        await middleware({"type": "http", "headers": []}, receive, send)

    assert exc_info.value.status_code == 413
    assert [message["type"] for message in sent] == ["http.response.start"]


def test_unauthenticated_oversized_json_is_rejected_before_auth() -> None:
    with patch("app.auth.get_user_by_session", new=AsyncMock(side_effect=AssertionError)):
        response = TestClient(create_app()).post(
            "/api/tasks",
            content=b"x" * (MAX_HTTP_REQUEST_BODY_BYTES + 1),
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 413
    assert response.json() == {"detail": REQUEST_BODY_TOO_LARGE_DETAIL}


@pytest.mark.asyncio
async def test_full_stack_rejects_forged_small_length_chunked_json() -> None:
    first = b'{"uri":"' + b"x" * (MAX_HTTP_REQUEST_BODY_BYTES // 2)
    second = b"x" * (MAX_HTTP_REQUEST_BODY_BYTES // 2) + b'"}'
    messages: list[Message] = [
        {"type": "http.request", "body": first, "more_body": True},
        {"type": "http.request", "body": second, "more_body": False},
    ]
    sent: list[Message] = []
    chunks_read = 0
    blocked = asyncio.Event()

    async def receive() -> Message:
        nonlocal chunks_read
        if messages:
            chunks_read += 1
            return messages.pop(0)
        await blocked.wait()
        raise AssertionError("unreachable")

    async def send(message: Message) -> None:
        sent.append(message)

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/tasks",
        "raw_path": b"/api/tasks",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"content-length", b"1"),
            (b"content-type", b"application/json"),
        ],
        "client": ("test", 1),
        "server": ("test", 80),
    }
    with patch("app.auth.get_user_by_session", new=AsyncMock(side_effect=AssertionError)):
        await create_app()(scope, receive, send)

    assert chunks_read == 2
    assert sent[0]["status"] == 413
    assert json.loads(sent[1]["body"]) == {
        "detail": REQUEST_BODY_TOO_LARGE_DETAIL
    }
