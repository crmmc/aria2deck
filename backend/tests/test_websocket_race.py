"""Test race condition handling in WebSocket broadcast.

Tests for:
1. Broadcast with failed socket
2. Failed socket cleanup
3. Concurrent broadcast operations
"""
import asyncio
import os
import tempfile
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest
from sqlmodel import select

from app.database import get_session, reset_engine, init_db as init_sqlmodel_db, dispose_engine
from app.db import init_db, execute
from app.core.config import settings
from app.core.security import hash_password
from app.core.state import AppState
from app.models import DownloadTask, UserTaskSubscription, utc_now_str


@pytest.fixture(scope="function")
def temp_db_ws():
    """Create a fresh temporary database for WebSocket tests."""
    temp_dir = tempfile.mkdtemp()
    db_path = os.path.join(temp_dir, "test.db")
    download_dir = os.path.join(temp_dir, "downloads")
    os.makedirs(download_dir, exist_ok=True)

    original_db_path = settings.database_path
    original_download_dir = settings.download_dir
    settings.database_path = db_path
    settings.download_dir = download_dir

    reset_engine()
    init_db()
    asyncio.run(init_sqlmodel_db())

    yield {
        "db_path": db_path,
        "download_dir": download_dir,
        "temp_dir": temp_dir,
    }

    asyncio.run(dispose_engine())
    settings.database_path = original_db_path
    settings.download_dir = original_download_dir
    reset_engine()


@pytest.fixture
def test_user_ws(temp_db_ws):
    """Create a test user for WebSocket tests."""
    user_id = execute(
        """
        INSERT INTO users (username, password_hash, is_admin, created_at, quota)
        VALUES (?, ?, ?, ?, ?)
        """,
        ["wsuser", hash_password("testpass"), 0, datetime.now(timezone.utc).isoformat(), 100 * 1024 * 1024 * 1024]
    )
    return {"id": user_id, "username": "wsuser", "quota": 100 * 1024 * 1024 * 1024}


@pytest.fixture
def mock_app_state_ws():
    """Create a mock AppState for WebSocket testing."""
    return AppState()


class MockWebSocket:
    """Mock WebSocket for testing."""

    def __init__(self, should_fail=False, fail_after=None):
        self.should_fail = should_fail
        self.fail_after = fail_after
        self.send_count = 0
        self.messages = []
        self.closed = False

    async def send_json(self, data):
        self.send_count += 1
        if self.should_fail:
            raise ConnectionError("WebSocket connection closed")
        if self.fail_after is not None and self.send_count > self.fail_after:
            raise ConnectionError("WebSocket connection closed after threshold")
        self.messages.append(data)

    async def close(self):
        self.closed = True


class TestBroadcastWithFailedSocket:
    """Test broadcast behavior when socket fails."""

    @pytest.mark.asyncio
    async def test_broadcast_with_failed_socket(self, temp_db_ws, test_user_ws, mock_app_state_ws):
        """Socket fails during broadcast should not crash the broadcast."""
        from app.aria2.sync import broadcast_notification

        user_id = test_user_ws["id"]

        # Create mix of working and failing sockets
        working_socket = MockWebSocket(should_fail=False)
        failing_socket = MockWebSocket(should_fail=True)

        # Register sockets
        mock_app_state_ws.ws_connections[user_id] = {working_socket, failing_socket}

        # Broadcast should not raise exception
        await broadcast_notification(
            mock_app_state_ws,
            user_id,
            "Test notification",
            level="info"
        )

        # Working socket should receive message
        assert len(working_socket.messages) == 1
        assert working_socket.messages[0]["type"] == "notification"
        assert working_socket.messages[0]["message"] == "Test notification"

        # Failing socket should have no messages
        assert len(failing_socket.messages) == 0

    @pytest.mark.asyncio
    async def test_broadcast_cleanup_failed_sockets(self, temp_db_ws, test_user_ws, mock_app_state_ws):
        """Failed sockets should be properly cleaned up after broadcast."""
        from app.aria2.sync import broadcast_notification

        user_id = test_user_ws["id"]

        # Create failing socket
        failing_socket = MockWebSocket(should_fail=True)

        # Register socket
        mock_app_state_ws.ws_connections[user_id] = {failing_socket}

        # Broadcast
        await broadcast_notification(
            mock_app_state_ws,
            user_id,
            "Test notification",
            level="error"
        )

        # Failed socket should be removed from connections
        # Note: The actual cleanup happens in unregister_ws which is called
        assert user_id in mock_app_state_ws.ws_connections
        # Socket should be discarded
        assert failing_socket not in mock_app_state_ws.ws_connections.get(user_id, set())

    @pytest.mark.asyncio
    async def test_broadcast_all_sockets_fail(self, temp_db_ws, test_user_ws, mock_app_state_ws):
        """All sockets failing should not crash broadcast."""
        from app.aria2.sync import broadcast_notification

        user_id = test_user_ws["id"]

        # Create all failing sockets
        failing_sockets = [MockWebSocket(should_fail=True) for _ in range(3)]

        # Register sockets
        mock_app_state_ws.ws_connections[user_id] = set(failing_sockets)

        # Broadcast should not raise exception
        await broadcast_notification(
            mock_app_state_ws,
            user_id,
            "Test notification",
            level="warning"
        )

        # All sockets should be cleaned up
        remaining = mock_app_state_ws.ws_connections.get(user_id, set())
        for sock in failing_sockets:
            assert sock not in remaining


