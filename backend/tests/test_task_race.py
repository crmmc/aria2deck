"""Test race condition handling in task creation.

Tests for:
1. Concurrent task creation with same uri_hash
2. IntegrityError recovery
"""
import asyncio
import os
import tempfile
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlmodel import select
from starlette.requests import Request

from app.database import get_session, reset_engine, init_db as init_sqlmodel_db, dispose_engine
from app.db import init_db, execute
from app.core.config import settings
from app.core.security import hash_password
from app.core.state import AppState
from app.models import DownloadTask, UserTaskSubscription, utc_now_str


@pytest.fixture(scope="function")
def temp_db_task():
    """Create a fresh temporary database for task tests."""
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
def test_user_task(temp_db_task):
    """Create a test user for task tests."""
    user_id = execute(
        """
        INSERT INTO users (username, password_hash, is_admin, created_at, quota)
        VALUES (?, ?, ?, ?, ?)
        """,
        ["taskuser", hash_password("testpass"), 0, datetime.now(timezone.utc).isoformat(), 100 * 1024 * 1024 * 1024]
    )
    return {"id": user_id, "username": "taskuser", "quota": 100 * 1024 * 1024 * 1024}


class TestConcurrentTaskCreation:
    """Test concurrent task creation scenarios."""

    @pytest.mark.asyncio
    async def test_concurrent_task_creation_same_uri_hash(self, temp_db_task, test_user_task):
        """Two concurrent requests creating the same task (same uri_hash).

        Only one task should be created, both should return the same task.
        """
        from app.routers.tasks import _find_or_create_task

        uri_hash = "test_hash_concurrent_123"
        uri = "https://example.com/file.zip"

        # Run concurrent task creation
        results = await asyncio.gather(
            _find_or_create_task(uri_hash, uri, "file.zip", 1024),
            _find_or_create_task(uri_hash, uri, "file.zip", 1024),
            _find_or_create_task(uri_hash, uri, "file.zip", 1024),
        )

        # All should return the same task
        tasks = [r[0] for r in results]
        is_new_flags = [r[1] for r in results]

        # All tasks should have the same ID
        task_ids = set(t.id for t in tasks)
        assert len(task_ids) == 1, f"Expected 1 unique task ID, got {len(task_ids)}"

        # Only one should be marked as new
        new_count = sum(1 for is_new in is_new_flags if is_new)
        assert new_count == 1, f"Expected exactly 1 new task, got {new_count}"

        # Verify only one task exists in database
        async with get_session() as db:
            result = await db.exec(
                select(DownloadTask).where(DownloadTask.uri_hash == uri_hash)
            )
            db_tasks = result.all()
            assert len(db_tasks) == 1, f"Expected 1 task in DB, got {len(db_tasks)}"

    @pytest.mark.asyncio
    async def test_task_creation_integrity_error_recovery(self, temp_db_task, test_user_task):
        """Verify IntegrityError is caught and existing task is returned."""
        from app.routers.tasks import _find_or_create_task

        uri_hash = "test_hash_integrity_456"
        uri = "https://example.com/another.zip"

        # Create task first
        task1, is_new1 = await _find_or_create_task(uri_hash, uri, "another.zip", 2048)
        assert is_new1 is True
        assert task1 is not None

        # Second call should find existing
        task2, is_new2 = await _find_or_create_task(uri_hash, uri, "another.zip", 2048)
        assert is_new2 is False
        assert task2 is not None
        assert task2.id == task1.id

    @pytest.mark.asyncio
    async def test_different_uri_hash_creates_different_tasks(self, temp_db_task, test_user_task):
        """Different uri_hash should create different tasks."""
        from app.routers.tasks import _find_or_create_task

        results = await asyncio.gather(
            _find_or_create_task("hash_a", "https://a.com/file.zip", "a.zip", 1024),
            _find_or_create_task("hash_b", "https://b.com/file.zip", "b.zip", 2048),
            _find_or_create_task("hash_c", "https://c.com/file.zip", "c.zip", 3072),
        )

        tasks = [r[0] for r in results]
        is_new_flags = [r[1] for r in results]

        # All should be new
        assert all(is_new_flags), "All tasks should be new"

        # All should have different IDs
        task_ids = set(t.id for t in tasks)
        assert len(task_ids) == 3, f"Expected 3 unique task IDs, got {len(task_ids)}"


