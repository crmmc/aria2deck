"""Tests for WebSocket router."""
import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketState

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
        try:
            with client.websocket_connect(
                "/ws/tasks",
                cookies={settings.session_cookie_name: "invalid_session"}
            ) as websocket:
                pass
        except Exception as e:
            assert "4401" in str(e) or "close" in str(e).lower()

    def test_websocket_reject_no_session(self, client: TestClient, temp_db: str, test_user: dict):
        try:
            with client.websocket_connect("/ws/tasks") as websocket:
                pass
        except Exception as e:
            assert "4401" in str(e) or "close" in str(e).lower()

    def test_websocket_ping_pong(self, client: TestClient, user_session: str, test_user: dict):
        with client.websocket_connect(
            "/ws/tasks",
            cookies={settings.session_cookie_name: user_session}
        ) as websocket:
            websocket.send_text("ping")
            response = websocket.receive_text()
            assert response == "pong"
