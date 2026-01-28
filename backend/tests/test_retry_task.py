"""Tests for the retry task endpoint (POST /tasks/{id}/retry).

Test scenarios:
1. Success case: Retry a failed task, verify new task created, old task deleted
2. Torrent task rejection: Try to retry a torrent task (uri = "[torrent]"), expect 400 error
3. Task not found: Try to retry non-existent task, expect 404
4. Unauthorized: Try to retry another user's task, expect 404
5. Aria2 failure: Mock aria2 client failure, verify old task preserved
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.db import fetch_one, fetch_all, execute


class TestRetryTaskSuccess:
    """Test successful retry of a failed task."""

    def test_retry_failed_task_creates_new_task(
        self,
        authenticated_client: TestClient,
        failed_task: dict,
        mock_aria2_client: AsyncMock,
    ):
        """Retry a failed HTTP task should create new task and delete old one."""
        with patch("app.routers.tasks.get_aria2_client", return_value=mock_aria2_client):
            response = authenticated_client.post(f"/api/tasks/{failed_task['id']}/retry")

        assert response.status_code == 200
        data = response.json()

        # Verify new task was created
        assert data["id"] != failed_task["id"]
        assert data["uri"] == failed_task["uri"]
        assert data["status"] in ["queued", "active"]
        assert data["gid"] == "test_gid_12345"

        # Verify old task was deleted
        old_task = fetch_one("SELECT * FROM tasks WHERE id = ?", [failed_task["id"]])
        assert old_task is None

        # Verify aria2 was called
        mock_aria2_client.add_uri.assert_called_once()

    def test_retry_task_preserves_original_uri(
        self,
        authenticated_client: TestClient,
        failed_task: dict,
        mock_aria2_client: AsyncMock,
    ):
        """Retry should use the original URI from the failed task."""
        original_uri = failed_task["uri"]

        with patch("app.routers.tasks.get_aria2_client", return_value=mock_aria2_client):
            response = authenticated_client.post(f"/api/tasks/{failed_task['id']}/retry")

        assert response.status_code == 200
        data = response.json()
        assert data["uri"] == original_uri

    def test_retry_task_cleans_up_old_aria2_task(
        self,
        authenticated_client: TestClient,
        failed_task: dict,
        mock_aria2_client: AsyncMock,
    ):
        """Retry should attempt to clean up the old task from aria2."""
        with patch("app.routers.tasks.get_aria2_client", return_value=mock_aria2_client):
            response = authenticated_client.post(f"/api/tasks/{failed_task['id']}/retry")

        assert response.status_code == 200

        # Verify cleanup was attempted
        mock_aria2_client.force_remove.assert_called_once_with(failed_task["gid"])
        mock_aria2_client.remove_download_result.assert_called_once_with(failed_task["gid"])


class TestRetryTorrentTaskRejection:
    """Test that torrent tasks cannot be retried."""

    def test_retry_torrent_task_returns_400(
        self,
        authenticated_client: TestClient,
        torrent_task: dict,
    ):
        """Retry a torrent task should return 400 error."""
        response = authenticated_client.post(f"/api/tasks/{torrent_task['id']}/retry")

        assert response.status_code == 400
        data = response.json()
        assert "detail" in data
        assert "torrent" in data["detail"].lower() or "种子" in data["detail"]

    def test_torrent_task_preserved_after_rejection(
        self,
        authenticated_client: TestClient,
        torrent_task: dict,
    ):
        """Torrent task should remain unchanged after retry rejection."""
        response = authenticated_client.post(f"/api/tasks/{torrent_task['id']}/retry")

        assert response.status_code == 400

        # Verify task is still in database
        task = fetch_one("SELECT * FROM tasks WHERE id = ?", [torrent_task["id"]])
        assert task is not None
        assert task["uri"] == "[torrent]"
        assert task["status"] == "error"


class TestRetryTaskNotFound:
    """Test retry of non-existent task."""

    def test_retry_nonexistent_task_returns_404(
        self,
        authenticated_client: TestClient,
    ):
        """Retry a non-existent task should return 404."""
        response = authenticated_client.post("/api/tasks/99999/retry")

        assert response.status_code == 404
        data = response.json()
        assert "detail" in data

    def test_retry_deleted_task_returns_404(
        self,
        authenticated_client: TestClient,
        failed_task: dict,
    ):
        """Retry a deleted task should return 404."""
        # Delete the task first
        execute("DELETE FROM tasks WHERE id = ?", [failed_task["id"]])

        response = authenticated_client.post(f"/api/tasks/{failed_task['id']}/retry")

        assert response.status_code == 404


class TestRetryTaskUnauthorized:
    """Test retry of another user's task."""

    def test_retry_other_user_task_returns_404(
        self,
        authenticated_client: TestClient,
        other_user_task: dict,
    ):
        """Retry another user's task should return 404 (task not visible)."""
        response = authenticated_client.post(f"/api/tasks/{other_user_task['id']}/retry")

        assert response.status_code == 404

    def test_other_user_task_preserved(
        self,
        authenticated_client: TestClient,
        other_user_task: dict,
    ):
        """Other user's task should remain unchanged."""
        response = authenticated_client.post(f"/api/tasks/{other_user_task['id']}/retry")

        assert response.status_code == 404

        # Verify task is still in database
        task = fetch_one("SELECT * FROM tasks WHERE id = ?", [other_user_task["id"]])
        assert task is not None
        assert task["status"] == "error"

    def test_retry_without_authentication_returns_401(
        self,
        client: TestClient,
        failed_task: dict,
    ):
        """Retry without authentication should return 401."""
        response = client.post(f"/api/tasks/{failed_task['id']}/retry")

        assert response.status_code == 401


