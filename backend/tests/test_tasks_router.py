"""Tests for tasks router endpoints."""
import base64
import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from fastapi.testclient import TestClient

from app.core.config import settings


@pytest.fixture
def auth_headers(authenticated_client: TestClient) -> dict:
    """Get auth headers from authenticated client."""
    return {}


class TestSSRFProtection:
    """Tests for SSRF protection functions."""

    def test_is_private_ip_loopback(self):
        """Test loopback addresses are detected as private."""
        import ipaddress
        from app.routers.tasks import _is_private_ip

        assert _is_private_ip(ipaddress.ip_address("127.0.0.1")) is True
        assert _is_private_ip(ipaddress.ip_address("::1")) is True

    def test_is_private_ip_private_ranges(self):
        """Test private IP ranges are detected."""
        import ipaddress
        from app.routers.tasks import _is_private_ip

        # Private ranges
        assert _is_private_ip(ipaddress.ip_address("10.0.0.1")) is True
        assert _is_private_ip(ipaddress.ip_address("172.16.0.1")) is True
        assert _is_private_ip(ipaddress.ip_address("192.168.1.1")) is True

    def test_is_private_ip_public(self):
        """Test public IPs are not detected as private."""
        import ipaddress
        from app.routers.tasks import _is_private_ip

        assert _is_private_ip(ipaddress.ip_address("8.8.8.8")) is False
        assert _is_private_ip(ipaddress.ip_address("1.1.1.1")) is False

    def test_check_url_safety_localhost(self):
        """Test localhost URLs are blocked."""
        from fastapi import HTTPException
        from app.routers.tasks import _check_url_safety

        with pytest.raises(HTTPException) as exc_info:
            _check_url_safety("http://localhost/file.zip")
        assert exc_info.value.status_code == 400
        assert "本机地址" in exc_info.value.detail

    def test_check_url_safety_127_0_0_1(self):
        """Test 127.0.0.1 URLs are blocked."""
        from fastapi import HTTPException
        from app.routers.tasks import _check_url_safety

        with pytest.raises(HTTPException) as exc_info:
            _check_url_safety("http://127.0.0.1/file.zip")
        assert exc_info.value.status_code == 400

    def test_check_url_safety_private_ip(self):
        """Test private IP URLs are blocked."""
        from fastapi import HTTPException
        from app.routers.tasks import _check_url_safety

        with pytest.raises(HTTPException) as exc_info:
            _check_url_safety("http://192.168.1.1/file.zip")
        assert exc_info.value.status_code == 400
        assert "内网地址" in exc_info.value.detail

    def test_check_url_safety_public_url(self):
        """Test public URLs are allowed."""
        from app.routers.tasks import _check_url_safety

        # Should not raise
        _check_url_safety("http://example.com/file.zip")
        _check_url_safety("https://github.com/file.zip")

    def test_check_url_safety_magnet(self):
        """Test magnet links bypass SSRF check."""
        from app.routers.tasks import _check_url_safety

        # Should not raise (magnet is not http/https/ftp)
        _check_url_safety("magnet:?xt=urn:btih:abc123")

    def test_check_url_safety_no_hostname(self):
        """Test URLs without hostname are blocked."""
        from fastapi import HTTPException
        from app.routers.tasks import _check_url_safety

        with pytest.raises(HTTPException) as exc_info:
            _check_url_safety("http:///file.zip")
        assert exc_info.value.status_code == 400


class TestHelperFunctions:
    """Tests for helper functions."""

    def test_get_display_name_with_name(self):
        """Test display name with valid name."""
        from app.routers.tasks import _get_display_name
        from app.models import DownloadTask

        task = DownloadTask(
            id=1,
            uri_hash="abc123",
            uri="http://example.com/file.zip",
            name="test_file.zip",
            status="active",
        )
        assert _get_display_name(task) == "test_file.zip"

    def test_get_display_name_metadata_prefix(self):
        """Test display name with [METADATA] prefix falls back to magnet."""
        from app.routers.tasks import _get_display_name
        from app.models import DownloadTask

        task = DownloadTask(
            id=1,
            uri_hash="abc123",
            uri="magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567",
            name="[METADATA]test",
            status="active",
        )
        result = _get_display_name(task)
        assert "magnet:?xt=urn:btih:" in result

    def test_get_display_name_no_name(self):
        """Test display name with no name."""
        from app.routers.tasks import _get_display_name
        from app.models import DownloadTask

        task = DownloadTask(
            id=1,
            uri_hash="abc123",
            uri="http://example.com/file.zip",
            name=None,
            status="active",
        )
        assert _get_display_name(task) == "未知文件"

    def test_check_disk_space(self):
        """Test disk space check function."""
        from app.routers.tasks import _check_disk_space

        ok, free = _check_disk_space()
        assert isinstance(ok, bool)
        assert isinstance(free, int)
        assert free > 0


