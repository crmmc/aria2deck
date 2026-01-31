"""Integration tests for space freeze behavior via actual handlers."""
import asyncio
import os
import tempfile
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import select

from app.aria2.listener import handle_aria2_event
from app.aria2.sync import sync_tasks
from app.core.config import settings
from app.core.security import hash_password
from app.core.state import AppState
from app.database import get_session, reset_engine, init_db as init_sqlmodel_db, dispose_engine
from app.db import init_db, execute
from app.models import DownloadTask, UserTaskSubscription, utc_now_str


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


def _create_user(username: str, quota: int) -> int:
    return execute(
        """
        INSERT INTO users (username, password_hash, is_admin, created_at, quota)
        VALUES (?, ?, ?, ?, ?)
        """,
        [username, hash_password("testpass"), 0, datetime.now(timezone.utc).isoformat(), quota],
    )


async def _create_task_with_subs(user_ids: list[int], gid: str, total_length: int, status: str = "queued") -> int:
    async with get_session() as db:
        task = DownloadTask(
            uri_hash=f"freeze_hash_{gid}",
            uri="https://example.com/freeze_test.zip",
            gid=gid,
            status=status,
            name="freeze_test.zip",
            total_length=total_length,
            completed_length=0,
            created_at=utc_now_str(),
            updated_at=utc_now_str(),
        )
        db.add(task)
        await db.commit()
        await db.refresh(task)

        for user_id in user_ids:
            sub = UserTaskSubscription(
                owner_id=user_id,
                task_id=task.id,
                frozen_space=0,
                status="pending",
                created_at=utc_now_str(),
            )
            db.add(sub)
        await db.commit()
        return task.id


