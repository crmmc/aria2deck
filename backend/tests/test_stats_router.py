"""Tests for stats router endpoints."""
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from app.core.config import settings


@pytest.fixture
def admin_client(client: TestClient, admin_session: str) -> TestClient:
    client.cookies.set(settings.session_cookie_name, admin_session)
    return client


class TestGetStats:

    def test_get_stats_success(self, authenticated_client: TestClient):
        mock_disk = MagicMock()
        mock_disk.total = 1000 * 1024 * 1024 * 1024
        mock_disk.used = 500 * 1024 * 1024 * 1024
        mock_disk.free = 500 * 1024 * 1024 * 1024

        with patch("shutil.disk_usage", return_value=mock_disk):
            response = authenticated_client.get("/api/stats")

        assert response.status_code == 200
        data = response.json()
        assert "disk_total_space" in data
        assert "disk_used_space" in data
        assert "disk_space_limited" in data
        assert "download_speed" in data
        assert "upload_speed" in data
        assert "active_task_count" in data

    def test_get_stats_empty_user_dir(self, authenticated_client: TestClient):
        mock_disk = MagicMock()
        mock_disk.total = 1000 * 1024 * 1024 * 1024
        mock_disk.used = 100 * 1024 * 1024 * 1024
        mock_disk.free = 900 * 1024 * 1024 * 1024

        with patch("shutil.disk_usage", return_value=mock_disk):
            response = authenticated_client.get("/api/stats")

        assert response.status_code == 200
        data = response.json()
        assert data["disk_used_space"] == 0

    def test_get_stats_disk_limited(self, authenticated_client: TestClient, test_user: dict):
        mock_disk = MagicMock()
        mock_disk.total = 50 * 1024 * 1024 * 1024
        mock_disk.used = 45 * 1024 * 1024 * 1024
        mock_disk.free = 5 * 1024 * 1024 * 1024

        with patch("shutil.disk_usage", return_value=mock_disk):
            response = authenticated_client.get("/api/stats")

        assert response.status_code == 200
        data = response.json()
        assert data["disk_space_limited"] is True

    def test_get_stats_not_limited(self, authenticated_client: TestClient, test_user: dict):
        mock_disk = MagicMock()
        mock_disk.total = 2000 * 1024 * 1024 * 1024
        mock_disk.used = 500 * 1024 * 1024 * 1024
        mock_disk.free = 1500 * 1024 * 1024 * 1024

        with patch("shutil.disk_usage", return_value=mock_disk):
            response = authenticated_client.get("/api/stats")

        assert response.status_code == 200
        data = response.json()
        assert data["disk_space_limited"] is False

    def test_get_stats_unauthorized(self, client: TestClient):
        response = client.get("/api/stats")
        assert response.status_code == 401


class TestGetMachineStats:

    def test_get_machine_stats_admin(self, admin_client: TestClient):
        mock_disk = MagicMock()
        mock_disk.total = 1000 * 1024 * 1024 * 1024
        mock_disk.used = 500 * 1024 * 1024 * 1024
        mock_disk.free = 500 * 1024 * 1024 * 1024

        with patch("shutil.disk_usage", return_value=mock_disk):
            response = admin_client.get("/api/stats/machine")

        assert response.status_code == 200
        data = response.json()
        assert data["disk_total"] == 1000 * 1024 * 1024 * 1024
        assert data["disk_used"] == 500 * 1024 * 1024 * 1024
        assert data["disk_free"] == 500 * 1024 * 1024 * 1024

    def test_get_machine_stats_non_admin(self, authenticated_client: TestClient):
        response = authenticated_client.get("/api/stats/machine")
        assert response.status_code == 403

    def test_get_machine_stats_unauthorized(self, client: TestClient):
        response = client.get("/api/stats/machine")
        assert response.status_code == 401


