"""Tests for WebSocket router."""
import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketState, WebSocketDisconnect

from app.core.config import settings


class TestTaskWebSocket:

    def test_websocket_connect_valid_session(self, client: TestClient, user_session: str, test_user: dict):
        with client.websocket_connect(
            "/ws/tasks",
            cookies={settings.session_cookie_name: user_session}
        ) as websocket:
            websocket.send_text("ping")
            response = websocket.receive_text()
            assert response == "pong"

    def test_websocket_reject_invalid_session(self, client: TestClient, temp_db: str, test_user: dict):
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect(
                "/ws/tasks",
                cookies={settings.session_cookie_name: "invalid_session"}
            ) as websocket:
                websocket.receive_text()
        assert exc_info.value.code == 4401

    def test_websocket_reject_no_session(self, client: TestClient, temp_db: str, test_user: dict):
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect("/ws/tasks") as websocket:
                websocket.receive_text()
        assert exc_info.value.code == 4401

    def test_websocket_ping_pong(self, client: TestClient, user_session: str, test_user: dict):
        with client.websocket_connect(
            "/ws/tasks",
            cookies={settings.session_cookie_name: user_session}
        ) as websocket:
            websocket.send_text("ping")
            response = websocket.receive_text()
            assert response == "pong"