class TestRetryTaskAria2Failure:
    """Test retry when aria2 fails to create new task."""

    def test_aria2_failure_preserves_old_task(
        self,
        authenticated_client: TestClient,
        failed_task: dict,
        mock_aria2_client: AsyncMock,
    ):
        """When aria2 fails, old task should be preserved."""
        mock_aria2_client.add_uri.side_effect = Exception("aria2 connection failed")

        with patch("app.routers.tasks.get_aria2_client", return_value=mock_aria2_client):
            response = authenticated_client.post(f"/api/tasks/{failed_task['id']}/retry")

        assert response.status_code == 500
        data = response.json()
        assert "detail" in data
        assert "aria2" in data["detail"].lower()

        # Verify old task is preserved
        old_task = fetch_one("SELECT * FROM tasks WHERE id = ?", [failed_task["id"]])
        assert old_task is not None
        assert old_task["status"] == "error"

    def test_aria2_failure_cleans_up_new_task(
        self,
        authenticated_client: TestClient,
        failed_task: dict,
        mock_aria2_client: AsyncMock,
    ):
        """When aria2 fails, new task record should be deleted."""
        mock_aria2_client.add_uri.side_effect = Exception("aria2 connection failed")

        # Count tasks before
        tasks_before = fetch_all("SELECT * FROM tasks")
        count_before = len(tasks_before)

        with patch("app.routers.tasks.get_aria2_client", return_value=mock_aria2_client):
            response = authenticated_client.post(f"/api/tasks/{failed_task['id']}/retry")

        assert response.status_code == 500

        # Count tasks after - should be same as before (no new task left behind)
        tasks_after = fetch_all("SELECT * FROM tasks")
        count_after = len(tasks_after)
        assert count_after == count_before


class TestRetryTaskDiskSpaceCheck:
    """Test disk space validation during retry."""

    def test_retry_fails_when_disk_full(
        self,
        authenticated_client: TestClient,
        failed_task: dict,
    ):
        """Retry should fail if disk space is insufficient."""
        with patch("app.routers.tasks._check_disk_space", return_value=(False, 100 * 1024 * 1024)):
            response = authenticated_client.post(f"/api/tasks/{failed_task['id']}/retry")

        assert response.status_code == 403
        data = response.json()
        assert "detail" in data
        assert "磁盘" in data["detail"] or "disk" in data["detail"].lower()

    # Note: File size and user quota checks are now handled in aria2 hook,
    # not during task creation. See test_hook_space_check.py for those tests.


class TestRetryTaskIdempotency:
    """Test edge cases around retry operations."""

    def test_cannot_retry_already_retried_task(
        self,
        authenticated_client: TestClient,
        failed_task: dict,
        mock_aria2_client: AsyncMock,
    ):
        """After successful retry, old task ID should no longer exist."""
        # First retry
        with patch("app.routers.tasks.get_aria2_client", return_value=mock_aria2_client):
            response1 = authenticated_client.post(f"/api/tasks/{failed_task['id']}/retry")

        assert response1.status_code == 200

        # Second retry with same ID should fail (task no longer exists)
        with patch("app.routers.tasks.get_aria2_client", return_value=mock_aria2_client):
            response2 = authenticated_client.post(f"/api/tasks/{failed_task['id']}/retry")

        assert response2.status_code == 404

    def test_retry_task_without_gid_works(
        self,
        authenticated_client: TestClient,
        test_user: dict,
        temp_db: str,
        mock_aria2_client: AsyncMock,
    ):
        """Retry a task that has no GID (never started in aria2) should work."""
        # Create a task without GID
        now = datetime.now(timezone.utc).isoformat()
        task_id = execute(
            """
            INSERT INTO tasks (owner_id, gid, uri, status, name, created_at, updated_at, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [test_user["id"], None, "https://example.com/test.zip", "error", "test.zip", now, now, "Failed to start"]
        )

        with patch("app.routers.tasks.get_aria2_client", return_value=mock_aria2_client):
            response = authenticated_client.post(f"/api/tasks/{task_id}/retry")

        assert response.status_code == 200

        # Verify force_remove was not called (no gid to remove)
        mock_aria2_client.force_remove.assert_not_called()