class TestCreateTask:
    """Tests for POST /api/tasks endpoint."""

    def test_create_task_unauthorized(self, client: TestClient, temp_db: str, test_user: dict):
        """Test creating task without auth."""
        response = client.post("/api/tasks", json={"uri": "http://example.com/file.zip"})
        assert response.status_code == 401

    def test_create_task_ssrf_blocked(self, authenticated_client: TestClient):
        """Test SSRF protection blocks localhost."""
        response = authenticated_client.post("/api/tasks", json={
            "uri": "http://localhost/file.zip"
        })
        assert response.status_code == 400
        assert "本机地址" in response.json()["detail"]

    def test_create_task_ssrf_private_ip(self, authenticated_client: TestClient):
        """Test SSRF protection blocks private IPs."""
        response = authenticated_client.post("/api/tasks", json={
            "uri": "http://192.168.1.1/file.zip"
        })
        assert response.status_code == 400
        assert "内网地址" in response.json()["detail"]

    def test_create_task_invalid_magnet(self, authenticated_client: TestClient):
        """Test invalid magnet link is rejected."""
        response = authenticated_client.post("/api/tasks", json={
            "uri": "magnet:?invalid"
        })
        assert response.status_code == 400
        assert "无效的磁力链接" in response.json()["detail"]

    @patch("app.routers.tasks.probe_url_with_get_fallback")
    def test_create_task_probe_failure(self, mock_probe, authenticated_client: TestClient):
        """Test task creation fails when probe fails."""
        mock_result = MagicMock()
        mock_result.success = False
        mock_result.error = "Connection refused"
        mock_probe.return_value = mock_result

        response = authenticated_client.post("/api/tasks", json={
            "uri": "http://example.com/file.zip"
        })
        assert response.status_code == 400
        assert "无法访问下载链接" in response.json()["detail"]

    @patch("app.routers.tasks.probe_url_with_get_fallback")
    @patch("app.routers.tasks.get_max_task_size")
    def test_create_task_exceeds_max_size(
        self, mock_max_size, mock_probe, authenticated_client: TestClient
    ):
        """Test task creation fails when file exceeds max size."""
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.final_url = "http://example.com/file.zip"
        mock_result.filename = "file.zip"
        mock_result.content_length = 100 * 1024 * 1024 * 1024  # 100GB
        mock_probe.return_value = mock_result
        mock_max_size.return_value = 10 * 1024 * 1024 * 1024  # 10GB limit

        response = authenticated_client.post("/api/tasks", json={
            "uri": "http://example.com/file.zip"
        })
        assert response.status_code == 403
        assert "超过系统限制" in response.json()["detail"]

    @patch("app.routers.tasks.probe_url_with_get_fallback")
    @patch("app.routers.tasks.get_user_space_info")
    @patch("app.routers.tasks.get_max_task_size")
    def test_create_task_exceeds_user_quota(
        self, mock_max_size, mock_space_info, mock_probe, authenticated_client: TestClient
    ):
        """Test task creation fails when file exceeds user quota."""
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.final_url = "http://example.com/file.zip"
        mock_result.filename = "file.zip"
        mock_result.content_length = 50 * 1024 * 1024 * 1024
        mock_probe.return_value = mock_result

        mock_max_size.return_value = 100 * 1024 * 1024 * 1024

        mock_space_info.return_value = {
            "used": 90 * 1024 * 1024 * 1024,
            "frozen": 0,
            "available": 10 * 1024 * 1024 * 1024,
            "quota": 100 * 1024 * 1024 * 1024,
        }

        response = authenticated_client.post("/api/tasks", json={
            "uri": "http://example.com/file.zip"
        })
        assert response.status_code == 403
        assert "超过可用空间" in response.json()["detail"]

    @patch("app.routers.tasks._check_disk_space")
    def test_create_task_disk_full(self, mock_disk, authenticated_client: TestClient):
        """Test task creation fails when disk is full."""
        mock_disk.return_value = (False, 100 * 1024 * 1024)  # 100MB free

        response = authenticated_client.post("/api/tasks", json={
            "uri": "http://example.com/file.zip"
        })
        assert response.status_code == 403
        assert "磁盘空间不足" in response.json()["detail"]

    @patch("app.routers.tasks.get_user_space_info")
    def test_create_magnet_task_insufficient_space(
        self, mock_space_info, authenticated_client: TestClient
    ):
        """Test magnet task creation fails with insufficient space."""
        mock_space_info.return_value = {
            "used": 100 * 1024 * 1024 * 1024,
            "frozen": 0,
            "available": 100,  # Only 100 bytes available
            "quota": 100 * 1024 * 1024 * 1024,
        }

        response = authenticated_client.post("/api/tasks", json={
            "uri": "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567"
        })
        assert response.status_code == 403
        assert "可用空间不足" in response.json()["detail"]

    @patch("app.routers.tasks.probe_url_with_get_fallback")
    @patch("app.routers.tasks._find_or_create_task")
    @patch("app.routers.tasks._create_subscription")
    @patch("app.routers.tasks.get_user_space_info")
    @patch("app.routers.tasks._check_disk_space")
    async def test_create_task_success(
        self,
        mock_disk,
        mock_space_info,
        mock_create_sub,
        mock_find_task,
        mock_probe,
        authenticated_client: TestClient,
    ):
        """Test successful task creation."""
        from app.models import DownloadTask, UserTaskSubscription

        mock_disk.return_value = (True, 100 * 1024 * 1024 * 1024)

        mock_result = MagicMock()
        mock_result.success = True
        mock_result.final_url = "http://example.com/file.zip"
        mock_result.filename = "file.zip"
        mock_result.content_length = 100 * 1024 * 1024  # 100MB
        mock_probe.return_value = mock_result

        mock_space_info.return_value = {
            "used": 0,
            "frozen": 0,
            "available": 100 * 1024 * 1024 * 1024,
            "quota": 100 * 1024 * 1024 * 1024,
        }

        task = DownloadTask(
            id=1,
            uri_hash="abc123",
            uri="http://example.com/file.zip",
            name="file.zip",
            total_length=100 * 1024 * 1024,
            status="queued",
        )
        mock_find_task.return_value = (task, True)

        subscription = UserTaskSubscription(
            id=1,
            owner_id=1,
            task_id=1,
            frozen_space=100 * 1024 * 1024,
            status="pending",
            created_at="2024-01-01T00:00:00Z",
        )
        mock_create_sub.return_value = subscription


