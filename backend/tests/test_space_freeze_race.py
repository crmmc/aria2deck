"""Test race condition handling in space freezing.

Tests for:
1. Optimistic lock prevents double freeze
2. Cumulative frozen tracking for multiple subscriptions
"""
import asyncio
import os
import tempfile
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import update
from sqlmodel import select

from app.database import get_session, reset_engine, init_db as init_sqlmodel_db, dispose_engine
from app.db import init_db, execute
from app.core.config import settings
from app.core.security import hash_password
from app.models import DownloadTask, User, UserTaskSubscription, utc_now_str


@pytest.fixture(scope="function")
def temp_db_freeze():
    """Create a fresh temporary database for freeze tests."""
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
def test_user_freeze(temp_db_freeze):
    """Create a test user for freeze tests."""
    user_id = execute(
        """
        INSERT INTO users (username, password_hash, is_admin, created_at, quota)
        VALUES (?, ?, ?, ?, ?)
        """,
        ["freezeuser", hash_password("testpass"), 0, datetime.now(timezone.utc).isoformat(), 100 * 1024 * 1024 * 1024]
    )
    return {"id": user_id, "username": "freezeuser", "quota": 100 * 1024 * 1024 * 1024}


@pytest.fixture
def test_task_freeze(temp_db_freeze):
    """Create a test task for freeze tests."""
    async def _create():
        async with get_session() as db:
            task = DownloadTask(
                uri_hash="freeze_test_hash_123",
                uri="https://example.com/freeze_test.zip",
                gid="test_gid_freeze_123",
                status="active",
                name="freeze_test.zip",
                total_length=10 * 1024 * 1024,  # 10MB
                completed_length=0,
                created_at=utc_now_str(),
                updated_at=utc_now_str(),
            )
            db.add(task)
            await db.commit()
            await db.refresh(task)
            return task

    return asyncio.run(_create())


class TestOptimisticLockPreventsDoubleFreeze:
    """Test optimistic lock prevents double freeze."""

    @pytest.mark.asyncio
    async def test_optimistic_lock_prevents_double_freeze(self, temp_db_freeze, test_user_freeze, test_task_freeze):
        """Same subscription can't be frozen twice.

        Uses optimistic locking pattern: only update if frozen_space is still 0.
        """
        task = test_task_freeze
        total_length = 10 * 1024 * 1024  # 10MB

        # Create subscription with frozen_space = 0
        async with get_session() as db:
            subscription = UserTaskSubscription(
                owner_id=test_user_freeze["id"],
                task_id=task.id,
                frozen_space=0,  # Not yet frozen
                status="pending",
                created_at=utc_now_str(),
            )
            db.add(subscription)
            await db.commit()
            await db.refresh(subscription)
            sub_id = subscription.id

        # Simulate concurrent freeze attempts using optimistic locking
        async def try_freeze(attempt_id: int) -> bool:
            """Try to freeze space using optimistic lock pattern."""
            async with get_session() as db:
                result = await db.execute(
                    update(UserTaskSubscription)
                    .where(
                        UserTaskSubscription.id == sub_id,
                        UserTaskSubscription.frozen_space == 0  # Optimistic lock
                    )
                    .values(frozen_space=total_length)
                )
                return result.rowcount > 0

        # Run concurrent freeze attempts
        results = await asyncio.gather(
            try_freeze(1),
            try_freeze(2),
            try_freeze(3),
        )

        # Only one should succeed
        success_count = sum(1 for r in results if r)
        assert success_count == 1, f"Expected exactly 1 successful freeze, got {success_count}"

        # Verify frozen_space is set correctly
        async with get_session() as db:
            result = await db.exec(
                select(UserTaskSubscription).where(UserTaskSubscription.id == sub_id)
            )
            sub = result.first()
            assert sub.frozen_space == total_length

    @pytest.mark.asyncio
    async def test_already_frozen_subscription_not_refrozen(self, temp_db_freeze, test_user_freeze, test_task_freeze):
        """Subscription with non-zero frozen_space is not refrozen."""
        task = test_task_freeze
        initial_frozen = 5 * 1024 * 1024  # 5MB
        new_frozen = 10 * 1024 * 1024  # 10MB

        # Create subscription with already frozen space
        async with get_session() as db:
            subscription = UserTaskSubscription(
                owner_id=test_user_freeze["id"],
                task_id=task.id,
                frozen_space=initial_frozen,  # Already frozen
                status="pending",
                created_at=utc_now_str(),
            )
            db.add(subscription)
            await db.commit()
            await db.refresh(subscription)
            sub_id = subscription.id

        # Try to freeze again (should fail due to optimistic lock)
        async with get_session() as db:
            result = await db.execute(
                update(UserTaskSubscription)
                .where(
                    UserTaskSubscription.id == sub_id,
                    UserTaskSubscription.frozen_space == 0  # Optimistic lock
                )
                .values(frozen_space=new_frozen)
            )
            updated = result.rowcount > 0

        # Should not update
        assert not updated

        # Verify frozen_space is unchanged
        async with get_session() as db:
            result = await db.exec(
                select(UserTaskSubscription).where(UserTaskSubscription.id == sub_id)
            )
            sub = result.first()
            assert sub.frozen_space == initial_frozen


