"""Test race condition handling in sync module.

Tests for:
1. Peak value atomic update
2. Peak value only increases (never decreases)
"""
import asyncio
import os
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import case, update
from sqlmodel import select

from app.database import get_session, reset_engine, init_db as init_sqlmodel_db, dispose_engine
from app.db import init_db, execute
from app.core.config import settings
from app.aria2.sync import _cleanup_orphan_aria2_tasks, _repair_inconsistent_completed_tasks, sync_tasks
from app.core.state import AppState
from app.models import DownloadTask, TaskHistory, UserTaskSubscription, utc_now_str


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


class TestOrphanCleanup:
    @pytest.mark.asyncio
    async def test_cleanup_stopped_orphan_immediately(self):
        client = AsyncMock()
        client.tell_active.return_value = []
        client.tell_waiting.return_value = []
        client.tell_stopped.return_value = [{"gid": "orphan_stopped_1"}]

        orphan_seen_at: dict[str, float] = {}
        await _cleanup_orphan_aria2_tasks(
            client=client,
            tracked_gids=set(),
            orphan_seen_at=orphan_seen_at,
            grace_seconds=60.0,
            max_actions=50,
        )

        client.remove_download_result.assert_awaited_once_with("orphan_stopped_1")
        client.force_remove.assert_not_awaited()
        assert "orphan_stopped_1" not in orphan_seen_at

    @pytest.mark.asyncio
    async def test_cleanup_active_orphan_after_grace(self):
        client = AsyncMock()
        client.tell_active.return_value = [{"gid": "orphan_active_1"}]
        client.tell_waiting.return_value = []
        client.tell_stopped.return_value = []

        orphan_seen_at = {"orphan_active_1": 0.0}
        with patch("app.aria2.sync.time.monotonic", return_value=120.0):
            await _cleanup_orphan_aria2_tasks(
                client=client,
                tracked_gids=set(),
                orphan_seen_at=orphan_seen_at,
                grace_seconds=60.0,
                max_actions=50,
            )

        client.force_remove.assert_awaited_once_with("orphan_active_1")
        client.remove_download_result.assert_awaited_once_with("orphan_active_1")
        assert "orphan_active_1" not in orphan_seen_at


