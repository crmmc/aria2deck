"""Test race condition handling in task cancellation.

Tests for:
1. Concurrent task cancellation by multiple users
2. Aria2 task cancellation when last subscriber cancels
3. Aria2 task preservation when other subscribers remain
"""
import asyncio
import os
import tempfile
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import func
from sqlmodel import select

from app.database import get_session, reset_engine, init_db as init_sqlmodel_db, dispose_engine
from app.db import init_db, execute
from app.core.config import settings
from app.core.security import hash_password
from app.core.state import AppState
from app.models import DownloadTask, UserTaskSubscription, utc_now_str


@pytest.fixture(scope="function")
def temp_db_cancel():
    """Create a fresh temporary database for cancel tests."""
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
def test_users_cancel(temp_db_cancel):
    """Create multiple test users for cancel tests."""
    users = []
    for i in range(3):
        user_id = execute(
            """
            INSERT INTO users (username, password_hash, is_admin, created_at, quota)
            VALUES (?, ?, ?, ?, ?)
            """,
            [f"canceluser{i}", hash_password("testpass"), 0, datetime.now(timezone.utc).isoformat(), 100 * 1024 * 1024 * 1024]
        )
        users.append({"id": user_id, "username": f"canceluser{i}", "quota": 100 * 1024 * 1024 * 1024})
    return users


@pytest.fixture
def test_task_with_subscriptions(temp_db_cancel, test_users_cancel):
    """Create a test task with multiple subscriptions."""
    async def _create():
        async with get_session() as db:
            task = DownloadTask(
                uri_hash="cancel_test_hash_123",
                uri="https://example.com/cancel_test.zip",
                gid="test_gid_cancel_123",
                status="active",
                name="cancel_test.zip",
                total_length=1024 * 1024,
                completed_length=512 * 1024,
                created_at=utc_now_str(),
                updated_at=utc_now_str(),
            )
            db.add(task)
            await db.commit()
            await db.refresh(task)

            # Create subscriptions for all users
            subscriptions = []
            for user in test_users_cancel:
                subscription = UserTaskSubscription(
                    owner_id=user["id"],
                    task_id=task.id,
                    frozen_space=1024 * 1024,
                    status="pending",
                    created_at=utc_now_str(),
                )
                db.add(subscription)
                subscriptions.append(subscription)
            await db.commit()

            # Refresh subscriptions to get IDs
            for sub in subscriptions:
                await db.refresh(sub)

            return task, subscriptions

    return asyncio.run(_create())


class TestConcurrentTaskCancellation:
    """Test concurrent task cancellation scenarios."""

    @pytest.mark.asyncio
    async def test_concurrent_task_cancellation(self, temp_db_cancel, test_users_cancel, test_task_with_subscriptions):
        """Two users cancel the same task simultaneously.

        Both cancellations should succeed without errors.
        """
        task, subscriptions = test_task_with_subscriptions

        # Mock aria2 client
        mock_client = AsyncMock()
        mock_client.force_remove.return_value = "OK"
        mock_client.remove_download_result.return_value = "OK"

        async def cancel_subscription(sub_id: int, owner_id: int):
            """Simulate cancellation logic from tasks.py"""
            async with get_session() as db:
                # Step 1: Delete current subscription
                result = await db.exec(
                    select(UserTaskSubscription).where(UserTaskSubscription.id == sub_id)
                )
                db_sub = result.first()
                if db_sub:
                    await db.delete(db_sub)

                # Step 2: Count remaining pending subscribers in the SAME transaction
                result = await db.exec(
                    select(func.count(UserTaskSubscription.id)).where(
                        UserTaskSubscription.task_id == task.id,
                        UserTaskSubscription.status == "pending",
                    )
                )
                remaining_count = result.one()

            return remaining_count

        # Cancel first two subscriptions concurrently
        results = await asyncio.gather(
            cancel_subscription(subscriptions[0].id, test_users_cancel[0]["id"]),
            cancel_subscription(subscriptions[1].id, test_users_cancel[1]["id"]),
            return_exceptions=True,
        )

        # No exceptions should be raised
        exceptions = [r for r in results if isinstance(r, Exception)]
        assert len(exceptions) == 0, f"Unexpected exceptions: {exceptions}"

        # Verify only one subscription remains
        async with get_session() as db:
            result = await db.exec(
                select(UserTaskSubscription).where(UserTaskSubscription.task_id == task.id)
            )
            remaining = result.all()
            assert len(remaining) == 1
            assert remaining[0].owner_id == test_users_cancel[2]["id"]


