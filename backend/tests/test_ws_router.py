"""Tests for WebSocket router."""

import asyncio
import queue
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update
from starlette.websockets import WebSocket, WebSocketDisconnect

from app.core.config import settings
from app.db.engine import transaction
from app.db.schema import sessions
from app.routers.ws import task_ws
from app.services.task_broadcast import (
    broadcast_notification,
    clear_connections,
    register_ws,
    release_ws_slot,
    remove_connections_for_session,
    reserve_ws_slot,
)
from tests.helpers_v0 import create_session_v0, create_user_v0, now_ms


TRUSTED_ORIGIN = "http://testserver"


async def _expire_session(session_id: str) -> None:
    async with transaction() as conn:
        await conn.execute(
            update(sessions)
            .where(sessions.c.id == session_id)
            .values(expires_at_ms=now_ms() - 1)
        )


async def _activate_test_socket(
    user_id: int,
    session_id: str,
    client_ip: str,
    websocket: object,
) -> bool:
    reservation = await register_ws(user_id, session_id, client_ip)
    if reservation is None:
        return False
    try:
        return await reservation.activate(websocket)
    finally:
        await reservation.release()


@pytest.fixture(autouse=True)
def reset_websocket_connections():
    asyncio.run(clear_connections())
    yield
    asyncio.run(clear_connections())


