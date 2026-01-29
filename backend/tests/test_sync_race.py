"""Test race condition handling in sync module.

Tests for:
1. Peak value atomic update
2. Peak value only increases (never decreases)
"""
import asyncio
import os
import tempfile
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import case, update
from sqlmodel import select

from app.database import get_session, reset_engine, init_db as init_sqlmodel_db, dispose_engine
from app.db import init_db, execute
from app.core.config import settings
from app.core.security import hash_password
from app.models import DownloadTask, utc_now_str


@pytest.fixture(scope="function")
def temp_db_sync():
    """Create a fresh temporary database for sync tests."""
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
def test_task_sync(temp_db_sync):
    """Create a test task for sync tests."""
    async def _create():
        async with get_session() as db:
            task = DownloadTask(
                uri_hash="sync_test_hash_123",
                uri="https://example.com/sync_test.zip",
                gid="test_gid_sync_123",
                status="active",
                name="sync_test.zip",
                total_length=100 * 1024 * 1024,
                completed_length=0,
                download_speed=0,
                upload_speed=0,
                peak_download_speed=0,
                peak_connections=0,
                created_at=utc_now_str(),
                updated_at=utc_now_str(),
            )
            db.add(task)
            await db.commit()
            await db.refresh(task)
            return task

    return asyncio.run(_create())


class TestPeakValueAtomicUpdate:
    """Test peak value atomic update."""

    @pytest.mark.asyncio
    async def test_peak_value_atomic_update(self, temp_db_sync, test_task_sync):
        """Concurrent updates don't overwrite higher values.

        Uses SQL CASE expression for atomic conditional update.
        """
        task = test_task_sync
        task_id = task.id

        # Simulate concurrent updates with different speed values
        speeds = [1000, 5000, 3000, 8000, 2000, 10000, 7000, 4000]

        async def update_peak(speed: int):
            """Update peak value using atomic CASE pattern."""
            async with get_session() as db:
                await db.execute(
                    update(DownloadTask)
                    .where(DownloadTask.id == task_id)
                    .values(
                        download_speed=speed,
                        peak_download_speed=case(
                            (DownloadTask.peak_download_speed < speed, speed),
                            else_=DownloadTask.peak_download_speed
                        ),
                    )
                )

        # Run concurrent updates
        await asyncio.gather(*[update_peak(s) for s in speeds])

        # Verify peak is the maximum value
        async with get_session() as db:
            result = await db.exec(
                select(DownloadTask).where(DownloadTask.id == task_id)
            )
            db_task = result.first()
            assert db_task.peak_download_speed == max(speeds), \
                f"Expected peak={max(speeds)}, got {db_task.peak_download_speed}"

    @pytest.mark.asyncio
    async def test_peak_connections_atomic_update(self, temp_db_sync, test_task_sync):
        """Concurrent connection count updates use atomic CASE pattern."""
        task = test_task_sync
        task_id = task.id

        # Simulate concurrent updates with different connection counts
        connections = [5, 10, 8, 15, 12, 20, 18, 25]

        async def update_peak_connections(conn: int):
            """Update peak connections using atomic CASE pattern."""
            async with get_session() as db:
                await db.execute(
                    update(DownloadTask)
                    .where(DownloadTask.id == task_id)
                    .values(
                        peak_connections=case(
                            (DownloadTask.peak_connections < conn, conn),
                            else_=DownloadTask.peak_connections
                        ),
                    )
                )

        # Run concurrent updates
        await asyncio.gather(*[update_peak_connections(c) for c in connections])

        # Verify peak is the maximum value
        async with get_session() as db:
            result = await db.exec(
                select(DownloadTask).where(DownloadTask.id == task_id)
            )
            db_task = result.first()
            assert db_task.peak_connections == max(connections), \
                f"Expected peak_connections={max(connections)}, got {db_task.peak_connections}"


