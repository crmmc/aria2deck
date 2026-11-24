"""Coverage tests for app/routers/ws.py edge paths."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.core.config import settings
from app.routers.ws import _origin_key, _revalidate_session, task_ws

TRUSTED_ORIGIN = "http://testserver"


@pytest.mark.parametrize(
    "origin",
    [
        "http://testserver/some/path",
        "http://testserver/?q=1",
        "http://testserver#frag",
        "ftp://testserver",
        "http://user:pass@testserver",
        "http://[invalid",
        "not-a-url",
    ],
)
def test_origin_key_rejects_malformed_origins(origin: str) -> None:
    assert _origin_key(origin) is None


def test_origin_key_accepts_valid_origin() -> None:
    assert _origin_key("HTTP://TestServer:80") == ("http", "testserver", 80)
    assert _origin_key("https://example.com") == ("https", "example.com", 443)


def test_websocket_rejects_origin_with_path(
    client: TestClient, user_session: str, test_user: dict
) -> None:
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(
            "/ws/tasks",
            cookies={settings.session_cookie_name: user_session},
            headers={"origin": "http://testserver/extra"},
        ) as websocket:
            websocket.receive_text()
    assert exc_info.value.code == 4403


def test_websocket_pong_is_ignored(
    client: TestClient, user_session: str, test_user: dict
) -> None:
    with client.websocket_connect(
        "/ws/tasks",
        cookies={settings.session_cookie_name: user_session},
        headers={"origin": TRUSTED_ORIGIN},
    ) as websocket:
        websocket.send_text("pong")
        websocket.send_text("ping")
        assert websocket.receive_text() == "pong"


def test_websocket_session_still_valid_keeps_connection(
    client: TestClient, user_session: str, test_user: dict
) -> None:
    user = SimpleNamespace(id=test_user["id"])
    revalidated = threading.Event()

    async def fake_get_user_by_session(session_id: str):
        revalidated.set()
        return user

    with patch("app.routers.ws.SESSION_REVALIDATION_INTERVAL_SECONDS", 0.01):
        with patch(
            "app.routers.ws.get_user_by_session",
            AsyncMock(side_effect=fake_get_user_by_session),
        ):
            with client.websocket_connect(
                "/ws/tasks",
                cookies={settings.session_cookie_name: user_session},
                headers={"origin": TRUSTED_ORIGIN},
            ) as websocket:
                assert revalidated.wait(timeout=10), "会话复验未在预期时间内执行"
                websocket.send_text("ping")
                assert websocket.receive_text() == "pong"


def test_websocket_closes_when_revalidation_raises(
    client: TestClient, user_session: str, test_user: dict
) -> None:
    with patch("app.routers.ws.SESSION_REVALIDATION_INTERVAL_SECONDS", 0.01):
        user = SimpleNamespace(id=test_user["id"])
        with patch(
            "app.routers.ws.get_user_by_session",
            AsyncMock(side_effect=[user, RuntimeError("db down")]),
        ):
            with client.websocket_connect(
                "/ws/tasks",
                cookies={settings.session_cookie_name: user_session},
                headers={"origin": TRUSTED_ORIGIN},
            ) as websocket:
                with pytest.raises(WebSocketDisconnect) as exc_info:
                    websocket.receive_text()
    assert exc_info.value.code == 1011


@pytest.mark.asyncio
async def test_revalidate_session_unit_close_failure_is_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.routers.ws.SESSION_REVALIDATION_INTERVAL_SECONDS", 0.01
    )
    async def fake_lookup(session_id: str):
        raise RuntimeError("db down")

    failing_close = AsyncMock(side_effect=RuntimeError("already closed"))
    websocket = MagicMock()
    websocket.close = failing_close
    unregister = AsyncMock()

    monkeypatch.setattr("app.routers.ws.get_user_by_session", fake_lookup)
    monkeypatch.setattr("app.routers.ws.unregister_ws", unregister)

    with patch("app.routers.ws.SESSION_REVALIDATION_INTERVAL_SECONDS", 0.01):
        await asyncio.wait_for(
            _revalidate_session(websocket, "session", 7), timeout=2
        )

    unregister.assert_awaited_once_with(7, websocket)
    failing_close.assert_awaited_once_with(code=1011)




@pytest.mark.asyncio
async def test_revalidate_session_closes_on_user_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_lookup(session_id: str):
        return SimpleNamespace(id=999)

    websocket = MagicMock()
    websocket.close = AsyncMock()
    unregister = AsyncMock()
    monkeypatch.setattr("app.routers.ws.get_user_by_session", fake_lookup)
    monkeypatch.setattr("app.routers.ws.unregister_ws", unregister)

    with patch("app.routers.ws.SESSION_REVALIDATION_INTERVAL_SECONDS", 0.01):
        await asyncio.wait_for(
            _revalidate_session(websocket, "session", 7), timeout=2
        )
    unregister.assert_awaited_once_with(7, websocket)
    websocket.close.assert_awaited_once_with(code=4401)


@pytest.mark.asyncio
async def test_task_ws_closes_when_reservation_activation_fails(
    user_session: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    websocket = MagicMock()
    websocket.headers = {"origin": "http://testserver"}
    websocket.cookies = {settings.session_cookie_name: user_session}
    websocket.url = SimpleNamespace(scheme="ws", hostname="testserver", port=None)
    websocket.client = SimpleNamespace(host="10.0.0.1")
    websocket.accept = AsyncMock()
    websocket.close = AsyncMock()

    class FailingReservation:
        async def activate(self, websocket):
            return False

        async def release(self):
            return None

    async def fake_register(user_id, session_id, client_ip):
        return FailingReservation()

    monkeypatch.setattr("app.routers.ws.register_ws", fake_register)

    await task_ws(websocket)

    websocket.close.assert_awaited_once_with(code=4401)
