"""Tests for WebSocket router."""

import queue
import threading
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.core.config import settings


class TestTaskWebSocket:
    def test_websocket_connect_valid_session(
        self, client: TestClient, user_session: str, test_user: dict
    ):
        with client.websocket_connect(
            "/ws/tasks", cookies={settings.session_cookie_name: user_session}
        ) as websocket:
            websocket.send_text("ping")
            response = websocket.receive_text()
            assert response == "pong"

    def test_websocket_reject_invalid_session(
        self, client: TestClient, temp_db: str, test_user: dict
    ):
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect(
                "/ws/tasks", cookies={settings.session_cookie_name: "invalid_session"}
            ) as websocket:
                websocket.receive_text()
        assert exc_info.value.code == 4401

    def test_websocket_reject_no_session(
        self, client: TestClient, temp_db: str, test_user: dict
    ):
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect("/ws/tasks") as websocket:
                websocket.receive_text()
        assert exc_info.value.code == 4401

    def test_websocket_ping_pong(
        self, client: TestClient, user_session: str, test_user: dict
    ):
        with client.websocket_connect(
            "/ws/tasks", cookies={settings.session_cookie_name: user_session}
        ) as websocket:
            websocket.send_text("ping")
            response = websocket.receive_text()
            assert response == "pong"

    def test_websocket_connect_trailing_slash(
        self, client: TestClient, user_session: str, test_user: dict
    ):
        with client.websocket_connect(
            "/ws/tasks/", cookies={settings.session_cookie_name: user_session}
        ) as websocket:
            websocket.send_text("ping")
            response = websocket.receive_text()
            assert response == "pong"

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
                "/ws/tasks", cookies={settings.session_cookie_name: user_session}
            ) as websocket:
                received: queue.Queue[str] = queue.Queue()

                def receive_one() -> None:
                    received.put(websocket.receive_text())

                thread = threading.Thread(target=receive_one, daemon=True)
                thread.start()
                thread.join(timeout=0.05)

                assert received.empty()
