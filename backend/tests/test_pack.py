"""Tests for the async folder pack download feature.

Test scenarios:
1. Pack API Tests - Create, List, Get, Cancel, Download pack tasks
2. Space Calculation Tests - Server space, user space, folder size
"""

import json
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


# ========== Helper ==========

def _create_user_files(user_id: int, user_dir: Path, files: list[tuple[str, int]]) -> list[int]:
    """Create StoredFile + UserFile records and physical files.

    Args:
        user_id: owner id
        user_dir: user download directory
        files: list of (filename, size) tuples

    Returns:
        list of UserFile IDs
    """
    ids = []
    for name, size in files:
        real_path = str(user_dir / name)
        os.makedirs(os.path.dirname(real_path), exist_ok=True)
        with open(real_path, "wb") as f:
            f.write(b"x" * size)

        stored_id = execute(
            "INSERT INTO stored_files (content_hash, real_path, size, is_directory, ref_count, original_name, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [f"hash_{name}_{user_id}", real_path, size, 0, 1, name, utc_now()],
        )
        uf_id = execute(
            "INSERT INTO user_files (owner_id, stored_file_id, display_name, created_at) VALUES (?, ?, ?, ?)",
            [user_id, stored_id, name, utc_now()],
        )
        ids.append(uf_id)
    return ids


def _create_user_dir_file(user_id: int, user_dir: Path, dir_name: str) -> int:
    """Create a StoredFile (directory) + UserFile record with physical directory.

    Returns:
        UserFile ID
    """
    real_path = str(user_dir / dir_name)
    os.makedirs(real_path, exist_ok=True)
    # Create some files inside
    for i in range(3):
        with open(os.path.join(real_path, f"file{i}.txt"), "w") as f:
            f.write(f"content {i}" * 100)

    total_size = sum(
        os.path.getsize(os.path.join(real_path, f))
        for f in os.listdir(real_path)
        if os.path.isfile(os.path.join(real_path, f))
    )

    stored_id = execute(
        "INSERT INTO stored_files (content_hash, real_path, size, is_directory, ref_count, original_name, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [f"hash_{dir_name}_{user_id}", real_path, total_size, 1, 1, dir_name, utc_now()],
    )
    uf_id = execute(
        "INSERT INTO user_files (owner_id, stored_file_id, display_name, created_at) VALUES (?, ?, ?, ?)",
        [user_id, stored_id, dir_name, utc_now()],
    )
    return uf_id


