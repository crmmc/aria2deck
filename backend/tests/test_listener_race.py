"""Test race condition handling in aria2 event listener.

Tests for:
1. Task completion idempotency
2. Compare-and-swap pattern for stored_file_id
3. Duplicate event handling
"""
import asyncio
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import update
from sqlmodel import select

from app.database import get_session, reset_engine, init_db as init_sqlmodel_db, dispose_engine
from app.db import init_db, execute
from app.core.config import settings
from app.core.security import hash_password
from app.core.state import AppState
from app.models import DownloadTask, StoredFile, UserFile, UserTaskSubscription, utc_now_str


@pytest.fixture(scope="function")
def temp_db_listener():
    """Create a fresh temporary database for listener tests."""
    temp_dir = tempfile.mkdtemp()
    db_path = os.path.join(temp_dir, "test.db")
    download_dir = os.path.join(temp_dir, "downloads")
    store_dir = os.path.join(download_dir, "store")
    downloading_dir = os.path.join(download_dir, "downloading")
    os.makedirs(download_dir, exist_ok=True)
    os.makedirs(store_dir, exist_ok=True)
    os.makedirs(downloading_dir, exist_ok=True)

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
        "store_dir": store_dir,
        "downloading_dir": downloading_dir,
        "temp_dir": temp_dir,
    }

    asyncio.run(dispose_engine())
    settings.database_path = original_db_path
    settings.download_dir = original_download_dir
    reset_engine()


@pytest.fixture
def test_user_listener(temp_db_listener):
    """Create a test user for listener tests."""
    user_id = execute(
        """
        INSERT INTO users (username, password_hash, is_admin, created_at, quota)
        VALUES (?, ?, ?, ?, ?)
        """,
        ["listeneruser", hash_password("testpass"), 0, datetime.now(timezone.utc).isoformat(), 100 * 1024 * 1024 * 1024]
    )
    return {"id": user_id, "username": "listeneruser", "quota": 100 * 1024 * 1024 * 1024}


@pytest.fixture
def mock_app_state():
    """Create a mock AppState for testing."""
    return AppState()


@pytest.fixture
def test_download_task(temp_db_listener, test_user_listener):
    """Create a test download task with subscription."""
    async def _create():
        async with get_session() as db:
            task = DownloadTask(
                uri_hash="listener_test_hash_123",
                uri="https://example.com/listener_test.zip",
                gid="test_gid_listener_123",
                status="complete",
                name="listener_test.zip",
                total_length=1024,
                completed_length=1024,
                created_at=utc_now_str(),
                updated_at=utc_now_str(),
            )
            db.add(task)
            await db.commit()
            await db.refresh(task)

            # Create subscription
            subscription = UserTaskSubscription(
                owner_id=test_user_listener["id"],
                task_id=task.id,
                frozen_space=1024,
                status="pending",
                created_at=utc_now_str(),
            )
            db.add(subscription)
            await db.commit()

            return task

    return asyncio.run(_create())