class TestCreateTorrentTask:
    """Tests for POST /api/tasks/torrent endpoint."""

    def test_create_torrent_unauthorized(self, client: TestClient, temp_db: str, test_user: dict):
        """Test creating torrent task without auth."""
        response = client.post("/api/tasks/torrent", json={"torrent": "abc123"})
        assert response.status_code == 401

    def test_create_torrent_invalid_base64(self, authenticated_client: TestClient):
        """Test invalid base64 torrent is rejected."""
        response = authenticated_client.post("/api/tasks/torrent", json={
            "torrent": "not-valid-base64!!!"
        })
        assert response.status_code == 400
        assert "无效的种子文件" in response.json()["detail"]

    def test_create_torrent_too_large(self, authenticated_client: TestClient):
        """Test torrent file size limit."""
        # Create a large base64 string (> 14MB)
        large_torrent = base64.b64encode(b"x" * (15 * 1024 * 1024)).decode()

        response = authenticated_client.post("/api/tasks/torrent", json={
            "torrent": large_torrent
        })
        assert response.status_code == 413
        assert "种子文件过大" in response.json()["detail"]

    @patch("app.routers.tasks._check_disk_space")
    def test_create_torrent_disk_full(self, mock_disk, authenticated_client: TestClient):
        """Test torrent creation fails when disk is full."""
        mock_disk.return_value = (False, 100 * 1024 * 1024)

        # Valid but minimal torrent-like base64
        torrent_data = base64.b64encode(b"d8:announce0:e").decode()

        response = authenticated_client.post("/api/tasks/torrent", json={
            "torrent": torrent_data
        })
        assert response.status_code == 403
        assert "磁盘空间不足" in response.json()["detail"]


