"""Tests for task cancellation endpoint edge cases."""
import pytest
from sqlmodel import select

from app.database import get_session
from app.models import DownloadTask, UserTaskSubscription, utc_now_str


class TestCancelTaskWithoutGid:
    """Cancel queued task without gid should mark task as cancelled."""

    @pytest.mark.asyncio
    async def test_cancel_task_without_gid_marks_error(self, authenticated_client, test_user):
        # Create queued task without gid
        async with get_session() as db:
            task = DownloadTask(
                uri_hash="cancel_no_gid_hash",
                uri="https://example.com/no_gid.zip",
                gid=None,
                status="queued",
                name="no_gid.zip",
                total_length=0,
                completed_length=0,
                created_at=utc_now_str(),
                updated_at=utc_now_str(),
            )
            db.add(task)
            await db.commit()
            await db.refresh(task)

            subscription = UserTaskSubscription(
                owner_id=test_user["id"],
                task_id=task.id,
                frozen_space=0,
                status="pending",
                created_at=utc_now_str(),
            )
            db.add(subscription)
            await db.commit()
            await db.refresh(subscription)

            sub_id = subscription.id

        response = authenticated_client.delete(f"/api/tasks/{sub_id}")
        assert response.status_code == 200

        async with get_session() as db:
            result = await db.exec(
                select(UserTaskSubscription).where(UserTaskSubscription.id == sub_id)
            )
            assert result.first() is None

            result = await db.exec(
                select(DownloadTask).where(DownloadTask.id == task.id)
            )
            db_task = result.first()
            assert db_task is not None
            assert db_task.status == "error"
            assert db_task.error_display == "已取消"