class TestCumulativeFrozenTracking:
    """Test cumulative frozen tracking for multiple subscriptions."""

    @pytest.mark.asyncio
    async def test_cumulative_frozen_tracking(self, temp_db_freeze, test_user_freeze):
        """Multiple subscriptions for same user track cumulative space."""
        user_id = test_user_freeze["id"]
        user_quota = test_user_freeze["quota"]
        task_size = 30 * 1024 * 1024 * 1024  # 30GB per task

        # Create multiple tasks
        tasks = []
        for i in range(3):
            async with get_session() as db:
                task = DownloadTask(
                    uri_hash=f"cumulative_hash_{i}",
                    uri=f"https://example.com/file{i}.zip",
                    gid=f"test_gid_cumulative_{i}",
                    status="active",
                    name=f"file{i}.zip",
                    total_length=task_size,
                    completed_length=0,
                    created_at=utc_now_str(),
                    updated_at=utc_now_str(),
                )
                db.add(task)
                await db.commit()
                await db.refresh(task)
                tasks.append(task)

        # Create subscriptions with frozen_space = 0
        subscriptions = []
        for task in tasks:
            async with get_session() as db:
                sub = UserTaskSubscription(
                    owner_id=user_id,
                    task_id=task.id,
                    frozen_space=0,
                    status="pending",
                    created_at=utc_now_str(),
                )
                db.add(sub)
                await db.commit()
                await db.refresh(sub)
                subscriptions.append(sub)

        # Simulate space check with cumulative tracking
        # User has 100GB quota, each task is 30GB
        # First 3 tasks should fit (90GB total), 4th would exceed

        cumulative_frozen = 0
        valid_count = 0

        for sub in subscriptions:
            # Calculate effective available space
            effective_available = user_quota - cumulative_frozen

            if task_size <= effective_available:
                # Freeze space
                async with get_session() as db:
                    result = await db.execute(
                        update(UserTaskSubscription)
                        .where(
                            UserTaskSubscription.id == sub.id,
                            UserTaskSubscription.frozen_space == 0
                        )
                        .values(frozen_space=task_size)
                    )
                    if result.rowcount > 0:
                        cumulative_frozen += task_size
                        valid_count += 1

        # All 3 should succeed (90GB < 100GB quota)
        assert valid_count == 3
        assert cumulative_frozen == 90 * 1024 * 1024 * 1024

        # Verify all subscriptions have frozen_space set
        async with get_session() as db:
            for sub in subscriptions:
                result = await db.exec(
                    select(UserTaskSubscription).where(UserTaskSubscription.id == sub.id)
                )
                db_sub = result.first()
                assert db_sub.frozen_space == task_size

    @pytest.mark.asyncio
    async def test_cumulative_frozen_exceeds_quota(self, temp_db_freeze, test_user_freeze):
        """Fourth subscription exceeds quota when cumulative frozen is tracked."""
        user_id = test_user_freeze["id"]
        user_quota = test_user_freeze["quota"]  # 100GB
        task_size = 30 * 1024 * 1024 * 1024  # 30GB per task

        # Create 4 tasks (total 120GB > 100GB quota)
        tasks = []
        for i in range(4):
            async with get_session() as db:
                task = DownloadTask(
                    uri_hash=f"exceed_hash_{i}",
                    uri=f"https://example.com/exceed{i}.zip",
                    gid=f"test_gid_exceed_{i}",
                    status="active",
                    name=f"exceed{i}.zip",
                    total_length=task_size,
                    completed_length=0,
                    created_at=utc_now_str(),
                    updated_at=utc_now_str(),
                )
                db.add(task)
                await db.commit()
                await db.refresh(task)
                tasks.append(task)

        # Create subscriptions
        subscriptions = []
        for task in tasks:
            async with get_session() as db:
                sub = UserTaskSubscription(
                    owner_id=user_id,
                    task_id=task.id,
                    frozen_space=0,
                    status="pending",
                    created_at=utc_now_str(),
                )
                db.add(sub)
                await db.commit()
                await db.refresh(sub)
                subscriptions.append(sub)

        # Process subscriptions with cumulative tracking
        cumulative_frozen = 0
        valid_count = 0
        failed_count = 0

        for sub in subscriptions:
            effective_available = user_quota - cumulative_frozen

            if task_size <= effective_available:
                async with get_session() as db:
                    result = await db.execute(
                        update(UserTaskSubscription)
                        .where(
                            UserTaskSubscription.id == sub.id,
                            UserTaskSubscription.frozen_space == 0
                        )
                        .values(frozen_space=task_size)
                    )
                    if result.rowcount > 0:
                        cumulative_frozen += task_size
                        valid_count += 1
            else:
                # Mark as failed
                async with get_session() as db:
                    await db.execute(
                        update(UserTaskSubscription)
                        .where(UserTaskSubscription.id == sub.id)
                        .values(
                            status="failed",
                            error_display="User quota space insufficient",
                            frozen_space=0
                        )
                    )
                failed_count += 1

        # First 3 should succeed, 4th should fail
        assert valid_count == 3
        assert failed_count == 1

        # Verify 4th subscription is marked as failed
        async with get_session() as db:
            result = await db.exec(
                select(UserTaskSubscription).where(UserTaskSubscription.id == subscriptions[3].id)
            )
            failed_sub = result.first()
            assert failed_sub.status == "failed"
            assert failed_sub.frozen_space == 0