class TestGetStatsWithActiveTasks:

    def test_get_stats_with_active_tasks(
        self, authenticated_client: TestClient, test_user: dict, temp_db: str
    ):
        from app.db import execute, utc_now

        execute(
            """INSERT INTO download_tasks
               (uri_hash, uri, gid, status, name, total_length, completed_length,
                download_speed, upload_speed, peak_download_speed, peak_connections, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ["hash1", "http://example.com/file1.zip", "gid1", "active", "file1.zip",
             1000000, 500000, 100000, 50000, 100000, 10, utc_now(), utc_now()]
        )

        execute(
            """INSERT INTO user_task_subscriptions
               (owner_id, task_id, frozen_space, status, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            [test_user["id"], 1, 1000000, "pending", utc_now()]
        )

        mock_disk = MagicMock()
        mock_disk.total = 1000 * 1024 * 1024 * 1024
        mock_disk.free = 500 * 1024 * 1024 * 1024

        with patch("shutil.disk_usage", return_value=mock_disk):
            response = authenticated_client.get("/api/stats")

        assert response.status_code == 200
        data = response.json()
        assert data["active_task_count"] == 1
        assert data["download_speed"] == 100000
        assert data["upload_speed"] == 50000

    def test_get_stats_multiple_active_tasks(
        self, authenticated_client: TestClient, test_user: dict, temp_db: str
    ):
        from app.db import execute, utc_now

        execute(
            """INSERT INTO download_tasks
               (uri_hash, uri, gid, status, name, total_length, completed_length,
                download_speed, upload_speed, peak_download_speed, peak_connections, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ["hash1", "http://example.com/file1.zip", "gid1", "active", "file1.zip",
             1000000, 0, 100000, 10000, 100000, 5, utc_now(), utc_now()]
        )
        execute(
            """INSERT INTO download_tasks
               (uri_hash, uri, gid, status, name, total_length, completed_length,
                download_speed, upload_speed, peak_download_speed, peak_connections, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ["hash2", "http://example.com/file2.zip", "gid2", "active", "file2.zip",
             2000000, 0, 200000, 20000, 200000, 10, utc_now(), utc_now()]
        )

        execute(
            """INSERT INTO user_task_subscriptions
               (owner_id, task_id, frozen_space, status, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            [test_user["id"], 1, 0, "pending", utc_now()]
        )
        execute(
            """INSERT INTO user_task_subscriptions
               (owner_id, task_id, frozen_space, status, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            [test_user["id"], 2, 0, "pending", utc_now()]
        )

        mock_disk = MagicMock()
        mock_disk.total = 1000 * 1024 * 1024 * 1024
        mock_disk.free = 500 * 1024 * 1024 * 1024

        with patch("shutil.disk_usage", return_value=mock_disk):
            response = authenticated_client.get("/api/stats")

        assert response.status_code == 200
        data = response.json()
        assert data["active_task_count"] == 2
        assert data["download_speed"] == 300000
        assert data["upload_speed"] == 30000

    def test_get_stats_no_active_tasks(
        self, authenticated_client: TestClient, test_user: dict, temp_db: str
    ):
        from app.db import execute, utc_now

        execute(
            """INSERT INTO download_tasks
               (uri_hash, uri, gid, status, name, total_length, completed_length,
                download_speed, upload_speed, peak_download_speed, peak_connections, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ["hash1", "http://example.com/file1.zip", "gid1", "complete", "file1.zip",
             1000000, 1000000, 0, 0, 100000, 10, utc_now(), utc_now()]
        )

        execute(
            """INSERT INTO user_task_subscriptions
               (owner_id, task_id, frozen_space, status, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            [test_user["id"], 1, 0, "success", utc_now()]
        )

        mock_disk = MagicMock()
        mock_disk.total = 1000 * 1024 * 1024 * 1024
        mock_disk.free = 500 * 1024 * 1024 * 1024

        with patch("shutil.disk_usage", return_value=mock_disk):
            response = authenticated_client.get("/api/stats")

        assert response.status_code == 200
        data = response.json()
        assert data["active_task_count"] == 0
        assert data["download_speed"] == 0
        assert data["upload_speed"] == 0


class TestGetStatsWithUserFiles:

    def test_get_stats_with_user_files(
        self, authenticated_client: TestClient, test_user: dict, temp_db: str
    ):
        import os
        from app.core.config import settings

        user_dir = os.path.join(settings.download_dir, str(test_user["id"]))
        os.makedirs(user_dir, exist_ok=True)

        test_file = os.path.join(user_dir, "test_file.bin")
        with open(test_file, "wb") as f:
            f.write(b"x" * 1024 * 100)

        mock_disk = MagicMock()
        mock_disk.total = 1000 * 1024 * 1024 * 1024
        mock_disk.free = 500 * 1024 * 1024 * 1024

        with patch("shutil.disk_usage", return_value=mock_disk):
            response = authenticated_client.get("/api/stats")

        assert response.status_code == 200
        data = response.json()
        assert data["disk_used_space"] == 1024 * 100

    def test_get_stats_with_nested_user_files(
        self, authenticated_client: TestClient, test_user: dict, temp_db: str
    ):
        import os
        from app.core.config import settings

        user_dir = os.path.join(settings.download_dir, str(test_user["id"]))
        nested_dir = os.path.join(user_dir, "subdir", "nested")
        os.makedirs(nested_dir, exist_ok=True)

        with open(os.path.join(user_dir, "file1.bin"), "wb") as f:
            f.write(b"x" * 1000)
        with open(os.path.join(nested_dir, "file2.bin"), "wb") as f:
            f.write(b"y" * 2000)

        mock_disk = MagicMock()
        mock_disk.total = 1000 * 1024 * 1024 * 1024
        mock_disk.free = 500 * 1024 * 1024 * 1024

        with patch("shutil.disk_usage", return_value=mock_disk):
            response = authenticated_client.get("/api/stats")

        assert response.status_code == 200
        data = response.json()
        assert data["disk_used_space"] == 3000