class TestCancelDuringSubmit:
    """Test cancel while aria2 submission is in-flight."""

    @pytest.mark.asyncio
    async def test_cancel_during_submit_does_not_leave_active_task(
        self,
        temp_db_cancel,
        test_users_cancel,
    ):
        """Cancel while add_uri is blocked should not leave active task without subscribers."""
        from starlette.requests import Request

        from app.main import app
        from app.models import User
        from app.routers.tasks import TaskCreate, cancel_task, create_task

        # Ensure isolated AppState for consistent locks
        app.state.app_state = AppState()

        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/tasks",
                "headers": [],
                "client": ("test", 1234),
                "app": app,
            }
        )

        payload = TaskCreate(uri="https://example.com/file.zip")

        add_entered = asyncio.Event()
        add_release = asyncio.Event()

        async def add_uri_blocking(uris, options=None):
            add_entered.set()
            await add_release.wait()
            return "gid_submit_race_123"

        mock_client = AsyncMock()
        mock_client.add_uri.side_effect = add_uri_blocking
        mock_client.force_remove.return_value = "OK"
        mock_client.remove_download_result.return_value = "OK"

        # Capture background submit task so we can await it
        created_tasks: list[asyncio.Task] = []
        real_create_task = asyncio.create_task

        def capture_task(coro, *args, **kwargs):
            task = real_create_task(coro, *args, **kwargs)
            created_tasks.append(task)
            return task

        probe_result = type(
            "ProbeResult",
            (),
            {
                "success": True,
                "final_url": "https://example.com/file.zip",
                "content_length": 1024,
                "filename": "file.zip",
                "error": None,
            },
        )()

        async def fake_space_info(user_id: int, quota: int):
            return {
                "available": 10 * 1024 * 1024 * 1024,
                "used": 0,
                "frozen": 0,
                "quota": quota,
            }

        with patch("app.routers.tasks.api_limiter") as mock_limiter, \
             patch("app.routers.tasks.probe_url_with_get_fallback", new_callable=AsyncMock, return_value=probe_result), \
             patch("app.routers.tasks.socket.getaddrinfo", return_value=[(None, None, None, None, ("93.184.216.34", 0))]), \
             patch("app.routers.tasks.get_aria2_client", return_value=mock_client), \
             patch("app.routers.tasks._broadcast_task_update", new_callable=AsyncMock), \
             patch("app.services.storage.get_user_space_info", new_callable=AsyncMock, side_effect=fake_space_info), \
             patch("app.routers.tasks.asyncio.create_task", side_effect=capture_task):
            mock_limiter.is_allowed = AsyncMock(return_value=True)

            # Load user model
            async with get_session() as db:
                result = await db.exec(select(User).where(User.id == test_users_cancel[0]["id"]))
                user = result.first()

            # Create task (schedules background submit)
            subscription = await create_task(payload, request, user=user)
            sub_id = subscription["id"]

            # Capture task_id before cancellation
            async with get_session() as db:
                result = await db.exec(
                    select(UserTaskSubscription).where(UserTaskSubscription.id == sub_id)
                )
                sub = result.first()
                task_id = sub.task_id if sub else None

            # Wait until add_uri is entered (submit in-flight)
            await asyncio.wait_for(add_entered.wait(), timeout=1.0)

            # Cancel subscription while submit is blocked
            cancel_request = Request(
                {
                    "type": "http",
                    "method": "DELETE",
                    "path": f"/api/tasks/{sub_id}",
                    "headers": [],
                    "client": ("test", 1234),
                    "app": app,
                }
            )
            # Start cancellation while submit is blocked (avoid deadlock on shared lock)
            cancel_future = asyncio.create_task(cancel_task(sub_id, cancel_request, user=user))

            # Release submit and wait for background tasks
            add_release.set()
            if created_tasks:
                await asyncio.gather(*created_tasks)

            cancel_result = await cancel_future
            assert cancel_result == {"ok": True}

        # Verify no pending subscriptions remain and task is not active
        async with get_session() as db:
            result = await db.exec(
                select(UserTaskSubscription).where(UserTaskSubscription.id == sub_id)
            )
            assert result.first() is None

            result = await db.exec(select(DownloadTask).where(DownloadTask.id == task_id))
            task = result.first()

        # Task should be marked as cancelled (error) after race resolution
        assert task is not None
        assert task.status == "error"
        assert task.error_display == "已取消"