def test_cleanup_pack_output_rejects_outside_download_dir(temp_db: str):
    from app.services.pack import cleanup_pack_output

    outside_dir = Path(tempfile.mkdtemp())
    outside_file = outside_dir / "outside.zip"
    outside_file.write_bytes(b"outside")

    deleted = cleanup_pack_output(outside_file)

    assert deleted is False
    assert outside_file.exists()


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
        user_download_dir: Path,
        test_user: dict,
    ):
        """Successfully create a pack task for valid file IDs."""
        file_ids = _create_user_files(test_user["id"], user_download_dir, [
            ("file1.txt", 1200),
            ("file2.txt", 1300),
        ])

        with patch("app.services.pack.PackTaskManager.start_pack", new_callable=AsyncMock) as mock_start_pack:
            response = authenticated_client.post(
                "/api/files/pack",
                json={"file_ids": file_ids}
            )

        assert response.status_code == 201
        data = response.json()

        assert data["owner_id"] == test_user["id"]
        assert data["status"] == "pending"
        assert data["folder_size"] > 0
        assert data["reserved_space"] == data["folder_size"]

    def test_create_pack_task_file_not_found(
        self,
        authenticated_client: TestClient,
        user_download_dir: Path,
    ):
        """Return 404 when file IDs do not exist."""
        response = authenticated_client.post(
            "/api/files/pack",
            json={"file_ids": [99999]}
        )

        assert response.status_code == 404
        assert "detail" in response.json()

    def test_create_pack_task_single_file(
        self,
        authenticated_client: TestClient,
        user_download_dir: Path,
        test_user: dict,
    ):
        """Can pack a single file."""
        file_ids = _create_user_files(test_user["id"], user_download_dir, [
            ("single_file.txt", 500),
        ])

        with patch("app.services.pack.PackTaskManager.start_pack", new_callable=AsyncMock):
            response = authenticated_client.post(
                "/api/files/pack",
                json={"file_ids": file_ids}
            )

        assert response.status_code == 201
        data = response.json()
        assert data["folder_size"] > 0

    def test_create_pack_task_empty_file_ids(
        self,
        authenticated_client: TestClient,
        user_download_dir: Path,
    ):
        """Return 422 when file_ids is empty (Pydantic min_length=1)."""
        response = authenticated_client.post(
            "/api/files/pack",
            json={"file_ids": []}
        )

        assert response.status_code == 422

    def test_create_pack_task_insufficient_space(
        self,
        authenticated_client: TestClient,
        user_download_dir: Path,
        test_user: dict,
    ):
        """Return 403 when user has insufficient space."""
        file_ids = _create_user_files(test_user["id"], user_download_dir, [
            ("bigfile.bin", 1000),
        ])

        # Mock very limited available space
        with patch("app.services.storage.get_user_space_info", new_callable=AsyncMock, return_value={
            "quota": 100, "used": 99, "frozen": 0, "available": 1,
        }):
            response = authenticated_client.post(
                "/api/files/pack",
                json={"file_ids": file_ids}
            )

        assert response.status_code == 403
        assert "detail" in response.json()

    def test_create_pack_task_duplicate_task(
        self,
        authenticated_client: TestClient,
        user_download_dir: Path,
        test_user: dict,
    ):
        """Return 409 when duplicate task exists for same file IDs."""
        file_ids = _create_user_files(test_user["id"], user_download_dir, [
            ("dup_file.txt", 500),
        ])

        # Create an existing pending task with same folder_path (JSON file_ids)
        folder_path_value = json.dumps(file_ids)
        now = utc_now()
        execute(
            """INSERT INTO pack_tasks
               (owner_id, folder_path, folder_size, reserved_space, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [test_user["id"], folder_path_value, 500, 500, "pending", now, now]
        )

        response = authenticated_client.post(
            "/api/files/pack",
            json={"file_ids": file_ids}
        )

        assert response.status_code == 409
        assert "detail" in response.json()

    def test_create_pack_task_rejects_done_duplicate(
        self,
        authenticated_client: TestClient,
        user_download_dir: Path,
        test_user: dict,
    ):
        """Return 409 with file hint when same files already packed."""
        file_ids = _create_user_files(test_user["id"], user_download_dir, [
            ("done_dup.txt", 300),
        ])

        # Simulate a completed pack task with stored_file_id
        folder_path_value = json.dumps(sorted(file_ids))
        stored_id = execute(
            "INSERT INTO stored_files (content_hash, real_path, size, is_directory, ref_count, original_name, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ["hash_pack_done", str(user_download_dir / "done_dup.7z"), 200, 0, 1, "done_dup.7z", utc_now()],
        )
        execute(
            "INSERT INTO user_files (owner_id, stored_file_id, display_name, created_at) VALUES (?, ?, ?, ?)",
            [test_user["id"], stored_id, "done_dup.7z", utc_now()],
        )
        now = utc_now()
        execute(
            """INSERT INTO pack_tasks
               (owner_id, folder_path, folder_size, reserved_space, status, stored_file_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [test_user["id"], folder_path_value, 300, 300, "done", stored_id, now, now],
        )

        response = authenticated_client.post(
            "/api/files/pack",
            json={"file_ids": file_ids},
        )

        assert response.status_code == 409
        assert "done_dup.7z" in response.json()["detail"]

    def test_create_pack_task_allows_repack_after_file_deleted(
        self,
        authenticated_client: TestClient,
        user_download_dir: Path,
        test_user: dict,
    ):
        """Allow repacking when the previous pack output UserFile was deleted."""
        file_ids = _create_user_files(test_user["id"], user_download_dir, [
            ("repack.txt", 300),
        ])

        # Simulate a completed pack task (stored_file exists but NO user_file)
        folder_path_value = json.dumps(sorted(file_ids))
        stored_id = execute(
            "INSERT INTO stored_files (content_hash, real_path, size, is_directory, ref_count, original_name, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ["hash_repack", str(user_download_dir / "repack.7z"), 200, 0, 0, "repack.7z", utc_now()],
        )
        now = utc_now()
        execute(
            """INSERT INTO pack_tasks
               (owner_id, folder_path, folder_size, reserved_space, status, stored_file_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [test_user["id"], folder_path_value, 300, 0, "done", stored_id, now, now],
        )
        # Note: no user_file row — simulates user having deleted the pack output

        response = authenticated_client.post(
            "/api/files/pack",
            json={"file_ids": file_ids},
        )

        # Should NOT be 409 — the file was deleted, allow repack
        assert response.status_code != 409

    def test_create_pack_task_sorted_file_ids_dedup(
        self,
        authenticated_client: TestClient,
        user_download_dir: Path,
        test_user: dict,
    ):
        """Detect duplicate even when file_ids are in different order."""
        file_ids = _create_user_files(test_user["id"], user_download_dir, [
            ("sort_a.txt", 100),
            ("sort_b.txt", 200),
        ])

        # Insert existing pending task with sorted file_ids
        folder_path_value = json.dumps(sorted(file_ids))
        now = utc_now()
        execute(
            """INSERT INTO pack_tasks
               (owner_id, folder_path, folder_size, reserved_space, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [test_user["id"], folder_path_value, 300, 300, "pending", now, now],
        )

        # Submit with reversed order
        response = authenticated_client.post(
            "/api/files/pack",
            json={"file_ids": list(reversed(file_ids))},
        )

        assert response.status_code == 409

    def test_create_pack_task_without_auth(
        self,
        client: TestClient,
    ):
        """Return 401 when not authenticated."""
        response = client.post(
            "/api/files/pack",
            json={"file_ids": [1]}
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

    def test_delete_done_task_success(
        self,
        authenticated_client: TestClient,
        done_pack_task: dict,
    ):
        """Can delete a completed task record."""
        response = authenticated_client.delete(f"/api/files/pack/{done_pack_task['id']}")

        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert "删除" in data["message"]

        # Verify task is deleted
        task = fetch_one("SELECT * FROM pack_tasks WHERE id = ?", [done_pack_task["id"]])
        assert task is None

    def test_delete_failed_task_success(
        self,
        authenticated_client: TestClient,
        failed_pack_task: dict,
    ):
        """Can delete a failed task record."""
        response = authenticated_client.delete(f"/api/files/pack/{failed_pack_task['id']}")

        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True

    def test_delete_cancelled_task_success(
        self,
        authenticated_client: TestClient,
        cancelled_pack_task: dict,
    ):
        """Can delete a cancelled task record."""
        response = authenticated_client.delete(f"/api/files/pack/{cancelled_pack_task['id']}")

        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True

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


# ========== Get Available Space Tests ==========

class TestGetAvailableSpace:
    """Tests for GET /api/files/pack/available-space endpoint."""

    def test_get_available_space_basic(
        self,
        authenticated_client: TestClient,
    ):
        """Return available, quota, and used."""
        with patch("app.services.storage.get_user_space_info", new_callable=AsyncMock, return_value={
            "quota": 100 * 1024 * 1024 * 1024,
            "used": 10 * 1024 * 1024 * 1024,
            "frozen": 0,
            "available": 90 * 1024 * 1024 * 1024,
        }):
            response = authenticated_client.get("/api/files/pack/available-space")

        assert response.status_code == 200
        data = response.json()

        assert "available" in data
        assert "quota" in data
        assert "used" in data


# ========== Space Calculation Tests ==========

class TestSpaceCalculation:
    """Tests for space calculation functions in pack.py."""

    @pytest.mark.asyncio
    async def test_get_server_available_space(
        self,
        temp_db: str,
    ):
        """get_server_available_space returns disk free minus reserved."""
        from app.services.pack import get_server_available_space, get_reserved_space

        # Mock disk usage
        mock_disk = MagicMock()
        mock_disk.free = 100 * 1024 * 1024 * 1024  # 100GB

        with patch("shutil.disk_usage", return_value=mock_disk):
            available = await get_server_available_space()

        reserved = await get_reserved_space()
        expected = 100 * 1024 * 1024 * 1024 - reserved
        assert available == expected

    @pytest.mark.asyncio
    async def test_get_server_available_space_with_reserved(
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
            available = await get_server_available_space()

        # Should subtract 3MB (1MB + 2MB) from pending/packing tasks
        expected = 100 * 1024 * 1024 * 1024 - 3000000
        assert available == expected

    @pytest.mark.asyncio
    async def test_get_user_available_space_for_pack(
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
            available = await get_user_available_space_for_pack(test_user["id"])

        # User quota is 100GB, used is ~10KB
        # Server available is 50GB
        # Should return min of (100GB - 10KB, 50GB) = 50GB
        assert available <= 50 * 1024 * 1024 * 1024

    @pytest.mark.asyncio
    async def test_get_user_available_space_with_other_tasks_reserved(
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
            available = await get_user_available_space_for_pack(test_user["id"])

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

    def test_utc_now_returns_iso_format(self, temp_db: str):
        """utc_now returns ISO format timestamp."""
        from app.services.pack import utc_now

        result = utc_now()
        # Should be parseable as ISO format
        parsed = datetime.fromisoformat(result)
        assert parsed.tzinfo is not None

    def test_is_any_task_running_false(self, temp_db: str):
        """is_any_task_running returns False when no tasks running."""
        from app.services.pack import PackTaskManager

        # Clear any existing tasks
        PackTaskManager._running_tasks.clear()
        assert PackTaskManager.is_any_task_running() is False

    def test_is_any_task_running_true(self, temp_db: str):
        """is_any_task_running returns True when tasks are running."""
        from app.services.pack import PackTaskManager

        mock_process = MagicMock()
        PackTaskManager._running_tasks[123] = mock_process
        try:
            assert PackTaskManager.is_any_task_running() is True
        finally:
            PackTaskManager._running_tasks.clear()

    def test_get_pack_format_legacy_7z_maps_tar_zst(self, temp_db: str):
        from app.services.pack import PackTaskManager

        with patch("app.routers.config.get_config_value", return_value="7z"):
            format_val = PackTaskManager.get_pack_format()
        assert format_val == "tar.zst"

    def test_get_compression_level_invalid_string(self, temp_db: str):
        """get_compression_level returns 5 for non-numeric string."""
        from app.services.pack import PackTaskManager

        with patch("app.routers.config.get_config_value", return_value="invalid"):
            level = PackTaskManager.get_compression_level()
        assert level == 5

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

    def test_get_pack_format_tar_zst(
        self,
        temp_db: str,
    ):
        from app.services.pack import PackTaskManager

        with patch("app.routers.config.get_config_value", return_value="tar.zst"):
            format_val = PackTaskManager.get_pack_format()

        assert format_val == "tar.zst"

    def test_get_pack_format_invalid(
        self,
        temp_db: str,
    ):
        """get_pack_format returns 'zip' for invalid values."""
        from app.services.pack import PackTaskManager

        with patch("app.routers.config.get_config_value", return_value="invalid"):
            format_val = PackTaskManager.get_pack_format()

        assert format_val == "zip"

    def test_build_archive_items_single_directory_flattens_content(
        self,
        test_user: dict,
        user_download_dir: Path,
        temp_db: str,
    ):
        from app.services.pack import PackTaskManager

        hash_dir = user_download_dir / ("a" * 64)
        logical_dir = hash_dir / "111"
        logical_dir.mkdir(parents=True, exist_ok=True)
        (logical_dir / "file1.txt").write_text("alpha")
        (logical_dir / "sub").mkdir()
        (logical_dir / "sub" / "nested.txt").write_text("beta")

        items = PackTaskManager._build_archive_items(
            sources=[hash_dir],
            source_names=["111"],
        )

        arcnames = {item.arcname for item in items}
        assert "file1.txt" in arcnames
        assert "sub/nested.txt" in arcnames
        assert "111/file1.txt" not in arcnames

    def test_build_archive_items_multi_keeps_selected_names(
        self,
        test_user: dict,
        user_download_dir: Path,
        temp_db: str,
    ):
        from app.services.pack import PackTaskManager

        hash_dir_1 = user_download_dir / ("a" * 64)
        logical_dir_1 = hash_dir_1 / "111"
        logical_dir_1.mkdir(parents=True, exist_ok=True)
        (logical_dir_1 / "a.txt").write_text("a")

        hash_dir_2 = user_download_dir / ("b" * 64)
        logical_dir_2 = hash_dir_2 / "222"
        logical_dir_2.mkdir(parents=True, exist_ok=True)
        (logical_dir_2 / "b.txt").write_text("b")

        items = PackTaskManager._build_archive_items(
            sources=[hash_dir_1, hash_dir_2],
            source_names=["111", "222"],
        )

        arcnames = {item.arcname for item in items}
        assert "111/a.txt" in arcnames
        assert "222/b.txt" in arcnames
        assert all(("a" * 64) not in arc for arc in arcnames)
        assert all(("b" * 64) not in arc for arc in arcnames)

    def test_build_archive_items_unwraps_hash_with_renamed_display_name(
        self,
        test_user: dict,
        user_download_dir: Path,
        temp_db: str,
    ):
        from app.services.pack import PackTaskManager

        hash_dir = user_download_dir / ("c" * 64)
        logical_dir = hash_dir / "actual-folder"
        logical_dir.mkdir(parents=True, exist_ok=True)
        (logical_dir / "inside.txt").write_text("content")

        items = PackTaskManager._build_archive_items(
            sources=[hash_dir],
            source_names=["renamed-folder"],
        )

        arcnames = {item.arcname for item in items}
        assert "inside.txt" in arcnames
        assert all(("c" * 64) not in arc for arc in arcnames)

    def test_build_archive_items_skips_symlink_entries(
        self,
        test_user: dict,
        user_download_dir: Path,
        temp_db: str,
    ):
        from app.services.pack import PackTaskManager

        hash_dir = user_download_dir / ("d" * 64)
        logical_dir = hash_dir / "folder"
        logical_dir.mkdir(parents=True, exist_ok=True)
        outside_file = user_download_dir / "outside.txt"
        outside_file.write_text("outside")
        (logical_dir / "inside.txt").write_text("inside")
        os.symlink(outside_file, logical_dir / "link.txt")

        items = PackTaskManager._build_archive_items(
            sources=[hash_dir],
            source_names=["folder"],
        )

        arcnames = {item.arcname for item in items}
        assert "inside.txt" in arcnames
        assert "link.txt" not in arcnames

    def test_safe_archive_name_blocks_dotdot(self, temp_db: str):
        from app.services.pack import PackTaskManager

        name = PackTaskManager._safe_archive_name("..", "fallback")
        assert name == "fallback"

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

    def test_get_compression_level_zero_allowed(
        self,
        temp_db: str,
    ):
        """get_compression_level allows 0 (store only, no compression)."""
        from app.services.pack import PackTaskManager

        with patch("app.routers.config.get_config_value", return_value="0"):
            level = PackTaskManager.get_compression_level()

        assert level == 0

    def test_get_compression_level_clamped_negative(
        self,
        temp_db: str,
    ):
        """get_compression_level clamps negative values to 0."""
        from app.services.pack import PackTaskManager

        with patch("app.routers.config.get_config_value", return_value="-1"):
            level = PackTaskManager.get_compression_level()

        assert level == 0

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
        import threading
        from app.services.pack import PackTaskManager, _RunningPackJob

        mock_task = MagicMock()
        cancel_event = threading.Event()

        task_id = 12345
        job = _RunningPackJob(task=mock_task, cancel_event=cancel_event)
        PackTaskManager._running_tasks[task_id] = job

        try:
            result = await PackTaskManager.cancel_pack(task_id)

            assert result is True
            assert cancel_event.is_set() is True
        finally:
            PackTaskManager._running_tasks.pop(task_id, None)


# ========== _do_pack Method Tests ==========

class TestDoPackMethod:

    @pytest.mark.asyncio
    async def test_do_pack_path_not_exists(
        self, test_user: dict, user_download_dir: Path, temp_db: str
    ):
        from app.services.pack import PackTaskManager

        abs_paths = [str(user_download_dir / "nonexistent.txt")]

        now = utc_now()
        task_id = execute(
            """INSERT INTO pack_tasks
               (owner_id, folder_path, folder_size, reserved_space, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [test_user["id"], json.dumps([1]), 1000, 1000, "pending", now, now]
        )

        await PackTaskManager._do_pack(task_id, test_user["id"], abs_paths, file_ids=[])

    @pytest.mark.asyncio
    async def test_do_pack_task_already_cancelled(
        self, test_user: dict, user_download_dir: Path, temp_db: str
    ):
        from app.services.pack import PackTaskManager

        file_path = user_download_dir / "cancel_test.txt"
        file_path.write_text("content")
        abs_paths = [str(file_path)]

        now = utc_now()
        task_id = execute(
            """INSERT INTO pack_tasks
               (owner_id, folder_path, folder_size, reserved_space, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [test_user["id"], json.dumps([1]), 1000, 1000, "cancelled", now, now]
        )

        await PackTaskManager._do_pack(task_id, test_user["id"], abs_paths, file_ids=[])

        task = fetch_one("SELECT * FROM pack_tasks WHERE id = ?", [task_id])
        assert task["status"] == "cancelled"

    @pytest.mark.asyncio
    async def test_do_pack_write_failure(
        self, test_user: dict, user_download_dir: Path, temp_db: str
    ):
        from app.services.pack import PackTaskManager

        file_path = user_download_dir / "7zz_test.txt"
        file_path.write_text("content")
        abs_paths = [str(file_path)]

        now = utc_now()
        task_id = execute(
            """INSERT INTO pack_tasks
               (owner_id, folder_path, folder_size, reserved_space, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [test_user["id"], json.dumps([1]), 1000, 1000, "pending", now, now]
        )

        with patch.object(PackTaskManager, "_write_archive_sync", side_effect=RuntimeError("write failed")):
            await PackTaskManager._do_pack(task_id, test_user["id"], abs_paths, file_ids=[])

        task = fetch_one("SELECT * FROM pack_tasks WHERE id = ?", [task_id])
        assert task["status"] == "failed"
        assert "write failed" in task["error_message"]

    @pytest.mark.asyncio
    async def test_update_task_error(self, test_user: dict, temp_db: str):
        from app.services.pack import PackTaskManager

        now = utc_now()
        task_id = execute(
            """INSERT INTO pack_tasks
               (owner_id, folder_path, folder_size, reserved_space, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [test_user["id"], "folder", 1000, 1000, "pending", now, now]
        )

        await PackTaskManager._update_task_error(task_id, "Test error message")

        task = fetch_one("SELECT * FROM pack_tasks WHERE id = ?", [task_id])
        assert task["status"] == "failed"
        assert task["error_message"] == "Test error message"
        assert task["reserved_space"] == 0

    @pytest.mark.asyncio
    async def test_update_task_error_already_done(self, test_user: dict, temp_db: str):
        from app.services.pack import PackTaskManager

        now = utc_now()
        task_id = execute(
            """INSERT INTO pack_tasks
               (owner_id, folder_path, folder_size, reserved_space, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [test_user["id"], "folder", 1000, 0, "done", now, now]
        )

        await PackTaskManager._update_task_error(task_id, "Should not update")

        task = fetch_one("SELECT * FROM pack_tasks WHERE id = ?", [task_id])
        assert task["status"] == "done"

    @pytest.mark.asyncio
    async def test_do_pack_subprocess_success(
        self, test_user: dict, user_download_dir: Path, temp_db: str
    ):
        from app.services.pack import PackTaskManager

        file_path = user_download_dir / "pack_success.txt"
        file_path.write_text("content")
        abs_paths = [str(file_path)]

        now = utc_now()
        task_id = execute(
            """INSERT INTO pack_tasks
               (owner_id, folder_path, folder_size, reserved_space, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [test_user["id"], json.dumps([1]), 1000, 1000, "pending", now, now]
        )

        mock_process = MagicMock()
        mock_process.returncode = 0
        mock_process.wait = AsyncMock()

        class MockStdout:
            def __init__(self):
                self._data = b"  0%\b\b\b\b 10%\b\b\b\b 50%\b\b\b\b100%\n"
                self._pos = 0
            async def read(self, n):
                chunk = self._data[self._pos:self._pos + n]
                self._pos += len(chunk)
                return chunk

        mock_process.stdout = MockStdout()

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            output_path = user_download_dir / "archive.zip"
            output_path.write_bytes(b"fake zip content")

            await PackTaskManager._do_pack(task_id, test_user["id"], abs_paths, file_ids=[])

        task = fetch_one("SELECT * FROM pack_tasks WHERE id = ?", [task_id])
        assert task["status"] == "done"
        assert task["progress"] == 100

    @pytest.mark.asyncio
    async def test_do_pack_handles_cancelled_status_before_run(
        self, test_user: dict, user_download_dir: Path, temp_db: str
    ):
        from app.services.pack import PackTaskManager

        file_path = user_download_dir / "fail_file.txt"
        file_path.write_text("content")
        abs_paths = [str(file_path)]

        now = utc_now()
        task_id = execute(
            """INSERT INTO pack_tasks
               (owner_id, folder_path, folder_size, reserved_space, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [test_user["id"], json.dumps([1]), 1000, 1000, "pending", now, now]
        )

        execute("UPDATE pack_tasks SET status = 'cancelled' WHERE id = ?", [task_id])
        await PackTaskManager._do_pack(task_id, test_user["id"], abs_paths, file_ids=[])

        task = fetch_one("SELECT * FROM pack_tasks WHERE id = ?", [task_id])
        assert task["status"] == "cancelled"

    @pytest.mark.asyncio
    async def test_do_pack_cancelled_during_startup(
        self, test_user: dict, user_download_dir: Path, temp_db: str
    ):
        from app.services.pack import PackTaskManager

        file_path = user_download_dir / "cancel_startup.txt"
        file_path.write_text("content")
        abs_paths = [str(file_path)]

        now = utc_now()
        task_id = execute(
            """INSERT INTO pack_tasks
               (owner_id, folder_path, folder_size, reserved_space, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [test_user["id"], json.dumps([1]), 1000, 1000, "pending", now, now]
        )

        execute("UPDATE pack_tasks SET status = 'cancelled' WHERE id = ?", [task_id])
        await PackTaskManager._do_pack(task_id, test_user["id"], abs_paths, file_ids=[])
        task = fetch_one("SELECT * FROM pack_tasks WHERE id = ?", [task_id])
        assert task["status"] == "cancelled"

    @pytest.mark.asyncio
    async def test_do_pack_status_changed_before_writer_start(
        self, test_user: dict, user_download_dir: Path, temp_db: str
    ):
        from app.services.pack import PackTaskManager

        file_path = user_download_dir / "cancel_before_writer.txt"
        file_path.write_text("content")
        abs_paths = [str(file_path)]

        now = utc_now()
        task_id = execute(
            """INSERT INTO pack_tasks
               (owner_id, folder_path, folder_size, reserved_space, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [test_user["id"], json.dumps([1]), 1000, 1000, "pending", now, now]
        )

        original_builder = PackTaskManager._build_archive_items

        def build_and_cancel(*args, **kwargs):
            execute("UPDATE pack_tasks SET status = 'cancelled' WHERE id = ?", [task_id])
            return original_builder(*args, **kwargs)

        with patch.object(PackTaskManager, "_build_archive_items", side_effect=build_and_cancel), \
             patch.object(PackTaskManager, "_write_archive_sync") as mock_writer:
            await PackTaskManager._do_pack(task_id, test_user["id"], abs_paths, file_ids=[])

        task = fetch_one("SELECT * FROM pack_tasks WHERE id = ?", [task_id])
        assert task["status"] == "cancelled"
        mock_writer.assert_not_called()

    @pytest.mark.asyncio
    async def test_do_pack_rolls_back_output_when_status_changes_after_register(
        self, test_user: dict, user_download_dir: Path, temp_db: str
    ):
        from app.services.pack import PackTaskManager

        file_path = user_download_dir / "rollback_output.txt"
        file_path.write_text("content")
        abs_paths = [str(file_path)]

        now = utc_now()
        task_id = execute(
            """INSERT INTO pack_tasks
               (owner_id, folder_path, folder_size, reserved_space, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [test_user["id"], json.dumps([1]), 1000, 1000, "pending", now, now]
        )

        async def fake_register_pack_output(*args, **kwargs):
            class _Stored:
                id = 321

            class _UserFile:
                id = 654

            return _Stored(), _UserFile()

        async def fake_is_task_status(_task_id: int, _status: str) -> bool:
            if not hasattr(fake_is_task_status, "count"):
                fake_is_task_status.count = 0
            fake_is_task_status.count += 1
            if fake_is_task_status.count == 2:
                execute("UPDATE pack_tasks SET status = 'cancelled' WHERE id = ?", [task_id])
            return fake_is_task_status.count == 1

        def fake_write_archive_sync(output_path: Path, *_args, **_kwargs):
            output_path.write_bytes(b"archive")

        with patch.object(PackTaskManager, "_write_archive_sync", side_effect=fake_write_archive_sync), \
             patch("app.services.storage.register_pack_output", new_callable=AsyncMock, side_effect=fake_register_pack_output), \
             patch("app.services.storage.delete_user_file_reference", new_callable=AsyncMock, return_value=True) as mock_delete_ref, \
             patch.object(PackTaskManager, "_is_task_status", side_effect=fake_is_task_status):
            await PackTaskManager._do_pack(task_id, test_user["id"], abs_paths, file_ids=[1], delete_source=True)

        task = fetch_one("SELECT * FROM pack_tasks WHERE id = ?", [task_id])
        assert task["status"] == "cancelled"
        mock_delete_ref.assert_awaited_once_with(654)

    @pytest.mark.asyncio
    async def test_do_pack_general_exception(
        self, test_user: dict, user_download_dir: Path, temp_db: str
    ):
        from app.services.pack import PackTaskManager

        file_path = user_download_dir / "exception_file.txt"
        file_path.write_text("content")
        abs_paths = [str(file_path)]

        now = utc_now()
        task_id = execute(
            """INSERT INTO pack_tasks
               (owner_id, folder_path, folder_size, reserved_space, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [test_user["id"], json.dumps([1]), 1000, 1000, "pending", now, now]
        )

        with patch.object(PackTaskManager, "_write_archive_sync", side_effect=RuntimeError("Unexpected error")):
            await PackTaskManager._do_pack(task_id, test_user["id"], abs_paths, file_ids=[])

        task = fetch_one("SELECT * FROM pack_tasks WHERE id = ?", [task_id])
        assert task["status"] == "failed"
        assert "Unexpected error" in task["error_message"]

    @pytest.mark.asyncio
    async def test_do_pack_multi_file_success(
        self, test_user: dict, user_download_dir: Path, temp_db: str
    ):
        from app.services.pack import PackTaskManager

        file1 = user_download_dir / "multi_file1.txt"
        file2 = user_download_dir / "multi_file2.txt"
        file1.write_text("content1")
        file2.write_text("content2")
        abs_paths = [str(file1), str(file2)]

        now = utc_now()
        task_id = execute(
            """INSERT INTO pack_tasks
               (owner_id, folder_path, folder_size, reserved_space, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [test_user["id"], json.dumps([1, 2]), 1000, 1000, "pending", now, now]
        )

        mock_process = MagicMock()
        mock_process.returncode = 0
        mock_process.wait = AsyncMock()

        class MockStdout:
            def __init__(self):
                self._data = b"100%\n"
                self._pos = 0
            async def read(self, n):
                chunk = self._data[self._pos:self._pos + n]
                self._pos += len(chunk)
                return chunk

        mock_process.stdout = MockStdout()

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            output_path = user_download_dir / "archive.zip"
            output_path.write_bytes(b"fake zip")

            await PackTaskManager._do_pack(task_id, test_user["id"], abs_paths, file_ids=[], output_name="archive")

        task = fetch_one("SELECT * FROM pack_tasks WHERE id = ?", [task_id])
        assert task["status"] == "done"


# ========== Multi-file Pack Tests ==========

class TestMultiFilePack:

    def test_create_multi_file_pack_task(
        self,
        authenticated_client: TestClient,
        user_download_dir: Path,
        test_user: dict,
    ):
        file_ids = _create_user_files(test_user["id"], user_download_dir, [
            ("file1.txt", 500),
            ("file2.txt", 600),
        ])

        with patch("app.services.pack.PackTaskManager.start_pack", new_callable=AsyncMock):
            response = authenticated_client.post(
                "/api/files/pack",
                json={"file_ids": file_ids, "output_name": "my_archive"}
            )

        assert response.status_code == 201
        data = response.json()
        assert data["folder_size"] > 0


# ========== Integration Tests ==========

class TestPackIntegration:
    """Integration tests for the complete pack workflow."""

    def test_full_pack_workflow_api(
        self,
        authenticated_client: TestClient,
        user_download_dir: Path,
        test_user: dict,
     ):
         """Test complete workflow: create -> list -> get -> cancel."""
         file_ids = _create_user_files(test_user["id"], user_download_dir, [
             ("workflow1.txt", 500),
             ("workflow2.txt", 600),
         ])

         # 1. Create pack task
         with patch("app.services.pack.PackTaskManager.start_pack", new_callable=AsyncMock):
             create_response = authenticated_client.post(
                 "/api/files/pack",
                 json={"file_ids": file_ids}
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


# ========== Clear Finished Pack Tasks Tests ==========

class TestClearFinishedPackTasks:
    """Tests for DELETE /api/files/pack endpoint (clear finished tasks)."""

    def test_clear_finished_removes_done_tasks(
        self,
        authenticated_client: TestClient,
        test_user: dict,
        user_download_dir: Path,
        temp_db: str,
    ):
        """Clear endpoint removes done/failed/cancelled tasks."""
        now = utc_now()
        
        # Create done task with output file
        output_path = user_download_dir / "done_task.zip"
        output_path.write_bytes(b"PK" + b"\x00" * 100)
        done_id = execute(
            """INSERT INTO pack_tasks
               (owner_id, folder_path, folder_size, reserved_space, output_path, output_size,
                status, progress, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [test_user["id"], "folder1", 1000, 0, str(output_path), 102,
             "done", 100, now, now]
        )
        
        # Create failed task
        failed_id = execute(
            """INSERT INTO pack_tasks
               (owner_id, folder_path, folder_size, reserved_space, status, progress,
                error_message, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [test_user["id"], "folder2", 1000, 0, "failed", 30,
             "error", now, now]
        )
        
        # Create cancelled task
        cancelled_id = execute(
            """INSERT INTO pack_tasks
               (owner_id, folder_path, folder_size, reserved_space, status, progress,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [test_user["id"], "folder3", 1000, 0, "cancelled", 20, now, now]
        )
        
        response = authenticated_client.delete("/api/files/pack")
        
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert data["count"] == 3
        
        # Verify all tasks deleted
        assert fetch_one("SELECT * FROM pack_tasks WHERE id = ?", [done_id]) is None
        assert fetch_one("SELECT * FROM pack_tasks WHERE id = ?", [failed_id]) is None
        assert fetch_one("SELECT * FROM pack_tasks WHERE id = ?", [cancelled_id]) is None

    def test_clear_finished_preserves_active_tasks(
        self,
        authenticated_client: TestClient,
        test_user: dict,
        temp_db: str,
    ):
        """Clear endpoint preserves pending and packing tasks."""
        now = utc_now()
        
        # Create pending task
        pending_id = execute(
            """INSERT INTO pack_tasks
               (owner_id, folder_path, folder_size, reserved_space, status, progress,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [test_user["id"], "folder1", 1000, 1000, "pending", 0, now, now]
        )
        
        # Create packing task
        packing_id = execute(
            """INSERT INTO pack_tasks
               (owner_id, folder_path, folder_size, reserved_space, status, progress,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [test_user["id"], "folder2", 2000, 2000, "packing", 50, now, now]
        )
        
        response = authenticated_client.delete("/api/files/pack")
        
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 0
        
        # Verify active tasks still exist
        pending = fetch_one("SELECT * FROM pack_tasks WHERE id = ?", [pending_id])
        assert pending is not None
        assert pending["status"] == "pending"
        
        packing = fetch_one("SELECT * FROM pack_tasks WHERE id = ?", [packing_id])
        assert packing is not None
        assert packing["status"] == "packing"

    def test_clear_finished_mixed_statuses(
        self,
        authenticated_client: TestClient,
        test_user: dict,
        temp_db: str,
    ):
        """Clear endpoint removes only terminal statuses from mixed set."""
        now = utc_now()
        
        # Create one of each status
        done_id = execute(
            """INSERT INTO pack_tasks
               (owner_id, folder_path, folder_size, reserved_space, status, progress,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [test_user["id"], "folder1", 1000, 0, "done", 100, now, now]
        )
        
        failed_id = execute(
            """INSERT INTO pack_tasks
               (owner_id, folder_path, folder_size, reserved_space, status, progress,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [test_user["id"], "folder2", 1000, 0, "failed", 30, now, now]
        )
        
        pending_id = execute(
            """INSERT INTO pack_tasks
               (owner_id, folder_path, folder_size, reserved_space, status, progress,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [test_user["id"], "folder3", 1000, 1000, "pending", 0, now, now]
        )
        
        packing_id = execute(
            """INSERT INTO pack_tasks
               (owner_id, folder_path, folder_size, reserved_space, status, progress,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [test_user["id"], "folder4", 2000, 2000, "packing", 50, now, now]
        )
        
        response = authenticated_client.delete("/api/files/pack")
        
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 2
        
        # Verify terminal tasks deleted
        assert fetch_one("SELECT * FROM pack_tasks WHERE id = ?", [done_id]) is None
        assert fetch_one("SELECT * FROM pack_tasks WHERE id = ?", [failed_id]) is None
        
        # Verify active tasks preserved
        assert fetch_one("SELECT * FROM pack_tasks WHERE id = ?", [pending_id]) is not None
        assert fetch_one("SELECT * FROM pack_tasks WHERE id = ?", [packing_id]) is not None

    def test_clear_finished_user_isolation(
        self,
        authenticated_client: TestClient,
        test_user: dict,
        test_admin: dict,
        temp_db: str,
    ):
        """Clear endpoint only removes current user's terminal tasks."""
        now = utc_now()
        
        # Create done task for test_user
        user_done_id = execute(
            """INSERT INTO pack_tasks
               (owner_id, folder_path, folder_size, reserved_space, status, progress,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [test_user["id"], "user_folder", 1000, 0, "done", 100, now, now]
        )
        
        # Create done task for admin (other user)
        admin_done_id = execute(
            """INSERT INTO pack_tasks
               (owner_id, folder_path, folder_size, reserved_space, status, progress,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [test_admin["id"], "admin_folder", 1000, 0, "done", 100, now, now]
        )
        
        response = authenticated_client.delete("/api/files/pack")
        
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1
        
        # Verify only test_user's task deleted
        assert fetch_one("SELECT * FROM pack_tasks WHERE id = ?", [user_done_id]) is None
        
        # Verify admin's task preserved
        admin_task = fetch_one("SELECT * FROM pack_tasks WHERE id = ?", [admin_done_id])
        assert admin_task is not None
        assert admin_task["owner_id"] == test_admin["id"]

    def test_clear_finished_no_tasks(
        self,
        authenticated_client: TestClient,
    ):
        """Clear endpoint returns count 0 when no terminal tasks exist."""
        response = authenticated_client.delete("/api/files/pack")
        
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert data["count"] == 0

    def test_clear_finished_cleans_output_files(
        self,
        authenticated_client: TestClient,
        test_user: dict,
        user_download_dir: Path,
        temp_db: str,
    ):
        """Clear endpoint deletes orphan output files for failed/cancelled tasks."""
        now = utc_now()
        
        output_path = user_download_dir / "partial_archive.zip"
        output_path.write_bytes(b"PK" + b"\x00" * 500)
        assert output_path.exists()
        
        task_id = execute(
            """INSERT INTO pack_tasks
               (owner_id, folder_path, folder_size, reserved_space, output_path, output_size,
                status, progress, error_message, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [test_user["id"], "folder", 1000, 0, str(output_path), 502,
             "failed", 30, "zip error", now, now]
        )
        
        response = authenticated_client.delete("/api/files/pack")
        
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1
        
        assert fetch_one("SELECT * FROM pack_tasks WHERE id = ?", [task_id]) is None
        assert not output_path.exists()