class TestStartEventSpaceFreeze:
    """Tests that start event freezes space per user independently."""

    @pytest.mark.asyncio
    async def test_start_event_freezes_each_user_independently(self, temp_db_freeze):
        user1 = _create_user("freezeuser1", 100 * 1024 * 1024 * 1024)
        user2 = _create_user("freezeuser2", 100 * 1024 * 1024 * 1024)
        gid = "gid_freeze_1"
        total_length = 1024

        task_id = await _create_task_with_subs([user1, user2], gid, total_length=0)
        state = AppState()

        mock_client = AsyncMock()
        mock_client.tell_status.return_value = {
            "gid": gid,
            "status": "active",
            "totalLength": str(total_length),
            "completedLength": "0",
            "downloadSpeed": "0",
            "uploadSpeed": "0",
            "files": [{"path": "dummy"}],
        }

        async def fake_space_info(user_id: int, quota: int):
            return {
                "available": total_length + 1,
                "used": 0,
                "frozen": 0,
                "quota": quota,
            }

        with patch("app.core.state.get_aria2_client", return_value=mock_client), \
             patch("app.services.storage.get_user_space_info", new_callable=AsyncMock, side_effect=fake_space_info), \
             patch("app.routers.tasks.broadcast_task_update_to_subscribers", new_callable=AsyncMock), \
             patch("app.aria2.listener._cancel_task", new_callable=AsyncMock) as cancel_mock:
            await handle_aria2_event(state, gid, "start")
            cancel_mock.assert_not_called()

        async with get_session() as db:
            result = await db.exec(
                select(UserTaskSubscription).where(UserTaskSubscription.task_id == task_id)
            )
            subs = result.all()
            assert len(subs) == 2
            for sub in subs:
                assert sub.frozen_space == total_length
                assert sub.status == "pending"

    @pytest.mark.asyncio
    async def test_start_event_marks_insufficient_user_failed(self, temp_db_freeze):
        user1 = _create_user("freezeuser3", 100 * 1024 * 1024 * 1024)
        user2 = _create_user("freezeuser4", 100 * 1024 * 1024 * 1024)
        gid = "gid_freeze_2"
        total_length = 2048

        task_id = await _create_task_with_subs([user1, user2], gid, total_length=0)
        state = AppState()

        mock_client = AsyncMock()
        mock_client.tell_status.return_value = {
            "gid": gid,
            "status": "active",
            "totalLength": str(total_length),
            "completedLength": "0",
            "downloadSpeed": "0",
            "uploadSpeed": "0",
            "files": [{"path": "dummy"}],
        }

        async def fake_space_info(user_id: int, quota: int):
            if user_id == user1:
                available = total_length
            else:
                available = total_length - 1
            return {
                "available": available,
                "used": 0,
                "frozen": 0,
                "quota": quota,
            }

        with patch("app.core.state.get_aria2_client", return_value=mock_client), \
             patch("app.services.storage.get_user_space_info", new_callable=AsyncMock, side_effect=fake_space_info), \
             patch("app.routers.tasks.broadcast_task_update_to_subscribers", new_callable=AsyncMock), \
             patch("app.aria2.listener._cancel_task", new_callable=AsyncMock) as cancel_mock:
            await handle_aria2_event(state, gid, "start")
            cancel_mock.assert_not_called()

        async with get_session() as db:
            result = await db.exec(
                select(UserTaskSubscription).where(UserTaskSubscription.task_id == task_id)
            )
            subs = {sub.owner_id: sub for sub in result.all()}

        assert subs[user1].frozen_space == total_length
        assert subs[user1].status == "pending"
        assert subs[user2].status == "failed"
        assert subs[user2].frozen_space == 0
        assert subs[user2].error_display == "用户配额空间不足"

    @pytest.mark.asyncio
    async def test_start_event_all_subscribers_insufficient_triggers_cancel(self, temp_db_freeze):
        user1 = _create_user("freezeuser7", 10 * 1024 * 1024 * 1024)
        user2 = _create_user("freezeuser8", 10 * 1024 * 1024 * 1024)
        gid = "gid_freeze_3"
        total_length = 4096

        task_id = await _create_task_with_subs([user1, user2], gid, total_length=0)
        state = AppState()

        mock_client = AsyncMock()
        mock_client.tell_status.return_value = {
            "gid": gid,
            "status": "active",
            "totalLength": str(total_length),
            "completedLength": "0",
            "downloadSpeed": "0",
            "uploadSpeed": "0",
            "files": [{"path": "dummy"}],
        }

        async def fake_space_info(user_id: int, quota: int):
            return {
                "available": total_length - 1,
                "used": 0,
                "frozen": 0,
                "quota": quota,
            }

        with patch("app.core.state.get_aria2_client", return_value=mock_client), \
             patch("app.services.storage.get_user_space_info", new_callable=AsyncMock, side_effect=fake_space_info), \
             patch("app.routers.tasks.broadcast_task_update_to_subscribers", new_callable=AsyncMock), \
             patch("app.aria2.listener._cancel_task", new_callable=AsyncMock) as cancel_mock:
            await handle_aria2_event(state, gid, "start")
            cancel_mock.assert_called_once()
            assert "所有订阅者空间不足" in cancel_mock.call_args.args[4]

        async with get_session() as db:
            result = await db.exec(
                select(UserTaskSubscription).where(UserTaskSubscription.task_id == task_id)
            )
            subs = result.all()
            assert len(subs) == 2
            for sub in subs:
                assert sub.status == "failed"
                assert sub.frozen_space == 0
                assert sub.error_display == "用户配额空间不足"

    @pytest.mark.asyncio
    async def test_start_event_oversize_task_triggers_cancel(self, temp_db_freeze):
        user_id = _create_user("freezeuser9", 10 * 1024 * 1024 * 1024)
        gid = "gid_freeze_4"
        total_length = 5 * 1024 * 1024

        await _create_task_with_subs([user_id], gid, total_length=0)
        state = AppState()

        mock_client = AsyncMock()
        mock_client.tell_status.return_value = {
            "gid": gid,
            "status": "active",
            "totalLength": str(total_length),
            "completedLength": "0",
            "downloadSpeed": "0",
            "uploadSpeed": "0",
            "files": [{"path": "dummy"}],
        }

        with patch("app.core.state.get_aria2_client", return_value=mock_client), \
             patch("app.routers.config.get_max_task_size", return_value=1), \
             patch("app.aria2.listener._cancel_task", new_callable=AsyncMock) as cancel_mock:
            await handle_aria2_event(state, gid, "start")
            cancel_mock.assert_called_once()
            assert "超过系统限制" in cancel_mock.call_args.args[4]