class TestConcurrentBroadcast:
    """Test concurrent broadcast operations."""

    @pytest.mark.asyncio
    async def test_concurrent_broadcast_to_same_user(self, temp_db_ws, test_user_ws, mock_app_state_ws):
        """Concurrent broadcasts to same user should not corrupt state."""
        from app.aria2.sync import broadcast_notification

        user_id = test_user_ws["id"]

        # Create working socket
        working_socket = MockWebSocket(should_fail=False)
        mock_app_state_ws.ws_connections[user_id] = {working_socket}

        # Concurrent broadcasts
        await asyncio.gather(
            broadcast_notification(mock_app_state_ws, user_id, "Message 1", "info"),
            broadcast_notification(mock_app_state_ws, user_id, "Message 2", "info"),
            broadcast_notification(mock_app_state_ws, user_id, "Message 3", "info"),
        )

        # All messages should be received
        assert len(working_socket.messages) == 3

    @pytest.mark.asyncio
    async def test_concurrent_broadcast_with_socket_registration(self, temp_db_ws, test_user_ws, mock_app_state_ws):
        """Concurrent broadcast and socket registration should not deadlock."""
        from app.aria2.sync import broadcast_notification, register_ws, unregister_ws

        user_id = test_user_ws["id"]

        # Initial socket
        initial_socket = MockWebSocket(should_fail=False)
        mock_app_state_ws.ws_connections[user_id] = {initial_socket}

        # New sockets to register
        new_sockets = [MockWebSocket(should_fail=False) for _ in range(3)]

        async def register_and_broadcast(socket, msg):
            await register_ws(mock_app_state_ws, user_id, socket)
            await broadcast_notification(mock_app_state_ws, user_id, msg, "info")

        # Concurrent registration and broadcast
        await asyncio.gather(
            register_and_broadcast(new_sockets[0], "Msg 1"),
            register_and_broadcast(new_sockets[1], "Msg 2"),
            register_and_broadcast(new_sockets[2], "Msg 3"),
        )

        # All sockets should be registered
        registered = mock_app_state_ws.ws_connections.get(user_id, set())
        assert initial_socket in registered
        for sock in new_sockets:
            assert sock in registered


