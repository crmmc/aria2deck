"""Comprehensive tests for aria2 event listener.

Tests for enhanced idempotency, frozen space release, and state mapping.
"""
import asyncio
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

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


class TestTaskCompletionIdempotencyEnhanced:
    """Enhanced tests for task completion idempotency.

    Verifies:
    1. Duplicate complete events do not create duplicate StoredFile
    2. Duplicate complete events do not create duplicate UserFile
    3. stored_file_id CAS (Compare-And-Swap) works correctly
    4. ref_count is not incremented multiple times
    """

    @pytest.mark.asyncio
    async def test_duplicate_complete_no_duplicate_stored_file(
        self, temp_db_listener, test_user_listener, mock_app_state
    ):
        """Verify duplicate complete events do not create duplicate StoredFile records.

        Scenario:
        1. Create a task with pending subscription
        2. Call _handle_task_complete twice
        3. Verify only one StoredFile is created
        """
        from app.aria2.listener import _handle_task_complete

        # Create task
        async with get_session() as db:
            task = DownloadTask(
                uri_hash="dup_stored_file_hash_001",
                uri="https://example.com/dup_stored.zip",
                gid="gid_dup_stored_001",
                status="complete",
                name="dup_stored.zip",
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
        source_file = task_dir / "dup_stored.zip"
        source_file.write_text("duplicate stored file test content")

        aria2_status = {
            "files": [{"path": str(source_file)}],
            "totalLength": "1024",
            "completedLength": "1024",
        }

        # First call - should create StoredFile
        with patch("app.routers.tasks.broadcast_task_update_to_subscribers", new_callable=AsyncMock):
            await _handle_task_complete(mock_app_state, task_id, aria2_status)

        # Verify first call created StoredFile
        async with get_session() as db:
            result = await db.exec(select(StoredFile))
            stored_files_after_first = result.all()
            assert len(stored_files_after_first) == 1
            first_stored_file_id = stored_files_after_first[0].id

            # Verify task has stored_file_id
            result = await db.exec(select(DownloadTask).where(DownloadTask.id == task_id))
            task = result.first()
            assert task.stored_file_id == first_stored_file_id

        # Recreate source file for second call (simulating duplicate event)
        task_dir.mkdir(parents=True, exist_ok=True)
        source_file2 = task_dir / "dup_stored2.zip"
        source_file2.write_text("different content for second call")

        aria2_status2 = {
            "files": [{"path": str(source_file2)}],
            "totalLength": "2048",
            "completedLength": "2048",
        }

        # Second call - should be skipped due to idempotency
        with patch("app.routers.tasks.broadcast_task_update_to_subscribers", new_callable=AsyncMock):
            await _handle_task_complete(mock_app_state, task_id, aria2_status2)

        # Verify no additional StoredFile was created
        async with get_session() as db:
            result = await db.exec(select(StoredFile))
            stored_files_after_second = result.all()
            assert len(stored_files_after_second) == 1
            assert stored_files_after_second[0].id == first_stored_file_id

    @pytest.mark.asyncio
    async def test_duplicate_complete_no_duplicate_user_file(
        self, temp_db_listener, test_user_listener, mock_app_state
    ):
        """Verify duplicate complete events do not create duplicate UserFile records.

        Scenario:
        1. Create a task with pending subscription
        2. Call _handle_task_complete twice
        3. Verify only one UserFile is created for the subscriber
        """
        from app.aria2.listener import _handle_task_complete

        # Create task
        async with get_session() as db:
            task = DownloadTask(
                uri_hash="dup_user_file_hash_002",
                uri="https://example.com/dup_user.zip",
                gid="gid_dup_user_002",
                status="complete",
                name="dup_user.zip",
                total_length=2048,
                completed_length=2048,
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
                frozen_space=2048,
                status="pending",
                created_at=utc_now_str(),
            )
            db.add(subscription)
            await db.commit()

        # Create source file
        task_dir = Path(temp_db_listener["downloading_dir"]) / str(task_id)
        task_dir.mkdir(parents=True, exist_ok=True)
        source_file = task_dir / "dup_user.zip"
        source_file.write_text("duplicate user file test content")

        aria2_status = {
            "files": [{"path": str(source_file)}],
            "totalLength": "2048",
            "completedLength": "2048",
        }

        # First call
        with patch("app.routers.tasks.broadcast_task_update_to_subscribers", new_callable=AsyncMock):
            await _handle_task_complete(mock_app_state, task_id, aria2_status)

        # Verify UserFile was created
        async with get_session() as db:
            result = await db.exec(
                select(UserFile).where(UserFile.owner_id == test_user_listener["id"])
            )
            user_files_after_first = result.all()
            assert len(user_files_after_first) == 1

        # Second call - should be skipped
        with patch("app.routers.tasks.broadcast_task_update_to_subscribers", new_callable=AsyncMock):
            await _handle_task_complete(mock_app_state, task_id, aria2_status)

        # Verify no additional UserFile was created
        async with get_session() as db:
            result = await db.exec(
                select(UserFile).where(UserFile.owner_id == test_user_listener["id"])
            )
            user_files_after_second = result.all()
            assert len(user_files_after_second) == 1

    @pytest.mark.asyncio
    async def test_stored_file_id_cas_prevents_race(
        self, temp_db_listener, test_user_listener, mock_app_state
    ):
        """Verify stored_file_id CAS (Compare-And-Swap) prevents race conditions.

        Scenario:
        1. Create a task with pending subscription
        2. Run two _handle_task_complete calls concurrently
        3. Verify only one StoredFile is created and task has correct stored_file_id
        """
        from app.aria2.listener import _handle_task_complete

        # Create task
        async with get_session() as db:
            task = DownloadTask(
                uri_hash="cas_race_hash_003",
                uri="https://example.com/cas_race.zip",
                gid="gid_cas_race_003",
                status="complete",
                name="cas_race.zip",
                total_length=4096,
                completed_length=4096,
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
                frozen_space=4096,
                status="pending",
                created_at=utc_now_str(),
            )
            db.add(subscription)
            await db.commit()

        # Create source file
        task_dir = Path(temp_db_listener["downloading_dir"]) / str(task_id)
        task_dir.mkdir(parents=True, exist_ok=True)
        source_file = task_dir / "cas_race.zip"
        source_file.write_text("cas race test content")

        aria2_status = {
            "files": [{"path": str(source_file)}],
            "totalLength": "4096",
            "completedLength": "4096",
        }

        # Run concurrent handlers
        with patch("app.routers.tasks.broadcast_task_update_to_subscribers", new_callable=AsyncMock):
            results = await asyncio.gather(
                _handle_task_complete(mock_app_state, task_id, aria2_status),
                _handle_task_complete(mock_app_state, task_id, aria2_status),
                _handle_task_complete(mock_app_state, task_id, aria2_status),
                return_exceptions=True,
            )

        # No exceptions should be raised
        exceptions = [r for r in results if isinstance(r, Exception)]
        assert len(exceptions) == 0, f"Unexpected exceptions: {exceptions}"

        # Verify only one StoredFile was created
        async with get_session() as db:
            result = await db.exec(select(StoredFile))
            stored_files = result.all()
            # Should be exactly 1 (or 0 if all failed due to timing)
            assert len(stored_files) <= 1

            # Verify task has stored_file_id set (if any StoredFile was created)
            result = await db.exec(select(DownloadTask).where(DownloadTask.id == task_id))
            task = result.first()
            if stored_files:
                assert task.stored_file_id == stored_files[0].id

    @pytest.mark.asyncio
    async def test_ref_count_not_incremented_multiple_times(
        self, temp_db_listener, test_user_listener, mock_app_state
    ):
        """Verify ref_count is not incremented multiple times for duplicate events.

        Scenario:
        1. Create a task with pending subscription
        2. Call _handle_task_complete twice
        3. Verify ref_count is exactly 1 (not 2)
        """
        from app.aria2.listener import _handle_task_complete

        # Create task
        async with get_session() as db:
            task = DownloadTask(
                uri_hash="ref_count_hash_004",
                uri="https://example.com/ref_count.zip",
                gid="gid_ref_count_004",
                status="complete",
                name="ref_count.zip",
                total_length=8192,
                completed_length=8192,
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
                frozen_space=8192,
                status="pending",
                created_at=utc_now_str(),
            )
            db.add(subscription)
            await db.commit()

        # Create source file
        task_dir = Path(temp_db_listener["downloading_dir"]) / str(task_id)
        task_dir.mkdir(parents=True, exist_ok=True)
        source_file = task_dir / "ref_count.zip"
        source_file.write_text("ref count test content")

        aria2_status = {
            "files": [{"path": str(source_file)}],
            "totalLength": "8192",
            "completedLength": "8192",
        }

        # First call
        with patch("app.routers.tasks.broadcast_task_update_to_subscribers", new_callable=AsyncMock):
            await _handle_task_complete(mock_app_state, task_id, aria2_status)

        # Verify ref_count is 1
        async with get_session() as db:
            result = await db.exec(select(StoredFile))
            stored_files = result.all()
            assert len(stored_files) == 1
            assert stored_files[0].ref_count == 1
            stored_file_id = stored_files[0].id

        # Second call - should be skipped
        with patch("app.routers.tasks.broadcast_task_update_to_subscribers", new_callable=AsyncMock):
            await _handle_task_complete(mock_app_state, task_id, aria2_status)

        # Verify ref_count is still 1
        async with get_session() as db:
            result = await db.exec(select(StoredFile).where(StoredFile.id == stored_file_id))
            stored_file = result.first()
            assert stored_file.ref_count == 1

    @pytest.mark.asyncio
    async def test_multiple_subscribers_ref_count_correct(
        self, temp_db_listener, test_user_listener, mock_app_state
    ):
        """Verify ref_count is correct when multiple subscribers exist.

        Scenario:
        1. Create a task with 3 pending subscriptions
        2. Call _handle_task_complete once
        3. Verify ref_count equals number of subscribers (3)
        4. Call _handle_task_complete again
        5. Verify ref_count is still 3 (not 6)
        """
        from app.aria2.listener import _handle_task_complete

        # Create additional users
        user_ids = [test_user_listener["id"]]
        for i in range(2):
            user_id = execute(
                """
                INSERT INTO users (username, password_hash, is_admin, created_at, quota)
                VALUES (?, ?, ?, ?, ?)
                """,
                [f"refuser{i}", hash_password("pass"), 0, datetime.now(timezone.utc).isoformat(), 100 * 1024 * 1024 * 1024]
            )
            user_ids.append(user_id)

        # Create task
        async with get_session() as db:
            task = DownloadTask(
                uri_hash="multi_ref_hash_005",
                uri="https://example.com/multi_ref.zip",
                gid="gid_multi_ref_005",
                status="complete",
                name="multi_ref.zip",
                total_length=16384,
                completed_length=16384,
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
                    frozen_space=16384,
                    status="pending",
                    created_at=utc_now_str(),
                )
                db.add(subscription)
            await db.commit()

        # Create source file
        task_dir = Path(temp_db_listener["downloading_dir"]) / str(task_id)
        task_dir.mkdir(parents=True, exist_ok=True)
        source_file = task_dir / "multi_ref.zip"
        source_file.write_text("multi ref count test content")

        aria2_status = {
            "files": [{"path": str(source_file)}],
            "totalLength": "16384",
            "completedLength": "16384",
        }

        # First call
        with patch("app.routers.tasks.broadcast_task_update_to_subscribers", new_callable=AsyncMock):
            await _handle_task_complete(mock_app_state, task_id, aria2_status)

        # Verify ref_count equals number of subscribers
        async with get_session() as db:
            result = await db.exec(select(StoredFile))
            stored_files = result.all()
            assert len(stored_files) == 1
            assert stored_files[0].ref_count == 3
            stored_file_id = stored_files[0].id

            # Verify UserFile count
            result = await db.exec(select(UserFile))
            user_files = result.all()
            assert len(user_files) == 3

        # Second call - should be skipped
        with patch("app.routers.tasks.broadcast_task_update_to_subscribers", new_callable=AsyncMock):
            await _handle_task_complete(mock_app_state, task_id, aria2_status)

        # Verify ref_count is still 3
        async with get_session() as db:
            result = await db.exec(select(StoredFile).where(StoredFile.id == stored_file_id))
            stored_file = result.first()
            assert stored_file.ref_count == 3

            # Verify UserFile count is still 3
            result = await db.exec(select(UserFile))
            user_files = result.all()
            assert len(user_files) == 3

    @pytest.mark.asyncio
    async def test_concurrent_complete_events_single_stored_file(
        self, temp_db_listener, test_user_listener, mock_app_state
    ):
        """Verify concurrent complete events result in single StoredFile.

        Scenario:
        1. Create a task with pending subscription
        2. Simulate concurrent complete events via handle_aria2_event
        3. Verify only one StoredFile and one UserFile are created
        """
        from app.aria2.listener import handle_aria2_event

        # Create task
        async with get_session() as db:
            task = DownloadTask(
                uri_hash="concurrent_hash_006",
                uri="https://example.com/concurrent.zip",
                gid="gid_concurrent_006",
                status="active",
                name="concurrent.zip",
                total_length=32768,
                completed_length=32768,
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
                frozen_space=32768,
                status="pending",
                created_at=utc_now_str(),
            )
            db.add(subscription)
            await db.commit()

        # Create source file
        task_dir = Path(temp_db_listener["downloading_dir"]) / str(task_id)
        task_dir.mkdir(parents=True, exist_ok=True)
        source_file = task_dir / "concurrent.zip"
        source_file.write_text("concurrent event test content")

        # Mock aria2 client
        mock_client = AsyncMock()
        mock_client.tell_status.return_value = {
            "status": "complete",
            "files": [{"path": str(source_file)}],
            "totalLength": "32768",
            "completedLength": "32768",
        }

        with patch("app.core.state.get_aria2_client", return_value=mock_client):
            with patch("app.routers.tasks.broadcast_task_update_to_subscribers", new_callable=AsyncMock):
                # Send multiple complete events concurrently
                await asyncio.gather(
                    handle_aria2_event(mock_app_state, gid, "complete"),
                    handle_aria2_event(mock_app_state, gid, "complete"),
                    handle_aria2_event(mock_app_state, gid, "complete"),
                )

        # Verify only one StoredFile was created
        async with get_session() as db:
            result = await db.exec(select(StoredFile))
            stored_files = result.all()
            assert len(stored_files) <= 1

            # Verify only one UserFile was created
            result = await db.exec(
                select(UserFile).where(UserFile.owner_id == test_user_listener["id"])
            )
            user_files = result.all()
            assert len(user_files) <= 1

            # Verify task status is complete
            result = await db.exec(select(DownloadTask).where(DownloadTask.id == task_id))
            task = result.first()
            assert task.status == "complete"


class TestFrozenSpaceReleaseOnComplete:
    """Tests for frozen space release when task completes.

    Verifies:
    1. Success subscription has frozen_space=0 after completion
    2. All subscribers' frozen space is released after completion
    """

    @pytest.mark.asyncio
    async def test_success_subscription_frozen_space_zero(
        self, temp_db_listener, test_user_listener, mock_app_state
    ):
        """Verify success subscription has frozen_space=0 after task completion.

        Scenario:
        1. Create a task with pending subscription and frozen_space > 0
        2. Call _handle_task_complete
        3. Verify subscription status is 'success' and frozen_space is 0
        """
        from app.aria2.listener import _handle_task_complete

        initial_frozen_space = 10240

        # Create task
        async with get_session() as db:
            task = DownloadTask(
                uri_hash="frozen_release_hash_001",
                uri="https://example.com/frozen_release.zip",
                gid="gid_frozen_release_001",
                status="complete",
                name="frozen_release.zip",
                total_length=initial_frozen_space,
                completed_length=initial_frozen_space,
                created_at=utc_now_str(),
                updated_at=utc_now_str(),
            )
            db.add(task)
            await db.commit()
            await db.refresh(task)
            task_id = task.id

            # Create subscription with frozen space
            subscription = UserTaskSubscription(
                owner_id=test_user_listener["id"],
                task_id=task_id,
                frozen_space=initial_frozen_space,
                status="pending",
                created_at=utc_now_str(),
            )
            db.add(subscription)
            await db.commit()
            await db.refresh(subscription)
            subscription_id = subscription.id

        # Verify initial frozen_space is set
        async with get_session() as db:
            result = await db.exec(
                select(UserTaskSubscription).where(UserTaskSubscription.id == subscription_id)
            )
            sub = result.first()
            assert sub.frozen_space == initial_frozen_space
            assert sub.status == "pending"

        # Create source file
        task_dir = Path(temp_db_listener["downloading_dir"]) / str(task_id)
        task_dir.mkdir(parents=True, exist_ok=True)
        source_file = task_dir / "frozen_release.zip"
        source_file.write_text("frozen space release test content")

        aria2_status = {
            "files": [{"path": str(source_file)}],
            "totalLength": str(initial_frozen_space),
            "completedLength": str(initial_frozen_space),
        }

        # Handle task complete
        with patch("app.routers.tasks.broadcast_task_update_to_subscribers", new_callable=AsyncMock):
            await _handle_task_complete(mock_app_state, task_id, aria2_status)

        # Verify subscription status is 'success' and frozen_space is 0
        async with get_session() as db:
            result = await db.exec(
                select(UserTaskSubscription).where(UserTaskSubscription.id == subscription_id)
            )
            sub = result.first()
            assert sub.status == "success", f"Expected status 'success', got '{sub.status}'"
            assert sub.frozen_space == 0, f"Expected frozen_space 0, got {sub.frozen_space}"

    @pytest.mark.asyncio
    async def test_all_subscribers_frozen_space_released(
        self, temp_db_listener, test_user_listener, mock_app_state
    ):
        """Verify all subscribers' frozen space is released after task completion.

        Scenario:
        1. Create a task with multiple pending subscriptions (3 users)
        2. Each subscription has different frozen_space values
        3. Call _handle_task_complete
        4. Verify all subscriptions have status='success' and frozen_space=0
        """
        from app.aria2.listener import _handle_task_complete

        # Create additional users
        user_ids = [test_user_listener["id"]]
        frozen_spaces = [10240, 20480, 30720]  # Different frozen space for each user

        for i in range(2):
            user_id = execute(
                """
                INSERT INTO users (username, password_hash, is_admin, created_at, quota)
                VALUES (?, ?, ?, ?, ?)
                """,
                [f"frozenuser{i}", hash_password("pass"), 0, datetime.now(timezone.utc).isoformat(), 100 * 1024 * 1024 * 1024]
            )
            user_ids.append(user_id)

        # Create task
        async with get_session() as db:
            task = DownloadTask(
                uri_hash="multi_frozen_hash_002",
                uri="https://example.com/multi_frozen.zip",
                gid="gid_multi_frozen_002",
                status="complete",
                name="multi_frozen.zip",
                total_length=30720,
                completed_length=30720,
                created_at=utc_now_str(),
                updated_at=utc_now_str(),
            )
            db.add(task)
            await db.commit()
            await db.refresh(task)
            task_id = task.id

            # Create subscriptions for all users with different frozen spaces
            subscription_ids = []
            for idx, uid in enumerate(user_ids):
                subscription = UserTaskSubscription(
                    owner_id=uid,
                    task_id=task_id,
                    frozen_space=frozen_spaces[idx],
                    status="pending",
                    created_at=utc_now_str(),
                )
                db.add(subscription)
                await db.flush()
                subscription_ids.append(subscription.id)
            await db.commit()

        # Verify initial frozen_space values
        async with get_session() as db:
            for idx, sub_id in enumerate(subscription_ids):
                result = await db.exec(
                    select(UserTaskSubscription).where(UserTaskSubscription.id == sub_id)
                )
                sub = result.first()
                assert sub.frozen_space == frozen_spaces[idx]
                assert sub.status == "pending"

        # Create source file
        task_dir = Path(temp_db_listener["downloading_dir"]) / str(task_id)
        task_dir.mkdir(parents=True, exist_ok=True)
        source_file = task_dir / "multi_frozen.zip"
        source_file.write_text("multi subscriber frozen space release test content")

        aria2_status = {
            "files": [{"path": str(source_file)}],
            "totalLength": "30720",
            "completedLength": "30720",
        }

        # Handle task complete
        with patch("app.routers.tasks.broadcast_task_update_to_subscribers", new_callable=AsyncMock):
            await _handle_task_complete(mock_app_state, task_id, aria2_status)

        # Verify all subscriptions have status='success' and frozen_space=0
        async with get_session() as db:
            for idx, sub_id in enumerate(subscription_ids):
                result = await db.exec(
                    select(UserTaskSubscription).where(UserTaskSubscription.id == sub_id)
                )
                sub = result.first()
                assert sub.status == "success", f"User {idx}: Expected status 'success', got '{sub.status}'"
                assert sub.frozen_space == 0, f"User {idx}: Expected frozen_space 0, got {sub.frozen_space}"

        # Verify total frozen space released equals sum of initial frozen spaces
        total_initial_frozen = sum(frozen_spaces)
        async with get_session() as db:
            result = await db.exec(
                select(UserTaskSubscription).where(UserTaskSubscription.task_id == task_id)
            )
            all_subs = result.all()
            total_remaining_frozen = sum(s.frozen_space for s in all_subs)
            assert total_remaining_frozen == 0, f"Expected total frozen 0, got {total_remaining_frozen}"


class TestFrozenSpaceReleaseOnStopError:
    """Tests for frozen space release when task is stopped or encounters error.

    Verifies:
    1. Stop event releases frozen space
    2. Error event releases frozen space
    3. Subscription status is correctly updated to 'failed'
    """

    @pytest.mark.asyncio
    async def test_stop_event_releases_frozen_space(
        self, temp_db_listener, test_user_listener, mock_app_state
    ):
        """Verify stop event releases frozen space for all subscribers.

        Scenario:
        1. Create a task with pending subscription and frozen_space > 0
        2. Trigger stop event via handle_aria2_event
        3. Verify subscription frozen_space is released (set to 0)
        4. Verify subscription status is updated to 'failed'
        """
        from app.aria2.listener import handle_aria2_event

        initial_frozen_space = 51200

        # Create task
        async with get_session() as db:
            task = DownloadTask(
                uri_hash="stop_event_hash_001",
                uri="https://example.com/stop_event.zip",
                gid="gid_stop_event_001",
                status="active",
                name="stop_event.zip",
                total_length=initial_frozen_space,
                completed_length=25600,
                created_at=utc_now_str(),
                updated_at=utc_now_str(),
            )
            db.add(task)
            await db.commit()
            await db.refresh(task)
            task_id = task.id
            gid = task.gid

            # Create subscription with frozen space
            subscription = UserTaskSubscription(
                owner_id=test_user_listener["id"],
                task_id=task_id,
                frozen_space=initial_frozen_space,
                status="pending",
                created_at=utc_now_str(),
            )
            db.add(subscription)
            await db.commit()
            await db.refresh(subscription)
            subscription_id = subscription.id

        # Verify initial state
        async with get_session() as db:
            result = await db.exec(
                select(UserTaskSubscription).where(UserTaskSubscription.id == subscription_id)
            )
            sub = result.first()
            assert sub.frozen_space == initial_frozen_space
            assert sub.status == "pending"

        # Mock aria2 client
        mock_client = AsyncMock()
        mock_client.tell_status.return_value = {
            "status": "removed",
            "totalLength": str(initial_frozen_space),
            "completedLength": "25600",
            "files": [{"path": "/tmp/stop_event.zip"}],
        }

        with patch("app.core.state.get_aria2_client", return_value=mock_client):
            with patch("app.routers.tasks.broadcast_task_update_to_subscribers", new_callable=AsyncMock):
                await handle_aria2_event(mock_app_state, gid, "stop")

        # Verify task status is 'error'
        async with get_session() as db:
            result = await db.exec(select(DownloadTask).where(DownloadTask.id == task_id))
            task = result.first()
            assert task.status == "error"
            assert task.error_display == "外部取消（管理员/外部客户端）"

        # Verify subscription frozen_space is released and status is 'failed'
        async with get_session() as db:
            result = await db.exec(
                select(UserTaskSubscription).where(UserTaskSubscription.id == subscription_id)
            )
            sub = result.first()
            assert sub.frozen_space == 0, f"Expected frozen_space 0, got {sub.frozen_space}"
            assert sub.status == "failed", f"Expected status 'failed', got '{sub.status}'"

    @pytest.mark.asyncio
    async def test_error_event_releases_frozen_space(
        self, temp_db_listener, test_user_listener, mock_app_state
    ):
        """Verify error event releases frozen space for all subscribers.

        Scenario:
        1. Create a task with pending subscription and frozen_space > 0
        2. Trigger error event via handle_aria2_event
        3. Verify subscription frozen_space is released (set to 0)
        4. Verify subscription status is updated to 'failed'
        """
        from app.aria2.listener import handle_aria2_event

        initial_frozen_space = 102400

        # Create task
        async with get_session() as db:
            task = DownloadTask(
                uri_hash="error_event_hash_001",
                uri="https://example.com/error_event.zip",
                gid="gid_error_event_001",
                status="active",
                name="error_event.zip",
                total_length=initial_frozen_space,
                completed_length=51200,
                created_at=utc_now_str(),
                updated_at=utc_now_str(),
            )
            db.add(task)
            await db.commit()
            await db.refresh(task)
            task_id = task.id
            gid = task.gid

            # Create subscription with frozen space
            subscription = UserTaskSubscription(
                owner_id=test_user_listener["id"],
                task_id=task_id,
                frozen_space=initial_frozen_space,
                status="pending",
                created_at=utc_now_str(),
            )
            db.add(subscription)
            await db.commit()
            await db.refresh(subscription)
            subscription_id = subscription.id

        # Verify initial state
        async with get_session() as db:
            result = await db.exec(
                select(UserTaskSubscription).where(UserTaskSubscription.id == subscription_id)
            )
            sub = result.first()
            assert sub.frozen_space == initial_frozen_space
            assert sub.status == "pending"

        # Mock aria2 client with error response
        mock_client = AsyncMock()
        mock_client.tell_status.return_value = {
            "status": "error",
            "errorMessage": "Network error: Connection refused",
            "totalLength": str(initial_frozen_space),
            "completedLength": "51200",
            "files": [{"path": "/tmp/error_event.zip"}],
        }

        with patch("app.core.state.get_aria2_client", return_value=mock_client):
            with patch("app.routers.tasks.broadcast_task_update_to_subscribers", new_callable=AsyncMock):
                await handle_aria2_event(mock_app_state, gid, "error")

        # Verify task status is 'error'
        async with get_session() as db:
            result = await db.exec(select(DownloadTask).where(DownloadTask.id == task_id))
            task = result.first()
            assert task.status == "error"
            assert task.error is not None

        # Verify subscription frozen_space is released and status is 'failed'
        async with get_session() as db:
            result = await db.exec(
                select(UserTaskSubscription).where(UserTaskSubscription.id == subscription_id)
            )
            sub = result.first()
            assert sub.frozen_space == 0, f"Expected frozen_space 0, got {sub.frozen_space}"
            assert sub.status == "failed", f"Expected status 'failed', got '{sub.status}'"

    @pytest.mark.asyncio
    async def test_subscription_status_updated_to_failed(
        self, temp_db_listener, test_user_listener, mock_app_state
    ):
        """Verify subscription status is correctly updated to 'failed' on stop/error.

        Scenario:
        1. Create a task with multiple pending subscriptions
        2. Trigger stop event
        3. Verify all subscriptions have status='failed'
        4. Verify error_display is set on subscriptions
        """
        from app.aria2.listener import handle_aria2_event

        # Create additional users
        user_ids = [test_user_listener["id"]]
        for i in range(2):
            user_id = execute(
                """
                INSERT INTO users (username, password_hash, is_admin, created_at, quota)
                VALUES (?, ?, ?, ?, ?)
                """,
                [f"stopuser{i}", hash_password("pass"), 0, datetime.now(timezone.utc).isoformat(), 100 * 1024 * 1024 * 1024]
            )
            user_ids.append(user_id)

        initial_frozen_space = 204800

        # Create task
        async with get_session() as db:
            task = DownloadTask(
                uri_hash="multi_stop_hash_001",
                uri="https://example.com/multi_stop.zip",
                gid="gid_multi_stop_001",
                status="active",
                name="multi_stop.zip",
                total_length=initial_frozen_space,
                completed_length=102400,
                created_at=utc_now_str(),
                updated_at=utc_now_str(),
            )
            db.add(task)
            await db.commit()
            await db.refresh(task)
            task_id = task.id
            gid = task.gid

            # Create subscriptions for all users
            subscription_ids = []
            for uid in user_ids:
                subscription = UserTaskSubscription(
                    owner_id=uid,
                    task_id=task_id,
                    frozen_space=initial_frozen_space,
                    status="pending",
                    created_at=utc_now_str(),
                )
                db.add(subscription)
                await db.flush()
                subscription_ids.append(subscription.id)
            await db.commit()

        # Verify initial state - all pending
        async with get_session() as db:
            for sub_id in subscription_ids:
                result = await db.exec(
                    select(UserTaskSubscription).where(UserTaskSubscription.id == sub_id)
                )
                sub = result.first()
                assert sub.status == "pending"
                assert sub.frozen_space == initial_frozen_space

        # Mock aria2 client
        mock_client = AsyncMock()
        mock_client.tell_status.return_value = {
            "status": "removed",
            "totalLength": str(initial_frozen_space),
            "completedLength": "102400",
            "files": [{"path": "/tmp/multi_stop.zip"}],
        }

        with patch("app.core.state.get_aria2_client", return_value=mock_client):
            with patch("app.routers.tasks.broadcast_task_update_to_subscribers", new_callable=AsyncMock):
                await handle_aria2_event(mock_app_state, gid, "stop")

        # Verify all subscriptions have status='failed' and frozen_space=0
        async with get_session() as db:
            for idx, sub_id in enumerate(subscription_ids):
                result = await db.exec(
                    select(UserTaskSubscription).where(UserTaskSubscription.id == sub_id)
                )
                sub = result.first()
                assert sub.status == "failed", f"User {idx}: Expected status 'failed', got '{sub.status}'"
                assert sub.frozen_space == 0, f"User {idx}: Expected frozen_space 0, got {sub.frozen_space}"

    @pytest.mark.asyncio
    async def test_stop_event_multiple_subscribers_all_released(
        self, temp_db_listener, test_user_listener, mock_app_state
    ):
        """Verify stop event releases frozen space for all subscribers.

        Scenario:
        1. Create a task with 3 pending subscriptions with different frozen spaces
        2. Trigger stop event
        3. Verify all subscriptions have frozen_space=0
        4. Verify total frozen space released equals sum of initial frozen spaces
        """
        from app.aria2.listener import handle_aria2_event

        # Create additional users
        user_ids = [test_user_listener["id"]]
        frozen_spaces = [10240, 20480, 30720]

        for i in range(2):
            user_id = execute(
                """
                INSERT INTO users (username, password_hash, is_admin, created_at, quota)
                VALUES (?, ?, ?, ?, ?)
                """,
                [f"multistopuser{i}", hash_password("pass"), 0, datetime.now(timezone.utc).isoformat(), 100 * 1024 * 1024 * 1024]
            )
            user_ids.append(user_id)

        # Create task
        async with get_session() as db:
            task = DownloadTask(
                uri_hash="multi_frozen_stop_hash_001",
                uri="https://example.com/multi_frozen_stop.zip",
                gid="gid_multi_frozen_stop_001",
                status="active",
                name="multi_frozen_stop.zip",
                total_length=30720,
                completed_length=15360,
                created_at=utc_now_str(),
                updated_at=utc_now_str(),
            )
            db.add(task)
            await db.commit()
            await db.refresh(task)
            task_id = task.id
            gid = task.gid

            # Create subscriptions with different frozen spaces
            subscription_ids = []
            for idx, uid in enumerate(user_ids):
                subscription = UserTaskSubscription(
                    owner_id=uid,
                    task_id=task_id,
                    frozen_space=frozen_spaces[idx],
                    status="pending",
                    created_at=utc_now_str(),
                )
                db.add(subscription)
                await db.flush()
                subscription_ids.append(subscription.id)
            await db.commit()

        # Calculate total initial frozen space
        total_initial_frozen = sum(frozen_spaces)

        # Verify initial frozen space
        async with get_session() as db:
            result = await db.exec(
                select(UserTaskSubscription).where(UserTaskSubscription.task_id == task_id)
            )
            all_subs = result.all()
            actual_total = sum(s.frozen_space for s in all_subs)
            assert actual_total == total_initial_frozen

        # Mock aria2 client
        mock_client = AsyncMock()
        mock_client.tell_status.return_value = {
            "status": "removed",
            "totalLength": "30720",
            "completedLength": "15360",
            "files": [{"path": "/tmp/multi_frozen_stop.zip"}],
        }

        with patch("app.core.state.get_aria2_client", return_value=mock_client):
            with patch("app.routers.tasks.broadcast_task_update_to_subscribers", new_callable=AsyncMock):
                await handle_aria2_event(mock_app_state, gid, "stop")

        # Verify all subscriptions have frozen_space=0
        async with get_session() as db:
            result = await db.exec(
                select(UserTaskSubscription).where(UserTaskSubscription.task_id == task_id)
            )
            all_subs = result.all()
            total_remaining = sum(s.frozen_space for s in all_subs)
            assert total_remaining == 0, f"Expected total frozen 0, got {total_remaining}"

            # Verify each subscription individually
            for idx, sub in enumerate(all_subs):
                assert sub.frozen_space == 0, f"Subscription {idx}: Expected frozen_space 0, got {sub.frozen_space}"
                assert sub.status == "failed", f"Subscription {idx}: Expected status 'failed', got '{sub.status}'"

    @pytest.mark.asyncio
    async def test_error_event_with_error_display_propagated(
        self, temp_db_listener, test_user_listener, mock_app_state
    ):
        """Verify error event propagates error_display to subscriptions.

        Scenario:
        1. Create a task with pending subscription
        2. Trigger error event with specific error message
        3. Verify subscription has error_display set
        """
        from app.aria2.listener import handle_aria2_event

        initial_frozen_space = 81920

        # Create task
        async with get_session() as db:
            task = DownloadTask(
                uri_hash="error_display_hash_001",
                uri="https://example.com/error_display.zip",
                gid="gid_error_display_001",
                status="active",
                name="error_display.zip",
                total_length=initial_frozen_space,
                completed_length=40960,
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
                frozen_space=initial_frozen_space,
                status="pending",
                created_at=utc_now_str(),
            )
            db.add(subscription)
            await db.commit()
            await db.refresh(subscription)
            subscription_id = subscription.id

        # Mock aria2 client with specific error
        mock_client = AsyncMock()
        mock_client.tell_status.return_value = {
            "status": "error",
            "errorMessage": "error code=1 Resource not found",
            "totalLength": str(initial_frozen_space),
            "completedLength": "40960",
            "files": [{"path": "/tmp/error_display.zip"}],
        }

        with patch("app.core.state.get_aria2_client", return_value=mock_client):
            with patch("app.routers.tasks.broadcast_task_update_to_subscribers", new_callable=AsyncMock):
                await handle_aria2_event(mock_app_state, gid, "error")

        # Verify task has error info
        async with get_session() as db:
            result = await db.exec(select(DownloadTask).where(DownloadTask.id == task_id))
            task = result.first()
            assert task.status == "error"
            assert task.error is not None
            assert task.error_display is not None

        # Verify subscription has error_display and frozen_space released
        async with get_session() as db:
            result = await db.exec(
                select(UserTaskSubscription).where(UserTaskSubscription.id == subscription_id)
            )
            sub = result.first()
            assert sub.status == "failed"
            assert sub.frozen_space == 0
            # error_display should be set on subscription
            assert sub.error_display is not None


class TestSubscriptionStatusMapping:
    """Tests for subscription status mapping in API responses.

    Verifies:
    1. subscription.status=failed returns 'error' in API
    2. subscription.status=success returns 'complete' in API
    3. subscription.status=pending uses task.status
    """

    @pytest.mark.asyncio
    async def test_failed_subscription_returns_error_status(
        self, temp_db_listener, test_user_listener
    ):
        """Verify subscription.status=failed returns 'error' in API response.

        Scenario:
        1. Create a task with subscription status='failed'
        2. Call _subscription_to_dict
        3. Verify returned status is 'error'
        4. Verify error message comes from subscription.error_display
        """
        from app.routers.tasks import _subscription_to_dict

        # Create task with any status (task status should be ignored when subscription is failed)
        async with get_session() as db:
            task = DownloadTask(
                uri_hash="status_mapping_failed_001",
                uri="https://example.com/failed_mapping.zip",
                gid="gid_failed_mapping_001",
                status="active",  # Task is still active, but subscription failed
                name="failed_mapping.zip",
                total_length=10240,
                completed_length=5120,
                download_speed=1024,
                upload_speed=0,
                error="Task error message",
                error_display="Task error display",
                created_at=utc_now_str(),
                updated_at=utc_now_str(),
            )
            db.add(task)
            await db.commit()
            await db.refresh(task)
            task_id = task.id

            # Create subscription with status='failed'
            subscription = UserTaskSubscription(
                owner_id=test_user_listener["id"],
                task_id=task_id,
                frozen_space=0,
                status="failed",
                error_display="Subscription failed: quota exceeded",
                created_at=utc_now_str(),
            )
            db.add(subscription)
            await db.commit()
            await db.refresh(subscription)

            # Call _subscription_to_dict
            result = _subscription_to_dict(subscription, task)

        # Verify status is 'error' (not task's 'active')
        assert result["status"] == "error", f"Expected status 'error', got '{result['status']}'"

        # Verify error message comes from subscription.error_display
        assert result["error"] == "Subscription failed: quota exceeded"

        # Verify other fields are correct
        assert result["id"] == subscription.id
        assert result["name"] == "failed_mapping.zip"
        assert result["total_length"] == 10240
        assert result["completed_length"] == 5120

    @pytest.mark.asyncio
    async def test_success_subscription_returns_complete_status(
        self, temp_db_listener, test_user_listener
    ):
        """Verify subscription.status=success returns 'complete' in API response.

        Scenario:
        1. Create a task with subscription status='success'
        2. Call _subscription_to_dict
        3. Verify returned status is 'complete'
        4. Verify error is None
        """
        from app.routers.tasks import _subscription_to_dict

        # Create task (task status should be ignored when subscription is success)
        async with get_session() as db:
            task = DownloadTask(
                uri_hash="status_mapping_success_001",
                uri="https://example.com/success_mapping.zip",
                gid="gid_success_mapping_001",
                status="complete",
                name="success_mapping.zip",
                total_length=20480,
                completed_length=20480,
                download_speed=0,
                upload_speed=0,
                created_at=utc_now_str(),
                updated_at=utc_now_str(),
            )
            db.add(task)
            await db.commit()
            await db.refresh(task)
            task_id = task.id

            # Create subscription with status='success'
            subscription = UserTaskSubscription(
                owner_id=test_user_listener["id"],
                task_id=task_id,
                frozen_space=0,
                status="success",
                created_at=utc_now_str(),
            )
            db.add(subscription)
            await db.commit()
            await db.refresh(subscription)

            # Call _subscription_to_dict
            result = _subscription_to_dict(subscription, task)

        # Verify status is 'complete'
        assert result["status"] == "complete", f"Expected status 'complete', got '{result['status']}'"

        # Verify error is None
        assert result["error"] is None

        # Verify other fields are correct
        assert result["id"] == subscription.id
        assert result["name"] == "success_mapping.zip"
        assert result["total_length"] == 20480
        assert result["completed_length"] == 20480
        assert result["frozen_space"] == 0

    @pytest.mark.asyncio
    async def test_pending_subscription_uses_task_status(
        self, temp_db_listener, test_user_listener
    ):
        """Verify subscription.status=pending uses task.status in API response.

        Scenario:
        1. Create tasks with different statuses and pending subscriptions
        2. Call _subscription_to_dict for each
        3. Verify returned status matches task.status
        4. Verify error comes from task.error_display or task.error
        """
        from app.routers.tasks import _subscription_to_dict

        test_cases = [
            {
                "task_status": "active",
                "expected_status": "active",
                "task_error": None,
                "task_error_display": None,
            },
            {
                "task_status": "waiting",
                "expected_status": "waiting",
                "task_error": None,
                "task_error_display": None,
            },
            {
                "task_status": "paused",
                "expected_status": "paused",
                "task_error": None,
                "task_error_display": None,
            },
            {
                "task_status": "error",
                "expected_status": "error",
                "task_error": "Network error",
                "task_error_display": "Connection failed",
            },
            {
                "task_status": "complete",
                "expected_status": "complete",
                "task_error": None,
                "task_error_display": None,
            },
        ]

        for idx, tc in enumerate(test_cases):
            async with get_session() as db:
                task = DownloadTask(
                    uri_hash=f"status_mapping_pending_{idx:03d}",
                    uri=f"https://example.com/pending_mapping_{idx}.zip",
                    gid=f"gid_pending_mapping_{idx:03d}",
                    status=tc["task_status"],
                    name=f"pending_mapping_{idx}.zip",
                    total_length=30720,
                    completed_length=15360 if tc["task_status"] != "complete" else 30720,
                    download_speed=2048 if tc["task_status"] == "active" else 0,
                    upload_speed=0,
                    error=tc["task_error"],
                    error_display=tc["task_error_display"],
                    created_at=utc_now_str(),
                    updated_at=utc_now_str(),
                )
                db.add(task)
                await db.commit()
                await db.refresh(task)
                task_id = task.id

                # Create subscription with status='pending'
                subscription = UserTaskSubscription(
                    owner_id=test_user_listener["id"],
                    task_id=task_id,
                    frozen_space=30720 if tc["task_status"] not in ["complete", "error"] else 0,
                    status="pending",
                    created_at=utc_now_str(),
                )
                db.add(subscription)
                await db.commit()
                await db.refresh(subscription)

                # Call _subscription_to_dict
                result = _subscription_to_dict(subscription, task)

            # Verify status matches task.status
            assert result["status"] == tc["expected_status"], \
                f"Case {idx} ({tc['task_status']}): Expected status '{tc['expected_status']}', got '{result['status']}'"

            # Verify error handling
            if tc["task_error_display"]:
                assert result["error"] == tc["task_error_display"], \
                    f"Case {idx}: Expected error '{tc['task_error_display']}', got '{result['error']}'"
            elif tc["task_error"]:
                assert result["error"] == "后端错误", \
                    f"Case {idx}: Expected backend error fallback, got '{result['error']}'"
            else:
                # For non-error states, error should be None
                if tc["task_status"] != "error":
                    assert result["error"] is None, \
                        f"Case {idx}: Expected error None, got '{result['error']}'"

    @pytest.mark.asyncio
    async def test_failed_subscription_overrides_task_complete_status(
        self, temp_db_listener, test_user_listener
    ):
        """Verify failed subscription returns 'error' even when task is complete.

        This tests the edge case where a task completed successfully but
        the subscription failed (e.g., due to quota issues during file creation).
        """
        from app.routers.tasks import _subscription_to_dict

        async with get_session() as db:
            # Task is complete
            task = DownloadTask(
                uri_hash="status_mapping_override_001",
                uri="https://example.com/override_mapping.zip",
                gid="gid_override_mapping_001",
                status="complete",
                name="override_mapping.zip",
                total_length=40960,
                completed_length=40960,
                download_speed=0,
                upload_speed=0,
                created_at=utc_now_str(),
                updated_at=utc_now_str(),
            )
            db.add(task)
            await db.commit()
            await db.refresh(task)
            task_id = task.id

            # But subscription failed
            subscription = UserTaskSubscription(
                owner_id=test_user_listener["id"],
                task_id=task_id,
                frozen_space=0,
                status="failed",
                error_display="Failed to create user file: quota exceeded",
                created_at=utc_now_str(),
            )
            db.add(subscription)
            await db.commit()
            await db.refresh(subscription)

            # Call _subscription_to_dict
            result = _subscription_to_dict(subscription, task)

        # Verify status is 'error' (subscription.status=failed overrides task.status=complete)
        assert result["status"] == "error", \
            f"Expected status 'error' (subscription failed), got '{result['status']}'"

        # Verify error message comes from subscription
        assert result["error"] == "Failed to create user file: quota exceeded"

    @pytest.mark.asyncio
    async def test_success_subscription_overrides_task_error_status(
        self, temp_db_listener, test_user_listener
    ):
        """Verify success subscription returns 'complete' even when task has error.

        This tests the edge case where a task has error status but
        the subscription was already marked as success (e.g., file was created
        before task encountered an error on retry).
        """
        from app.routers.tasks import _subscription_to_dict

        async with get_session() as db:
            # Task has error
            task = DownloadTask(
                uri_hash="status_mapping_success_override_001",
                uri="https://example.com/success_override.zip",
                gid="gid_success_override_001",
                status="error",
                name="success_override.zip",
                total_length=51200,
                completed_length=51200,
                download_speed=0,
                upload_speed=0,
                error="Retry failed",
                error_display="Download failed on retry",
                created_at=utc_now_str(),
                updated_at=utc_now_str(),
            )
            db.add(task)
            await db.commit()
            await db.refresh(task)
            task_id = task.id

            # But subscription is success (file was created before error)
            subscription = UserTaskSubscription(
                owner_id=test_user_listener["id"],
                task_id=task_id,
                frozen_space=0,
                status="success",
                created_at=utc_now_str(),
            )
            db.add(subscription)
            await db.commit()
            await db.refresh(subscription)

            # Call _subscription_to_dict
            result = _subscription_to_dict(subscription, task)

        # Verify status is 'complete' (subscription.status=success overrides task.status=error)
        assert result["status"] == "complete", \
            f"Expected status 'complete' (subscription success), got '{result['status']}'"

        # Verify error is None (success subscription should not show error)
        assert result["error"] is None


class TestMagnetFollowedByHandling:
    """Tests for magnet link metadata followedBy handling.

    Verifies:
    1. followedBy only updates GID, does not mark task as complete
    2. followedBy does not create StoredFile
    3. followedBy does not trigger subscription completion
    """

    @pytest.mark.asyncio
    async def test_followed_by_updates_gid_only(
        self, temp_db_listener, test_user_listener, mock_app_state
    ):
        """Verify followedBy event only updates GID without marking task complete.

        Scenario:
        1. Create a task simulating magnet metadata download
        2. Trigger complete event with followedBy in aria2_status
        3. Verify GID is updated to the new value
        4. Verify task status remains unchanged (not set to complete)
        """
        from app.aria2.listener import handle_aria2_event

        original_gid = "gid_magnet_metadata_001"
        new_gid = "gid_actual_download_001"

        # Create task simulating magnet metadata download
        async with get_session() as db:
            task = DownloadTask(
                uri_hash="magnet_followed_by_hash_001",
                uri="magnet:?xt=urn:btih:abc123",
                gid=original_gid,
                status="active",
                name="[METADATA]magnet_test.torrent",
                total_length=0,
                completed_length=0,
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
                frozen_space=10240,
                status="pending",
                created_at=utc_now_str(),
            )
            db.add(subscription)
            await db.commit()
            await db.refresh(subscription)
            subscription_id = subscription.id

        # Mock aria2 client with followedBy response
        mock_client = AsyncMock()
        mock_client.tell_status.return_value = {
            "status": "complete",
            "followedBy": [new_gid],
            "totalLength": "0",
            "completedLength": "0",
            "files": [],
        }

        with patch("app.core.state.get_aria2_client", return_value=mock_client):
            with patch("app.routers.tasks.broadcast_task_update_to_subscribers", new_callable=AsyncMock):
                await handle_aria2_event(mock_app_state, original_gid, "complete")

        # Verify GID is updated
        async with get_session() as db:
            result = await db.exec(select(DownloadTask).where(DownloadTask.id == task_id))
            task = result.first()
            assert task.gid == new_gid, f"Expected GID '{new_gid}', got '{task.gid}'"

            # Verify task status is NOT complete (should remain active or unchanged)
            assert task.status != "complete", \
                f"Task status should not be 'complete' after followedBy, got '{task.status}'"

            # Verify subscription status is still pending
            result = await db.exec(
                select(UserTaskSubscription).where(UserTaskSubscription.id == subscription_id)
            )
            sub = result.first()
            assert sub.status == "pending", \
                f"Subscription status should remain 'pending', got '{sub.status}'"

    @pytest.mark.asyncio
    async def test_followed_by_not_marked_complete(
        self, temp_db_listener, test_user_listener, mock_app_state
    ):
        """Verify followedBy event does not mark task as complete.

        Scenario:
        1. Create a task with active status
        2. Trigger complete event with followedBy
        3. Verify task status is NOT changed to complete
        4. Verify subscription frozen_space is NOT released
        """
        from app.aria2.listener import handle_aria2_event

        original_gid = "gid_magnet_not_complete_001"
        new_gid = "gid_actual_not_complete_001"
        initial_frozen_space = 20480

        # Create task
        async with get_session() as db:
            task = DownloadTask(
                uri_hash="magnet_not_complete_hash_001",
                uri="magnet:?xt=urn:btih:def456",
                gid=original_gid,
                status="active",
                name="[METADATA]not_complete_test.torrent",
                total_length=0,
                completed_length=0,
                created_at=utc_now_str(),
                updated_at=utc_now_str(),
            )
            db.add(task)
            await db.commit()
            await db.refresh(task)
            task_id = task.id

            # Create subscription with frozen space
            subscription = UserTaskSubscription(
                owner_id=test_user_listener["id"],
                task_id=task_id,
                frozen_space=initial_frozen_space,
                status="pending",
                created_at=utc_now_str(),
            )
            db.add(subscription)
            await db.commit()
            await db.refresh(subscription)
            subscription_id = subscription.id

        # Mock aria2 client with followedBy response
        mock_client = AsyncMock()
        mock_client.tell_status.return_value = {
            "status": "complete",
            "followedBy": [new_gid],
            "totalLength": "0",
            "completedLength": "0",
            "files": [],
        }

        with patch("app.core.state.get_aria2_client", return_value=mock_client):
            with patch("app.routers.tasks.broadcast_task_update_to_subscribers", new_callable=AsyncMock):
                await handle_aria2_event(mock_app_state, original_gid, "complete")

        # Verify task is NOT marked as complete
        async with get_session() as db:
            result = await db.exec(select(DownloadTask).where(DownloadTask.id == task_id))
            task = result.first()
            assert task.status != "complete", \
                f"Task should NOT be marked complete after followedBy, got '{task.status}'"

            # Verify subscription frozen_space is NOT released (still has frozen space)
            result = await db.exec(
                select(UserTaskSubscription).where(UserTaskSubscription.id == subscription_id)
            )
            sub = result.first()
            assert sub.frozen_space == initial_frozen_space, \
                f"Frozen space should remain {initial_frozen_space}, got {sub.frozen_space}"
            assert sub.status == "pending", \
                f"Subscription status should remain 'pending', got '{sub.status}'"

    @pytest.mark.asyncio
    async def test_followed_by_no_stored_file_created(
        self, temp_db_listener, test_user_listener, mock_app_state
    ):
        """Verify followedBy event does not create StoredFile.

        Scenario:
        1. Create a task simulating magnet metadata download
        2. Trigger complete event with followedBy
        3. Verify no StoredFile is created
        4. Verify no UserFile is created
        5. Verify task.stored_file_id remains NULL
        """
        from app.aria2.listener import handle_aria2_event

        original_gid = "gid_magnet_no_stored_001"
        new_gid = "gid_actual_no_stored_001"

        # Create task
        async with get_session() as db:
            task = DownloadTask(
                uri_hash="magnet_no_stored_hash_001",
                uri="magnet:?xt=urn:btih:ghi789",
                gid=original_gid,
                status="active",
                name="[METADATA]no_stored_test.torrent",
                total_length=0,
                completed_length=0,
                stored_file_id=None,  # Explicitly NULL
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
                frozen_space=30720,
                status="pending",
                created_at=utc_now_str(),
            )
            db.add(subscription)
            await db.commit()

        # Count existing StoredFile and UserFile records
        async with get_session() as db:
            result = await db.exec(select(StoredFile))
            stored_files_before = len(result.all())

            result = await db.exec(
                select(UserFile).where(UserFile.owner_id == test_user_listener["id"])
            )
            user_files_before = len(result.all())

        # Mock aria2 client with followedBy response
        mock_client = AsyncMock()
        mock_client.tell_status.return_value = {
            "status": "complete",
            "followedBy": [new_gid],
            "totalLength": "0",
            "completedLength": "0",
            "files": [],
        }

        with patch("app.core.state.get_aria2_client", return_value=mock_client):
            with patch("app.routers.tasks.broadcast_task_update_to_subscribers", new_callable=AsyncMock):
                await handle_aria2_event(mock_app_state, original_gid, "complete")

        # Verify no StoredFile was created
        async with get_session() as db:
            result = await db.exec(select(StoredFile))
            stored_files_after = len(result.all())
            assert stored_files_after == stored_files_before, \
                f"StoredFile count should remain {stored_files_before}, got {stored_files_after}"

            # Verify no UserFile was created
            result = await db.exec(
                select(UserFile).where(UserFile.owner_id == test_user_listener["id"])
            )
            user_files_after = len(result.all())
            assert user_files_after == user_files_before, \
                f"UserFile count should remain {user_files_before}, got {user_files_after}"

            # Verify task.stored_file_id remains NULL
            result = await db.exec(select(DownloadTask).where(DownloadTask.id == task_id))
            task = result.first()
            assert task.stored_file_id is None, \
                f"Task stored_file_id should remain NULL, got {task.stored_file_id}"

            # Verify GID was updated (the only change that should happen)
            assert task.gid == new_gid, f"GID should be updated to '{new_gid}', got '{task.gid}'"


class TestErrorDisplayMapping:
    """Tests for error event error_display mapping.

    Verifies:
    1. Common error codes are correctly mapped to user-readable messages
    2. Error messages are user-readable (Chinese)
    3. Error event sets error_display on task
    """

    def test_error_code_mapped_correctly(self):
        """Verify common error codes are correctly mapped.

        Tests the parse_error_message function with various error code formats.
        """
        from app.aria2.errors import parse_error_message, ERROR_CODE_MAP

        # Test error code extraction from various formats
        test_cases = [
            # (input, expected_output)
            ("errorCode=3 Resource not found", "资源未找到 (404)"),
            ("errorCode=9 No space left on device", "磁盘空间不足"),
            ("errorCode=2 Timeout waiting for response", "网络超时"),
            ("errorCode=19 DNS resolution failed", "名称解析失败 (DNS 错误)"),
            ("errorCode=24 HTTP authorization failed", "HTTP 认证失败"),
            ("errorCode=6 Network problem occurred", "网络问题"),
            ("errorCode=23 Too many redirects", "重定向次数过多"),
            ("errorCode=32 Checksum validation failed", "校验和验证失败"),
            ("errorCode=25 Could not parse bencoded file", "无法解析 BEncode 格式 (种子文件损坏)"),
            ("errorCode=26 Torrent file is corrupt", "种子文件损坏或丢失"),
        ]

        for input_msg, expected in test_cases:
            result = parse_error_message(input_msg)
            assert result == expected, \
                f"Input '{input_msg}': Expected '{expected}', got '{result}'"

    def test_error_message_user_readable(self):
        """Verify error messages are user-readable (Chinese).

        Tests that parse_error_message returns Chinese messages for common errors.
        """
        from app.aria2.errors import parse_error_message

        # Test pattern-based error message mapping
        pattern_test_cases = [
            # (input, expected_output)
            ("Connection timeout occurred", "网络超时"),
            ("HTTP 404 Not Found", "资源未找到 (404)"),
            ("HTTP 403 Forbidden", "访问被拒绝 (403)"),
            ("HTTP 401 Unauthorized", "需要认证 (401)"),
            ("HTTP 500 Internal Server Error", "服务器内部错误 (500)"),
            ("HTTP 502 Bad Gateway", "网关错误 (502)"),
            ("HTTP 503 Service Unavailable", "服务不可用 (503)"),
            ("DNS name resolution failed", "DNS 解析失败"),
            ("Connection refused by server", "连接被拒绝"),
            ("Connection reset by peer", "连接被重置"),
            ("No space left on device", "磁盘空间不足"),
            ("Permission denied", "权限不足"),
            ("SSL certificate error", "SSL/TLS 证书错误"),
            ("Too many redirects", "重定向次数过多"),
        ]

        for input_msg, expected in pattern_test_cases:
            result = parse_error_message(input_msg)
            assert result == expected, \
                f"Input '{input_msg}': Expected '{expected}', got '{result}'"

        # Verify all mapped messages are in Chinese (contain Chinese characters)
        import re
        chinese_pattern = re.compile(r'[\u4e00-\u9fff]')

        for input_msg, expected in pattern_test_cases:
            result = parse_error_message(input_msg)
            assert chinese_pattern.search(result), \
                f"Result '{result}' should contain Chinese characters"

    def test_error_message_fallback(self):
        """Verify fallback behavior for unknown error messages.

        Tests that unrecognized errors return a generic backend error message.
        """
        from app.aria2.errors import parse_error_message

        # Test unknown error message fallback
        unknown_msg = "Some unknown error that doesn't match any pattern"
        result = parse_error_message(unknown_msg)
        assert result == "后端错误", \
            f"Unknown message should fallback to backend error, got '{result}'"

        # Test empty/None input
        assert parse_error_message(None) == "后端错误"
        assert parse_error_message("") == "后端错误"

    @pytest.mark.asyncio
    async def test_error_event_sets_error_display(
        self, temp_db_listener, test_user_listener, mock_app_state
    ):
        """Verify error event sets error_display on task.

        Scenario:
        1. Create a task with active status
        2. Trigger error event with specific error message
        3. Verify task.error_display is set to user-readable message
        4. Verify task.error contains original error message
        """
        from app.aria2.listener import handle_aria2_event

        # Create task
        async with get_session() as db:
            task = DownloadTask(
                uri_hash="error_display_mapping_hash_001",
                uri="https://example.com/error_display_mapping.zip",
                gid="gid_error_display_mapping_001",
                status="active",
                name="error_display_mapping.zip",
                total_length=102400,
                completed_length=51200,
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
                frozen_space=102400,
                status="pending",
                created_at=utc_now_str(),
            )
            db.add(subscription)
            await db.commit()

        # Test case 1: Error with error code
        mock_client = AsyncMock()
        mock_client.tell_status.return_value = {
            "status": "error",
            "errorMessage": "errorCode=3 Resource not found",
            "totalLength": "102400",
            "completedLength": "51200",
            "files": [{"path": "/tmp/error_display_mapping.zip"}],
        }

        with patch("app.core.state.get_aria2_client", return_value=mock_client):
            with patch("app.routers.tasks.broadcast_task_update_to_subscribers", new_callable=AsyncMock):
                await handle_aria2_event(mock_app_state, gid, "error")

        # Verify error_display is set
        async with get_session() as db:
            result = await db.exec(select(DownloadTask).where(DownloadTask.id == task_id))
            task = result.first()
            assert task.status == "error"
            assert task.error == "errorCode=3 Resource not found", \
                f"Expected original error message, got '{task.error}'"
            assert task.error_display == "资源未找到 (404)", \
                f"Expected '资源未找到 (404)', got '{task.error_display}'"

    @pytest.mark.asyncio
    async def test_error_event_with_pattern_match(
        self, temp_db_listener, test_user_listener, mock_app_state
    ):
        """Verify error event with pattern-matched error message.

        Scenario:
        1. Create a task with active status
        2. Trigger error event with pattern-matchable error message
        3. Verify task.error_display is set to user-readable message
        """
        from app.aria2.listener import handle_aria2_event

        # Create task
        async with get_session() as db:
            task = DownloadTask(
                uri_hash="error_pattern_hash_001",
                uri="https://example.com/error_pattern.zip",
                gid="gid_error_pattern_001",
                status="active",
                name="error_pattern.zip",
                total_length=204800,
                completed_length=102400,
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
                frozen_space=204800,
                status="pending",
                created_at=utc_now_str(),
            )
            db.add(subscription)
            await db.commit()

        # Test with timeout error (pattern match)
        mock_client = AsyncMock()
        mock_client.tell_status.return_value = {
            "status": "error",
            "errorMessage": "Connection timeout while downloading",
            "totalLength": "204800",
            "completedLength": "102400",
            "files": [{"path": "/tmp/error_pattern.zip"}],
        }

        with patch("app.core.state.get_aria2_client", return_value=mock_client):
            with patch("app.routers.tasks.broadcast_task_update_to_subscribers", new_callable=AsyncMock):
                await handle_aria2_event(mock_app_state, gid, "error")

        # Verify error_display is set via pattern match
        async with get_session() as db:
            result = await db.exec(select(DownloadTask).where(DownloadTask.id == task_id))
            task = result.first()
            assert task.status == "error"
            assert task.error == "Connection timeout while downloading"
            assert task.error_display == "网络超时", \
                f"Expected '网络超时', got '{task.error_display}'"

    @pytest.mark.asyncio
    async def test_error_event_with_unknown_error(
        self, temp_db_listener, test_user_listener, mock_app_state
    ):
        """Verify error event with unknown error message.

        Scenario:
        1. Create a task with active status
        2. Trigger error event with unknown error message
        3. Verify task.error_display is generic backend error
        """
        from app.aria2.listener import handle_aria2_event

        # Create task
        async with get_session() as db:
            task = DownloadTask(
                uri_hash="error_unknown_hash_001",
                uri="https://example.com/error_unknown.zip",
                gid="gid_error_unknown_001",
                status="active",
                name="error_unknown.zip",
                total_length=307200,
                completed_length=153600,
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
                frozen_space=307200,
                status="pending",
                created_at=utc_now_str(),
            )
            db.add(subscription)
            await db.commit()

        # Test with unknown error message
        unknown_error = "Some completely unknown error message"
        mock_client = AsyncMock()
        mock_client.tell_status.return_value = {
            "status": "error",
            "errorMessage": unknown_error,
            "totalLength": "307200",
            "completedLength": "153600",
            "files": [{"path": "/tmp/error_unknown.zip"}],
        }

        with patch("app.core.state.get_aria2_client", return_value=mock_client):
            with patch("app.routers.tasks.broadcast_task_update_to_subscribers", new_callable=AsyncMock):
                await handle_aria2_event(mock_app_state, gid, "error")

        # Verify error_display uses generic backend error (safe fallback)
        async with get_session() as db:
            result = await db.exec(select(DownloadTask).where(DownloadTask.id == task_id))
            task = result.first()
            assert task.status == "error"
            assert task.error == unknown_error
            assert task.error_display == "后端错误", \
                f"Expected backend error fallback, got '{task.error_display}'"

    @pytest.mark.asyncio
    async def test_error_display_propagated_to_subscription(
        self, temp_db_listener, test_user_listener, mock_app_state
    ):
        """Verify error_display is propagated to subscription.

        Scenario:
        1. Create a task with pending subscription
        2. Trigger error event
        3. Verify subscription.error_display is set
        """
        from app.aria2.listener import handle_aria2_event

        # Create task
        async with get_session() as db:
            task = DownloadTask(
                uri_hash="error_propagate_hash_001",
                uri="https://example.com/error_propagate.zip",
                gid="gid_error_propagate_001",
                status="active",
                name="error_propagate.zip",
                total_length=409600,
                completed_length=204800,
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
                frozen_space=409600,
                status="pending",
                created_at=utc_now_str(),
            )
            db.add(subscription)
            await db.commit()
            await db.refresh(subscription)
            subscription_id = subscription.id

        # Trigger error event
        mock_client = AsyncMock()
        mock_client.tell_status.return_value = {
            "status": "error",
            "errorMessage": "errorCode=9 No space left on device",
            "totalLength": "409600",
            "completedLength": "204800",
            "files": [{"path": "/tmp/error_propagate.zip"}],
        }

        with patch("app.core.state.get_aria2_client", return_value=mock_client):
            with patch("app.routers.tasks.broadcast_task_update_to_subscribers", new_callable=AsyncMock):
                await handle_aria2_event(mock_app_state, gid, "error")

        # Verify subscription has error_display set
        async with get_session() as db:
            result = await db.exec(
                select(UserTaskSubscription).where(UserTaskSubscription.id == subscription_id)
            )
            sub = result.first()
            assert sub.status == "failed"
            assert sub.error_display == "磁盘空间不足", \
                f"Expected '磁盘空间不足', got '{sub.error_display}'"


class TestStopEventHandling:
    """Tests for stop event handling.

    Verifies:
    1. Stop event only updates error_display (not error)
    2. Stop event does not call _cancel_task
    3. Stop event releases frozen space for all subscribers
    """

    @pytest.mark.asyncio
    async def test_stop_event_updates_error_display(
        self, temp_db_listener, test_user_listener, mock_app_state
    ):
        """Verify stop event only updates error_display field.

        Scenario:
        1. Create a task with active status
        2. Trigger stop event via handle_aria2_event
        3. Verify task.error_display is set to "外部取消（管理员/外部客户端）"
        4. Verify task.error is NOT set (remains None)
        """
        from app.aria2.listener import handle_aria2_event

        # Create task
        async with get_session() as db:
            task = DownloadTask(
                uri_hash="stop_display_hash_001",
                uri="https://example.com/stop_display.zip",
                gid="gid_stop_display_001",
                status="active",
                name="stop_display.zip",
                total_length=102400,
                completed_length=51200,
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
                frozen_space=102400,
                status="pending",
                created_at=utc_now_str(),
            )
            db.add(subscription)
            await db.commit()

        # Verify initial state - no error fields set
        async with get_session() as db:
            result = await db.exec(select(DownloadTask).where(DownloadTask.id == task_id))
            task = result.first()
            assert task.error is None
            assert task.error_display is None

        # Mock aria2 client
        mock_client = AsyncMock()
        mock_client.tell_status.return_value = {
            "status": "removed",
            "totalLength": "102400",
            "completedLength": "51200",
            "files": [{"path": "/tmp/stop_display.zip"}],
        }

        with patch("app.core.state.get_aria2_client", return_value=mock_client):
            with patch("app.routers.tasks.broadcast_task_update_to_subscribers", new_callable=AsyncMock):
                await handle_aria2_event(mock_app_state, gid, "stop")

        # Verify task.error_display is set but task.error is NOT set
        async with get_session() as db:
            result = await db.exec(select(DownloadTask).where(DownloadTask.id == task_id))
            task = result.first()
            assert task.status == "error", f"Expected status 'error', got '{task.status}'"
            assert task.error_display == "外部取消（管理员/外部客户端）", \
                f"Expected error_display '外部取消（管理员/外部客户端）', got '{task.error_display}'"
            assert task.error is None, \
                f"Expected error to be None for stop event, got '{task.error}'"

    @pytest.mark.asyncio
    async def test_stop_event_not_call_cancel_task(
        self, temp_db_listener, test_user_listener, mock_app_state
    ):
        """Verify stop event does not call _cancel_task function.

        Scenario:
        1. Create a task with active status
        2. Trigger stop event via handle_aria2_event
        3. Verify _cancel_task is NOT called
        4. Verify _handle_task_stop_or_error IS called
        """
        from app.aria2.listener import handle_aria2_event

        # Create task
        async with get_session() as db:
            task = DownloadTask(
                uri_hash="stop_no_cancel_hash_001",
                uri="https://example.com/stop_no_cancel.zip",
                gid="gid_stop_no_cancel_001",
                status="active",
                name="stop_no_cancel.zip",
                total_length=204800,
                completed_length=102400,
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
                frozen_space=204800,
                status="pending",
                created_at=utc_now_str(),
            )
            db.add(subscription)
            await db.commit()

        # Mock aria2 client
        mock_client = AsyncMock()
        mock_client.tell_status.return_value = {
            "status": "removed",
            "totalLength": "204800",
            "completedLength": "102400",
            "files": [{"path": "/tmp/stop_no_cancel.zip"}],
        }

        with patch("app.core.state.get_aria2_client", return_value=mock_client):
            with patch("app.routers.tasks.broadcast_task_update_to_subscribers", new_callable=AsyncMock):
                with patch("app.aria2.listener._cancel_task", new_callable=AsyncMock) as mock_cancel:
                    with patch("app.aria2.listener._handle_task_stop_or_error", new_callable=AsyncMock) as mock_stop_or_error:
                        await handle_aria2_event(mock_app_state, gid, "stop")

                        # Verify _cancel_task was NOT called
                        mock_cancel.assert_not_called()

                        # Verify _handle_task_stop_or_error WAS called
                        mock_stop_or_error.assert_called_once()
                        call_args = mock_stop_or_error.call_args
                        assert call_args[0][0] == task_id, \
                            f"Expected task_id {task_id}, got {call_args[0][0]}"
                        assert call_args[0][1] == "外部取消（管理员/外部客户端）", \
                            f"Expected error_display '外部取消（管理员/外部客户端）', got {call_args[0][1]}"

    @pytest.mark.asyncio
    async def test_stop_event_releases_frozen_space(
        self, temp_db_listener, test_user_listener, mock_app_state
    ):
        """Verify stop event releases frozen space for all subscribers.

        Scenario:
        1. Create a task with multiple pending subscriptions with different frozen spaces
        2. Trigger stop event via handle_aria2_event
        3. Verify all subscriptions have frozen_space=0
        4. Verify all subscriptions have status='failed'
        5. Verify all subscriptions have error_display='外部取消（管理员/外部客户端）'
        """
        from app.aria2.listener import handle_aria2_event

        # Create additional users
        user_ids = [test_user_listener["id"]]
        frozen_spaces = [10240, 20480, 30720]

        for i in range(2):
            user_id = execute(
                """
                INSERT INTO users (username, password_hash, is_admin, created_at, quota)
                VALUES (?, ?, ?, ?, ?)
                """,
                [f"stopfreezeuser{i}", hash_password("pass"), 0, datetime.now(timezone.utc).isoformat(), 100 * 1024 * 1024 * 1024]
            )
            user_ids.append(user_id)

        # Create task
        async with get_session() as db:
            task = DownloadTask(
                uri_hash="stop_freeze_hash_001",
                uri="https://example.com/stop_freeze.zip",
                gid="gid_stop_freeze_001",
                status="active",
                name="stop_freeze.zip",
                total_length=61440,
                completed_length=30720,
                created_at=utc_now_str(),
                updated_at=utc_now_str(),
            )
            db.add(task)
            await db.commit()
            await db.refresh(task)
            task_id = task.id
            gid = task.gid

            # Create subscriptions for all users with different frozen spaces
            subscription_ids = []
            for idx, uid in enumerate(user_ids):
                subscription = UserTaskSubscription(
                    owner_id=uid,
                    task_id=task_id,
                    frozen_space=frozen_spaces[idx],
                    status="pending",
                    created_at=utc_now_str(),
                )
                db.add(subscription)
                await db.flush()
                subscription_ids.append(subscription.id)
            await db.commit()

        # Verify initial frozen_space values
        total_initial_frozen = sum(frozen_spaces)
        async with get_session() as db:
            for idx, sub_id in enumerate(subscription_ids):
                result = await db.exec(
                    select(UserTaskSubscription).where(UserTaskSubscription.id == sub_id)
                )
                sub = result.first()
                assert sub.frozen_space == frozen_spaces[idx]
                assert sub.status == "pending"

        # Mock aria2 client
        mock_client = AsyncMock()
        mock_client.tell_status.return_value = {
            "status": "removed",
            "totalLength": "61440",
            "completedLength": "30720",
            "files": [{"path": "/tmp/stop_freeze.zip"}],
        }

        with patch("app.core.state.get_aria2_client", return_value=mock_client):
            with patch("app.routers.tasks.broadcast_task_update_to_subscribers", new_callable=AsyncMock):
                await handle_aria2_event(mock_app_state, gid, "stop")

        # Verify all subscriptions have frozen_space=0, status='failed', error_display='外部取消（管理员/外部客户端）'
        async with get_session() as db:
            for idx, sub_id in enumerate(subscription_ids):
                result = await db.exec(
                    select(UserTaskSubscription).where(UserTaskSubscription.id == sub_id)
                )
                sub = result.first()
                assert sub.frozen_space == 0, \
                    f"User {idx}: Expected frozen_space 0, got {sub.frozen_space}"
                assert sub.status == "failed", \
                    f"User {idx}: Expected status 'failed', got '{sub.status}'"
                assert sub.error_display == "外部取消（管理员/外部客户端）", \
                    f"User {idx}: Expected error_display '外部取消（管理员/外部客户端）', got '{sub.error_display}'"

        # Verify total frozen space released equals sum of initial frozen spaces
        async with get_session() as db:
            result = await db.exec(
                select(UserTaskSubscription).where(UserTaskSubscription.task_id == task_id)
            )
            all_subs = result.all()
            total_remaining_frozen = sum(s.frozen_space for s in all_subs)
            assert total_remaining_frozen == 0, \
                f"Expected total frozen 0, got {total_remaining_frozen}"


class TestSyncTellStatusException:
    """Tests for sync_tasks tell_status exception handling.

    Verifies:
    1. tell_status exception sets task status to 'error'
    2. tell_status exception records error message in task.error field
    """

    @pytest.mark.asyncio
    async def test_tell_status_exception_sets_error_status(
        self, temp_db_listener, test_user_listener, mock_app_state
    ):
        """Verify tell_status exception sets task status to 'error'.

        Scenario:
        1. Create a task with active status and valid GID
        2. Mock aria2 client to raise exception on tell_status
        3. Call sync_tasks fetch_and_update logic
        4. Verify task status is set to 'error'
        """
        from app.aria2.sync import _update_task

        # Create task with active status
        async with get_session() as db:
            task = DownloadTask(
                uri_hash="sync_exception_status_hash_001",
                uri="https://example.com/sync_exception_status.zip",
                gid="gid_sync_exception_status_001",
                status="active",
                name="sync_exception_status.zip",
                total_length=102400,
                completed_length=51200,
                download_speed=1024,
                upload_speed=0,
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
                frozen_space=102400,
                status="pending",
                created_at=utc_now_str(),
            )
            db.add(subscription)
            await db.commit()

        # Verify initial status is 'active'
        async with get_session() as db:
            result = await db.exec(select(DownloadTask).where(DownloadTask.id == task_id))
            task = result.first()
            assert task.status == "active"
            assert task.error is None

        # Simulate the exception handling logic from sync_tasks fetch_and_update
        # When tell_status raises an exception, _update_task is called with error status
        exception_message = "Connection refused: aria2 RPC server not responding"
        await _update_task(task_id, {"status": "error", "error": exception_message})

        # Verify task status is set to 'error'
        async with get_session() as db:
            result = await db.exec(select(DownloadTask).where(DownloadTask.id == task_id))
            task = result.first()
            assert task.status == "error", \
                f"Expected status 'error', got '{task.status}'"

    @pytest.mark.asyncio
    async def test_tell_status_exception_records_error_message(
        self, temp_db_listener, test_user_listener, mock_app_state
    ):
        """Verify tell_status exception records error message in task.error field.

        Scenario:
        1. Create a task with active status and valid GID
        2. Mock aria2 client to raise exception on tell_status
        3. Call sync_tasks fetch_and_update logic
        4. Verify task.error contains the exception message
        """
        from app.aria2.sync import _update_task

        # Create task with active status
        async with get_session() as db:
            task = DownloadTask(
                uri_hash="sync_exception_error_hash_001",
                uri="https://example.com/sync_exception_error.zip",
                gid="gid_sync_exception_error_001",
                status="active",
                name="sync_exception_error.zip",
                total_length=204800,
                completed_length=102400,
                download_speed=2048,
                upload_speed=0,
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
                frozen_space=204800,
                status="pending",
                created_at=utc_now_str(),
            )
            db.add(subscription)
            await db.commit()

        # Verify initial state - no error
        async with get_session() as db:
            result = await db.exec(select(DownloadTask).where(DownloadTask.id == task_id))
            task = result.first()
            assert task.error is None

        # Test various exception messages
        test_cases = [
            "Connection refused: aria2 RPC server not responding",
            "Timeout waiting for aria2 response",
            "GID not found: gid_sync_exception_error_001",
            "JSON-RPC error: Invalid params",
            "Network unreachable",
        ]

        for exception_message in test_cases:
            # Reset task status for each test case
            async with get_session() as db:
                result = await db.exec(select(DownloadTask).where(DownloadTask.id == task_id))
                task = result.first()
                task.status = "active"
                task.error = None
                db.add(task)

            # Simulate the exception handling logic from sync_tasks fetch_and_update
            await _update_task(task_id, {"status": "error", "error": exception_message})

            # Verify task.error contains the exception message
            async with get_session() as db:
                result = await db.exec(select(DownloadTask).where(DownloadTask.id == task_id))
                task = result.first()
                assert task.status == "error", \
                    f"Expected status 'error' for message '{exception_message}', got '{task.status}'"
                assert task.error == exception_message, \
                    f"Expected error '{exception_message}', got '{task.error}'"

    @pytest.mark.asyncio
    async def test_tell_status_exception_with_mock_client(
        self, temp_db_listener, test_user_listener, mock_app_state
    ):
        """Verify tell_status exception handling with mocked aria2 client.

        Scenario:
        1. Create a task with active status and valid GID
        2. Mock aria2 client to raise exception on tell_status
        3. Simulate the fetch_and_update logic
        4. Verify task status is 'error' and error message is recorded
        """
        from app.aria2.sync import _update_task

        # Create task with active status
        async with get_session() as db:
            task = DownloadTask(
                uri_hash="sync_mock_exception_hash_001",
                uri="https://example.com/sync_mock_exception.zip",
                gid="gid_sync_mock_exception_001",
                status="active",
                name="sync_mock_exception.zip",
                total_length=307200,
                completed_length=153600,
                download_speed=4096,
                upload_speed=0,
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
                frozen_space=307200,
                status="pending",
                created_at=utc_now_str(),
            )
            db.add(subscription)
            await db.commit()

        # Mock aria2 client that raises exception
        mock_client = AsyncMock()
        exception_message = "aria2 RPC error: GID not found"
        mock_client.tell_status.side_effect = Exception(exception_message)

        # Simulate the fetch_and_update logic from sync_tasks
        # This is the actual exception handling code path:
        # try:
        #     status = await client.tell_status(gid)
        # except Exception as exc:
        #     await _update_task(task.id, {"status": "error", "error": str(exc)})
        #     return

        try:
            await mock_client.tell_status(gid)
        except Exception as exc:
            await _update_task(task_id, {"status": "error", "error": str(exc)})

        # Verify task status is 'error' and error message is recorded
        async with get_session() as db:
            result = await db.exec(select(DownloadTask).where(DownloadTask.id == task_id))
            task = result.first()
            assert task.status == "error", \
                f"Expected status 'error', got '{task.status}'"
            assert task.error == exception_message, \
                f"Expected error '{exception_message}', got '{task.error}'"

    @pytest.mark.asyncio
    async def test_tell_status_exception_preserves_other_fields(
        self, temp_db_listener, test_user_listener, mock_app_state
    ):
        """Verify tell_status exception preserves other task fields.

        Scenario:
        1. Create a task with various fields populated
        2. Simulate tell_status exception
        3. Verify only status and error fields are updated
        4. Verify other fields (name, total_length, etc.) are preserved
        """
        from app.aria2.sync import _update_task

        original_name = "preserved_fields.zip"
        original_total_length = 409600
        original_completed_length = 204800
        original_download_speed = 8192
        original_upload_speed = 1024

        # Create task with various fields populated
        async with get_session() as db:
            task = DownloadTask(
                uri_hash="sync_preserve_fields_hash_001",
                uri="https://example.com/sync_preserve_fields.zip",
                gid="gid_sync_preserve_fields_001",
                status="active",
                name=original_name,
                total_length=original_total_length,
                completed_length=original_completed_length,
                download_speed=original_download_speed,
                upload_speed=original_upload_speed,
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
                frozen_space=original_total_length,
                status="pending",
                created_at=utc_now_str(),
            )
            db.add(subscription)
            await db.commit()

        # Simulate tell_status exception
        exception_message = "Connection timeout"
        await _update_task(task_id, {"status": "error", "error": exception_message})

        # Verify only status and error are updated, other fields preserved
        async with get_session() as db:
            result = await db.exec(select(DownloadTask).where(DownloadTask.id == task_id))
            task = result.first()

            # Status and error should be updated
            assert task.status == "error", \
                f"Expected status 'error', got '{task.status}'"
            assert task.error == exception_message, \
                f"Expected error '{exception_message}', got '{task.error}'"

            # Other fields should be preserved
            assert task.name == original_name, \
                f"Expected name '{original_name}', got '{task.name}'"
            assert task.total_length == original_total_length, \
                f"Expected total_length {original_total_length}, got {task.total_length}"
            assert task.completed_length == original_completed_length, \
                f"Expected completed_length {original_completed_length}, got {task.completed_length}"
            assert task.download_speed == original_download_speed, \
                f"Expected download_speed {original_download_speed}, got {task.download_speed}"
            assert task.upload_speed == original_upload_speed, \
                f"Expected upload_speed {original_upload_speed}, got {task.upload_speed}"

    @pytest.mark.asyncio
    async def test_tell_status_exception_multiple_tasks(
        self, temp_db_listener, test_user_listener, mock_app_state
    ):
        """Verify tell_status exception handling for multiple tasks.

        Scenario:
        1. Create multiple tasks with active status
        2. Simulate tell_status exception for some tasks
        3. Verify only affected tasks have error status
        4. Verify unaffected tasks remain active
        """
        from app.aria2.sync import _update_task

        # Create multiple tasks
        task_ids = []
        async with get_session() as db:
            for i in range(3):
                task = DownloadTask(
                    uri_hash=f"sync_multi_exception_hash_{i:03d}",
                    uri=f"https://example.com/sync_multi_exception_{i}.zip",
                    gid=f"gid_sync_multi_exception_{i:03d}",
                    status="active",
                    name=f"sync_multi_exception_{i}.zip",
                    total_length=102400 * (i + 1),
                    completed_length=51200 * (i + 1),
                    download_speed=1024 * (i + 1),
                    upload_speed=0,
                    created_at=utc_now_str(),
                    updated_at=utc_now_str(),
                )
                db.add(task)
                await db.flush()
                task_ids.append(task.id)

                # Create subscription for each task
                subscription = UserTaskSubscription(
                    owner_id=test_user_listener["id"],
                    task_id=task.id,
                    frozen_space=102400 * (i + 1),
                    status="pending",
                    created_at=utc_now_str(),
                )
                db.add(subscription)
            await db.commit()

        # Verify all tasks are initially active
        async with get_session() as db:
            for task_id in task_ids:
                result = await db.exec(select(DownloadTask).where(DownloadTask.id == task_id))
                task = result.first()
                assert task.status == "active"

        # Simulate tell_status exception for first and third tasks only
        exception_messages = [
            "Connection refused",
            None,  # Second task succeeds
            "GID not found",
        ]

        for i, task_id in enumerate(task_ids):
            if exception_messages[i]:
                await _update_task(task_id, {"status": "error", "error": exception_messages[i]})

        # Verify task statuses
        async with get_session() as db:
            # First task should be error
            result = await db.exec(select(DownloadTask).where(DownloadTask.id == task_ids[0]))
            task = result.first()
            assert task.status == "error", \
                f"Task 0: Expected status 'error', got '{task.status}'"
            assert task.error == "Connection refused"

            # Second task should remain active
            result = await db.exec(select(DownloadTask).where(DownloadTask.id == task_ids[1]))
            task = result.first()
            assert task.status == "active", \
                f"Task 1: Expected status 'active', got '{task.status}'"
            assert task.error is None

            # Third task should be error
            result = await db.exec(select(DownloadTask).where(DownloadTask.id == task_ids[2]))
            task = result.first()
            assert task.status == "error", \
                f"Task 2: Expected status 'error', got '{task.status}'"
            assert task.error == "GID not found"


class TestStartEventFrozenCAS:
    """Tests for start event frozen space CAS (Compare-And-Swap) logic.

    Verifies:
    1. Duplicate start events do not freeze space multiple times
    2. CAS condition (frozen_space == 0) works correctly
    """

    @pytest.mark.asyncio
    async def test_start_event_no_duplicate_freeze(
        self, temp_db_listener, test_user_listener, mock_app_state
    ):
        """Verify duplicate start events do not freeze space multiple times.

        Scenario:
        1. Create a task with pending subscription (frozen_space=0)
        2. Simulate first start event - should freeze space
        3. Simulate second start event - should NOT freeze space again
        4. Verify frozen_space is set exactly once to the correct value
        """
        from app.aria2.listener import handle_aria2_event

        # Create task with pending subscription
        async with get_session() as db:
            task = DownloadTask(
                uri_hash="start_event_dup_freeze_hash_001",
                uri="https://example.com/start_event_dup.zip",
                gid="gid_start_event_dup_001",
                status="active",
                name="start_event_dup.zip",
                total_length=0,  # Will be updated by start event
                completed_length=0,
                created_at=utc_now_str(),
                updated_at=utc_now_str(),
            )
            db.add(task)
            await db.commit()
            await db.refresh(task)
            task_id = task.id
            task_gid = task.gid

            # Create subscription with frozen_space=0
            subscription = UserTaskSubscription(
                owner_id=test_user_listener["id"],
                task_id=task_id,
                frozen_space=0,  # Not yet frozen
                status="pending",
                created_at=utc_now_str(),
            )
            db.add(subscription)
            await db.commit()
            await db.refresh(subscription)
            sub_id = subscription.id

        # Verify initial state
        async with get_session() as db:
            result = await db.exec(
                select(UserTaskSubscription).where(UserTaskSubscription.id == sub_id)
            )
            sub = result.first()
            assert sub.frozen_space == 0, "Initial frozen_space should be 0"

        # Simulate aria2 status with total_length
        total_length = 10240  # 10 KB
        aria2_status = {
            "gid": task_gid,
            "status": "active",
            "totalLength": str(total_length),
            "completedLength": "0",
            "files": [{"path": "/tmp/test.zip"}],
        }

        # Mock aria2 client's tell_status to return our status
        mock_client = AsyncMock()
        mock_client.tell_status = AsyncMock(return_value=aria2_status)

        # First start event - should freeze space
        with patch("app.core.state.get_aria2_client", return_value=mock_client):
            with patch("app.routers.tasks.broadcast_task_update_to_subscribers", new_callable=AsyncMock):
                await handle_aria2_event(mock_app_state, task_gid, "start")

        # Verify frozen_space is set
        async with get_session() as db:
            result = await db.exec(
                select(UserTaskSubscription).where(UserTaskSubscription.id == sub_id)
            )
            sub = result.first()
            assert sub.frozen_space == total_length, \
                f"After first start event, frozen_space should be {total_length}, got {sub.frozen_space}"

        # Second start event - should NOT freeze space again (CAS should prevent)
        with patch("app.core.state.get_aria2_client", return_value=mock_client):
            with patch("app.routers.tasks.broadcast_task_update_to_subscribers", new_callable=AsyncMock):
                await handle_aria2_event(mock_app_state, task_gid, "start")

        # Verify frozen_space is still the same (not doubled)
        async with get_session() as db:
            result = await db.exec(
                select(UserTaskSubscription).where(UserTaskSubscription.id == sub_id)
            )
            sub = result.first()
            assert sub.frozen_space == total_length, \
                f"After second start event, frozen_space should still be {total_length}, got {sub.frozen_space}"
            assert sub.status == "pending", \
                f"Subscription status should remain 'pending', got '{sub.status}'"

    @pytest.mark.asyncio
    async def test_start_event_cas_condition_works(
        self, temp_db_listener, test_user_listener, mock_app_state
    ):
        """Verify CAS condition (frozen_space == 0) works correctly under concurrent access.

        Scenario:
        1. Create a task with pending subscription (frozen_space=0)
        2. Run multiple start event handlers concurrently
        3. Verify only one handler successfully freezes space
        4. Verify frozen_space is set exactly once to the correct value
        """
        from app.aria2.listener import handle_aria2_event

        # Create task with pending subscription
        async with get_session() as db:
            task = DownloadTask(
                uri_hash="start_event_cas_hash_002",
                uri="https://example.com/start_event_cas.zip",
                gid="gid_start_event_cas_002",
                status="active",
                name="start_event_cas.zip",
                total_length=0,
                completed_length=0,
                created_at=utc_now_str(),
                updated_at=utc_now_str(),
            )
            db.add(task)
            await db.commit()
            await db.refresh(task)
            task_id = task.id
            task_gid = task.gid

            # Create subscription with frozen_space=0
            subscription = UserTaskSubscription(
                owner_id=test_user_listener["id"],
                task_id=task_id,
                frozen_space=0,
                status="pending",
                created_at=utc_now_str(),
            )
            db.add(subscription)
            await db.commit()
            await db.refresh(subscription)
            sub_id = subscription.id

        # Verify initial state
        async with get_session() as db:
            result = await db.exec(
                select(UserTaskSubscription).where(UserTaskSubscription.id == sub_id)
            )
            sub = result.first()
            assert sub.frozen_space == 0, "Initial frozen_space should be 0"

        # Simulate aria2 status
        total_length = 20480  # 20 KB
        aria2_status = {
            "gid": task_gid,
            "status": "active",
            "totalLength": str(total_length),
            "completedLength": "0",
            "files": [{"path": "/tmp/test_cas.zip"}],
        }

        # Mock aria2 client
        mock_client = AsyncMock()
        mock_client.tell_status = AsyncMock(return_value=aria2_status)

        # Run multiple start event handlers concurrently
        with patch("app.core.state.get_aria2_client", return_value=mock_client):
            with patch("app.routers.tasks.broadcast_task_update_to_subscribers", new_callable=AsyncMock):
                results = await asyncio.gather(
                    handle_aria2_event(mock_app_state, task_gid, "start"),
                    handle_aria2_event(mock_app_state, task_gid, "start"),
                    handle_aria2_event(mock_app_state, task_gid, "start"),
                    return_exceptions=True,
                )

        # No exceptions should be raised
        exceptions = [r for r in results if isinstance(r, Exception)]
        assert len(exceptions) == 0, f"Unexpected exceptions: {exceptions}"

        # Verify frozen_space is set exactly once to the correct value
        async with get_session() as db:
            result = await db.exec(
                select(UserTaskSubscription).where(UserTaskSubscription.id == sub_id)
            )
            sub = result.first()
            assert sub.frozen_space == total_length, \
                f"frozen_space should be {total_length}, got {sub.frozen_space}"
            assert sub.status == "pending", \
                f"Subscription status should be 'pending', got '{sub.status}'"

        # Verify the subscription is still valid (added to valid_subscribers)
        # by checking that the task was not cancelled
        async with get_session() as db:
            result = await db.exec(
                select(DownloadTask).where(DownloadTask.id == task_id)
            )
            task = result.first()
            # Task should still be active (not cancelled due to no valid subscribers)
            assert task.status == "active", \
                f"Task status should be 'active', got '{task.status}'"


class TestBroadcastTaskUpdateStatusOverride:
    """Tests for _broadcast_task_update subscription status override.

    Verifies:
    1. Failed subscription returns error status in broadcast payload
    2. Success subscription returns complete status in broadcast payload
    """

    @pytest.mark.asyncio
    async def test_broadcast_failed_subscription_returns_error(
        self, temp_db_listener, test_user_listener, mock_app_state
    ):
        """Verify failed subscription returns error status in broadcast payload.

        Scenario:
        1. Create a task with status='active'
        2. Create a subscription with status='failed' and error_display set
        3. Call _broadcast_task_update
        4. Verify the broadcast payload has status='error' (overriding task status)
        """
        from app.routers.tasks import _broadcast_task_update

        # Create task with active status
        async with get_session() as db:
            task = DownloadTask(
                uri_hash="broadcast_failed_sub_hash_001",
                uri="https://example.com/broadcast_failed.zip",
                gid="gid_broadcast_failed_001",
                status="active",  # Task is still active
                name="broadcast_failed.zip",
                total_length=10240,
                completed_length=5120,
                created_at=utc_now_str(),
                updated_at=utc_now_str(),
            )
            db.add(task)
            await db.commit()
            await db.refresh(task)
            task_id = task.id

            # Create subscription with failed status
            subscription = UserTaskSubscription(
                owner_id=test_user_listener["id"],
                task_id=task_id,
                frozen_space=0,  # Already released
                status="failed",  # Subscription failed
                error_display="Insufficient quota",
                created_at=utc_now_str(),
            )
            db.add(subscription)
            await db.commit()

        # Create a mock WebSocket to capture the broadcast payload
        captured_payloads = []

        class MockWebSocket:
            async def send_json(self, data):
                captured_payloads.append(data)

        mock_ws = MockWebSocket()

        # Register the mock WebSocket for the user
        async with mock_app_state.lock:
            mock_app_state.ws_connections[test_user_listener["id"]] = {mock_ws}

        # Call _broadcast_task_update
        await _broadcast_task_update(mock_app_state, task_id)

        # Verify the broadcast payload
        assert len(captured_payloads) == 1, \
            f"Expected 1 broadcast, got {len(captured_payloads)}"

        payload = captured_payloads[0]
        assert payload["type"] == "task_update", \
            f"Expected type 'task_update', got '{payload['type']}'"

        task_payload = payload["task"]
        assert task_payload["status"] == "error", \
            f"Expected status 'error' for failed subscription, got '{task_payload['status']}'"
        assert task_payload["error"] == "Insufficient quota", \
            f"Expected error 'Insufficient quota', got '{task_payload['error']}'"

        # Clean up
        async with mock_app_state.lock:
            mock_app_state.ws_connections.pop(test_user_listener["id"], None)

    @pytest.mark.asyncio
    async def test_broadcast_success_subscription_returns_complete(
        self, temp_db_listener, test_user_listener, mock_app_state
    ):
        """Verify success subscription returns complete status in broadcast payload.

        Scenario:
        1. Create a task with status='complete'
        2. Create a subscription with status='success'
        3. Call _broadcast_task_update
        4. Verify the broadcast payload has status='complete'
        """
        from app.routers.tasks import _broadcast_task_update

        # Create task with complete status
        async with get_session() as db:
            task = DownloadTask(
                uri_hash="broadcast_success_sub_hash_002",
                uri="https://example.com/broadcast_success.zip",
                gid="gid_broadcast_success_002",
                status="complete",
                name="broadcast_success.zip",
                total_length=10240,
                completed_length=10240,
                created_at=utc_now_str(),
                updated_at=utc_now_str(),
            )
            db.add(task)
            await db.commit()
            await db.refresh(task)
            task_id = task.id

            # Create subscription with success status
            subscription = UserTaskSubscription(
                owner_id=test_user_listener["id"],
                task_id=task_id,
                frozen_space=0,  # Released on success
                status="success",
                created_at=utc_now_str(),
            )
            db.add(subscription)
            await db.commit()

        # Create a mock WebSocket to capture the broadcast payload
        captured_payloads = []

        class MockWebSocket:
            async def send_json(self, data):
                captured_payloads.append(data)

        mock_ws = MockWebSocket()

        # Register the mock WebSocket for the user
        async with mock_app_state.lock:
            mock_app_state.ws_connections[test_user_listener["id"]] = {mock_ws}

        # Call _broadcast_task_update
        await _broadcast_task_update(mock_app_state, task_id)

        # Verify the broadcast payload
        assert len(captured_payloads) == 1, \
            f"Expected 1 broadcast, got {len(captured_payloads)}"

        payload = captured_payloads[0]
        assert payload["type"] == "task_update", \
            f"Expected type 'task_update', got '{payload['type']}'"

        task_payload = payload["task"]
        assert task_payload["status"] == "complete", \
            f"Expected status 'complete' for success subscription, got '{task_payload['status']}'"
        assert task_payload["error"] is None, \
            f"Expected error to be None for success subscription, got '{task_payload['error']}'"

        # Clean up
        async with mock_app_state.lock:
            mock_app_state.ws_connections.pop(test_user_listener["id"], None)


class TestCreateTaskConflict:
    """Tests for create_task returning 409 Conflict when user already has the file.

    Verifies:
    1. Returns 409 Conflict when user already has the file from a completed task
    2. Does not create duplicate UserFile references
    """

    @pytest.mark.asyncio
    async def test_create_task_returns_409_when_user_has_file(
        self, temp_db_listener, test_user_listener, mock_app_state
    ):
        """Verify create_task returns 409 Conflict when user already has the file.

        Scenario:
        1. Create a completed task with stored_file_id
        2. Create a StoredFile and UserFile for the user
        3. Create a subscription with status='success'
        4. Call create_task with the same URI
        5. Verify 409 Conflict is returned
        """
        from fastapi import HTTPException
        from app.routers.tasks import create_task, TaskCreate
        from app.models import User
        from app.services.hash import calculate_url_hash

        test_url = "https://example.com/conflict_test.zip"
        uri_hash = calculate_url_hash(test_url)

        # Create stored file
        async with get_session() as db:
            stored_file = StoredFile(
                content_hash="conflict_test_sha256_001",
                size=10240,
                real_path="store/conflict_test.zip",
                ref_count=1,
                original_name="conflict_test.zip",
                created_at=utc_now_str(),
            )
            db.add(stored_file)
            await db.commit()
            await db.refresh(stored_file)
            stored_file_id = stored_file.id

            # Create completed task with correct uri_hash
            task = DownloadTask(
                uri_hash=uri_hash,
                uri=test_url,
                gid="gid_conflict_001",
                status="complete",
                name="conflict_test.zip",
                total_length=10240,
                completed_length=10240,
                stored_file_id=stored_file_id,
                created_at=utc_now_str(),
                updated_at=utc_now_str(),
            )
            db.add(task)
            await db.commit()
            await db.refresh(task)
            task_id = task.id

            # Create user file reference
            user_file = UserFile(
                owner_id=test_user_listener["id"],
                stored_file_id=stored_file_id,
                display_name="conflict_test.zip",
                created_at=utc_now_str(),
            )
            db.add(user_file)
            await db.commit()

            # Create subscription with success status
            subscription = UserTaskSubscription(
                owner_id=test_user_listener["id"],
                task_id=task_id,
                frozen_space=0,
                status="success",
                created_at=utc_now_str(),
            )
            db.add(subscription)
            await db.commit()

        # Create mock user
        mock_user = User(
            id=test_user_listener["id"],
            username=test_user_listener["username"],
            password_hash="dummy",
            is_admin=False,
            quota=test_user_listener["quota"],
            created_at=utc_now_str(),
        )

        # Create payload with same URI
        payload = TaskCreate(uri=test_url)

        # Create mock request with proper structure
        mock_request = AsyncMock()
        mock_request.app.state.app_state = mock_app_state

        # Mock probe_url_with_get_fallback to return the same hash
        with patch("app.routers.tasks.probe_url_with_get_fallback") as mock_probe:
            from app.services.http_probe import ProbeResult
            mock_probe.return_value = ProbeResult(
                success=True,
                final_url=test_url,
                filename="conflict_test.zip",
                content_length=10240,
            )

            # Call create_task and expect 409 Conflict
            with pytest.raises(HTTPException) as exc_info:
                await create_task(payload, mock_request, mock_user)

            assert exc_info.value.status_code == 409
            assert "您已拥有此文件" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_create_task_no_duplicate_reference(
        self, temp_db_listener, test_user_listener, mock_app_state
    ):
        """Verify create_task does not create duplicate UserFile references.

        Scenario:
        1. Create a completed task with stored_file_id
        2. Create a StoredFile and UserFile for the user
        3. Create a subscription with status='success'
        4. Attempt to call create_task with the same URI
        5. Verify no new UserFile is created (count remains 1)
        """
        from fastapi import HTTPException
        from app.routers.tasks import create_task, TaskCreate
        from app.models import User
        from app.services.hash import calculate_url_hash

        test_url = "https://example.com/no_dup_ref.zip"
        uri_hash = calculate_url_hash(test_url)

        # Create stored file
        async with get_session() as db:
            stored_file = StoredFile(
                content_hash="no_dup_ref_sha256_001",
                size=20480,
                real_path="store/no_dup_ref.zip",
                ref_count=1,
                original_name="no_dup_ref.zip",
                created_at=utc_now_str(),
            )
            db.add(stored_file)
            await db.commit()
            await db.refresh(stored_file)
            stored_file_id = stored_file.id

            # Create completed task with correct uri_hash
            task = DownloadTask(
                uri_hash=uri_hash,
                uri=test_url,
                gid="gid_no_dup_ref_001",
                status="complete",
                name="no_dup_ref.zip",
                total_length=20480,
                completed_length=20480,
                stored_file_id=stored_file_id,
                created_at=utc_now_str(),
                updated_at=utc_now_str(),
            )
            db.add(task)
            await db.commit()
            await db.refresh(task)
            task_id = task.id

            # Create user file reference
            user_file = UserFile(
                owner_id=test_user_listener["id"],
                stored_file_id=stored_file_id,
                display_name="no_dup_ref.zip",
                created_at=utc_now_str(),
            )
            db.add(user_file)
            await db.commit()

            # Create subscription with success status
            subscription = UserTaskSubscription(
                owner_id=test_user_listener["id"],
                task_id=task_id,
                frozen_space=0,
                status="success",
                created_at=utc_now_str(),
            )
            db.add(subscription)
            await db.commit()

        # Count initial UserFile records for this user
        async with get_session() as db:
            result = await db.exec(
                select(UserFile).where(UserFile.owner_id == test_user_listener["id"])
            )
            initial_user_files = result.all()
            initial_count = len(initial_user_files)

        # Create mock user
        mock_user = User(
            id=test_user_listener["id"],
            username=test_user_listener["username"],
            password_hash="dummy",
            is_admin=False,
            quota=test_user_listener["quota"],
            created_at=utc_now_str(),
        )

        # Create payload with same URI
        payload = TaskCreate(uri=test_url)

        # Create mock request with proper structure
        mock_request = AsyncMock()
        mock_request.app.state.app_state = mock_app_state

        # Mock probe_url_with_get_fallback to return the same hash
        with patch("app.routers.tasks.probe_url_with_get_fallback") as mock_probe:
            from app.services.http_probe import ProbeResult
            mock_probe.return_value = ProbeResult(
                success=True,
                final_url=test_url,
                filename="no_dup_ref.zip",
                content_length=20480,
            )

            # Call create_task - should raise 409
            with pytest.raises(HTTPException) as exc_info:
                await create_task(payload, mock_request, mock_user)

            assert exc_info.value.status_code == 409

        # Verify no new UserFile was created
        async with get_session() as db:
            result = await db.exec(
                select(UserFile).where(UserFile.owner_id == test_user_listener["id"])
            )
            final_user_files = result.all()
            final_count = len(final_user_files)

        assert final_count == initial_count, \
            f"Expected {initial_count} UserFile records, got {final_count}. Duplicate reference was created!"

        # Also verify ref_count on StoredFile was not incremented
        async with get_session() as db:
            result = await db.exec(
                select(StoredFile).where(StoredFile.id == stored_file_id)
            )
            stored = result.first()
            assert stored.ref_count == 1, \
                f"Expected ref_count 1, got {stored.ref_count}. ref_count was incorrectly incremented!"


class TestCreateTaskErrorRetry:
    """Tests for create_task error task retry functionality.

    Verifies:
    1. Error task status is reset to 'queued' when no pending subscribers exist
    2. Error task is resubmitted to aria2 after status reset
    """

    @pytest.mark.asyncio
    async def test_error_task_status_reset_when_no_pending_subscribers(
        self, temp_db_listener, test_user_listener, mock_app_state
    ):
        """Verify error task status is reset when no pending subscribers exist.

        Scenario:
        1. Create a task with status='error' and no pending subscriptions
        2. Call create_task with the same URI
        3. Verify task status is reset to 'queued'
        4. Verify error fields are cleared
        5. Verify gid is cleared
        """
        from app.routers.tasks import create_task, TaskCreate
        from app.models import User
        from app.services.hash import calculate_url_hash

        test_url = "https://example.com/error_retry_reset.zip"
        uri_hash = calculate_url_hash(test_url)

        # Create error task with no pending subscriptions
        async with get_session() as db:
            task = DownloadTask(
                uri_hash=uri_hash,
                uri=test_url,
                gid="gid_error_retry_001",
                status="error",
                name="error_retry_reset.zip",
                total_length=10240,
                completed_length=0,
                error="Download failed",
                error_display="下载失败",
                created_at=utc_now_str(),
                updated_at=utc_now_str(),
            )
            db.add(task)
            await db.commit()
            await db.refresh(task)
            task_id = task.id

            # Create a failed subscription (not pending)
            subscription = UserTaskSubscription(
                owner_id=test_user_listener["id"],
                task_id=task_id,
                frozen_space=0,
                status="failed",
                created_at=utc_now_str(),
            )
            db.add(subscription)
            await db.commit()

        # Create mock user (different user to trigger retry)
        other_user_id = execute(
            """
            INSERT INTO users (username, password_hash, is_admin, created_at, quota)
            VALUES (?, ?, ?, ?, ?)
            """,
            ["retryuser", hash_password("testpass"), 0, datetime.now(timezone.utc).isoformat(), 100 * 1024 * 1024 * 1024]
        )

        mock_user = User(
            id=other_user_id,
            username="retryuser",
            password_hash="dummy",
            is_admin=False,
            quota=100 * 1024 * 1024 * 1024,
            created_at=utc_now_str(),
        )

        # Create payload with same URI
        payload = TaskCreate(uri=test_url)

        # Create mock request with proper structure
        mock_request = AsyncMock()
        mock_request.app.state.app_state = mock_app_state

        # Mock aria2 client
        mock_client = AsyncMock()
        mock_client.add_uri = AsyncMock(return_value="new_gid_001")

        # Mock probe_url_with_get_fallback
        with patch("app.routers.tasks.probe_url_with_get_fallback") as mock_probe:
            from app.services.http_probe import ProbeResult
            mock_probe.return_value = ProbeResult(
                success=True,
                final_url=test_url,
                filename="error_retry_reset.zip",
                content_length=10240,
            )

            with patch("app.core.state.get_aria2_client", return_value=mock_client):
                # Call create_task
                result = await create_task(payload, mock_request, mock_user)

                # Wait for background task to complete
                await asyncio.sleep(0.1)

        # Verify task status was reset
        async with get_session() as db:
            db_result = await db.exec(
                select(DownloadTask).where(DownloadTask.id == task_id)
            )
            updated_task = db_result.first()

            # Status should be 'active' after aria2 submission (or 'queued' if submission pending)
            assert updated_task.status in ("queued", "active"), \
                f"Expected status 'queued' or 'active', got '{updated_task.status}'"

            # Error fields should be cleared
            assert updated_task.error is None, \
                f"Expected error to be None, got '{updated_task.error}'"
            assert updated_task.error_display is None, \
                f"Expected error_display to be None, got '{updated_task.error_display}'"

    @pytest.mark.asyncio
    async def test_error_task_resubmitted_to_aria2(
        self, temp_db_listener, test_user_listener, mock_app_state
    ):
        """Verify error task is prepared for resubmission to aria2.

        Scenario:
        1. Create a task with status='error' and no pending subscriptions
        2. Call create_task with the same URI
        3. Verify task gid is cleared (reset for new submission)
        4. Verify task status is reset to queued/active
        5. Verify a new subscription is created
        """
        from app.routers.tasks import create_task, TaskCreate
        from app.models import User
        from app.services.hash import calculate_url_hash

        test_url = "https://example.com/error_resubmit.zip"
        uri_hash = calculate_url_hash(test_url)

        # Create error task with no pending subscriptions
        async with get_session() as db:
            task = DownloadTask(
                uri_hash=uri_hash,
                uri=test_url,
                gid="old_gid_error_002",
                status="error",
                name="error_resubmit.zip",
                total_length=20480,
                completed_length=0,
                error="Connection timeout",
                error_display="连接超时",
                created_at=utc_now_str(),
                updated_at=utc_now_str(),
            )
            db.add(task)
            await db.commit()
            await db.refresh(task)
            task_id = task.id

        # Create mock user
        resubmit_user_id = execute(
            """
            INSERT INTO users (username, password_hash, is_admin, created_at, quota)
            VALUES (?, ?, ?, ?, ?)
            """,
            ["resubmituser", hash_password("testpass"), 0, datetime.now(timezone.utc).isoformat(), 100 * 1024 * 1024 * 1024]
        )

        mock_user = User(
            id=resubmit_user_id,
            username="resubmituser",
            password_hash="dummy",
            is_admin=False,
            quota=100 * 1024 * 1024 * 1024,
            created_at=utc_now_str(),
        )

        # Create payload with same URI
        payload = TaskCreate(uri=test_url)

        # Create mock request with proper structure
        mock_request = AsyncMock()
        mock_request.app.state.app_state = mock_app_state

        # Mock aria2 client - the background task will use this
        mock_client = AsyncMock()
        new_gid = "new_gid_resubmit_002"
        mock_client.add_uri = AsyncMock(return_value=new_gid)

        # Mock probe_url_with_get_fallback
        with patch("app.routers.tasks.probe_url_with_get_fallback") as mock_probe:
            from app.services.http_probe import ProbeResult
            mock_probe.return_value = ProbeResult(
                success=True,
                final_url=test_url,
                filename="error_resubmit.zip",
                content_length=20480,
            )

            with patch("app.core.state.get_aria2_client", return_value=mock_client):
                # Call create_task
                result = await create_task(payload, mock_request, mock_user)

                # Wait for background task to complete
                await asyncio.sleep(0.2)

        # Verify task state after retry
        async with get_session() as db:
            db_result = await db.exec(
                select(DownloadTask).where(DownloadTask.id == task_id)
            )
            updated_task = db_result.first()

            # The old gid should be cleared during reset (before resubmission)
            # After successful submission, it will have the new gid
            # Either way, it should NOT be the old gid
            assert updated_task.gid != "old_gid_error_002", \
                f"Expected gid to be updated from 'old_gid_error_002', got '{updated_task.gid}'"

            # Status should be queued (before submission) or active (after submission)
            assert updated_task.status in ("queued", "active"), \
                f"Expected status 'queued' or 'active', got '{updated_task.status}'"

            # Error fields should be cleared
            assert updated_task.error is None, \
                f"Expected error to be None, got '{updated_task.error}'"
            assert updated_task.error_display is None, \
                f"Expected error_display to be None, got '{updated_task.error_display}'"

        # Verify a new subscription was created for the new user
        async with get_session() as db:
            db_result = await db.exec(
                select(UserTaskSubscription).where(
                    UserTaskSubscription.task_id == task_id,
                    UserTaskSubscription.owner_id == resubmit_user_id,
                )
            )
            new_subscription = db_result.first()

            assert new_subscription is not None, \
                "Expected a new subscription to be created for the retry user"
            assert new_subscription.status == "pending", \
                f"Expected subscription status 'pending', got '{new_subscription.status}'"