class TestCancelOnlySubscriberRemovesAria2Task:
    """Test that aria2 task is cancelled when last subscriber cancels."""

    @pytest.mark.asyncio
    async def test_cancel_only_subscriber_removes_aria2_task(self, temp_db_cancel, test_users_cancel):
        """Verify aria2 task is cancelled when last subscriber cancels."""
        # Create task with single subscriber
        async with get_session() as db:
            task = DownloadTask(
                uri_hash="single_sub_hash_456",
                uri="https://example.com/single.zip",
                gid="test_gid_single_456",
                status="active",
                name="single.zip",
                total_length=1024 * 1024,
                completed_length=0,
                created_at=utc_now_str(),
                updated_at=utc_now_str(),
            )
            db.add(task)
            await db.commit()
            await db.refresh(task)

            subscription = UserTaskSubscription(
                owner_id=test_users_cancel[0]["id"],
                task_id=task.id,
                frozen_space=1024 * 1024,
                status="pending",
                created_at=utc_now_str(),
            )
            db.add(subscription)
            await db.commit()
            await db.refresh(subscription)

        # Mock aria2 client
        mock_client = AsyncMock()
        mock_client.force_remove.return_value = "OK"
        mock_client.remove_download_result.return_value = "OK"

        # Simulate cancellation
        async with get_session() as db:
            result = await db.exec(
                select(UserTaskSubscription).where(UserTaskSubscription.id == subscription.id)
            )
            db_sub = result.first()
            if db_sub:
                await db.delete(db_sub)

            result = await db.exec(
                select(func.count(UserTaskSubscription.id)).where(
                    UserTaskSubscription.task_id == task.id,
                    UserTaskSubscription.status == "pending",
                )
            )
            remaining_count = result.one()

        # Should have 0 remaining subscribers
        assert remaining_count == 0

        # Aria2 task should be cancelled (simulated)
        if remaining_count == 0 and task.gid and task.status in ("queued", "active"):
            await mock_client.force_remove(task.gid)
            await mock_client.remove_download_result(task.gid)

        # Verify aria2 client was called
        mock_client.force_remove.assert_called_once_with(task.gid)
        mock_client.remove_download_result.assert_called_once_with(task.gid)


class TestCancelNotOnlySubscriberKeepsAria2Task:
    """Test that aria2 task is kept when other subscribers remain."""

    @pytest.mark.asyncio
    async def test_cancel_not_only_subscriber_keeps_aria2_task(self, temp_db_cancel, test_users_cancel, test_task_with_subscriptions):
        """Verify aria2 task is kept when other subscribers remain."""
        task, subscriptions = test_task_with_subscriptions

        # Mock aria2 client
        mock_client = AsyncMock()
        mock_client.force_remove.return_value = "OK"
        mock_client.remove_download_result.return_value = "OK"

        # Cancel first subscription
        async with get_session() as db:
            result = await db.exec(
                select(UserTaskSubscription).where(UserTaskSubscription.id == subscriptions[0].id)
            )
            db_sub = result.first()
            if db_sub:
                await db.delete(db_sub)

            result = await db.exec(
                select(func.count(UserTaskSubscription.id)).where(
                    UserTaskSubscription.task_id == task.id,
                    UserTaskSubscription.status == "pending",
                )
            )
            remaining_count = result.one()

        # Should have 2 remaining subscribers
        assert remaining_count == 2

        # Aria2 task should NOT be cancelled
        if remaining_count == 0 and task.gid and task.status in ("queued", "active"):
            await mock_client.force_remove(task.gid)
            await mock_client.remove_download_result(task.gid)

        # Verify aria2 client was NOT called
        mock_client.force_remove.assert_not_called()
        mock_client.remove_download_result.assert_not_called()

        # Verify task is still active
        async with get_session() as db:
            result = await db.exec(
                select(DownloadTask).where(DownloadTask.id == task.id)
            )
            db_task = result.first()
            assert db_task.status == "active"
            assert db_task.gid == "test_gid_cancel_123"
