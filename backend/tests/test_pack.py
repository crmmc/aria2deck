"""Tests for the async folder pack download feature.

Test scenarios:
1. Pack API Tests - Create, List, Get, Cancel, Download pack tasks
2. Space Calculation Tests - Server space, user space, folder size
"""

import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.db import execute, fetch_one, fetch_all, utc_now


# ========== Fixtures ==========

@pytest.fixture
def user_download_dir(test_user: dict, temp_db: str) -> Path:
    """Create user download directory with test files."""
    user_dir = Path(settings.download_dir) / str(test_user["id"])
    user_dir.mkdir(parents=True, exist_ok=True)
    return user_dir


@pytest.fixture
def test_folder(user_download_dir: Path) -> Path:
    """Create a test folder with files for packing."""
    folder = user_download_dir / "test_folder"
    folder.mkdir(exist_ok=True)

    # Create test files
    (folder / "file1.txt").write_text("Hello World!")
    (folder / "file2.txt").write_text("Test content " * 100)

    # Create subfolder with file
    subfolder = folder / "subfolder"
    subfolder.mkdir(exist_ok=True)
    (subfolder / "nested.txt").write_text("Nested file content")

    return folder


@pytest.fixture
def empty_folder(user_download_dir: Path) -> Path:
    """Create an empty folder."""
    folder = user_download_dir / "empty_folder"
    folder.mkdir(exist_ok=True)
    return folder


@pytest.fixture
def test_file(user_download_dir: Path) -> Path:
    """Create a single file (not a directory)."""
    file_path = user_download_dir / "single_file.txt"
    file_path.write_text("Single file content")
    return file_path