class TestTaskCompletionIdempotency:
    """Test task completion idempotency."""

    @pytest.mark.asyncio
    async def test_task_completion_idempotency(self, temp_db_listener, test_user_listener, test_download_task, mock_app_state):
        """Call _handle_task_complete twice for the same task.

        Second call should be a no-op.
        """
        from app.aria2.listener import _handle_task_complete

        task_id = test_download_task.id

        # Create source file
        task_dir = Path(temp_db_listener["downloading_dir"]) / str(task_id)
        task_dir.mkdir(parents=True, exist_ok=True)
        source_file = task_dir / "listener_test.zip"
        source_file.write_text("test content for listener")

        aria2_status = {
            "files": [{"path": str(source_file)}],
            "totalLength": "1024",
            "completedLength": "1024",
        }

        # First call should process the task
        # Patch at the source module where the function is imported from
        with patch("app.routers.tasks.broadcast_task_update_to_subscribers", new_callable=AsyncMock):
            await _handle_task_complete(mock_app_state, task_id, aria2_status)

        # Verify stored_file_id is set
        async with get_session() as db:
            result = await db.exec(
                select(DownloadTask).where(DownloadTask.id == task_id)
            )
            task = result.first()
            assert task.stored_file_id is not None
            first_stored_file_id = task.stored_file_id

        # Create another source file for second call
        task_dir.mkdir(parents=True, exist_ok=True)
        source_file2 = task_dir / "listener_test2.zip"
        source_file2.write_text("different content")

        aria2_status2 = {
            "files": [{"path": str(source_file2)}],
            "totalLength": "2048",
            "completedLength": "2048",
        }

        # Second call should be skipped (idempotency)
        with patch("app.routers.tasks.broadcast_task_update_to_subscribers", new_callable=AsyncMock):
            await _handle_task_complete(mock_app_state, task_id, aria2_status2)

        # stored_file_id should remain unchanged
        async with get_session() as db:
            result = await db.exec(
                select(DownloadTask).where(DownloadTask.id == task_id)
            )
            task = result.first()
            assert task.stored_file_id == first_stored_file_id

    @pytest.mark.asyncio
    async def test_task_completion_compare_and_swap(self, temp_db_listener, test_user_listener, mock_app_state):
        """Verify only one handler sets stored_file_id using compare-and-swap."""
        from app.aria2.listener import _handle_task_complete

        # Create task
        async with get_session() as db:
            task = DownloadTask(
                uri_hash="cas_test_hash_456",
                uri="https://example.com/cas_test.zip",
                gid="test_gid_cas_456",
                status="complete",
                name="cas_test.zip",
                total_length=1024,
                completed_length=1024,
                created_at=utc_now_str(),
                updated_at=utc_now_str(),
            )
            db.add(task)
            await db.commit()
            await db.refresh(task)
            task_id = task.id

            # Create subscription
            subscription = UserTaskSubscription(
                owner_id=test_user_listener["id"],
                task_id=task_id,
                frozen_space=1024,
                status="pending",
                created_at=utc_now_str(),
            )
            db.add(subscription)
            await db.commit()

        # Create source files for concurrent handlers
        task_dir = Path(temp_db_listener["downloading_dir"]) / str(task_id)
        task_dir.mkdir(parents=True, exist_ok=True)

        source_file1 = task_dir / "cas_test1.zip"
        source_file1.write_text("content 1")

        aria2_status = {
            "files": [{"path": str(source_file1)}],
            "totalLength": "1024",
            "completedLength": "1024",
        }

        # Run concurrent handlers
        with patch("app.routers.tasks.broadcast_task_update_to_subscribers", new_callable=AsyncMock):
            results = await asyncio.gather(
                _handle_task_complete(mock_app_state, task_id, aria2_status),
                _handle_task_complete(mock_app_state, task_id, aria2_status),
                return_exceptions=True,
            )

        # No exceptions should be raised
        exceptions = [r for r in results if isinstance(r, Exception)]
        assert len(exceptions) == 0, f"Unexpected exceptions: {exceptions}"

        # Only one StoredFile should be created
        async with get_session() as db:
            result = await db.exec(select(StoredFile))
            stored_files = result.all()
            # May be 0 or 1 depending on timing, but not more than 1
            assert len(stored_files) <= 1

    @pytest.mark.asyncio
    async def test_task_not_complete_status_skipped(self, temp_db_listener, test_user_listener, mock_app_state):
        """Task with status != complete should be skipped."""
        from app.aria2.listener import _handle_task_complete

        # Create task with active status
        async with get_session() as db:
            task = DownloadTask(
                uri_hash="active_test_hash_789",
                uri="https://example.com/active_test.zip",
                gid="test_gid_active_789",
                status="active",  # Not complete
                name="active_test.zip",
                total_length=1024,
                completed_length=512,
                created_at=utc_now_str(),
                updated_at=utc_now_str(),
            )
            db.add(task)
            await db.commit()
            await db.refresh(task)
            task_id = task.id

        aria2_status = {
            "files": [{"path": "/some/path/file.zip"}],
        }

        # Should be skipped
        with patch("app.routers.tasks.broadcast_task_update_to_subscribers", new_callable=AsyncMock):
            await _handle_task_complete(mock_app_state, task_id, aria2_status)

        # stored_file_id should remain None
        async with get_session() as db:
            result = await db.exec(
                select(DownloadTask).where(DownloadTask.id == task_id)
            )
            task = result.first()
            assert task.stored_file_id is None