class TestSyncTasksSpaceFreeze:
    """Tests that sync loop freezes space per user using actual sync_tasks."""

    @pytest.mark.asyncio
    async def test_sync_tasks_freezes_each_user(self, temp_db_freeze):
        user1 = _create_user("freezeuser5", 100 * 1024 * 1024 * 1024)
        user2 = _create_user("freezeuser6", 100 * 1024 * 1024 * 1024)
        gid = "gid_freeze_sync"
        total_length = 4096

        task_id = await _create_task_with_subs([user1, user2], gid, total_length=0, status="active")

        state = AppState()
        mock_client = AsyncMock()
        mock_client.tell_status.return_value = {
            "gid": gid,
            "status": "active",
            "totalLength": str(total_length),
            "completedLength": "0",
            "downloadSpeed": "0",
            "uploadSpeed": "0",
            "files": [{"path": "dummy"}],
            "connections": "0",
        }

        async def fake_space_info(user_id: int, quota: int):
            return {
                "available": total_length + 1,
                "used": 0,
                "frozen": 0,
                "quota": quota,
            }

        with patch("app.core.state.get_aria2_client", return_value=mock_client), \
             patch("app.services.storage.get_user_space_info", new_callable=AsyncMock, side_effect=fake_space_info), \
             patch("app.routers.tasks.broadcast_task_update_to_subscribers", new_callable=AsyncMock), \
             patch("app.aria2.sync.asyncio.sleep", new_callable=AsyncMock, side_effect=asyncio.CancelledError):
            with pytest.raises(asyncio.CancelledError):
                await sync_tasks(state, interval=0.01)

        async with get_session() as db:
            result = await db.exec(
                select(UserTaskSubscription).where(UserTaskSubscription.task_id == task_id)
            )
            subs = result.all()
            assert len(subs) == 2
            for sub in subs:
                assert sub.frozen_space == total_length
                assert sub.status == "pending"

    @pytest.mark.asyncio
    async def test_sync_tasks_all_subscribers_insufficient_triggers_cancel(self, temp_db_freeze):
        user1 = _create_user("freezeuser10", 10 * 1024 * 1024 * 1024)
        user2 = _create_user("freezeuser11", 10 * 1024 * 1024 * 1024)
        gid = "gid_freeze_sync_insufficient"
        total_length = 4096

        task_id = await _create_task_with_subs([user1, user2], gid, total_length=0, status="active")

        state = AppState()
        mock_client = AsyncMock()
        mock_client.tell_status.return_value = {
            "gid": gid,
            "status": "active",
            "totalLength": str(total_length),
            "completedLength": "0",
            "downloadSpeed": "0",
            "uploadSpeed": "0",
            "files": [{"path": "dummy"}],
            "connections": "0",
        }
        mock_client.force_remove.return_value = "OK"
        mock_client.remove_download_result.return_value = "OK"

        async def fake_space_info(user_id: int, quota: int):
            return {
                "available": total_length - 1,
                "used": 0,
                "frozen": 0,
                "quota": quota,
            }

        with patch("app.core.state.get_aria2_client", return_value=mock_client), \
             patch("app.services.storage.get_user_space_info", new_callable=AsyncMock, side_effect=fake_space_info), \
             patch("app.routers.tasks.broadcast_task_update_to_subscribers", new_callable=AsyncMock), \
             patch("app.aria2.sync.asyncio.sleep", new_callable=AsyncMock, side_effect=asyncio.CancelledError):
            with pytest.raises(asyncio.CancelledError):
                await sync_tasks(state, interval=0.01)

        async with get_session() as db:
            result = await db.exec(
                select(UserTaskSubscription).where(UserTaskSubscription.task_id == task_id)
            )
            subs = result.all()
            assert len(subs) == 2
            for sub in subs:
                assert sub.status == "failed"
                assert sub.frozen_space == 0
                assert sub.error_display == "用户配额空间不足"

            result = await db.exec(select(DownloadTask).where(DownloadTask.id == task_id))
            task = result.first()
            assert task is not None
            assert task.status == "error"
            assert task.gid is None
