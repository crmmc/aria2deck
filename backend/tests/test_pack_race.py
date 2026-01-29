"""Test race condition handling in pack service.

Tests for:
1. Pack status CAS prevents overwrite
2. Concurrent pack status update
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
from app.db import init_db, execute, utc_now
from app.core.config import settings
from app.core.security import hash_password
from app.models import PackTask


@pytest.fixture(scope="function")
def temp_db_pack_race():
    """Create a fresh temporary database for pack race tests."""
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
def test_user_pack_race(temp_db_pack_race):
    """Create a test user for pack race tests."""
    user_id = execute(
        """
        INSERT INTO users (username, password_hash, is_admin, created_at, quota)
        VALUES (?, ?, ?, ?, ?)
        """,
        ["packraceuser", hash_password("testpass"), 0, datetime.now(timezone.utc).isoformat(), 100 * 1024 * 1024 * 1024]
    )
    return {"id": user_id, "username": "packraceuser", "quota": 100 * 1024 * 1024 * 1024}


@pytest.fixture
def packing_task_race(test_user_pack_race, temp_db_pack_race):
    """Create a packing (in-progress) task for race tests."""
    now = utc_now()
    task_id = execute(
        """
        INSERT INTO pack_tasks
        (owner_id, folder_path, folder_size, reserved_space, status, progress, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [test_user_pack_race["id"], "test_folder", 2000000, 2000000, "packing", 50, now, now]
    )
    return {"id": task_id, "owner_id": test_user_pack_race["id"], "status": "packing"}


class TestPackStatusCASPreventsOverwrite:
    """Test that CAS pattern prevents status overwrite."""

    @pytest.mark.asyncio
    async def test_pack_status_cas_prevents_overwrite(self, temp_db_pack_race, packing_task_race):
        """Cancelled task status is not overwritten by completion.

        Uses CAS pattern: only packing can become done.
        """
        task_id = packing_task_race["id"]

        # First, cancel the task (simulating user cancellation)
        async with get_session() as db:
            result = await db.execute(
                update(PackTask)
                .where(
                    PackTask.id == task_id,
                    PackTask.status.in_(["pending", "packing"])  # CAS condition
                )
                .values(
                    status="cancelled",
                    reserved_space=0,
                    updated_at=utc_now()
                )
            )
            cancel_success = result.rowcount > 0

        assert cancel_success, "Cancel should succeed"

        # Verify status is cancelled
        async with get_session() as db:
            result = await db.exec(
                select(PackTask).where(PackTask.id == task_id)
            )
            task = result.first()
            assert task.status == "cancelled"

        # Now try to mark as done (simulating completion after cancel)
        async with get_session() as db:
            result = await db.execute(
                update(PackTask)
                .where(
                    PackTask.id == task_id,
                    PackTask.status == "packing"  # CAS: only packing can become done
                )
                .values(
                    status="done",
                    progress=100,
                    reserved_space=0,
                    updated_at=utc_now()
                )
            )
            done_success = result.rowcount > 0

        # Should fail because status is no longer "packing"
        assert not done_success, "Done update should fail (status is cancelled)"

        # Verify status is still cancelled
        async with get_session() as db:
            result = await db.exec(
                select(PackTask).where(PackTask.id == task_id)
            )
            task = result.first()
            assert task.status == "cancelled", f"Status should remain cancelled, got {task.status}"

    @pytest.mark.asyncio
    async def test_pack_status_cas_allows_valid_transition(self, temp_db_pack_race, packing_task_race):
        """Valid status transition (packing -> done) succeeds."""
        task_id = packing_task_race["id"]

        # Mark as done (valid transition)
        async with get_session() as db:
            result = await db.execute(
                update(PackTask)
                .where(
                    PackTask.id == task_id,
                    PackTask.status == "packing"  # CAS: only packing can become done
                )
                .values(
                    status="done",
                    progress=100,
                    output_size=1500000,
                    reserved_space=0,
                    updated_at=utc_now()
                )
            )
            success = result.rowcount > 0

        assert success, "Done update should succeed"

        # Verify status is done
        async with get_session() as db:
            result = await db.exec(
                select(PackTask).where(PackTask.id == task_id)
            )
            task = result.first()
            assert task.status == "done"
            assert task.progress == 100
            assert task.output_size == 1500000