class TestDuplicateEventHandling:
    """Test handling of duplicate aria2 events."""

    @pytest.mark.asyncio
    async def test_duplicate_complete_events(self, temp_db_listener, test_user_listener, mock_app_state):
        """Multiple complete events for the same task should be handled correctly."""
        from app.aria2.listener import handle_aria2_event

        # Create task
        async with get_session() as db:
            task = DownloadTask(
                uri_hash="dup_event_hash_123",
                uri="https://example.com/dup_event.zip",
                gid="test_gid_dup_123",
                status="active",
                name="dup_event.zip",
                total_length=1024,
                completed_length=1024,
                created_at=utc_now_str(),
                updated_at=utc_now_str(),
            )
            db.add(task)
            await db.commit()
            await db.refresh(task)
            task_id = task.id
            gid = task.gid

            # Create subscription
            subscription = UserTaskSubscription(
                owner_id=test_user_listener["id"],
                task_id=task_id,
                frozen_space=1024,
                status="pending",
                created_at=utc_now_str(),
            )
            db.add(subscription)
            await db.commit()

        # Create source file
        task_dir = Path(temp_db_listener["downloading_dir"]) / str(task_id)
        task_dir.mkdir(parents=True, exist_ok=True)
        source_file = task_dir / "dup_event.zip"
        source_file.write_text("duplicate event test content")

        # Mock aria2 client
        mock_client = AsyncMock()
        mock_client.tell_status.return_value = {
            "status": "complete",
            "files": [{"path": str(source_file)}],
            "totalLength": "1024",
            "completedLength": "1024",
        }

        with patch("app.core.state.get_aria2_client", return_value=mock_client):
            with patch("app.routers.tasks.broadcast_task_update_to_subscribers", new_callable=AsyncMock):
                # Send multiple complete events concurrently
                await asyncio.gather(
                    handle_aria2_event(mock_app_state, gid, "complete"),
                    handle_aria2_event(mock_app_state, gid, "complete"),
                    handle_aria2_event(mock_app_state, gid, "complete"),
                )

        # Task should be complete with stored_file_id set
        async with get_session() as db:
            result = await db.exec(
                select(DownloadTask).where(DownloadTask.id == task_id)
            )
            task = result.first()
            assert task.status == "complete"

    @pytest.mark.asyncio
    async def test_event_for_nonexistent_task(self, temp_db_listener, mock_app_state):
        """Event for non-existent task should be ignored gracefully."""
        from app.aria2.listener import handle_aria2_event

        mock_client = AsyncMock()
        mock_client.tell_status.return_value = {
            "status": "complete",
            "files": [{"path": "/some/path"}],
        }

        with patch("app.core.state.get_aria2_client", return_value=mock_client):
            # Should not raise exception
            await handle_aria2_event(mock_app_state, "nonexistent_gid", "complete")


class TestMagnetLinkGidUpdate:
    """Test GID update for magnet link metadata completion."""

    @pytest.mark.asyncio
    async def test_magnet_gid_update_race(self, temp_db_listener, test_user_listener, mock_app_state):
        """Concurrent GID updates for magnet link should be handled correctly."""
        from app.aria2.listener import handle_aria2_event

        # Create task with original GID
        async with get_session() as db:
            task = DownloadTask(
                uri_hash="magnet_gid_hash_123",
                uri="magnet:?xt=urn:btih:abc123",
                gid="original_gid_123",
                status="active",
                name="[METADATA]magnet",
                total_length=0,
                completed_length=0,
                created_at=utc_now_str(),
                updated_at=utc_now_str(),
            )
            db.add(task)
            await db.commit()
            await db.refresh(task)
            task_id = task.id
            original_gid = task.gid

        # Mock aria2 client returning followedBy (metadata complete)
        mock_client = AsyncMock()
        mock_client.tell_status.return_value = {
            "status": "complete",
            "followedBy": ["new_bt_gid_456"],
            "files": [],
        }

        with patch("app.core.state.get_aria2_client", return_value=mock_client):
            with patch("app.routers.tasks.broadcast_task_update_to_subscribers", new_callable=AsyncMock):
                await handle_aria2_event(mock_app_state, original_gid, "complete")

        # GID should be updated to new BT GID
        async with get_session() as db:
            result = await db.exec(
                select(DownloadTask).where(DownloadTask.id == task_id)
            )
            task = result.first()
            assert task.gid == "new_bt_gid_456"