class TestTaskCreationWithSubscription:
    """Test task creation with subscription handling."""

    @pytest.mark.asyncio
    async def test_concurrent_subscription_to_same_task(self, temp_db_task, test_user_task):
        """Multiple users subscribing to the same task concurrently."""
        from app.routers.tasks import _find_or_create_task, _create_subscription
        from app.models import User

        # Create multiple users
        user_ids = [test_user_task["id"]]
        for i in range(4):
            user_id = execute(
                """
                INSERT INTO users (username, password_hash, is_admin, created_at, quota)
                VALUES (?, ?, ?, ?, ?)
                """,
                [f"subuser{i}", hash_password("pass"), 0, datetime.now(timezone.utc).isoformat(), 100 * 1024 * 1024 * 1024]
            )
            user_ids.append(user_id)

        # Create task
        uri_hash = "test_hash_subscription_789"
        task, _ = await _find_or_create_task(uri_hash, "https://example.com/sub.zip", "sub.zip", 1024)

        # Create subscriptions concurrently
        async def create_sub(user_id):
            async with get_session() as db:
                result = await db.exec(select(User).where(User.id == user_id))
                user = result.first()
                if user:
                    return await _create_subscription(user, task, frozen_space=1024)
            return None

        results = await asyncio.gather(*[create_sub(uid) for uid in user_ids])

        # All should succeed
        successful = [r for r in results if r is not None]
        assert len(successful) == 5, f"Expected 5 subscriptions, got {len(successful)}"

        # Verify subscriptions in database
        async with get_session() as db:
            result = await db.exec(
                select(UserTaskSubscription).where(UserTaskSubscription.task_id == task.id)
            )
            subscriptions = result.all()
            assert len(subscriptions) == 5


class TestTaskStatusTransitions:
    """Test task status transitions under race conditions."""

    @pytest.mark.asyncio
    async def test_task_status_update_race(self, temp_db_task, test_user_task):
        """Concurrent status updates should not corrupt task state."""
        from app.routers.tasks import _find_or_create_task

        uri_hash = "test_hash_status_race"
        task, _ = await _find_or_create_task(uri_hash, "https://example.com/status.zip", "status.zip", 1024)

        # Simulate concurrent status updates
        async def update_status(new_status):
            async with get_session() as db:
                result = await db.exec(
                    select(DownloadTask).where(DownloadTask.id == task.id)
                )
                db_task = result.first()
                if db_task:
                    db_task.status = new_status
                    db_task.updated_at = utc_now_str()
                    db.add(db_task)

        # Run concurrent updates
        await asyncio.gather(
            update_status("active"),
            update_status("active"),
            update_status("active"),
        )

        # Task should be in a valid state
        async with get_session() as db:
            result = await db.exec(
                select(DownloadTask).where(DownloadTask.id == task.id)
            )
            db_task = result.first()
            assert db_task.status == "active"


