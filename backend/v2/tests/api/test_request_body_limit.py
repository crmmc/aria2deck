"""A5：全局请求体上限中间件契约（16 MiB，413 中文错误，双保险）。"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

LIMIT = 16 * 1024 * 1024


def _body_of_length(total: int) -> str:
    """构造恰好 total 字节的登录 JSON（用户名吸收填充，格式合法）。"""
    tail = '","password":"' + "ab" * 32 + '"}'
    username_length = total - len('{"username":"') - len(tail)
    assert username_length > 0
    return '{"username":"' + "a" * username_length + tail


def test_body_just_under_or_at_limit_passes_middleware(client: TestClient) -> None:
    # 超长用户名被 pydantic 拒绝（422），但说明中间件放行了恰好 16 MiB 的请求体。
    response = client.post(
        "/api/v2/auth/login", content=_body_of_length(LIMIT).encode(), headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_input"


def test_oversized_body_with_content_length_is_rejected_413(client: TestClient) -> None:
    response = client.post(
        "/api/v2/auth/login",
        content=_body_of_length(LIMIT + 1).encode(),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413
    assert response.json() == {
        "error": {"code": "task_too_large", "message": "请求体过大，最大支持 16 MiB"}
    }


def test_chunked_body_without_content_length_is_rejected_after_read(
    client: TestClient,
) -> None:
    def chunks() -> Iterator[bytes]:
        payload = _body_of_length(LIMIT + 1024).encode()
        step = 1024 * 1024
        for start in range(0, len(payload), step):
            yield payload[start : start + step]

    response = client.post(
        "/api/v2/auth/login", content=chunks(), headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 413
    assert response.json()["error"]["message"] == "请求体过大，最大支持 16 MiB"


def test_normal_small_bodies_are_unaffected(client: TestClient) -> None:
    response = client.post("/api/v2/auth/login", json={"username": "u", "password": "ab" * 32})
    assert response.status_code in {200, 401}
    assert client.get("/api/health").status_code == 200


async def test_malformed_content_length_falls_back_to_actual_read_counting(
    app: FastAPI,
) -> None:
    """非数字 content-length（httpx 测试栈发不出，用原生 ASGI 注入）：
    预检跳过、按实际读取计数兜底，小请求体不误拒也不崩溃。"""
    async with app.router.lifespan_context(app):
        body = json.dumps({"username": "u", "password": "ab" * 32}).encode()
        scope: dict[str, Any] = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "scheme": "http",
            "method": "POST",
            "path": "/api/v2/auth/login",
            "raw_path": b"/api/v2/auth/login",
            "query_string": b"",
            "headers": [
                (b"host", b"test"),
                (b"content-type", b"application/json"),
                (b"content-length", b"not-a-number"),
            ],
            "app": app,
            "client": ("127.0.0.1", 12345),
            "server": ("test", 80),
        }

        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": body, "more_body": False}

        messages: list[dict[str, Any]] = []

        async def send(message: dict[str, Any]) -> None:
            messages.append(message)

        await app(scope, receive, send)

        status = next(
            message["status"] for message in messages if message["type"] == "http.response.start"
        )
        assert status in {200, 401}