@pytest.fixture
def pending_pack_task(test_user: dict, temp_db: str) -> dict:
    """Create a pending pack task in the database."""
    now = utc_now()
    task_id = execute(
        """
        INSERT INTO pack_tasks
        (owner_id, folder_path, folder_size, reserved_space, status, progress, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [test_user["id"], "test_folder", 1000000, 1000000, "pending", 0, now, now]
    )
    return fetch_one("SELECT * FROM pack_tasks WHERE id = ?", [task_id])


@pytest.fixture
def packing_task(test_user: dict, temp_db: str) -> dict:
    """Create a packing (in-progress) task."""
    now = utc_now()
    task_id = execute(
        """
        INSERT INTO pack_tasks
        (owner_id, folder_path, folder_size, reserved_space, status, progress, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [test_user["id"], "test_folder", 2000000, 2000000, "packing", 50, now, now]
    )
    return fetch_one("SELECT * FROM pack_tasks WHERE id = ?", [task_id])


@pytest.fixture
def done_pack_task(test_user: dict, user_download_dir: Path, temp_db: str) -> dict:
    """Create a completed pack task with output file."""
    # Create the output file
    output_path = user_download_dir / "test_folder.zip"
    output_path.write_bytes(b"PK" + b"\x00" * 100)  # Minimal zip-like content

    now = utc_now()
    task_id = execute(
        """
        INSERT INTO pack_tasks
        (owner_id, folder_path, folder_size, reserved_space, output_path, output_size,
         status, progress, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [test_user["id"], "test_folder", 1000000, 0, str(output_path), 102,
         "done", 100, now, now]
    )
    return fetch_one("SELECT * FROM pack_tasks WHERE id = ?", [task_id])


@pytest.fixture
def failed_pack_task(test_user: dict, temp_db: str) -> dict:
    """Create a failed pack task."""
    now = utc_now()
    task_id = execute(
        """
        INSERT INTO pack_tasks
        (owner_id, folder_path, folder_size, reserved_space, status, progress,
         error_message, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [test_user["id"], "test_folder", 1000000, 0, "failed", 30,
         "7z command not found", now, now]
    )
    return fetch_one("SELECT * FROM pack_tasks WHERE id = ?", [task_id])


@pytest.fixture
def cancelled_pack_task(test_user: dict, temp_db: str) -> dict:
    """Create a cancelled pack task."""
    now = utc_now()
    task_id = execute(
        """
        INSERT INTO pack_tasks
        (owner_id, folder_path, folder_size, reserved_space, status, progress,
         created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [test_user["id"], "test_folder", 1000000, 0, "cancelled", 20, now, now]
    )
    return fetch_one("SELECT * FROM pack_tasks WHERE id = ?", [task_id])


@pytest.fixture
def other_user_pack_task(test_admin: dict, temp_db: str) -> dict:
    """Create a pack task belonging to another user (admin)."""
    now = utc_now()
    task_id = execute(
        """
        INSERT INTO pack_tasks
        (owner_id, folder_path, folder_size, reserved_space, status, progress,
         created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [test_admin["id"], "admin_folder", 5000000, 5000000, "pending", 0, now, now]
    )
    return fetch_one("SELECT * FROM pack_tasks WHERE id = ?", [task_id])


# ========== Create Pack Task Tests ==========

class TestCreatePackTask:
    """Tests for POST /api/files/pack endpoint."""

    def test_create_pack_task_success(
        self,
        authenticated_client: TestClient,
        test_folder: Path,
        test_user: dict,
    ):
        """Successfully create a pack task for a valid folder."""
        # Calculate expected folder size
        folder_path = test_folder.relative_to(Path(settings.download_dir) / str(test_user["id"]))

        with patch("app.routers.files.asyncio.create_task") as mock_create_task:
            with patch("app.services.pack.get_server_available_space", return_value=100 * 1024 * 1024 * 1024):
                response = authenticated_client.post(
                    "/api/files/pack",
                    json={"folder_path": str(folder_path)}
                )

        assert response.status_code == 201
        data = response.json()

        assert data["owner_id"] == test_user["id"]
        assert data["folder_path"] == str(folder_path)
        assert data["status"] == "pending"
        assert data["folder_size"] > 0
        assert data["reserved_space"] == data["folder_size"]

        # Verify async task was started
        mock_create_task.assert_called_once()

    def test_create_pack_task_folder_not_found(
        self,
        authenticated_client: TestClient,
        user_download_dir: Path,
    ):
        """Return 404 when folder does not exist."""
        response = authenticated_client.post(
            "/api/files/pack",
            json={"folder_path": "nonexistent_folder"}
        )

        assert response.status_code == 404
        assert "detail" in response.json()

    def test_create_pack_task_path_is_file(
        self,
        authenticated_client: TestClient,
        test_file: Path,
        test_user: dict,
    ):
        """Return 400 when path is a file, not a directory."""
        file_path = test_file.relative_to(Path(settings.download_dir) / str(test_user["id"]))

        response = authenticated_client.post(
            "/api/files/pack",
            json={"folder_path": str(file_path)}
        )

        assert response.status_code == 400
        assert "detail" in response.json()

    def test_create_pack_task_empty_folder(
        self,
        authenticated_client: TestClient,
        empty_folder: Path,
        test_user: dict,
    ):
        """Return 400 when folder is empty."""
        folder_path = empty_folder.relative_to(Path(settings.download_dir) / str(test_user["id"]))

        response = authenticated_client.post(
            "/api/files/pack",
            json={"folder_path": str(folder_path)}
        )

        assert response.status_code == 400
        assert "detail" in response.json()

    def test_create_pack_task_insufficient_space(
        self,
        authenticated_client: TestClient,
        test_folder: Path,
        test_user: dict,
    ):
        """Return 403 when user has insufficient space."""
        folder_path = test_folder.relative_to(Path(settings.download_dir) / str(test_user["id"]))

        # Mock very limited available space
        with patch("app.services.pack.get_server_available_space", return_value=10):
            with patch("app.services.pack.get_user_available_space_for_pack", return_value=10):
                response = authenticated_client.post(
                    "/api/files/pack",
                    json={"folder_path": str(folder_path)}
                )

        assert response.status_code == 403
        assert "detail" in response.json()

    def test_create_pack_task_duplicate_task(
        self,
        authenticated_client: TestClient,
        test_folder: Path,
        test_user: dict,
        pending_pack_task: dict,
    ):
        """Return 409 when duplicate task exists for same folder."""
        # pending_pack_task already created for "test_folder"
        folder_path = test_folder.relative_to(Path(settings.download_dir) / str(test_user["id"]))

        response = authenticated_client.post(
            "/api/files/pack",
            json={"folder_path": str(folder_path)}
        )

        assert response.status_code == 409
        assert "detail" in response.json()

    def test_create_pack_task_path_traversal_attack(
        self,
        authenticated_client: TestClient,
        user_download_dir: Path,
    ):
        """Return 403 for path traversal attack attempts."""
        response = authenticated_client.post(
            "/api/files/pack",
            json={"folder_path": "../../../etc"}
        )

        assert response.status_code == 403

    def test_create_pack_task_without_auth(
        self,
        client: TestClient,
    ):
        """Return 401 when not authenticated."""
        response = client.post(
            "/api/files/pack",
            json={"folder_path": "test_folder"}
        )

        assert response.status_code == 401


# ========== List Pack Tasks Tests ==========

class TestListPackTasks:
    """Tests for GET /api/files/pack endpoint."""

    def test_list_pack_tasks_returns_user_tasks_only(
        self,
        authenticated_client: TestClient,
        pending_pack_task: dict,
        other_user_pack_task: dict,
    ):
        """Only return tasks belonging to the authenticated user."""
        response = authenticated_client.get("/api/files/pack")

        assert response.status_code == 200
        tasks = response.json()

        # Should only contain user's task, not admin's task
        assert len(tasks) == 1
        assert tasks[0]["id"] == pending_pack_task["id"]

    def test_list_pack_tasks_ordered_by_created_at_desc(
        self,
        authenticated_client: TestClient,
        test_user: dict,
        temp_db: str,
    ):
        """Tasks should be returned in descending order by created_at."""
        # Create multiple tasks with different timestamps
        now = datetime.now(timezone.utc)

        task1_id = execute(
            """INSERT INTO pack_tasks
               (owner_id, folder_path, folder_size, reserved_space, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [test_user["id"], "folder1", 100, 100, "pending",
             (now.replace(hour=1)).isoformat(), utc_now()]
        )

        task2_id = execute(
            """INSERT INTO pack_tasks
               (owner_id, folder_path, folder_size, reserved_space, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [test_user["id"], "folder2", 200, 200, "done",
             (now.replace(hour=3)).isoformat(), utc_now()]
        )

        task3_id = execute(
            """INSERT INTO pack_tasks
               (owner_id, folder_path, folder_size, reserved_space, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [test_user["id"], "folder3", 300, 300, "packing",
             (now.replace(hour=2)).isoformat(), utc_now()]
        )

        response = authenticated_client.get("/api/files/pack")

        assert response.status_code == 200
        tasks = response.json()

        assert len(tasks) == 3
        # Most recent first (hour=3, hour=2, hour=1)
        assert tasks[0]["id"] == task2_id
        assert tasks[1]["id"] == task3_id
        assert tasks[2]["id"] == task1_id

    def test_list_pack_tasks_empty(
        self,
        authenticated_client: TestClient,
    ):
        """Return empty list when user has no tasks."""
        response = authenticated_client.get("/api/files/pack")

        assert response.status_code == 200
        assert response.json() == []


# ========== Get Pack Task Tests ==========

class TestGetPackTask:
    """Tests for GET /api/files/pack/{task_id} endpoint."""

    def test_get_pack_task_success(
        self,
        authenticated_client: TestClient,
        pending_pack_task: dict,
    ):
        """Successfully get own pack task details."""
        response = authenticated_client.get(f"/api/files/pack/{pending_pack_task['id']}")

        assert response.status_code == 200
        data = response.json()

        assert data["id"] == pending_pack_task["id"]
        assert data["folder_path"] == pending_pack_task["folder_path"]
        assert data["status"] == pending_pack_task["status"]

    def test_get_pack_task_not_found(
        self,
        authenticated_client: TestClient,
    ):
        """Return 404 for non-existent task."""
        response = authenticated_client.get("/api/files/pack/99999")

        assert response.status_code == 404

    def test_get_pack_task_other_user(
        self,
        authenticated_client: TestClient,
        other_user_pack_task: dict,
    ):
        """Return 404 when accessing another user's task."""
        response = authenticated_client.get(f"/api/files/pack/{other_user_pack_task['id']}")

        assert response.status_code == 404


# ========== Cancel Pack Task Tests ==========

class TestCancelPackTask:
    """Tests for DELETE /api/files/pack/{task_id} endpoint."""

    def test_cancel_pending_task_success(
        self,
        authenticated_client: TestClient,
        pending_pack_task: dict,
    ):
        """Successfully cancel a pending task."""
        with patch("app.services.pack.PackTaskManager.cancel_pack", new_callable=AsyncMock) as mock_cancel:
            mock_cancel.return_value = False  # No running process
            response = authenticated_client.delete(f"/api/files/pack/{pending_pack_task['id']}")

        assert response.status_code == 200
        assert response.json()["ok"] is True

        # Verify task status updated
        task = fetch_one("SELECT * FROM pack_tasks WHERE id = ?", [pending_pack_task["id"]])
        assert task["status"] == "cancelled"
        assert task["reserved_space"] == 0

    def test_cancel_packing_task_kills_process(
        self,
        authenticated_client: TestClient,
        packing_task: dict,
    ):
        """Successfully cancel a packing task (kills 7z process)."""
        with patch("app.services.pack.PackTaskManager.cancel_pack", new_callable=AsyncMock) as mock_cancel:
            mock_cancel.return_value = True  # Process was running and terminated
            response = authenticated_client.delete(f"/api/files/pack/{packing_task['id']}")

        assert response.status_code == 200
        mock_cancel.assert_called_once_with(packing_task["id"])

        # Verify reserved space released
        task = fetch_one("SELECT * FROM pack_tasks WHERE id = ?", [packing_task["id"]])
        assert task["reserved_space"] == 0

    def test_cancel_done_task_fails(
        self,
        authenticated_client: TestClient,
        done_pack_task: dict,
    ):
        """Cannot cancel a completed task."""
        response = authenticated_client.delete(f"/api/files/pack/{done_pack_task['id']}")

        assert response.status_code == 400
        assert "detail" in response.json()

    def test_cancel_failed_task_fails(
        self,
        authenticated_client: TestClient,
        failed_pack_task: dict,
    ):
        """Cannot cancel a failed task."""
        response = authenticated_client.delete(f"/api/files/pack/{failed_pack_task['id']}")

        assert response.status_code == 400

    def test_cancel_cancelled_task_fails(
        self,
        authenticated_client: TestClient,
        cancelled_pack_task: dict,
    ):
        """Cannot cancel an already cancelled task."""
        response = authenticated_client.delete(f"/api/files/pack/{cancelled_pack_task['id']}")

        assert response.status_code == 400

    def test_cancel_task_not_found(
        self,
        authenticated_client: TestClient,
    ):
        """Return 404 for non-existent task."""
        response = authenticated_client.delete("/api/files/pack/99999")

        assert response.status_code == 404

    def test_cancel_other_user_task(
        self,
        authenticated_client: TestClient,
        other_user_pack_task: dict,
    ):
        """Cannot cancel another user's task."""
        response = authenticated_client.delete(f"/api/files/pack/{other_user_pack_task['id']}")

        assert response.status_code == 404


# ========== Download Pack Result Tests ==========

class TestDownloadPackResult:
    """Tests for GET /api/files/pack/{task_id}/download endpoint."""

    def test_download_completed_pack_success(
        self,
        authenticated_client: TestClient,
        done_pack_task: dict,
    ):
        """Successfully download a completed pack file."""
        response = authenticated_client.get(f"/api/files/pack/{done_pack_task['id']}/download")

        assert response.status_code == 200
        assert response.headers["content-type"] == "application/octet-stream"
        assert "content-disposition" in response.headers

    def test_download_task_not_done(
        self,
        authenticated_client: TestClient,
        pending_pack_task: dict,
    ):
        """Return 400 when task is not done."""
        response = authenticated_client.get(f"/api/files/pack/{pending_pack_task['id']}/download")

        assert response.status_code == 400
        assert "detail" in response.json()

    def test_download_output_file_missing(
        self,
        authenticated_client: TestClient,
        done_pack_task: dict,
    ):
        """Return 404 when output file is missing."""
        # Delete the output file
        output_path = Path(done_pack_task["output_path"])
        if output_path.exists():
            output_path.unlink()

        response = authenticated_client.get(f"/api/files/pack/{done_pack_task['id']}/download")

        assert response.status_code == 404

    def test_download_task_not_found(
        self,
        authenticated_client: TestClient,
    ):
        """Return 404 for non-existent task."""
        response = authenticated_client.get("/api/files/pack/99999/download")

        assert response.status_code == 404

    def test_download_other_user_task(
        self,
        authenticated_client: TestClient,
        other_user_pack_task: dict,
    ):
        """Cannot download another user's task."""
        response = authenticated_client.get(f"/api/files/pack/{other_user_pack_task['id']}/download")

        assert response.status_code == 404

    def test_download_path_traversal_protection(
        self,
        authenticated_client: TestClient,
        test_user: dict,
        temp_db: str,
    ):
        """Return 403 when output_path is outside user directory."""
        now = utc_now()
        task_id = execute(
            """INSERT INTO pack_tasks
               (owner_id, folder_path, folder_size, reserved_space, output_path, output_size,
                status, progress, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [test_user["id"], "folder", 1000, 0, "/etc/passwd", 100,
             "done", 100, now, now]
        )

        response = authenticated_client.get(f"/api/files/pack/{task_id}/download")

        assert response.status_code == 403


# ========== Get Available Space Tests ==========

class TestGetAvailableSpace:
    """Tests for GET /api/files/pack/available-space endpoint."""

    def test_get_available_space_basic(
        self,
        authenticated_client: TestClient,
    ):
        """Return user_available and server_available."""
        with patch("app.services.pack.get_user_available_space_for_pack", return_value=50 * 1024 * 1024 * 1024):
            with patch("app.services.pack.get_server_available_space", return_value=100 * 1024 * 1024 * 1024):
                response = authenticated_client.get("/api/files/pack/available-space")

        assert response.status_code == 200
        data = response.json()

        assert data["user_available"] == 50 * 1024 * 1024 * 1024
        assert data["server_available"] == 100 * 1024 * 1024 * 1024

    def test_get_available_space_with_folder_path(
        self,
        authenticated_client: TestClient,
        test_folder: Path,
        test_user: dict,
    ):
        """Return folder_size when folder_path is provided."""
        folder_path = test_folder.relative_to(Path(settings.download_dir) / str(test_user["id"]))

        with patch("app.services.pack.get_user_available_space_for_pack", return_value=50 * 1024 * 1024 * 1024):
            with patch("app.services.pack.get_server_available_space", return_value=100 * 1024 * 1024 * 1024):
                response = authenticated_client.get(
                    f"/api/files/pack/available-space?folder_path={folder_path}"
                )

        assert response.status_code == 200
        data = response.json()

        assert "folder_size" in data
        assert data["folder_size"] > 0

    def test_get_available_space_nonexistent_folder(
        self,
        authenticated_client: TestClient,
    ):
        """Return folder_size=0 for non-existent folder."""
        with patch("app.services.pack.get_user_available_space_for_pack", return_value=50 * 1024 * 1024 * 1024):
            with patch("app.services.pack.get_server_available_space", return_value=100 * 1024 * 1024 * 1024):
                response = authenticated_client.get(
                    "/api/files/pack/available-space?folder_path=nonexistent"
                )

        assert response.status_code == 200
        data = response.json()

        assert data["folder_size"] == 0


# ========== Space Calculation Tests ==========

class TestSpaceCalculation:
    """Tests for space calculation functions in pack.py."""

    def test_get_server_available_space(
        self,
        temp_db: str,
    ):
        """get_server_available_space returns disk free minus reserved."""
        from app.services.pack import get_server_available_space, get_reserved_space

        # Mock disk usage
        mock_disk = MagicMock()
        mock_disk.free = 100 * 1024 * 1024 * 1024  # 100GB

        with patch("shutil.disk_usage", return_value=mock_disk):
            available = get_server_available_space()

        reserved = get_reserved_space()
        expected = 100 * 1024 * 1024 * 1024 - reserved
        assert available == expected

    def test_get_server_available_space_with_reserved(
        self,
        test_user: dict,
        temp_db: str,
    ):
        """get_server_available_space correctly subtracts reserved space."""
        from app.services.pack import get_server_available_space

        # Create pack tasks with reserved space
        now = utc_now()
        execute(
            """INSERT INTO pack_tasks
               (owner_id, folder_path, folder_size, reserved_space, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [test_user["id"], "folder1", 1000000, 1000000, "pending", now, now]
        )
        execute(
            """INSERT INTO pack_tasks
               (owner_id, folder_path, folder_size, reserved_space, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [test_user["id"], "folder2", 2000000, 2000000, "packing", now, now]
        )
        # Completed task should not count
        execute(
            """INSERT INTO pack_tasks
               (owner_id, folder_path, folder_size, reserved_space, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [test_user["id"], "folder3", 3000000, 0, "done", now, now]
        )

        mock_disk = MagicMock()
        mock_disk.free = 100 * 1024 * 1024 * 1024  # 100GB

        with patch("shutil.disk_usage", return_value=mock_disk):
            available = get_server_available_space()

        # Should subtract 3MB (1MB + 2MB) from pending/packing tasks
        expected = 100 * 1024 * 1024 * 1024 - 3000000
        assert available == expected

    def test_get_user_available_space_for_pack(
        self,
        test_user: dict,
        user_download_dir: Path,
        temp_db: str,
    ):
        """get_user_available_space_for_pack returns min of quota remaining and server available."""
        from app.services.pack import get_user_available_space_for_pack

        # Create a file to use some space
        test_file = user_download_dir / "existing_file.bin"
        test_file.write_bytes(b"\x00" * 10000)  # 10KB

        mock_disk = MagicMock()
        mock_disk.free = 50 * 1024 * 1024 * 1024  # 50GB server space

        with patch("shutil.disk_usage", return_value=mock_disk):
            available = get_user_available_space_for_pack(test_user["id"])

        # User quota is 100GB, used is ~10KB
        # Server available is 50GB
        # Should return min of (100GB - 10KB, 50GB) = 50GB
        assert available <= 50 * 1024 * 1024 * 1024

    def test_get_user_available_space_with_other_tasks_reserved(
        self,
        test_user: dict,
        test_admin: dict,
        user_download_dir: Path,
        temp_db: str,
    ):
        """get_user_available_space correctly accounts for reserved space from other tasks."""
        from app.services.pack import get_user_available_space_for_pack

        # Create reserved space from another user's task
        now = utc_now()
        execute(
            """INSERT INTO pack_tasks
               (owner_id, folder_path, folder_size, reserved_space, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [test_admin["id"], "admin_folder", 10 * 1024 * 1024 * 1024, 10 * 1024 * 1024 * 1024,
             "packing", now, now]
        )

        mock_disk = MagicMock()
        mock_disk.free = 50 * 1024 * 1024 * 1024  # 50GB server space

        with patch("shutil.disk_usage", return_value=mock_disk):
            available = get_user_available_space_for_pack(test_user["id"])

        # Server available = 50GB - 10GB reserved = 40GB
        # User quota remaining = ~100GB
        # Should return min = 40GB
        assert available <= 40 * 1024 * 1024 * 1024

    def test_calculate_folder_size_basic(
        self,
        test_folder: Path,
    ):
        """calculate_folder_size correctly sums all file sizes recursively."""
        from app.services.pack import calculate_folder_size

        size = calculate_folder_size(test_folder)

        # Should be > 0 (we created files)
        assert size > 0

        # Calculate expected size manually
        expected = 0
        for f in test_folder.rglob("*"):
            if f.is_file():
                expected += f.stat().st_size

        assert size == expected

    def test_calculate_folder_size_empty(
        self,
        empty_folder: Path,
    ):
        """calculate_folder_size returns 0 for empty folder."""
        from app.services.pack import calculate_folder_size

        size = calculate_folder_size(empty_folder)
        assert size == 0

    def test_calculate_folder_size_nonexistent(
        self,
        temp_db: str,
    ):
        """calculate_folder_size returns 0 for non-existent folder."""
        from app.services.pack import calculate_folder_size

        size = calculate_folder_size(Path("/nonexistent/path"))
        assert size == 0


# ========== PackTaskManager Tests ==========

class TestPackTaskManager:
    """Tests for PackTaskManager class."""

    def test_get_pack_format_default(
        self,
        temp_db: str,
    ):
        """get_pack_format returns 'zip' by default."""
        from app.services.pack import PackTaskManager

        with patch("app.routers.config.get_config_value", return_value=None):
            format_val = PackTaskManager.get_pack_format()

        assert format_val == "zip"

    def test_get_pack_format_zip(
        self,
        temp_db: str,
    ):
        """get_pack_format returns 'zip' when configured."""
        from app.services.pack import PackTaskManager

        with patch("app.routers.config.get_config_value", return_value="zip"):
            format_val = PackTaskManager.get_pack_format()

        assert format_val == "zip"

    def test_get_pack_format_7z(
        self,
        temp_db: str,
    ):
        """get_pack_format returns '7z' when configured."""
        from app.services.pack import PackTaskManager

        with patch("app.routers.config.get_config_value", return_value="7z"):
            format_val = PackTaskManager.get_pack_format()

        assert format_val == "7z"

    def test_get_pack_format_invalid(
        self,
        temp_db: str,
    ):
        """get_pack_format returns 'zip' for invalid values."""
        from app.services.pack import PackTaskManager

        with patch("app.routers.config.get_config_value", return_value="invalid"):
            format_val = PackTaskManager.get_pack_format()

        assert format_val == "zip"

    def test_get_compression_level_default(
        self,
        temp_db: str,
    ):
        """get_compression_level returns 5 by default."""
        from app.services.pack import PackTaskManager

        with patch("app.routers.config.get_config_value", return_value=None):
            level = PackTaskManager.get_compression_level()

        assert level == 5

    def test_get_compression_level_valid(
        self,
        temp_db: str,
    ):
        """get_compression_level returns configured value."""
        from app.services.pack import PackTaskManager

        with patch("app.routers.config.get_config_value", return_value="7"):
            level = PackTaskManager.get_compression_level()

        assert level == 7

    def test_get_compression_level_clamped_low(
        self,
        temp_db: str,
    ):
        """get_compression_level clamps values below 1 to 1."""
        from app.services.pack import PackTaskManager

        with patch("app.routers.config.get_config_value", return_value="0"):
            level = PackTaskManager.get_compression_level()

        assert level == 1

    def test_get_compression_level_clamped_high(
        self,
        temp_db: str,
    ):
        """get_compression_level clamps values above 9 to 9."""
        from app.services.pack import PackTaskManager

        with patch("app.routers.config.get_config_value", return_value="15"):
            level = PackTaskManager.get_compression_level()

        assert level == 9

    @pytest.mark.asyncio
    async def test_cancel_pack_not_running(
        self,
        temp_db: str,
    ):
        """cancel_pack returns False when task is not running."""
        from app.services.pack import PackTaskManager

        result = await PackTaskManager.cancel_pack(99999)
        assert result is False

    @pytest.mark.asyncio
    async def test_cancel_pack_running(
        self,
        temp_db: str,
    ):
        """cancel_pack terminates running process and returns True."""
        import asyncio
        from app.services.pack import PackTaskManager

        # Mock a running process
        mock_process = MagicMock()
        mock_process.terminate = MagicMock()
        mock_process.wait = AsyncMock()

        task_id = 12345
        PackTaskManager._running_tasks[task_id] = mock_process

        try:
            result = await PackTaskManager.cancel_pack(task_id)

            assert result is True
            mock_process.terminate.assert_called_once()
        finally:
            # Cleanup
            PackTaskManager._running_tasks.pop(task_id, None)


# ========== Integration Tests ==========

class TestPackIntegration:
    """Integration tests for the complete pack workflow."""

    def test_full_pack_workflow_api(
        self,
        authenticated_client: TestClient,
        test_folder: Path,
        test_user: dict,
    ):
        """Test complete workflow: create -> list -> get -> cancel."""
        folder_path = test_folder.relative_to(Path(settings.download_dir) / str(test_user["id"]))

        # 1. Create pack task
        with patch("app.routers.files.asyncio.create_task"):
            with patch("app.services.pack.get_server_available_space", return_value=100 * 1024 * 1024 * 1024):
                create_response = authenticated_client.post(
                    "/api/files/pack",
                    json={"folder_path": str(folder_path)}
                )

        assert create_response.status_code == 201
        task_id = create_response.json()["id"]

        # 2. List tasks - should include new task
        list_response = authenticated_client.get("/api/files/pack")
        assert list_response.status_code == 200
        tasks = list_response.json()
        assert any(t["id"] == task_id for t in tasks)

        # 3. Get task details
        get_response = authenticated_client.get(f"/api/files/pack/{task_id}")
        assert get_response.status_code == 200
        assert get_response.json()["id"] == task_id

        # 4. Cancel task
        with patch("app.services.pack.PackTaskManager.cancel_pack", new_callable=AsyncMock) as mock_cancel:
            mock_cancel.return_value = False
            cancel_response = authenticated_client.delete(f"/api/files/pack/{task_id}")

        assert cancel_response.status_code == 200

        # 5. Verify task is cancelled
        task = fetch_one("SELECT * FROM pack_tasks WHERE id = ?", [task_id])
        assert task["status"] == "cancelled"