class TestConcurrentSubscriptionCreation:
    """Test concurrent subscription creation scenarios."""

    @pytest.mark.asyncio
    async def test_concurrent_subscription_creation_returns_existing(self, temp_db_task, test_user_task):
        """Concurrent creation returns same subscription.

        When two requests try to create the same subscription simultaneously,
        one succeeds and the other catches IntegrityError and returns existing.
        """
        from app.routers.tasks import _find_or_create_task, _create_subscription
        from app.models import User

        # Create task
        uri_hash = "test_hash_concurrent_sub_123"
        task, _ = await _find_or_create_task(uri_hash, "https://example.com/concurrent.zip", "concurrent.zip", 1024)

        # Get user
        async with get_session() as db:
            result = await db.exec(select(User).where(User.id == test_user_task["id"]))
            user = result.first()

        # Create subscriptions concurrently for the SAME user
        results = await asyncio.gather(
            _create_subscription(user, task, frozen_space=1024),
            _create_subscription(user, task, frozen_space=1024),
            _create_subscription(user, task, frozen_space=1024),
            return_exceptions=True,
        )

        # Filter out exceptions
        successful = [r for r in results if r is not None and not isinstance(r, Exception)]

        # All should return a subscription (either new or existing)
        assert len(successful) == 3, f"Expected 3 subscriptions returned, got {len(successful)}"

        # All should have the same ID (same subscription)
        sub_ids = set(s.id for s in successful)
        assert len(sub_ids) == 1, f"Expected 1 unique subscription ID, got {len(sub_ids)}"

        # Verify only one subscription exists in database
        async with get_session() as db:
            result = await db.exec(
                select(UserTaskSubscription).where(
                    UserTaskSubscription.owner_id == test_user_task["id"],
                    UserTaskSubscription.task_id == task.id,
                )
            )
            subscriptions = result.all()
            assert len(subscriptions) == 1, f"Expected 1 subscription in DB, got {len(subscriptions)}"

    @pytest.mark.asyncio
    async def test_subscription_integrity_error_recovery(self, temp_db_task, test_user_task):
        """Verify IntegrityError is caught and existing subscription is returned."""
        from app.routers.tasks import _find_or_create_task, _create_subscription
        from app.models import User

        # Create task
        uri_hash = "test_hash_integrity_sub_456"
        task, _ = await _find_or_create_task(uri_hash, "https://example.com/integrity.zip", "integrity.zip", 2048)

        # Get user
        async with get_session() as db:
            result = await db.exec(select(User).where(User.id == test_user_task["id"]))
            user = result.first()

        # Create subscription first
        sub1 = await _create_subscription(user, task, frozen_space=2048)
        assert sub1 is not None

        # Second call should return existing (via IntegrityError recovery)
        sub2 = await _create_subscription(user, task, frozen_space=2048)
        assert sub2 is not None
        assert sub2.id == sub1.id

    @pytest.mark.asyncio
    async def test_different_users_create_different_subscriptions(self, temp_db_task, test_user_task):
        """Different users should create different subscriptions."""
        from app.routers.tasks import _find_or_create_task, _create_subscription
        from app.models import User

        # Create additional users
        user_ids = [test_user_task["id"]]
        for i in range(2):
            user_id = execute(
                """
                INSERT INTO users (username, password_hash, is_admin, created_at, quota)
                VALUES (?, ?, ?, ?, ?)
                """,
                [f"diffuser{i}", hash_password("pass"), 0, datetime.now(timezone.utc).isoformat(), 100 * 1024 * 1024 * 1024]
            )
            user_ids.append(user_id)

        # Create task
        uri_hash = "test_hash_diff_users_789"
        task, _ = await _find_or_create_task(uri_hash, "https://example.com/diff.zip", "diff.zip", 1024)

        # Create subscriptions for different users concurrently
        async def create_sub(user_id):
            async with get_session() as db:
                result = await db.exec(select(User).where(User.id == user_id))
                user = result.first()
                if user:
                    return await _create_subscription(user, task, frozen_space=1024)
            return None

        results = await asyncio.gather(*[create_sub(uid) for uid in user_ids])

        # All should succeed (different users)
        successful = [r for r in results if r is not None]
        assert len(successful) == 3, f"Expected 3 subscriptions, got {len(successful)}"

        # All should have different IDs
        sub_ids = set(s.id for s in successful)
        assert len(sub_ids) == 3, f"Expected 3 unique subscription IDs, got {len(sub_ids)}"

        # Verify subscriptions in database
        async with get_session() as db:
            result = await db.exec(
                select(UserTaskSubscription).where(UserTaskSubscription.task_id == task.id)
            )
            subscriptions = result.all()
            assert len(subscriptions) == 3