class TestConcurrentPackStatusUpdate:
    """Test concurrent pack status updates."""

    @pytest.mark.asyncio
    async def test_concurrent_pack_status_update(self, temp_db_pack_race, packing_task_race):
        """Only one status update succeeds when concurrent updates occur."""
        task_id = packing_task_race["id"]

        async def try_cancel():
            """Try to cancel the task."""
            async with get_session() as db:
                result = await db.execute(
                    update(PackTask)
                    .where(
                        PackTask.id == task_id,
                        PackTask.status.in_(["pending", "packing"])
                    )
                    .values(
                        status="cancelled",
                        reserved_space=0,
                        updated_at=utc_now()
                    )
                )
                return "cancelled" if result.rowcount > 0 else None

        async def try_complete():
            """Try to complete the task."""
            async with get_session() as db:
                result = await db.execute(
                    update(PackTask)
                    .where(
                        PackTask.id == task_id,
                        PackTask.status == "packing"
                    )
                    .values(
                        status="done",
                        progress=100,
                        reserved_space=0,
                        updated_at=utc_now()
                    )
                )
                return "done" if result.rowcount > 0 else None

        async def try_fail():
            """Try to fail the task."""
            async with get_session() as db:
                result = await db.execute(
                    update(PackTask)
                    .where(
                        PackTask.id == task_id,
                        PackTask.status.in_(["pending", "packing"])
                    )
                    .values(
                        status="failed",
                        error_message="Test error",
                        reserved_space=0,
                        updated_at=utc_now()
                    )
                )
                return "failed" if result.rowcount > 0 else None

        # Run concurrent status updates
        results = await asyncio.gather(
            try_cancel(),
            try_complete(),
            try_fail(),
            return_exceptions=True,
        )

        # Filter out None results and exceptions
        successful = [r for r in results if r is not None and not isinstance(r, Exception)]

        # Only one should succeed
        assert len(successful) == 1, f"Expected exactly 1 successful update, got {len(successful)}: {successful}"

        # Verify final status matches the successful update
        async with get_session() as db:
            result = await db.exec(
                select(PackTask).where(PackTask.id == task_id)
            )
            task = result.first()
            assert task.status == successful[0], f"Final status should be {successful[0]}, got {task.status}"

    @pytest.mark.asyncio
    async def test_multiple_cancel_attempts(self, temp_db_pack_race, packing_task_race):
        """Multiple concurrent cancel attempts - only one succeeds."""
        task_id = packing_task_race["id"]

        async def try_cancel(attempt_id: int):
            """Try to cancel the task."""
            async with get_session() as db:
                result = await db.execute(
                    update(PackTask)
                    .where(
                        PackTask.id == task_id,
                        PackTask.status.in_(["pending", "packing"])
                    )
                    .values(
                        status="cancelled",
                        reserved_space=0,
                        updated_at=utc_now()
                    )
                )
                return attempt_id if result.rowcount > 0 else None

        # Run multiple concurrent cancel attempts
        results = await asyncio.gather(
            try_cancel(1),
            try_cancel(2),
            try_cancel(3),
            try_cancel(4),
            try_cancel(5),
        )

        # Only one should succeed
        successful = [r for r in results if r is not None]
        assert len(successful) == 1, f"Expected exactly 1 successful cancel, got {len(successful)}"

        # Verify status is cancelled
        async with get_session() as db:
            result = await db.exec(
                select(PackTask).where(PackTask.id == task_id)
            )
            task = result.first()
            assert task.status == "cancelled"


class TestPackStatusTransitionValidation:
    """Test pack status transition validation."""

    @pytest.mark.asyncio
    async def test_pending_to_packing_transition(self, temp_db_pack_race, test_user_pack_race):
        """Pending task can transition to packing."""
        now = utc_now()
        task_id = execute(
            """
            INSERT INTO pack_tasks
            (owner_id, folder_path, folder_size, reserved_space, status, progress, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [test_user_pack_race["id"], "pending_folder", 1000000, 1000000, "pending", 0, now, now]
        )

        # Transition to packing
        async with get_session() as db:
            result = await db.execute(
                update(PackTask)
                .where(
                    PackTask.id == task_id,
                    PackTask.status == "pending"  # CAS: only pending can become packing
                )
                .values(
                    status="packing",
                    updated_at=utc_now()
                )
            )
            success = result.rowcount > 0

        assert success, "Pending to packing transition should succeed"

        # Verify status
        async with get_session() as db:
            result = await db.exec(
                select(PackTask).where(PackTask.id == task_id)
            )
            task = result.first()
            assert task.status == "packing"

    @pytest.mark.asyncio
    async def test_done_cannot_transition(self, temp_db_pack_race, test_user_pack_race):
        """Done task cannot transition to other states."""
        now = utc_now()
        task_id = execute(
            """
            INSERT INTO pack_tasks
            (owner_id, folder_path, folder_size, reserved_space, status, progress, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [test_user_pack_race["id"], "done_folder", 1000000, 0, "done", 100, now, now]
        )

        # Try to cancel (should fail)
        async with get_session() as db:
            result = await db.execute(
                update(PackTask)
                .where(
                    PackTask.id == task_id,
                    PackTask.status.in_(["pending", "packing"])
                )
                .values(
                    status="cancelled",
                    updated_at=utc_now()
                )
            )
            success = result.rowcount > 0

        assert not success, "Done task should not be cancellable"

        # Verify status unchanged
        async with get_session() as db:
            result = await db.exec(
                select(PackTask).where(PackTask.id == task_id)
            )
            task = result.first()
            assert task.status == "done"

    @pytest.mark.asyncio
    async def test_cancelled_cannot_transition(self, temp_db_pack_race, test_user_pack_race):
        """Cancelled task cannot transition to other states."""
        now = utc_now()
        task_id = execute(
            """
            INSERT INTO pack_tasks
            (owner_id, folder_path, folder_size, reserved_space, status, progress, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [test_user_pack_race["id"], "cancelled_folder", 1000000, 0, "cancelled", 20, now, now]
        )

        # Try to complete (should fail)
        async with get_session() as db:
            result = await db.execute(
                update(PackTask)
                .where(
                    PackTask.id == task_id,
                    PackTask.status == "packing"
                )
                .values(
                    status="done",
                    progress=100,
                    updated_at=utc_now()
                )
            )
            success = result.rowcount > 0

        assert not success, "Cancelled task should not be completable"

        # Verify status unchanged
        async with get_session() as db:
            result = await db.exec(
                select(PackTask).where(PackTask.id == task_id)
            )
            task = result.first()
            assert task.status == "cancelled"