class TestRepairInconsistentComplete:
    @pytest.mark.asyncio
    async def test_repair_inconsistent_complete_cleans_artifacts(self, temp_db_sync):
        user_id = execute(
            """
            INSERT INTO users (username, password_hash, is_admin, created_at, quota)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                "repair_user",
                "x",
                0,
                datetime.now(timezone.utc).isoformat(),
                100 * 1024 * 1024 * 1024,
            ],
        )

        stale_updated_at = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
        task_id = execute(
            """
            INSERT INTO download_tasks
            (uri_hash, uri, gid, status, name, total_length, completed_length,
             download_speed, upload_speed, peak_download_speed, peak_connections, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                "repair_inconsistent_hash",
                "https://example.com/inconsistent.zip",
                "gid-repair-inconsistent",
                "complete",
                "inconsistent.zip",
                2048,
                2048,
                0,
                0,
                0,
                0,
                stale_updated_at,
                stale_updated_at,
            ],
        )
        execute(
            """
            INSERT INTO user_task_subscriptions (owner_id, task_id, frozen_space, status, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            [user_id, task_id, 2048, "pending", stale_updated_at],
        )

        task_dir = os.path.join(settings.download_dir, "downloading", str(task_id))
        os.makedirs(task_dir, exist_ok=True)
        with open(os.path.join(task_dir, "partial.bin"), "w") as f:
            f.write("partial")

        state = AppState()
        mock_client = AsyncMock()
        with patch("app.core.state.get_aria2_client", return_value=mock_client):
            with patch(
                "app.routers.tasks.broadcast_task_update_to_subscribers",
                new_callable=AsyncMock,
            ) as mock_broadcast:
                await _repair_inconsistent_completed_tasks(state)

        mock_client.force_remove.assert_awaited_once_with("gid-repair-inconsistent")
        mock_client.remove_download_result.assert_awaited_once_with("gid-repair-inconsistent")
        mock_broadcast.assert_awaited_once_with(state, task_id)
        assert not os.path.exists(task_dir)

        async with get_session() as db:
            task = await db.get(DownloadTask, task_id)
            assert task is not None
            assert task.status == "error"
            assert task.gid is None
            assert task.error_display == "下载完成但文件未入库"

            sub = (
                await db.exec(
                    select(UserTaskSubscription).where(UserTaskSubscription.task_id == task_id)
                )
            ).first()
            assert sub is not None
            assert sub.status == "failed"
            assert sub.frozen_space == 0

            history_rows = (
                await db.exec(select(TaskHistory).where(TaskHistory.owner_id == user_id))
            ).all()
            assert len(history_rows) == 1
            assert history_rows[0].result == "failed"
            assert history_rows[0].reason == "下载完成但文件未入库"


@pytest.mark.asyncio
class TestSyncTaskSelectionAndErrorHandling:
    async def test_sync_tracks_waiting_and_paused_tasks(self, temp_db_sync):
        async with get_session() as db:
            waiting_task = DownloadTask(
                uri_hash="sync_waiting_hash",
                uri="https://example.com/waiting.bin",
                gid="gid-sync-waiting",
                status="waiting",
                name="waiting.bin",
                created_at=utc_now_str(),
                updated_at=utc_now_str(),
            )
            paused_task = DownloadTask(
                uri_hash="sync_paused_hash",
                uri="https://example.com/paused.bin",
                gid="gid-sync-paused",
                status="paused",
                name="paused.bin",
                created_at=utc_now_str(),
                updated_at=utc_now_str(),
            )
            db.add(waiting_task)
            db.add(paused_task)

        state = AppState()
        mock_client = AsyncMock()
        mock_client.tell_status.side_effect = [
            {
                "gid": "gid-sync-waiting",
                "status": "waiting",
                "totalLength": "0",
                "completedLength": "0",
                "downloadSpeed": "0",
                "uploadSpeed": "0",
            },
            {
                "gid": "gid-sync-paused",
                "status": "paused",
                "totalLength": "0",
                "completedLength": "0",
                "downloadSpeed": "0",
                "uploadSpeed": "0",
            },
        ]

        with patch("app.core.state.get_aria2_client", return_value=mock_client), \
             patch("app.aria2.sync._repair_inconsistent_completed_tasks", new_callable=AsyncMock), \
             patch("app.aria2.sync._cleanup_orphan_aria2_tasks", new_callable=AsyncMock), \
             patch("app.routers.tasks.broadcast_task_update_to_subscribers", new_callable=AsyncMock), \
             patch("app.aria2.sync.asyncio.sleep", new_callable=AsyncMock, side_effect=asyncio.CancelledError):
            with pytest.raises(asyncio.CancelledError):
                await sync_tasks(state, interval=0.01)

        called_gids = {call.args[0] for call in mock_client.tell_status.await_args_list}
        assert called_gids == {"gid-sync-waiting", "gid-sync-paused"}

    async def test_sync_transient_tell_status_error_keeps_task_active(self, temp_db_sync):
        async with get_session() as db:
            task = DownloadTask(
                uri_hash="sync_transient_error_hash",
                uri="https://example.com/transient.bin",
                gid="gid-sync-transient",
                status="active",
                name="transient.bin",
                created_at=utc_now_str(),
                updated_at=utc_now_str(),
            )
            db.add(task)
            await db.flush()
            assert task.id is not None
            task_id = task.id

        state = AppState()
        mock_client = AsyncMock()
        mock_client.tell_status.side_effect = RuntimeError(
            "Cannot connect to host localhost:6800 ssl:default [Connection refused]"
        )

        with patch("app.core.state.get_aria2_client", return_value=mock_client), \
             patch("app.aria2.sync._repair_inconsistent_completed_tasks", new_callable=AsyncMock), \
             patch("app.aria2.sync._cleanup_orphan_aria2_tasks", new_callable=AsyncMock), \
             patch("app.aria2.sync.cleanup_failed_task_artifacts", new_callable=AsyncMock) as mock_cleanup, \
             patch("app.routers.tasks.broadcast_task_update_to_subscribers", new_callable=AsyncMock) as mock_broadcast, \
             patch("app.aria2.sync.asyncio.sleep", new_callable=AsyncMock, side_effect=asyncio.CancelledError):
            with pytest.raises(asyncio.CancelledError):
                await sync_tasks(state, interval=0.01)

        async with get_session() as db:
            updated = await db.get(DownloadTask, task_id)
            assert updated is not None
            assert updated.status == "active"
            assert updated.gid == "gid-sync-transient"
            assert updated.error is None
            assert updated.error_display is None

        mock_cleanup.assert_not_awaited()
        mock_broadcast.assert_not_awaited()