class TestTaskWebSocket:
    def test_websocket_connect_valid_session(
        self, client: TestClient, user_session: str, test_user: dict
    ):
        with client.websocket_connect(
            "/ws/tasks",
            cookies={settings.session_cookie_name: user_session},
            headers={"origin": TRUSTED_ORIGIN},
        ) as websocket:
            websocket.send_text("ping")
            assert websocket.receive_text() == "pong"

    def test_websocket_reject_invalid_session(
        self, client: TestClient, temp_db: str, test_user: dict
    ):
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect(
                "/ws/tasks",
                cookies={settings.session_cookie_name: "invalid_session"},
                headers={"origin": TRUSTED_ORIGIN},
            ) as websocket:
                websocket.receive_text()
        assert exc_info.value.code == 4401

    def test_websocket_reject_no_session(
        self, client: TestClient, temp_db: str, test_user: dict
    ):
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect(
                "/ws/tasks", headers={"origin": TRUSTED_ORIGIN}
            ) as websocket:
                websocket.receive_text()
        assert exc_info.value.code == 4401

    def test_websocket_ping_pong(
        self, client: TestClient, user_session: str, test_user: dict
    ):
        with client.websocket_connect(
            "/ws/tasks",
            cookies={settings.session_cookie_name: user_session},
            headers={"origin": TRUSTED_ORIGIN},
        ) as websocket:
            websocket.send_text("ping")
            assert websocket.receive_text() == "pong"

    def test_websocket_connect_trailing_slash(
        self, client: TestClient, user_session: str, test_user: dict
    ):
        with client.websocket_connect(
            "/ws/tasks/",
            cookies={settings.session_cookie_name: user_session},
            headers={"origin": TRUSTED_ORIGIN},
        ) as websocket:
            websocket.send_text("ping")
            assert websocket.receive_text() == "pong"

    def test_websocket_unknown_path_closed(self, client: TestClient, temp_db: str):
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect("/ws/unknown") as websocket:
                websocket.receive_text()
        assert exc_info.value.code == 4404

    def test_websocket_does_not_emit_unsolicited_json_ping(
        self, client: TestClient, user_session: str, test_user: dict
    ):
        with patch("app.routers.ws.HEARTBEAT_INTERVAL", 0.01, create=True):
            with client.websocket_connect(
                "/ws/tasks",
                cookies={settings.session_cookie_name: user_session},
                headers={"origin": TRUSTED_ORIGIN},
            ) as websocket:
                received: queue.Queue[str] = queue.Queue()

                def receive_one() -> None:
                    received.put(websocket.receive_text())

                thread = threading.Thread(target=receive_one, daemon=True)
                thread.start()
                thread.join(timeout=0.05)

                assert received.empty()

    def test_websocket_allows_explicit_dev_origin_in_debug(
        self,
        client: TestClient,
        user_session: str,
        test_user: dict,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setattr(settings, "debug", True)
        with client.websocket_connect(
            "/ws/tasks",
            cookies={settings.session_cookie_name: user_session},
            headers={"origin": "http://localhost:3000"},
        ) as websocket:
            websocket.send_text("ping")
            assert websocket.receive_text() == "pong"

    def test_websocket_rejects_dev_origin_in_production(
        self,
        client: TestClient,
        user_session: str,
        test_user: dict,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setattr(settings, "debug", False)
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect(
                "/ws/tasks",
                cookies={settings.session_cookie_name: user_session},
                headers={"origin": "http://localhost:3000"},
            ) as websocket:
                websocket.receive_text()
        assert exc_info.value.code == 4403

    def test_websocket_allows_configured_origin_in_production(
        self,
        client: TestClient,
        user_session: str,
        test_user: dict,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setattr(settings, "debug", False)
        monkeypatch.setenv("ARIA2C_CORS_ORIGINS", "https://frontend.example")
        with client.websocket_connect(
            "/ws/tasks",
            cookies={settings.session_cookie_name: user_session},
            headers={"origin": "https://frontend.example"},
        ) as websocket:
            websocket.send_text("ping")
            assert websocket.receive_text() == "pong"

    def test_websocket_rejects_missing_origin(
        self, client: TestClient, user_session: str, test_user: dict
    ):
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect(
                "/ws/tasks", cookies={settings.session_cookie_name: user_session}
            ) as websocket:
                websocket.receive_text()
        assert exc_info.value.code == 4403

    def test_websocket_rejects_untrusted_origin(
        self, client: TestClient, user_session: str, test_user: dict
    ):
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect(
                "/ws/tasks",
                cookies={settings.session_cookie_name: user_session},
                headers={"origin": "https://evil.example"},
            ) as websocket:
                websocket.receive_text()
        assert exc_info.value.code == 4403

    def test_websocket_rejects_user_connection_limit(
        self, client: TestClient, user_session: str, test_user: dict
    ):
        with patch("app.services.task_broadcast.MAX_WS_CONNECTIONS_PER_USER", 1):
            with client.websocket_connect(
                "/ws/tasks",
                cookies={settings.session_cookie_name: user_session},
                headers={"origin": TRUSTED_ORIGIN},
            ):
                with pytest.raises(WebSocketDisconnect) as exc_info:
                    with client.websocket_connect(
                        "/ws/tasks",
                        cookies={settings.session_cookie_name: user_session},
                        headers={"origin": TRUSTED_ORIGIN},
                    ) as websocket:
                        websocket.receive_text()
        assert exc_info.value.code == 4429

    def test_websocket_disconnect_releases_limit(
        self, client: TestClient, user_session: str, test_user: dict
    ):
        with patch("app.services.task_broadcast.MAX_WS_CONNECTIONS_PER_USER", 1):
            with client.websocket_connect(
                "/ws/tasks",
                cookies={settings.session_cookie_name: user_session},
                headers={"origin": TRUSTED_ORIGIN},
            ) as websocket:
                websocket.send_text("ping")
                assert websocket.receive_text() == "pong"

            with client.websocket_connect(
                "/ws/tasks",
                cookies={settings.session_cookie_name: user_session},
                headers={"origin": TRUSTED_ORIGIN},
            ) as websocket:
                websocket.send_text("ping")
                assert websocket.receive_text() == "pong"

    def test_logout_closes_existing_connection(
        self, client: TestClient, user_session: str, test_user: dict
    ):
        client.cookies.set(settings.session_cookie_name, user_session)
        with client.websocket_connect(
            "/ws/tasks",
            cookies={settings.session_cookie_name: user_session},
            headers={"origin": TRUSTED_ORIGIN},
        ) as websocket:
            response = client.post("/api/auth/logout")
            assert response.status_code == 200
            with pytest.raises(WebSocketDisconnect) as exc_info:
                websocket.receive_text()
        assert exc_info.value.code == 4401

    def test_self_password_change_closes_existing_connection(
        self, client: TestClient, user_session: str, test_user: dict
    ):
        client.cookies.set(settings.session_cookie_name, user_session)
        with client.websocket_connect(
            "/ws/tasks",
            cookies={settings.session_cookie_name: user_session},
            headers={"origin": TRUSTED_ORIGIN},
        ) as websocket:
            response = client.post(
                "/api/auth/change-password",
                json={"old_password": "testpass", "new_password": "newpassword123"},
            )
            assert response.status_code == 200
            with pytest.raises(WebSocketDisconnect) as exc_info:
                websocket.receive_text()
        assert exc_info.value.code == 4401

    def test_admin_password_reset_closes_existing_connection(
        self,
        client: TestClient,
        test_admin: dict,
        admin_session: str,
        temp_db: str,
    ):
        user = asyncio.run(create_user_v0(username="ws-password-reset"))
        session_id = asyncio.run(create_session_v0(user["id"], "ws-reset-session"))
        with client.websocket_connect(
            "/ws/tasks",
            cookies={settings.session_cookie_name: session_id},
            headers={"origin": TRUSTED_ORIGIN},
        ) as websocket:
            client.cookies.set(settings.session_cookie_name, admin_session)
            response = client.put(
                f"/api/users/{user['id']}", json={"password": "newpassword123"}
            )
            assert response.status_code == 200
            with pytest.raises(WebSocketDisconnect) as exc_info:
                websocket.receive_text()
        assert exc_info.value.code == 4401

    def test_expired_session_is_closed_after_revalidation(
        self, client: TestClient, user_session: str, test_user: dict
    ):
        with patch("app.routers.ws.SESSION_REVALIDATION_INTERVAL_SECONDS", 0.01):
            with client.websocket_connect(
                "/ws/tasks",
                cookies={settings.session_cookie_name: user_session},
                headers={"origin": TRUSTED_ORIGIN},
            ) as websocket:
                asyncio.run(_expire_session(user_session))
                with pytest.raises(WebSocketDisconnect) as exc_info:
                    websocket.receive_text()
        assert exc_info.value.code == 4401


@pytest.mark.asyncio
async def test_socket_is_not_broadcast_visible_before_accept_finishes(
    user_session: str,
    test_user: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inbound: asyncio.Queue[dict] = asyncio.Queue()
    await inbound.put({"type": "websocket.connect"})
    accept_started = asyncio.Event()
    release_accept = asyncio.Event()
    activated = asyncio.Event()
    sent_messages: list[dict] = []

    async def receive() -> dict:
        return await inbound.get()

    async def send(message: dict) -> None:
        sent_messages.append(message)
        if message["type"] == "websocket.accept":
            accept_started.set()
            await release_accept.wait()

    class ObservedReservation:
        def __init__(self, inner) -> None:
            self.inner = inner

        async def activate(self, websocket: WebSocket) -> bool:
            result = await self.inner.activate(websocket)
            activated.set()
            return result

        async def release(self) -> None:
            await self.inner.release()

    async def observed_register(
        user_id: int,
        session_id: str,
        client_ip: str,
    ):
        reservation = await register_ws(user_id, session_id, client_ip)
        return ObservedReservation(reservation) if reservation is not None else None

    monkeypatch.setattr("app.routers.ws.register_ws", observed_register)
    monkeypatch.setattr(
        "app.services.task_broadcast.MAX_WS_CONNECTIONS_PER_USER", 1
    )
    scope = {
        "type": "websocket",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "scheme": "ws",
        "server": ("testserver", 80),
        "client": ("10.0.0.1", 50000),
        "root_path": "",
        "path": "/ws/tasks",
        "raw_path": b"/ws/tasks",
        "query_string": b"",
        "headers": [
            (b"host", b"testserver"),
            (b"origin", TRUSTED_ORIGIN.encode()),
            (
                b"cookie",
                f"{settings.session_cookie_name}={user_session}".encode(),
            ),
        ],
        "subprotocols": [],
        "state": {},
    }
    websocket = WebSocket(scope, receive, send)
    handler = asyncio.create_task(task_ws(websocket))

    try:
        await asyncio.wait_for(accept_started.wait(), 1)
        rejected_messages: list[dict] = []

        async def send_rejected(message: dict) -> None:
            rejected_messages.append(message)

        rejected = WebSocket(scope, receive, send_rejected)
        await task_ws(rejected)
        assert [message["type"] for message in rejected_messages] == [
            "websocket.close"
        ]
        assert rejected_messages[0]["code"] == 4429

        await broadcast_notification(test_user["id"], "before accept")
        assert not activated.is_set()
        assert [message["type"] for message in sent_messages] == [
            "websocket.accept"
        ]

        release_accept.set()
        await asyncio.wait_for(activated.wait(), 1)
        await broadcast_notification(test_user["id"], "after accept")
        assert [message["type"] for message in sent_messages] == [
            "websocket.accept",
            "websocket.send",
        ]
    finally:
        release_accept.set()
        await inbound.put({"type": "websocket.disconnect", "code": 1000})
        result = await asyncio.gather(handler, return_exceptions=True)

    assert result == [None]
    reusable = await reserve_ws_slot(
        test_user["id"], user_session, "10.0.0.1"
    )
    assert reusable is not None
    await release_ws_slot(reusable)


@pytest.mark.asyncio
async def test_accept_failure_releases_reserved_slot(
    user_session: str,
    test_user: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.services.task_broadcast.MAX_WS_CONNECTIONS_PER_USER", 1
    )
    websocket = MagicMock()
    websocket.client = SimpleNamespace(host="10.0.0.1")
    websocket.headers = {"origin": TRUSTED_ORIGIN}
    websocket.cookies = {settings.session_cookie_name: user_session}
    websocket.url = SimpleNamespace(
        scheme="ws",
        hostname="testserver",
        port=None,
    )
    websocket.accept = AsyncMock(side_effect=RuntimeError("accept failed"))
    websocket.close = AsyncMock()

    with pytest.raises(RuntimeError, match="accept failed"):
        await task_ws(websocket)

    reusable = await reserve_ws_slot(
        test_user["id"], user_session, "10.0.0.1"
    )
    assert reusable is not None
    await release_ws_slot(reusable)
    websocket.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_ip_connection_limit_is_atomic() -> None:
    await clear_connections()
    try:
        with patch("app.services.task_broadcast.MAX_WS_CONNECTIONS_PER_IP", 1):
            reservations = await asyncio.gather(
                reserve_ws_slot(1, "session-1", "10.0.0.1"),
                reserve_ws_slot(2, "session-2", "10.0.0.1"),
            )
            assert sum(item is not None for item in reservations) == 1
            for reservation in reservations:
                if reservation is not None:
                    await release_ws_slot(reservation)
    finally:
        await clear_connections()


@pytest.mark.asyncio
async def test_revoked_connection_is_not_broadcast_to() -> None:
    websocket = AsyncMock()
    await clear_connections()
    try:
        with patch("app.services.task_broadcast.MAX_WS_CONNECTIONS_PER_USER", 2):
            assert await _activate_test_socket(
                1, "revoked-session", "10.0.0.1", websocket
            )
            pending = await reserve_ws_slot(1, "revoked-session", "10.0.0.2")
            assert pending is not None
            await remove_connections_for_session("revoked-session")
            await broadcast_notification(1, "hidden")
            first_reuse = await reserve_ws_slot(1, "new-session-1", "10.0.0.1")
            second_reuse = await reserve_ws_slot(1, "new-session-2", "10.0.0.2")
            assert first_reuse is not None
            assert second_reuse is not None
    finally:
        await clear_connections()

    websocket.close.assert_awaited_once_with(code=4401)
    websocket.send_json.assert_not_awaited()


@pytest.mark.asyncio
async def test_broadcast_failure_unregisters_connection() -> None:
    healthy = AsyncMock()
    failed = AsyncMock()
    failed.send_json.side_effect = RuntimeError("disconnected")
    await clear_connections()
    try:
        assert await _activate_test_socket(
            1, "healthy-session", "10.0.0.1", healthy
        )
        assert await _activate_test_socket(
            1, "failed-session", "10.0.0.2", failed
        )
        await broadcast_notification(1, "first")
        await broadcast_notification(1, "second")
    finally:
        await clear_connections()

    assert healthy.send_json.await_count == 2
    assert failed.send_json.await_count == 1
    failed.close.assert_awaited_once_with(code=1011)


def test_login_session_replacement_closes_old_connection(
    client: TestClient,
    user_session: str,
    test_user: dict,
) -> None:
    client.cookies.set(settings.session_cookie_name, user_session)
    with client.websocket_connect(
        "/ws/tasks",
        cookies={settings.session_cookie_name: user_session},
        headers={"origin": TRUSTED_ORIGIN},
    ) as websocket:
        response = client.post(
            "/api/auth/login",
            json={"username": "testuser", "password": "testpass"},
        )
        assert response.status_code == 200
        assert response.cookies.get(settings.session_cookie_name) != user_session
        with pytest.raises(WebSocketDisconnect) as exc_info:
            websocket.receive_text()
    assert exc_info.value.code == 4401


def test_admin_delete_user_closes_existing_connection(
    client: TestClient,
    test_admin: dict,
    admin_session: str,
    temp_db: str,
) -> None:
    user = asyncio.run(create_user_v0(username="ws-delete-user"))
    session_id = asyncio.run(create_session_v0(user["id"], "ws-delete-session"))
    with client.websocket_connect(
        "/ws/tasks",
        cookies={settings.session_cookie_name: session_id},
        headers={"origin": TRUSTED_ORIGIN},
    ) as websocket:
        client.cookies.set(settings.session_cookie_name, admin_session)
        response = client.delete(f"/api/users/{user['id']}")
        assert response.status_code == 200
        with pytest.raises(WebSocketDisconnect) as exc_info:
            websocket.receive_text()
    assert exc_info.value.code == 4401
