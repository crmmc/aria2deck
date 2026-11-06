"""A6 API 契约：下载并发限流在文件/分享下载端点真实生效并随响应结束释放。

httpx 的 ASGITransport 会把响应整体缓冲（后台释放任务提前执行），TestClient
的事件循环又与 lifespan 所在循环不同，因此「流未结束仍占用」的语义用
原生 ASGI 调用 + 暂停 send 验证，全部请求跑在同一事件循环上。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aria2deck.domain.settings import DOWNLOAD_CONNECTION_LIMIT_KEYS
from aria2deck.infrastructure.config import Settings
from tests.api.conftest import OTHER_PASSWORD, login
from tests.api.test_files import seed_file, setup_users

SESSION_COOKIE = "aria2deck_v2_session"


def _tighten(client: TestClient, limits: dict[str, int]) -> None:
    login(client)  # 管理员收紧设置
    response = client.patch("/api/v2/settings", json={"download_connection_limits": limits})
    assert response.status_code == 200, response.text


def _scope(app: FastAPI, path: str, *, method: str = "GET", cookie: str | None = None) -> dict[str, Any]:
    headers = [(b"host", b"test")]
    if cookie is not None:
        headers.append((b"cookie", f"{SESSION_COOKIE}={cookie}".encode()))
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "scheme": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers,
        "app": app,
        "client": ("127.0.0.1", 12345),
        "server": ("test", 80),
    }


def _receive() -> Any:
    """先给一个空 http.request，之后一直挂起模拟长连接。

    不能立刻回 http.disconnect：Starlette 的断连监听协程收到 disconnect 会
    取消整个任务组（流任务被一并取消）；也不能反复回 http.request（会忙轮询
    饿死事件循环）。挂起的 receive 会在响应流自然结束时随任务组取消。
    """
    state = {"served": False}

    async def receive() -> dict[str, Any]:
        if state["served"]:
            await asyncio.Future()
        state["served"] = True
        return {"type": "http.request", "body": b"", "more_body": False}

    return receive


async def _call(app: FastAPI, scope: dict[str, Any]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await app(scope, _receive(), send)
    return messages


async def _post_json(
    app: FastAPI,
    path: str,
    payload: dict[str, Any],
    *,
    cookie: str | None = None,
    method: str = "POST",
) -> list[dict[str, Any]]:
    body = json.dumps(payload).encode()
    scope = _scope(app, path, method=method, cookie=cookie)
    scope["headers"] = [
        *scope["headers"],
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
    ]
    messages: list[dict[str, Any]] = []
    served = False

    async def receive() -> dict[str, Any]:
        nonlocal served
        if served:
            await asyncio.Future()  # 挂起模拟长连接，见 _receive 说明
        served = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await app(scope, receive, send)
    return messages


async def _held_first_response(
    app: FastAPI, scope: dict[str, Any]
) -> tuple[asyncio.Task[None], asyncio.Event]:
    """发起首个下载请求，收到首个 body 块后暂停 send（模拟客户端慢慢接收）。"""
    seen_chunk = asyncio.Event()
    gate = asyncio.Event()

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.body" and message.get("body"):
            if not seen_chunk.is_set():
                seen_chunk.set()
                await gate.wait()

    task = asyncio.create_task(app(scope, _receive(), send))
    await asyncio.wait_for(seen_chunk.wait(), timeout=5.0)
    return task, gate


def _status(messages: list[dict[str, Any]]) -> int:
    return next(
        message["status"] for message in messages if message["type"] == "http.response.start"
    )


def _body(messages: list[dict[str, Any]]) -> str:
    return b"".join(
        message.get("body", b"") for message in messages if message["type"] == "http.response.body"
    ).decode()


def _session_cookie(messages: list[dict[str, Any]]) -> str:
    for message in messages:
        if message["type"] != "http.response.start":
            continue
        for name, value in message["headers"]:
            if name == b"set-cookie" and value.startswith(f"{SESSION_COOKIE}=".encode()):
                return value.decode().split("=", 1)[1].split(";", 1)[0]
    raise AssertionError("响应未携带会话 Cookie")


async def _bootstrap_admin(app: FastAPI) -> str:
    created = await _post_json(
        app, "/api/v2/users", {"username": "admin", "password": "ab" * 32, "is_admin": True}
    )
    assert _status(created) == 201
    logged_in = await _post_json(
        app, "/api/v2/auth/login", {"username": "admin", "password": "ab" * 32}
    )
    assert _status(logged_in) == 200
    return _session_cookie(logged_in)


def test_file_download_error_releases_lease(
    app: FastAPI, client: TestClient, settings: Settings
) -> None:
    setup_users(client)
    _tighten(client, {"authenticated_per_user_connections": 1})
    missing = client.get("/api/v2/files/999999/download")
    assert missing.status_code == 404
    # 失败路径同样释放占用：下一次（仍会 404 而非 429）。
    assert client.get("/api/v2/files/999999/download").status_code == 404


async def test_file_download_holds_slot_until_stream_completes(
    app: FastAPI, settings: Settings
) -> None:
    async with app.router.lifespan_context(app):
        cookie = await _bootstrap_admin(app)
        tightened = await _post_json(
            app,
            "/api/v2/settings",
            {"download_connection_limits": {"authenticated_per_user_connections": 1}},
            cookie=cookie,
            method="PATCH",
        )
        assert _status(tightened) == 200
        user = await _post_json(
            app,
            "/api/v2/users",
            {"username": "user", "password": OTHER_PASSWORD},
            cookie=cookie,
        )
        assert _status(user) == 201
        user_id = json.loads(_body(user))["id"]
        _, file_id = await asyncio.to_thread(
            seed_file, settings, user_id=user_id, index=1, payload=b"payload-1" * 512
        )
        user_login = await _post_json(
            app, "/api/v2/auth/login", {"username": "user", "password": OTHER_PASSWORD}
        )
        user_cookie = _session_cookie(user_login)

        path = f"/api/v2/files/{file_id}/download"
        first_task, gate = await _held_first_response(
            app, _scope(app, path, cookie=user_cookie)
        )

        second = await _call(app, _scope(app, path, cookie=user_cookie))
        assert _status(second) == 429
        assert "当前账号的下载连接数已达上限" in _body(second)

        gate.set()  # 首个流结束 → 后台释放
        await asyncio.wait_for(first_task, timeout=5.0)

        third = await _call(app, _scope(app, path, cookie=user_cookie))
        assert _status(third) == 200


async def test_share_download_anonymous_limit_per_share(
    app: FastAPI, settings: Settings
) -> None:
    async with app.router.lifespan_context(app):
        cookie = await _bootstrap_admin(app)
        user = await _post_json(
            app, "/api/v2/users", {"username": "user", "password": OTHER_PASSWORD}, cookie=cookie
        )
        assert _status(user) == 201
        user_id = json.loads(_body(user))["id"]
        _, file_id = await asyncio.to_thread(
            seed_file, settings, user_id=user_id, index=7, payload=b"shared-7" * 128
        )
        user_login = await _post_json(
            app, "/api/v2/auth/login", {"username": "user", "password": OTHER_PASSWORD}
        )
        user_cookie = _session_cookie(user_login)
        created = await _post_json(
            app, "/api/v2/shares", {"user_file_id": file_id}, cookie=user_cookie
        )
        assert _status(created) == 201, _body(created)
        code = json.loads(_body(created))["code"]
        tightened = await _post_json(
            app,
            "/api/v2/settings",
            {"download_connection_limits": {"anonymous_per_file_connections": 1}},
            cookie=cookie,
            method="PATCH",
        )
        assert _status(tightened) == 200

        path = f"/api/s/{code}/download"
        first_task, gate = await _held_first_response(app, _scope(app, path))

        second = await _call(app, _scope(app, path))
        assert _status(second) == 429
        assert "当前文件的匿名下载连接数已达上限" in _body(second)

        gate.set()
        await asyncio.wait_for(first_task, timeout=5.0)

        third = await _call(app, _scope(app, path))
        assert _status(third) == 200


async def _seed_user_file(
    app: FastAPI, settings: Settings, *, index: int, payload: bytes
) -> tuple[str, str, int]:
    """建管理员+用户+一个可下载文件，返回 (admin_cookie, user_cookie, file_id)。"""
    admin_cookie = await _bootstrap_admin(app)
    user = await _post_json(
        app,
        "/api/v2/users",
        {"username": "user", "password": OTHER_PASSWORD},
        cookie=admin_cookie,
    )
    assert _status(user) == 201
    user_id = json.loads(_body(user))["id"]
    _, file_id = await asyncio.to_thread(
        seed_file, settings, user_id=user_id, index=index, payload=payload
    )
    user_login = await _post_json(
        app, "/api/v2/auth/login", {"username": "user", "password": OTHER_PASSWORD}
    )
    return admin_cookie, _session_cookie(user_login), file_id


async def _tighten_limits(app: FastAPI, admin_cookie: str, limits: dict[str, int]) -> None:
    tightened = await _post_json(
        app,
        "/api/v2/settings",
        {"download_connection_limits": limits},
        cookie=admin_cookie,
        method="PATCH",
    )
    assert _status(tightened) == 200, _body(tightened)


async def _create_share(
    app: FastAPI, user_cookie: str, file_id: int, **overrides: object
) -> str:
    payload: dict[str, Any] = {"user_file_id": file_id}
    payload.update(overrides)
    created = await _post_json(app, "/api/v2/shares", payload, cookie=user_cookie)
    assert _status(created) == 201, _body(created)
    return str(json.loads(_body(created))["code"])


def _failing_once_iterator() -> Any:
    """首次调用：吐出一块后抛 OSError（模拟读盘中途失败）；之后恢复正常。"""
    attempts = {"count": 0}

    async def iterator(target: object) -> AsyncIterator[bytes]:
        if attempts["count"] == 0:
            attempts["count"] += 1
            yield b"partial-chunk"
            raise OSError("simulated disk failure mid-stream")
        yield b"recovered-chunk"

    return iterator


async def test_file_download_mid_stream_failure_releases_connection_slot(
    app: FastAPI, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P2 回归：starlette 在流中途异常时跳过 BackgroundTask，连接槽必须仍被释放。"""
    async with app.router.lifespan_context(app):
        admin_cookie, user_cookie, file_id = await _seed_user_file(
            app, settings, index=11, payload=b"leak-check" * 128
        )
        await _tighten_limits(app, admin_cookie, {"authenticated_per_user_connections": 1})
        monkeypatch.setattr(
            app.state.library_download, "iter_chunks", _failing_once_iterator()
        )

        path = f"/api/v2/files/{file_id}/download"
        with pytest.raises(OSError):
            await _call(app, _scope(app, path, cookie=user_cookie))

        # 中途异常后槽已释放：per_user=1 下再次下载不被 429 拒绝。
        retry = await _call(app, _scope(app, path, cookie=user_cookie))
        assert _status(retry) == 200