class TestListTasks:
    """Tests for GET /api/tasks endpoint."""

    def test_list_tasks_unauthorized(self, client: TestClient, temp_db: str, test_user: dict):
        """Test listing tasks without auth."""
        response = client.get("/api/tasks")
        assert response.status_code == 401

    def test_list_tasks_empty(self, authenticated_client: TestClient):
        """Test listing tasks when none exist."""
        response = authenticated_client.get("/api/tasks")
        assert response.status_code == 200
        assert response.json() == []

    def test_list_tasks_with_status_filter(self, authenticated_client: TestClient):
        """Test listing tasks with status filter."""
        # Test various filters
        for status_filter in ["active", "current", "complete", "error"]:
            response = authenticated_client.get(f"/api/tasks?status_filter={status_filter}")
            assert response.status_code == 200
            assert isinstance(response.json(), list)


class TestCancelTask:
    """Tests for DELETE /api/tasks/{subscription_id} endpoint."""

    def test_cancel_task_unauthorized(self, client: TestClient, temp_db: str, test_user: dict):
        """Test canceling task without auth."""
        response = client.delete("/api/tasks/1")
        assert response.status_code == 401

    def test_cancel_task_not_found(self, authenticated_client: TestClient):
        """Test canceling non-existent task."""
        response = authenticated_client.delete("/api/tasks/99999")
        assert response.status_code == 404
        assert "任务不存在" in response.json()["detail"]


class TestClearHistory:
    """Tests for DELETE /api/tasks endpoint."""

    def test_clear_history_unauthorized(self, client: TestClient, temp_db: str, test_user: dict):
        """Test clearing history without auth."""
        response = client.delete("/api/tasks")
        assert response.status_code == 401

    def test_clear_history_empty(self, authenticated_client: TestClient):
        """Test clearing history when none exist."""
        response = authenticated_client.delete("/api/tasks")
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert data["count"] == 0


class TestRateLimiting:
    """Tests for rate limiting on task endpoints."""

    @patch("app.routers.tasks.api_limiter.is_allowed")
    def test_create_task_rate_limited(self, mock_limiter, authenticated_client: TestClient):
        """Test rate limiting on task creation."""
        mock_limiter.return_value = False

        response = authenticated_client.post("/api/tasks", json={
            "uri": "http://example.com/file.zip"
        })
        assert response.status_code == 429
        assert "操作过于频繁" in response.json()["detail"]

    @patch("app.routers.tasks.api_limiter.is_allowed")
    def test_create_torrent_rate_limited(self, mock_limiter, authenticated_client: TestClient):
        """Test rate limiting on torrent creation."""
        mock_limiter.return_value = False

        response = authenticated_client.post("/api/tasks/torrent", json={
            "torrent": "abc123"
        })
        assert response.status_code == 429
        assert "操作过于频繁" in response.json()["detail"]