class TestTorrentTaskSpaceCheck:
    """Test space checks for torrent task creation."""

    @pytest.mark.asyncio
    async def test_create_torrent_task_rechecks_space_under_lock(self, temp_db_task, test_user_task):
        """Space should be rechecked inside user lock for known-size torrents."""
        from app.main import app
        from app.models import User
        from app.routers.tasks import TorrentCreate, create_torrent_task

        app.state.app_state = AppState()

        # Existing task with known size
        async with get_session() as db:
            task = DownloadTask(
                uri_hash="torrent_hash_known_1",
                uri="magnet:?xt=urn:btih:torrent_hash_known_1",
                gid="gid_torrent_known_1",
                status="active",
                name="known.torrent",
                total_length=5 * 1024 * 1024,
                completed_length=0,
                created_at=utc_now_str(),
                updated_at=utc_now_str(),
            )
            db.add(task)
            await db.commit()
            await db.refresh(task)

        async with get_session() as db:
            result = await db.exec(select(User).where(User.id == test_user_task["id"]))
            user = result.first()

        space_calls = 0

        async def fake_space_info(user_id: int, quota: int):
            nonlocal space_calls
            space_calls += 1
            if space_calls == 1:
                return {
                    "available": 10 * 1024 * 1024,
                    "used": 0,
                    "frozen": 0,
                    "quota": quota,
                }
            return {
                "available": 0,
                "used": 0,
                "frozen": 0,
                "quota": quota,
            }

        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/tasks/torrent",
                "headers": [],
                "client": ("test", 1234),
                "app": app,
            }
        )

        payload = TorrentCreate(torrent="dummy_base64_payload")

        with patch("app.routers.tasks.api_limiter") as mock_limiter, \
             patch("app.routers.tasks.extract_info_hash_from_torrent_base64", return_value=task.uri_hash), \
             patch("app.routers.tasks.get_user_space_info", new_callable=AsyncMock, side_effect=fake_space_info), \
             patch("app.routers.tasks._check_disk_space", return_value=(True, 10**12)):
            mock_limiter.is_allowed = AsyncMock(return_value=True)

            with pytest.raises(HTTPException) as exc_info:
                await create_torrent_task(payload, request, user=user)

            assert exc_info.value.status_code == 403
            assert "超过可用空间" in exc_info.value.detail

        async with get_session() as db:
            result = await db.exec(
                select(UserTaskSubscription).where(
                    UserTaskSubscription.owner_id == user.id,
                    UserTaskSubscription.task_id == task.id,
                )
            )
            assert result.first() is None

    @pytest.mark.asyncio
    async def test_create_torrent_task_freezes_space_when_available(self, temp_db_task, test_user_task):
        """Known-size torrent should freeze space when user has enough quota."""
        from app.main import app
        from app.models import User
        from app.routers.tasks import TorrentCreate, create_torrent_task

        app.state.app_state = AppState()

        async with get_session() as db:
            task = DownloadTask(
                uri_hash="torrent_hash_known_2",
                uri="magnet:?xt=urn:btih:torrent_hash_known_2",
                gid="gid_torrent_known_2",
                status="active",
                name="known2.torrent",
                total_length=6 * 1024 * 1024,
                completed_length=0,
                created_at=utc_now_str(),
                updated_at=utc_now_str(),
            )
            db.add(task)
            await db.commit()
            await db.refresh(task)

        async with get_session() as db:
            result = await db.exec(select(User).where(User.id == test_user_task["id"]))
            user = result.first()

        async def fake_space_info(user_id: int, quota: int):
            return {
                "available": 10 * 1024 * 1024,
                "used": 0,
                "frozen": 0,
                "quota": quota,
            }

        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/tasks/torrent",
                "headers": [],
                "client": ("test", 1234),
                "app": app,
            }
        )

        payload = TorrentCreate(torrent="dummy_base64_payload")

        with patch("app.routers.tasks.api_limiter") as mock_limiter, \
             patch("app.routers.tasks.extract_info_hash_from_torrent_base64", return_value=task.uri_hash), \
             patch("app.routers.tasks.get_user_space_info", new_callable=AsyncMock, side_effect=fake_space_info), \
             patch("app.routers.tasks._check_disk_space", return_value=(True, 10**12)):
            mock_limiter.is_allowed = AsyncMock(return_value=True)

            response = await create_torrent_task(payload, request, user=user)
            assert response["frozen_space"] == task.total_length

        async with get_session() as db:
            result = await db.exec(
                select(UserTaskSubscription).where(
                    UserTaskSubscription.owner_id == user.id,
                    UserTaskSubscription.task_id == task.id,
                )
            )
            subscription = result.first()
            assert subscription is not None
            assert subscription.frozen_space == task.total_length
            assert subscription.status == "pending"