class TestTaskBroadcast:
    """Test task update broadcast."""

    @pytest.mark.asyncio
    async def test_task_broadcast_to_subscribers(self, temp_db_ws, test_user_ws, mock_app_state_ws):
        """Task update should be broadcast to all subscribers."""
        from app.routers.tasks import broadcast_task_update_to_subscribers

        user_id = test_user_ws["id"]

        # Create task and subscription
        async with get_session() as db:
            task = DownloadTask(
                uri_hash="broadcast_test_hash_123",
                uri="https://example.com/broadcast.zip",
                gid="test_gid_broadcast_123",
                status="active",
                name="broadcast.zip",
                total_length=1024,
                completed_length=512,
                created_at=utc_now_str(),
                updated_at=utc_now_str(),
            )
            db.add(task)
            await db.commit()
            await db.refresh(task)
            task_id = task.id

            subscription = UserTaskSubscription(
                owner_id=user_id,
                task_id=task_id,
                frozen_space=1024,
                status="pending",
                created_at=utc_now_str(),
            )
            db.add(subscription)
            await db.commit()

        # Register socket
        working_socket = MockWebSocket(should_fail=False)
        mock_app_state_ws.ws_connections[user_id] = {working_socket}

        # Broadcast task update
        await broadcast_task_update_to_subscribers(mock_app_state_ws, task_id)

        # Socket should receive task update
        assert len(working_socket.messages) == 1
        assert working_socket.messages[0]["type"] == "task_update"
        assert working_socket.messages[0]["task"]["name"] == "broadcast.zip"

    @pytest.mark.asyncio
    async def test_task_broadcast_with_mixed_sockets(self, temp_db_ws, test_user_ws, mock_app_state_ws):
        """Task broadcast with mix of working and failing sockets."""
        from app.routers.tasks import broadcast_task_update_to_subscribers

        user_id = test_user_ws["id"]

        # Create task and subscription
        async with get_session() as db:
            task = DownloadTask(
                uri_hash="mixed_broadcast_hash_456",
                uri="https://example.com/mixed.zip",
                gid="test_gid_mixed_456",
                status="active",
                name="mixed.zip",
                total_length=2048,
                completed_length=1024,
                created_at=utc_now_str(),
                updated_at=utc_now_str(),
            )
            db.add(task)
            await db.commit()
            await db.refresh(task)
            task_id = task.id

            subscription = UserTaskSubscription(
                owner_id=user_id,
                task_id=task_id,
                frozen_space=2048,
                status="pending",
                created_at=utc_now_str(),
            )
            db.add(subscription)
            await db.commit()

        # Register mix of sockets
        working_socket = MockWebSocket(should_fail=False)
        failing_socket = MockWebSocket(should_fail=True)
        mock_app_state_ws.ws_connections[user_id] = {working_socket, failing_socket}

        # Broadcast should not raise exception
        await broadcast_task_update_to_subscribers(mock_app_state_ws, task_id)

        # Working socket should receive message
        assert len(working_socket.messages) == 1

        # Failing socket should be cleaned up
        remaining = mock_app_state_ws.ws_connections.get(user_id, set())
        assert failing_socket not in remaining


class TestWebSocketRegistration:
    """Test WebSocket registration and unregistration."""

    @pytest.mark.asyncio
    async def test_register_ws(self, mock_app_state_ws):
        """Test WebSocket registration."""
        from app.aria2.sync import register_ws

        user_id = 1
        socket = MockWebSocket()

        await register_ws(mock_app_state_ws, user_id, socket)

        assert user_id in mock_app_state_ws.ws_connections
        assert socket in mock_app_state_ws.ws_connections[user_id]

    @pytest.mark.asyncio
    async def test_unregister_ws(self, mock_app_state_ws):
        """Test WebSocket unregistration."""
        from app.aria2.sync import register_ws, unregister_ws

        user_id = 1
        socket = MockWebSocket()

        await register_ws(mock_app_state_ws, user_id, socket)
        await unregister_ws(mock_app_state_ws, user_id, socket)

        assert socket not in mock_app_state_ws.ws_connections.get(user_id, set())

    @pytest.mark.asyncio
    async def test_concurrent_register_unregister(self, mock_app_state_ws):
        """Concurrent register and unregister should not corrupt state."""
        from app.aria2.sync import register_ws, unregister_ws

        user_id = 1
        sockets = [MockWebSocket() for _ in range(10)]

        # Register all
        await asyncio.gather(*[register_ws(mock_app_state_ws, user_id, s) for s in sockets])

        # All should be registered
        registered = mock_app_state_ws.ws_connections.get(user_id, set())
        assert len(registered) == 10

        # Unregister half concurrently
        await asyncio.gather(*[unregister_ws(mock_app_state_ws, user_id, s) for s in sockets[:5]])

        # Half should remain
        remaining = mock_app_state_ws.ws_connections.get(user_id, set())
        assert len(remaining) == 5
        for sock in sockets[5:]:
            assert sock in remaining