class TestSubscriptionToDict:
    """Tests for _subscription_to_dict helper."""

    def test_subscription_to_dict_pending(self):
        """Test subscription dict for pending status."""
        from app.routers.tasks import _subscription_to_dict
        from app.models import DownloadTask, UserTaskSubscription

        task = DownloadTask(
            id=1,
            uri_hash="abc123",
            uri="http://example.com/file.zip",
            name="file.zip",
            total_length=1000,
            completed_length=500,
            download_speed=100,
            upload_speed=0,
            status="active",
        )
        subscription = UserTaskSubscription(
            id=1,
            owner_id=1,
            task_id=1,
            frozen_space=1000,
            status="pending",
            created_at="2024-01-01T00:00:00Z",
        )

        result = _subscription_to_dict(subscription, task)

        assert result["id"] == 1
        assert result["name"] == "file.zip"
        assert result["status"] == "active"
        assert result["total_length"] == 1000
        assert result["completed_length"] == 500
        assert result["frozen_space"] == 1000
        assert result["error"] is None

    def test_subscription_to_dict_failed(self):
        """Test subscription dict for failed status."""
        from app.routers.tasks import _subscription_to_dict
        from app.models import DownloadTask, UserTaskSubscription

        task = DownloadTask(
            id=1,
            uri_hash="abc123",
            uri="http://example.com/file.zip",
            name="file.zip",
            status="error",
            error_display="Download failed",
        )
        subscription = UserTaskSubscription(
            id=1,
            owner_id=1,
            task_id=1,
            frozen_space=0,
            status="failed",
            error_display="用户空间不足",
            created_at="2024-01-01T00:00:00Z",
        )

        result = _subscription_to_dict(subscription, task)

        assert result["status"] == "error"
        assert result["error"] == "用户空间不足"

    def test_subscription_to_dict_success(self):
        """Test subscription dict for success status."""
        from app.routers.tasks import _subscription_to_dict
        from app.models import DownloadTask, UserTaskSubscription

        task = DownloadTask(
            id=1,
            uri_hash="abc123",
            uri="http://example.com/file.zip",
            name="file.zip",
            status="complete",
        )
        subscription = UserTaskSubscription(
            id=1,
            owner_id=1,
            task_id=1,
            frozen_space=0,
            status="success",
            created_at="2024-01-01T00:00:00Z",
        )

        result = _subscription_to_dict(subscription, task)

        assert result["status"] == "complete"
        assert result["error"] is None

    def test_subscription_to_dict_task_error(self):
        """Test subscription dict when task has error but subscription is pending."""
        from app.routers.tasks import _subscription_to_dict
        from app.models import DownloadTask, UserTaskSubscription

        task = DownloadTask(
            id=1,
            uri_hash="abc123",
            uri="http://example.com/file.zip",
            name="file.zip",
            status="error",
            error_display="Connection timeout",
        )
        subscription = UserTaskSubscription(
            id=1,
            owner_id=1,
            task_id=1,
            frozen_space=0,
            status="pending",
            created_at="2024-01-01T00:00:00Z",
        )

        result = _subscription_to_dict(subscription, task)

        assert result["status"] == "error"
        assert result["error"] == "Connection timeout"