class TestPeakValueOnlyIncreases:
    """Test that peak value never decreases."""

    @pytest.mark.asyncio
    async def test_peak_value_only_increases(self, temp_db_sync, test_task_sync):
        """Peak value never decreases even with lower current values."""
        task = test_task_sync
        task_id = task.id

        # Set initial peak value
        initial_peak = 10000
        async with get_session() as db:
            await db.execute(
                update(DownloadTask)
                .where(DownloadTask.id == task_id)
                .values(peak_download_speed=initial_peak)
            )

        # Try to update with lower values
        lower_speeds = [5000, 3000, 1000, 8000, 2000]

        async def update_with_lower(speed: int):
            """Try to update peak with potentially lower value."""
            async with get_session() as db:
                await db.execute(
                    update(DownloadTask)
                    .where(DownloadTask.id == task_id)
                    .values(
                        download_speed=speed,
                        peak_download_speed=case(
                            (DownloadTask.peak_download_speed < speed, speed),
                            else_=DownloadTask.peak_download_speed
                        ),
                    )
                )

        # Run updates
        await asyncio.gather(*[update_with_lower(s) for s in lower_speeds])

        # Verify peak is still the initial value (none of the updates were higher)
        async with get_session() as db:
            result = await db.exec(
                select(DownloadTask).where(DownloadTask.id == task_id)
            )
            db_task = result.first()
            assert db_task.peak_download_speed == initial_peak, \
                f"Peak should remain {initial_peak}, got {db_task.peak_download_speed}"

    @pytest.mark.asyncio
    async def test_peak_value_increases_with_higher(self, temp_db_sync, test_task_sync):
        """Peak value increases when higher value is provided."""
        task = test_task_sync
        task_id = task.id

        # Set initial peak value
        initial_peak = 5000
        async with get_session() as db:
            await db.execute(
                update(DownloadTask)
                .where(DownloadTask.id == task_id)
                .values(peak_download_speed=initial_peak)
            )

        # Update with higher value
        higher_speed = 15000
        async with get_session() as db:
            await db.execute(
                update(DownloadTask)
                .where(DownloadTask.id == task_id)
                .values(
                    download_speed=higher_speed,
                    peak_download_speed=case(
                        (DownloadTask.peak_download_speed < higher_speed, higher_speed),
                        else_=DownloadTask.peak_download_speed
                    ),
                )
            )

        # Verify peak is updated
        async with get_session() as db:
            result = await db.exec(
                select(DownloadTask).where(DownloadTask.id == task_id)
            )
            db_task = result.first()
            assert db_task.peak_download_speed == higher_speed, \
                f"Peak should be {higher_speed}, got {db_task.peak_download_speed}"

    @pytest.mark.asyncio
    async def test_mixed_higher_lower_updates(self, temp_db_sync, test_task_sync):
        """Mixed higher and lower updates result in correct peak."""
        task = test_task_sync
        task_id = task.id

        # Set initial peak value
        initial_peak = 5000
        async with get_session() as db:
            await db.execute(
                update(DownloadTask)
                .where(DownloadTask.id == task_id)
                .values(peak_download_speed=initial_peak)
            )

        # Mix of higher and lower values
        speeds = [3000, 8000, 2000, 12000, 1000, 6000, 15000, 4000]
        expected_peak = max(initial_peak, max(speeds))

        async def update_speed(speed: int):
            async with get_session() as db:
                await db.execute(
                    update(DownloadTask)
                    .where(DownloadTask.id == task_id)
                    .values(
                        download_speed=speed,
                        peak_download_speed=case(
                            (DownloadTask.peak_download_speed < speed, speed),
                            else_=DownloadTask.peak_download_speed
                        ),
                    )
                )

        # Run concurrent updates
        await asyncio.gather(*[update_speed(s) for s in speeds])

        # Verify peak is the maximum of all values
        async with get_session() as db:
            result = await db.exec(
                select(DownloadTask).where(DownloadTask.id == task_id)
            )
            db_task = result.first()
            assert db_task.peak_download_speed == expected_peak, \
                f"Peak should be {expected_peak}, got {db_task.peak_download_speed}"


class TestPeakValueSequentialUpdates:
    """Test peak value with sequential updates."""

    @pytest.mark.asyncio
    async def test_sequential_increasing_updates(self, temp_db_sync, test_task_sync):
        """Sequential increasing updates all succeed."""
        task = test_task_sync
        task_id = task.id

        speeds = [1000, 2000, 3000, 4000, 5000]

        for speed in speeds:
            async with get_session() as db:
                await db.execute(
                    update(DownloadTask)
                    .where(DownloadTask.id == task_id)
                    .values(
                        download_speed=speed,
                        peak_download_speed=case(
                            (DownloadTask.peak_download_speed < speed, speed),
                            else_=DownloadTask.peak_download_speed
                        ),
                    )
                )

            # Verify peak after each update
            async with get_session() as db:
                result = await db.exec(
                    select(DownloadTask).where(DownloadTask.id == task_id)
                )
                db_task = result.first()
                assert db_task.peak_download_speed == speed

    @pytest.mark.asyncio
    async def test_sequential_decreasing_updates(self, temp_db_sync, test_task_sync):
        """Sequential decreasing updates don't change peak."""
        task = test_task_sync
        task_id = task.id

        # Set initial high peak
        initial_peak = 10000
        async with get_session() as db:
            await db.execute(
                update(DownloadTask)
                .where(DownloadTask.id == task_id)
                .values(peak_download_speed=initial_peak)
            )

        speeds = [8000, 6000, 4000, 2000, 1000]

        for speed in speeds:
            async with get_session() as db:
                await db.execute(
                    update(DownloadTask)
                    .where(DownloadTask.id == task_id)
                    .values(
                        download_speed=speed,
                        peak_download_speed=case(
                            (DownloadTask.peak_download_speed < speed, speed),
                            else_=DownloadTask.peak_download_speed
                        ),
                    )
                )

            # Verify peak remains unchanged
            async with get_session() as db:
                result = await db.exec(
                    select(DownloadTask).where(DownloadTask.id == task_id)
                )
                db_task = result.first()
                assert db_task.peak_download_speed == initial_peak