async def test_share_download_mid_stream_failure_releases_anonymous_slot(
    app: FastAPI, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P2 回归：公开分享流中途异常同样不得泄漏匿名连接槽。"""
    async with app.router.lifespan_context(app):
        admin_cookie, user_cookie, file_id = await _seed_user_file(
            app, settings, index=12, payload=b"anon-leak" * 128
        )
        code = await _create_share(app, user_cookie, file_id)
        await _tighten_limits(app, admin_cookie, {"anonymous_total_connections": 1})
        monkeypatch.setattr(
            app.state.library_download, "iter_chunks", _failing_once_iterator()
        )

        path = f"/api/s/{code}/download"
        with pytest.raises(OSError):
            await _call(app, _scope(app, path))

        retry = await _call(app, _scope(app, path))
        assert _status(retry) == 200


async def test_client_disconnect_releases_slot_exactly_once(
    app: FastAPI, settings: Settings
) -> None:
    """客户端断开：后台任务与 ASGI finally 两个释放路径只落一次，槽可复用。"""
    async with app.router.lifespan_context(app):
        admin_cookie, user_cookie, file_id = await _seed_user_file(
            app, settings, index=13, payload=b"disconnect" * 128
        )
        await _tighten_limits(app, admin_cookie, {"authenticated_per_user_connections": 1})
        path = f"/api/v2/files/{file_id}/download"

        seen_chunk = asyncio.Event()
        gate = asyncio.Event()

        async def send(message: dict[str, Any]) -> None:
            # 首个块送达后挂起，模拟客户端慢慢接收。
            if message["type"] == "http.response.body" and message.get("body"):
                if not seen_chunk.is_set():
                    seen_chunk.set()
                    await gate.wait()

        async def receive() -> dict[str, Any]:
            # 断开只发生在首个块已发出之后：流任务在 send 挂起时被取消。
            await seen_chunk.wait()
            return {"type": "http.disconnect"}

        disconnected = asyncio.create_task(
            app(_scope(app, path, cookie=user_cookie), receive, send)
        )
        await asyncio.wait_for(disconnected, timeout=5.0)
        gate.set()  # 释放 send 协程可能残留的挂起（已取消则为空操作）

        # 断开后槽已释放（且未双重释放破坏计数）：可再次下载。
        retry = await _call(app, _scope(app, path, cookie=user_cookie))
        assert _status(retry) == 200


async def test_invalid_share_access_does_not_consume_anonymous_slot(
    app: FastAPI, settings: Settings
) -> None:
    """#2 回归：无效分享码/令牌在校验期被拒，不消耗匿名连接档（V1 同序）。"""
    async with app.router.lifespan_context(app):
        admin_cookie, user_cookie, file_id = await _seed_user_file(
            app, settings, index=14, payload=b"ordering" * 128
        )
        plain_code = await _create_share(app, user_cookie, file_id)
        guarded_code = await _create_share(
            app, user_cookie, file_id, password="share-pass"  # noqa: S106  # 测试夹具口令
        )
        await _tighten_limits(app, admin_cookie, {"anonymous_total_connections": 1})

        # 占满唯一的匿名池名额。
        first_task, gate = await _held_first_response(
            app, _scope(app, f"/api/s/{plain_code}/download")
        )

        invalid_code = await _call(app, _scope(app, "/api/s/no-such-code/download"))
        assert _status(invalid_code) == 404  # 先校验分享码：不是 429

        wrong_token = await _call(app, _scope(app, f"/api/s/{guarded_code}/download"))
        assert _status(wrong_token) == 401  # 先校验令牌：不是 429

        gate.set()
        await asyncio.wait_for(first_task, timeout=5.0)


async def test_share_per_file_limit_is_shared_across_share_codes_for_same_content(
    app: FastAPI, settings: Settings
) -> None:
    """#3 回归：per-file 档按内容身份计（V1 content_hash 口径），同内容两分享码共享。"""
    async with app.router.lifespan_context(app):
        admin_cookie, user_cookie, file_id = await _seed_user_file(
            app, settings, index=15, payload=b"same-content" * 128
        )
        code_a = await _create_share(app, user_cookie, file_id)
        code_b = await _create_share(app, user_cookie, file_id)
        await _tighten_limits(app, admin_cookie, {"anonymous_per_file_connections": 1})

        first_task, gate = await _held_first_response(
            app, _scope(app, f"/api/s/{code_a}/download")
        )

        # 换分享码不换内容：仍受同一 per-file 档约束。
        second = await _call(app, _scope(app, f"/api/s/{code_b}/download"))
        assert _status(second) == 429
        assert "当前文件的匿名下载连接数已达上限" in _body(second)

        gate.set()
        await asyncio.wait_for(first_task, timeout=5.0)

        third = await _call(app, _scope(app, f"/api/s/{code_b}/download"))
        assert _status(third) == 200


async def test_zero_download_limits_persist_and_take_effect(
    app: FastAPI, settings: Settings
) -> None:
    """#6 契约：六档 0 值保存成功且回读一致；匿名池 0 全面拒绝、其余 0 不限。"""
    async with app.router.lifespan_context(app):
        admin_cookie, user_cookie, file_id = await _seed_user_file(
            app, settings, index=16, payload=b"zero-limits" * 128
        )
        code = await _create_share(app, user_cookie, file_id)
        zeros = {key: 0 for key in DOWNLOAD_CONNECTION_LIMIT_KEYS}
        patched = await _post_json(
            app,
            "/api/v2/settings",
            {"download_connection_limits": zeros},
            cookie=admin_cookie,
            method="PATCH",
        )
        assert _status(patched) == 200
        assert json.loads(_body(patched))["download_connection_limits"] == zeros

        # 匿名池 0：匿名下载全面拒绝（429，先于流式开始）。
        blocked = await _call(app, _scope(app, f"/api/s/{code}/download"))
        assert _status(blocked) == 429
        assert "匿名下载连接数已达上限" in _body(blocked)

        # 总量/认证分档 0：不限，认证下载正常放行。
        allowed = await _call(
            app, _scope(app, f"/api/v2/files/{file_id}/download", cookie=user_cookie)
        )
        assert _status(allowed) == 200