class TestListTasksWithData:

    def test_list_tasks_with_subscriptions(
        self, authenticated_client: TestClient, test_user: dict, temp_db: str
    ):
        from app.db import execute, utc_now

        execute(
            """INSERT INTO download_tasks
               (uri_hash, uri, gid, status, name, total_length, completed_length,
                download_speed, upload_speed, peak_download_speed, peak_connections, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ["list_hash1", "http://example.com/file1.zip", "gid1", "active", "file1.zip",
             1000000, 500000, 100000, 0, 100000, 10, utc_now(), utc_now()]
        )

        execute(
            """INSERT INTO user_task_subscriptions
               (owner_id, task_id, frozen_space, status, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            [test_user["id"], 1, 1000000, "pending", utc_now()]
        )

        response = authenticated_client.get("/api/tasks")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["name"] == "file1.zip"
        assert data[0]["status"] == "active"

    def test_list_tasks_filter_by_status(
        self, authenticated_client: TestClient, test_user: dict, temp_db: str
    ):
        from app.db import execute, utc_now

        execute(
            """INSERT INTO download_tasks
               (uri_hash, uri, gid, status, name, total_length, completed_length,
                download_speed, upload_speed, peak_download_speed, peak_connections, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ["filter_hash1", "http://example.com/file1.zip", "gid1", "active", "active_file.zip",
             1000000, 500000, 100000, 0, 100000, 10, utc_now(), utc_now()]
        )
        execute(
            """INSERT INTO download_tasks
               (uri_hash, uri, gid, status, name, total_length, completed_length,
                download_speed, upload_speed, peak_download_speed, peak_connections, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ["filter_hash2", "http://example.com/file2.zip", "gid2", "complete", "complete_file.zip",
             2000000, 2000000, 0, 0, 200000, 20, utc_now(), utc_now()]
        )

        execute(
            """INSERT INTO user_task_subscriptions
               (owner_id, task_id, frozen_space, status, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            [test_user["id"], 1, 1000000, "pending", utc_now()]
        )
        execute(
            """INSERT INTO user_task_subscriptions
               (owner_id, task_id, frozen_space, status, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            [test_user["id"], 2, 0, "success", utc_now()]
        )

        response = authenticated_client.get("/api/tasks?status=active")
        assert response.status_code == 200
        data = response.json()
        assert len(data) >= 1
        active_tasks = [t for t in data if t["status"] == "active"]
        assert len(active_tasks) >= 1


class TestCancelTaskWithData:

    @patch("app.routers.tasks._get_client")
    def test_cancel_pending_subscription(
        self, mock_get_client, authenticated_client: TestClient, test_user: dict, temp_db: str
    ):
        mock_client = AsyncMock()
        mock_client.force_remove.return_value = "gid1"
        mock_client.remove_download_result.return_value = "OK"
        mock_get_client.return_value = mock_client

        from app.db import execute, utc_now

        execute(
            """INSERT INTO download_tasks
               (uri_hash, uri, gid, status, name, total_length, completed_length,
                download_speed, upload_speed, peak_download_speed, peak_connections, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ["cancel_hash1", "http://example.com/file.zip", "gid1", "active", "file.zip",
             1000000, 500000, 100000, 0, 100000, 10, utc_now(), utc_now()]
        )

        execute(
            """INSERT INTO user_task_subscriptions
               (owner_id, task_id, frozen_space, status, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            [test_user["id"], 1, 1000000, "pending", utc_now()]
        )

        response = authenticated_client.delete("/api/tasks/1")
        assert response.status_code == 200
        assert response.json()["ok"] is True

    @patch("app.routers.tasks._get_client")
    def test_cancel_other_user_subscription(
        self, mock_get_client, authenticated_client: TestClient, test_user: dict, test_admin: dict, temp_db: str
    ):
        mock_client = AsyncMock()
        mock_client.force_remove.return_value = "gid1"
        mock_client.remove_download_result.return_value = "OK"
        mock_get_client.return_value = mock_client

        from app.db import execute, utc_now

        execute(
            """INSERT INTO download_tasks
               (uri_hash, uri, gid, status, name, total_length, completed_length,
                download_speed, upload_speed, peak_download_speed, peak_connections, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ["cancel_other_hash", "http://example.com/file.zip", "gid1", "active", "file.zip",
             1000000, 500000, 100000, 0, 100000, 10, utc_now(), utc_now()]
        )

        execute(
            """INSERT INTO user_task_subscriptions
               (owner_id, task_id, frozen_space, status, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            [test_admin["id"], 1, 1000000, "pending", utc_now()]
        )

        response = authenticated_client.delete("/api/tasks/1")
        assert response.status_code == 404


class TestClearHistoryWithData:

    def test_clear_history_with_completed_tasks(
        self, authenticated_client: TestClient, test_user: dict, temp_db: str
    ):
        from app.db import execute, utc_now

        execute(
            """INSERT INTO download_tasks
               (uri_hash, uri, gid, status, name, total_length, completed_length,
                download_speed, upload_speed, peak_download_speed, peak_connections, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ["clear_hash1", "http://example.com/file.zip", "gid1", "complete", "file.zip",
             1000000, 1000000, 0, 0, 100000, 10, utc_now(), utc_now()]
        )

        execute(
            """INSERT INTO user_task_subscriptions
               (owner_id, task_id, frozen_space, status, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            [test_user["id"], 1, 0, "success", utc_now()]
        )

        response = authenticated_client.delete("/api/tasks")
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert data["count"] >= 0

    def test_clear_history_with_failed_tasks(
        self, authenticated_client: TestClient, test_user: dict, temp_db: str
    ):
        from app.db import execute, utc_now

        execute(
            """INSERT INTO download_tasks
               (uri_hash, uri, gid, status, name, total_length, completed_length,
                download_speed, upload_speed, peak_download_speed, peak_connections, error_display, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ["clear_failed_hash", "http://example.com/failed.zip", "gid_fail", "error", "failed.zip",
             1000000, 0, 0, 0, 0, 0, "Connection timeout", utc_now(), utc_now()]
        )

        execute(
            """INSERT INTO user_task_subscriptions
               (owner_id, task_id, frozen_space, status, error_display, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [test_user["id"], 1, 0, "failed", "Connection timeout", utc_now()]
        )

        response = authenticated_client.delete("/api/tasks")
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert data["count"] == 1

    def test_clear_history_mixed_statuses(
        self, authenticated_client: TestClient, test_user: dict, temp_db: str
    ):
        from app.db import execute, utc_now

        execute(
            """INSERT INTO download_tasks
               (uri_hash, uri, gid, status, name, total_length, completed_length,
                download_speed, upload_speed, peak_download_speed, peak_connections, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ["mixed_hash1", "http://example.com/active.zip", "gid_active", "active", "active.zip",
             1000000, 500000, 100000, 0, 100000, 10, utc_now(), utc_now()]
        )
        execute(
            """INSERT INTO download_tasks
               (uri_hash, uri, gid, status, name, total_length, completed_length,
                download_speed, upload_speed, peak_download_speed, peak_connections, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ["mixed_hash2", "http://example.com/complete.zip", "gid_complete", "complete", "complete.zip",
             2000000, 2000000, 0, 0, 200000, 20, utc_now(), utc_now()]
        )

        execute(
            """INSERT INTO user_task_subscriptions
               (owner_id, task_id, frozen_space, status, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            [test_user["id"], 1, 1000000, "pending", utc_now()]
        )
        execute(
            """INSERT INTO user_task_subscriptions
               (owner_id, task_id, frozen_space, status, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            [test_user["id"], 2, 0, "success", utc_now()]
        )

        response = authenticated_client.delete("/api/tasks")
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert data["count"] == 1

        list_response = authenticated_client.get("/api/tasks")
        assert list_response.status_code == 200
        remaining = list_response.json()
        assert len(remaining) == 1
        assert remaining[0]["status"] == "active"


class TestDNSResolutionSSRF:

    def test_check_url_safety_dns_resolves_to_private(self):
        from fastapi import HTTPException
        from app.routers.tasks import _check_url_safety
        import socket

        mock_result = [(socket.AF_INET, socket.SOCK_STREAM, 0, '', ('192.168.1.100', 80))]

        with patch("socket.getaddrinfo", return_value=mock_result):
            with pytest.raises(HTTPException) as exc_info:
                _check_url_safety("http://evil.example.com/file.zip")
            assert exc_info.value.status_code == 400
            assert "内网地址" in exc_info.value.detail

    def test_check_url_safety_dns_failure_allowed(self):
        from app.routers.tasks import _check_url_safety
        import socket

        with patch("socket.getaddrinfo", side_effect=socket.gaierror("DNS failed")):
            _check_url_safety("http://nonexistent.example.com/file.zip")

    def test_check_url_safety_invalid_ip_in_dns(self):
        from app.routers.tasks import _check_url_safety
        import socket

        mock_result = [(socket.AF_INET, socket.SOCK_STREAM, 0, '', ('invalid_ip', 80))]

        with patch("socket.getaddrinfo", return_value=mock_result):
            _check_url_safety("http://example.com/file.zip")


class TestSubscriptionToCompleteTask:

    def test_subscribe_to_already_complete_task_returns_success(
        self, authenticated_client: TestClient, test_user: dict, temp_db: str
    ):
        from app.db import execute, utc_now
        import os
        from app.core.config import settings

        store_dir = os.path.join(settings.download_dir, "store", "ab", "abc123")
        os.makedirs(store_dir, exist_ok=True)
        test_file = os.path.join(store_dir, "complete_file.zip")
        with open(test_file, "wb") as f:
            f.write(b"x" * 1000)

        execute(
            """INSERT INTO stored_files
               (content_hash, real_path, size, is_directory, ref_count, original_name, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ["abc123", store_dir, 1000, 0, 1, "complete_file.zip", utc_now()]
        )

        execute(
            """INSERT INTO download_tasks
               (uri_hash, uri, gid, status, name, total_length, completed_length,
                download_speed, upload_speed, peak_download_speed, peak_connections,
                stored_file_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ["complete_task_hash", "http://example.com/complete.zip", "gid_complete", "complete",
             "complete.zip", 1000, 1000, 0, 0, 100000, 10, 1, utc_now(), utc_now()]
        )

        execute(
            """INSERT INTO user_task_subscriptions
               (owner_id, task_id, frozen_space, status, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            [test_user["id"], 1, 0, "success", utc_now()]
        )

        response = authenticated_client.get("/api/tasks?status=success")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1


class TestTaskStatusFiltering:

    def test_list_tasks_filter_by_active(
        self, authenticated_client: TestClient, test_user: dict, temp_db: str
    ):
        from app.db import execute, utc_now

        execute(
            """INSERT INTO download_tasks
               (uri_hash, uri, gid, status, name, total_length, completed_length,
                download_speed, upload_speed, peak_download_speed, peak_connections, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ["filter_hash1", "http://example.com/active.zip", "gid_active", "active", "active.zip",
             1000000, 500000, 100000, 0, 100000, 10, utc_now(), utc_now()]
        )
        execute(
            """INSERT INTO download_tasks
               (uri_hash, uri, gid, status, name, total_length, completed_length,
                download_speed, upload_speed, peak_download_speed, peak_connections, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ["filter_hash2", "http://example.com/complete.zip", "gid_complete", "complete", "complete.zip",
             2000000, 2000000, 0, 0, 200000, 20, utc_now(), utc_now()]
        )

        execute(
            """INSERT INTO user_task_subscriptions
               (owner_id, task_id, frozen_space, status, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            [test_user["id"], 1, 1000000, "pending", utc_now()]
        )
        execute(
            """INSERT INTO user_task_subscriptions
               (owner_id, task_id, frozen_space, status, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            [test_user["id"], 2, 0, "success", utc_now()]
        )

        response = authenticated_client.get("/api/tasks?status_filter=active")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["status"] == "active"

    def test_list_tasks_filter_by_complete(
        self, authenticated_client: TestClient, test_user: dict, temp_db: str
    ):
        from app.db import execute, utc_now

        execute(
            """INSERT INTO download_tasks
               (uri_hash, uri, gid, status, name, total_length, completed_length,
                download_speed, upload_speed, peak_download_speed, peak_connections, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ["success_hash", "http://example.com/done.zip", "gid_done", "complete", "done.zip",
             1000000, 1000000, 0, 0, 100000, 10, utc_now(), utc_now()]
        )

        execute(
            """INSERT INTO user_task_subscriptions
               (owner_id, task_id, frozen_space, status, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            [test_user["id"], 1, 0, "success", utc_now()]
        )

        response = authenticated_client.get("/api/tasks?status_filter=complete")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1

    def test_list_tasks_filter_by_error(
        self, authenticated_client: TestClient, test_user: dict, temp_db: str
    ):
        from app.db import execute, utc_now

        execute(
            """INSERT INTO download_tasks
               (uri_hash, uri, gid, status, name, total_length, completed_length,
                download_speed, upload_speed, peak_download_speed, peak_connections, error_display, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ["failed_hash", "http://example.com/fail.zip", "gid_fail", "error", "fail.zip",
             1000000, 0, 0, 0, 0, 0, "Connection failed", utc_now(), utc_now()]
        )

        execute(
            """INSERT INTO user_task_subscriptions
               (owner_id, task_id, frozen_space, status, error_display, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [test_user["id"], 1, 0, "failed", "Connection failed", utc_now()]
        )

        response = authenticated_client.get("/api/tasks?status_filter=error")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1

    def test_list_tasks_filter_by_current(
        self, authenticated_client: TestClient, test_user: dict, temp_db: str
    ):
        from app.db import execute, utc_now

        execute(
            """INSERT INTO download_tasks
               (uri_hash, uri, gid, status, name, total_length, completed_length,
                download_speed, upload_speed, peak_download_speed, peak_connections, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ["current_hash1", "http://example.com/active.zip", "gid_active", "active", "active.zip",
             1000000, 500000, 100000, 0, 100000, 10, utc_now(), utc_now()]
        )
        execute(
            """INSERT INTO download_tasks
               (uri_hash, uri, gid, status, name, total_length, completed_length,
                download_speed, upload_speed, peak_download_speed, peak_connections, error_display, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ["current_hash2", "http://example.com/error.zip", "gid_error", "error", "error.zip",
             2000000, 0, 0, 0, 0, 0, "Failed", utc_now(), utc_now()]
        )
        execute(
            """INSERT INTO download_tasks
               (uri_hash, uri, gid, status, name, total_length, completed_length,
                download_speed, upload_speed, peak_download_speed, peak_connections, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ["current_hash3", "http://example.com/complete.zip", "gid_complete", "complete", "complete.zip",
             3000000, 3000000, 0, 0, 300000, 30, utc_now(), utc_now()]
        )

        execute(
            """INSERT INTO user_task_subscriptions
               (owner_id, task_id, frozen_space, status, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            [test_user["id"], 1, 1000000, "pending", utc_now()]
        )
        execute(
            """INSERT INTO user_task_subscriptions
               (owner_id, task_id, frozen_space, status, error_display, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [test_user["id"], 2, 0, "failed", "Failed", utc_now()]
        )
        execute(
            """INSERT INTO user_task_subscriptions
               (owner_id, task_id, frozen_space, status, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            [test_user["id"], 3, 0, "success", utc_now()]
        )

        response = authenticated_client.get("/api/tasks?status_filter=current")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
