"""Tests for stats router endpoints."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.repositories.usage import apply_usage_delta
from tests.helpers_v0 import create_global_download_v0, create_user_task_v0


@pytest.fixture
def admin_client(client: TestClient, admin_session: str) -> TestClient:
    client.cookies.set(settings.session_cookie_name, admin_session)
    return client


def _disk_usage(
    *,
    total: int = 1000 * 1024 * 1024 * 1024,
    used: int = 500 * 1024 * 1024 * 1024,
    free: int = 500 * 1024 * 1024 * 1024,
) -> MagicMock:
    mock_disk = MagicMock()
    mock_disk.total = total
    mock_disk.used = used
    mock_disk.free = free
    return mock_disk


def _seed_usage(
    user_id: int,
    *,
    used_bytes: int = 0,
    reserved_bytes: int = 0,
) -> None:
    asyncio.run(
        apply_usage_delta(
            user_id,
            used_delta=used_bytes,
            reserved_delta=reserved_bytes,
        )
    )


def _create_user_download_task(
    user_id: int,
    resource_key: str,
    *,
    user_status: str = "active",
    global_status: str = "active",
) -> None:
    download = asyncio.run(
        create_global_download_v0(
            resource_key=resource_key,
            source_uri=f"https://example.com/{resource_key}.zip",
            resource_kind="http",
            status=global_status,
            display_name=f"{resource_key}.zip",
            total_bytes=1_000_000,
        )
    )
    asyncio.run(
        create_user_task_v0(
            user_id=user_id,
            global_download_id=download["id"],
            status=user_status,
            display_name=f"{resource_key}.zip",
        )
    )


class TestGetStats:
    def test_get_stats_success(
        self, authenticated_client: TestClient, test_user: dict
    ) -> None:
        with patch("shutil.disk_usage", return_value=_disk_usage()):
            response = authenticated_client.get("/api/stats")

        assert response.status_code == 200
        data = response.json()
        assert data["disk_total_space"] == test_user["quota_bytes"]
        assert data["disk_used_space"] == 0
        assert data["disk_frozen_space"] == 0
        assert data["disk_space_limited"] is False
        assert data["download_speed"] == 0
        assert data["upload_speed"] == 0
        assert data["active_task_count"] == 0

    def test_get_stats_empty_user_dir(self, authenticated_client: TestClient) -> None:
        with patch(
            "shutil.disk_usage",
            return_value=_disk_usage(
                used=100 * 1024 * 1024 * 1024,
                free=900 * 1024 * 1024 * 1024,
            ),
        ):
            response = authenticated_client.get("/api/stats")

        assert response.status_code == 200
        data = response.json()
        assert data["disk_used_space"] == 0

    def test_get_stats_disk_limited(self, authenticated_client: TestClient) -> None:
        with patch(
            "shutil.disk_usage",
            return_value=_disk_usage(
                total=50 * 1024 * 1024 * 1024,
                used=45 * 1024 * 1024 * 1024,
                free=5 * 1024 * 1024 * 1024,
            ),
        ):
            response = authenticated_client.get("/api/stats")

        assert response.status_code == 200
        data = response.json()
        assert data["disk_space_limited"] is True

    def test_get_stats_not_limited(self, authenticated_client: TestClient) -> None:
        with patch(
            "shutil.disk_usage",
            return_value=_disk_usage(
                total=2000 * 1024 * 1024 * 1024,
                free=1500 * 1024 * 1024 * 1024,
            ),
        ):
            response = authenticated_client.get("/api/stats")

        assert response.status_code == 200
        data = response.json()
        assert data["disk_space_limited"] is False

    def test_get_stats_unauthorized(self, client: TestClient) -> None:
        response = client.get("/api/stats")
        assert response.status_code == 401


class TestGetMachineStats:
    def test_get_machine_stats_admin(self, admin_client: TestClient) -> None:
        with (
            patch("shutil.disk_usage", return_value=_disk_usage()),
            patch(
                "app.routers.stats._get_directory_size_bytes",
                return_value=120 * 1024 * 1024 * 1024,
            ),
        ):
            response = admin_client.get("/api/stats/machine")

        assert response.status_code == 200
        data = response.json()
        assert data["disk_total"] == 1000 * 1024 * 1024 * 1024
        assert data["disk_used"] == 500 * 1024 * 1024 * 1024
        assert data["disk_free"] == 500 * 1024 * 1024 * 1024
        assert data["download_used"] == 120 * 1024 * 1024 * 1024
        assert data["system_used"] == 380 * 1024 * 1024 * 1024

    def test_get_machine_stats_non_admin(
        self, authenticated_client: TestClient
    ) -> None:
        response = authenticated_client.get("/api/stats/machine")
        assert response.status_code == 403

    def test_get_machine_stats_unauthorized(self, client: TestClient) -> None:
        response = client.get("/api/stats/machine")
        assert response.status_code == 401


class TestGetStatsWithActiveTasks:
    def test_get_stats_with_active_tasks(
        self, authenticated_client: TestClient, test_user: dict
    ) -> None:
        _create_user_download_task(test_user["id"], "active-task-1")

        with patch("shutil.disk_usage", return_value=_disk_usage()):
            response = authenticated_client.get("/api/stats")

        assert response.status_code == 200
        data = response.json()
        assert data["active_task_count"] == 1
        assert data["download_speed"] == 0
        assert data["upload_speed"] == 0

    def test_get_stats_multiple_active_tasks(
        self, authenticated_client: TestClient, test_user: dict
    ) -> None:
        _create_user_download_task(
            test_user["id"],
            "active-task-1",
            user_status="queued",
        )
        _create_user_download_task(
            test_user["id"],
            "active-task-2",
            user_status="paused",
        )

        with patch("shutil.disk_usage", return_value=_disk_usage()):
            response = authenticated_client.get("/api/stats")

        assert response.status_code == 200
        data = response.json()
        assert data["active_task_count"] == 2
        assert data["download_speed"] == 0
        assert data["upload_speed"] == 0

    def test_get_stats_no_active_tasks(
        self, authenticated_client: TestClient, test_user: dict
    ) -> None:
        _create_user_download_task(
            test_user["id"],
            "completed-task",
            user_status="completed",
            global_status="completed",
        )
        _create_user_download_task(
            test_user["id"],
            "completed-user-task",
            user_status="completed",
            global_status="active",
        )

        with patch("shutil.disk_usage", return_value=_disk_usage()):
            response = authenticated_client.get("/api/stats")

        assert response.status_code == 200
        data = response.json()
        assert data["active_task_count"] == 0
        assert data["download_speed"] == 0
        assert data["upload_speed"] == 0

    def test_get_stats_uses_effective_current_task_count(
        self, authenticated_client: TestClient, test_user: dict
    ) -> None:
        _create_user_download_task(
            test_user["id"],
            "effective-active",
            user_status="active",
            global_status="active",
        )
        _create_user_download_task(
            test_user["id"],
            "effective-completed",
            user_status="active",
            global_status="completed",
        )
        _create_user_download_task(
            test_user["id"],
            "effective-failed",
            user_status="active",
            global_status="failed",
        )
        _create_user_download_task(
            test_user["id"],
            "effective-paused",
            user_status="paused",
            global_status="active",
        )

        with patch("shutil.disk_usage", return_value=_disk_usage()):
            response = authenticated_client.get("/api/stats")

        assert response.status_code == 200
        assert response.json()["active_task_count"] == 2


class TestGetStatsWithUsageRows:
    def test_get_stats_with_used_usage(
        self, authenticated_client: TestClient, test_user: dict
    ) -> None:
        _seed_usage(test_user["id"], used_bytes=1024 * 100)

        with patch("shutil.disk_usage", return_value=_disk_usage()):
            response = authenticated_client.get("/api/stats")

        assert response.status_code == 200
        data = response.json()
        assert data["disk_used_space"] == 1024 * 100

    def test_get_stats_with_multiple_usage_deltas(
        self, authenticated_client: TestClient, test_user: dict
    ) -> None:
        _seed_usage(test_user["id"], used_bytes=1000)
        _seed_usage(test_user["id"], used_bytes=2000)

        with patch("shutil.disk_usage", return_value=_disk_usage()):
            response = authenticated_client.get("/api/stats")

        assert response.status_code == 200
        data = response.json()
        assert data["disk_used_space"] == 3000

    def test_get_stats_returns_reserved_space(
        self, authenticated_client: TestClient, test_user: dict
    ) -> None:
        _seed_usage(test_user["id"], reserved_bytes=5_000_000)

        with patch("shutil.disk_usage", return_value=_disk_usage()):
            response = authenticated_client.get("/api/stats")

        assert response.status_code == 200
        data = response.json()
        assert data["disk_frozen_space"] == 5_000_000