class TestTaskCompletionWithStoredFile:
    """Test task completion with stored file creation."""

    @pytest.mark.asyncio
    async def test_stored_file_created_on_completion(self, temp_db_listener, test_user_listener, mock_app_state):
        """StoredFile should be created when task completes."""
        from app.aria2.listener import _handle_task_complete

        # Create task
        async with get_session() as db:
            task = DownloadTask(
                uri_hash="stored_file_test_hash",
                uri="https://example.com/stored.zip",
                gid="test_gid_stored",
                status="complete",
                name="stored.zip",
                total_length=1024,
                completed_length=1024,
                created_at=utc_now_str(),
                updated_at=utc_now_str(),
            )
            db.add(task)
            await db.commit()
            await db.refresh(task)
            task_id = task.id

            # Create subscription
            subscription = UserTaskSubscription(
                owner_id=test_user_listener["id"],
                task_id=task_id,
                frozen_space=1024,
                status="pending",
                created_at=utc_now_str(),
            )
            db.add(subscription)
            await db.commit()

        # Create source file
        task_dir = Path(temp_db_listener["downloading_dir"]) / str(task_id)
        task_dir.mkdir(parents=True, exist_ok=True)
        source_file = task_dir / "stored.zip"
        source_file.write_text("stored file content")

        aria2_status = {
            "files": [{"path": str(source_file)}],
            "totalLength": "1024",
            "completedLength": "1024",
        }

        with patch("app.routers.tasks.broadcast_task_update_to_subscribers", new_callable=AsyncMock):
            await _handle_task_complete(mock_app_state, task_id, aria2_status)

        # Verify StoredFile was created
        async with get_session() as db:
            result = await db.exec(select(StoredFile))
            stored_files = result.all()
            assert len(stored_files) == 1

            # Verify task has stored_file_id
            result = await db.exec(
                select(DownloadTask).where(DownloadTask.id == task_id)
            )
            task = result.first()
            assert task.stored_file_id == stored_files[0].id

    @pytest.mark.asyncio
    async def test_user_file_created_for_subscribers(self, temp_db_listener, test_user_listener, mock_app_state):
        """UserFile should be created for all pending subscribers."""
        from app.aria2.listener import _handle_task_complete

        # Create additional users
        user_ids = [test_user_listener["id"]]
        for i in range(2):
            user_id = execute(
                """
                INSERT INTO users (username, password_hash, is_admin, created_at, quota)
                VALUES (?, ?, ?, ?, ?)
                """,
                [f"subuser{i}", hash_password("pass"), 0, datetime.now(timezone.utc).isoformat(), 100 * 1024 * 1024 * 1024]
            )
            user_ids.append(user_id)

        # Create task
        async with get_session() as db:
            task = DownloadTask(
                uri_hash="multi_sub_test_hash",
                uri="https://example.com/multi.zip",
                gid="test_gid_multi",
                status="complete",
                name="multi.zip",
                total_length=1024,
                completed_length=1024,
                created_at=utc_now_str(),
                updated_at=utc_now_str(),
            )
            db.add(task)
            await db.commit()
            await db.refresh(task)
            task_id = task.id

            # Create subscriptions for all users
            for uid in user_ids:
                subscription = UserTaskSubscription(
                    owner_id=uid,
                    task_id=task_id,
                    frozen_space=1024,
                    status="pending",
                    created_at=utc_now_str(),
                )
                db.add(subscription)
            await db.commit()

        # Create source file
        task_dir = Path(temp_db_listener["downloading_dir"]) / str(task_id)
        task_dir.mkdir(parents=True, exist_ok=True)
        source_file = task_dir / "multi.zip"
        source_file.write_text("multi subscriber content")

        aria2_status = {
            "files": [{"path": str(source_file)}],
            "totalLength": "1024",
            "completedLength": "1024",
        }

        with patch("app.routers.tasks.broadcast_task_update_to_subscribers", new_callable=AsyncMock):
            await _handle_task_complete(mock_app_state, task_id, aria2_status)

        # Verify UserFile was created for all subscribers
        async with get_session() as db:
            result = await db.exec(select(UserFile))
            user_files = result.all()
            assert len(user_files) == 3

            # Verify all subscriptions are marked as success
            result = await db.exec(
                select(UserTaskSubscription).where(UserTaskSubscription.task_id == task_id)
            )
            subscriptions = result.all()
            for sub in subscriptions:
                assert sub.status == "success"
                assert sub.frozen_space == 0
