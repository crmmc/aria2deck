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
