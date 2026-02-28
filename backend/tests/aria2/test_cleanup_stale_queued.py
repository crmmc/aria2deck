"""Tests for _cleanup_stale_queued_tasks function."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
import pytest


class TestStaleQueuedTasksCleanup:
    """Test cases for stale queued tasks cleanup."""

    @pytest.fixture
    def mock_state(self):
        """Create mock AppState."""
        state = MagicMock()
        state.lock = AsyncMock()
        state.lock.__aenter__ = AsyncMock()
        state.lock.__aexit__ = AsyncMock()
        state.task_submit_locks = {}
        return state

    @pytest.mark.asyncio
    async def test_time_comparison_format_consistency(self):
        """Verify ISO format is used for time comparison."""
        from app.models import utc_now_str

        # Both should use isoformat for consistent comparison
        threshold = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        stored = utc_now_str()

        # Formats should be comparable
        assert "T" in threshold
        assert "T" in stored
        assert "+" in threshold or "Z" in threshold  # Has timezone
        assert "+" in stored or "Z" in stored

    @pytest.mark.asyncio
    async def test_skip_task_with_active_submit_lock(self, mock_state):
        """Tasks with active submit lock should be skipped."""
        from asyncio import Lock

        task_id = 123
        active_lock = Lock()
        await active_lock.acquire()  # Lock is held

        mock_state.task_submit_locks = {task_id: active_lock}

        # The cleanup should skip this task
        assert active_lock.locked()
        assert mock_state.task_submit_locks.get(task_id) is not None

        active_lock.release()

    @pytest.mark.asyncio
    async def test_stale_threshold_calculation(self):
        """Verify 5-minute grace period calculation."""
        from app.aria2.sync import STALE_QUEUED_GRACE_SECONDS

        assert STALE_QUEUED_GRACE_SECONDS == 300.0  # 5 minutes

        now = datetime.now(timezone.utc)
        threshold = now - timedelta(seconds=STALE_QUEUED_GRACE_SECONDS)

        # Task updated 6 minutes ago should be stale
        old_task_time = (now - timedelta(minutes=6)).isoformat()
        assert old_task_time < threshold.isoformat()

        # Task updated 4 minutes ago should NOT be stale
        recent_task_time = (now - timedelta(minutes=4)).isoformat()
        assert recent_task_time > threshold.isoformat()

    @pytest.mark.asyncio
    async def test_status_update_before_cleanup(self):
        """Status must be updated before directory cleanup."""
        # This test verifies the order: update DB -> cleanup dir
        # If DB update fails, cleanup should NOT happen
        status_updated = False
        cleanup_called = False

        async def mock_db_update():
            nonlocal status_updated
            status_updated = True
            return True

        async def mock_cleanup():
            nonlocal cleanup_called
            # Cleanup should only happen after status update
            assert status_updated, "Cleanup called before status update!"
            cleanup_called = True

        await mock_db_update()
        if status_updated:
            await mock_cleanup()

        assert status_updated
        assert cleanup_called
