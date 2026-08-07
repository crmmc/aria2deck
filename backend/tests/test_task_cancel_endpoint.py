"""Tests for v0 task cancellation endpoint."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.repositories.downloads import get_global_by_resource_key, get_user_task
from app.services.download_service import create_user_download
from app.services.usage_service import get_usage
from tests.fakes import make_aria2_client


def _create_download_for_user(
    *,
    user: dict,
    resource_key: str,
    uri: str,
    display_name: str,
    total_bytes: int,
    gid: str,
) -> tuple[dict, AsyncMock]:
    client = make_aria2_client(add_uri=gid)
    task = asyncio.run(
        create_user_download(
            user_id=user["id"],
            quota_bytes=user["quota_bytes"],
            uri=uri,
            resource_key=resource_key,
            resource_kind="http",
            display_name=display_name,
            total_bytes=total_bytes,
            aria2_client=client,
        )
    )
    return task, client


class TestCancelTaskEndpoint:
    def test_cancel_active_v0_task_marks_user_task_cancelled(
        self,
        authenticated_client: TestClient,
        test_user: dict,
    ) -> None:
        task, _ = _create_download_for_user(
            user=test_user,
            resource_key="http:cancel-endpoint",
            uri="https://example.com/cancel-endpoint.bin",
            display_name="cancel-endpoint.bin",
            total_bytes=700,
            gid="gid-cancel-endpoint",
        )
        cancel_client = make_aria2_client(force_remove="gid-cancel-endpoint")

        with patch("app.services.task_service._get_client", return_value=cancel_client):
            response = authenticated_client.delete(f"/api/tasks/{task['id']}")

        assert response.status_code == 200
        assert response.json() == {"ok": True}

        global_download = asyncio.run(
            get_global_by_resource_key("http:cancel-endpoint")
        )
        stored_task = asyncio.run(
            get_user_task(test_user["id"], task["global_download_id"])
        )
        usage = asyncio.run(
            get_usage(test_user["id"], quota_bytes=test_user["quota_bytes"])
        )

        assert stored_task is not None
        assert stored_task["status"] == "cancelled"
        assert stored_task["reserved_bytes"] == 0
        assert stored_task["finished_at_ms"] is not None
        assert usage["reserved_bytes"] == 0
        assert global_download is not None
        assert global_download["status"] == "cancelled"
        assert global_download["aria2_gid"] is None
        cancel_client.force_remove.assert_awaited_once_with("gid-cancel-endpoint")

    def test_cancel_one_users_shared_task_keeps_global_download_active(
        self,
        authenticated_client: TestClient,
        test_user: dict,
        test_admin: dict,
    ) -> None:
        setup_client = make_aria2_client(add_uri="gid-shared-endpoint")
        first = asyncio.run(
            create_user_download(
                user_id=test_user["id"],
                quota_bytes=test_user["quota_bytes"],
                uri="https://example.com/shared-endpoint.bin",
                resource_key="http:shared-endpoint",
                resource_kind="http",
                display_name="shared-endpoint.bin",
                total_bytes=900,
                aria2_client=setup_client,
            )
        )
        second = asyncio.run(
            create_user_download(
                user_id=test_admin["id"],
                quota_bytes=test_admin["quota_bytes"],
                uri="https://example.com/shared-endpoint.bin",
                resource_key="http:shared-endpoint",
                resource_kind="http",
                display_name="shared-endpoint.bin",
                total_bytes=900,
                aria2_client=setup_client,
            )
        )
        cancel_client = make_aria2_client()

        with patch("app.services.task_service._get_client", return_value=cancel_client):
            response = authenticated_client.delete(f"/api/tasks/{first['id']}")

        assert response.status_code == 200
        assert response.json() == {"ok": True}

        global_download = asyncio.run(
            get_global_by_resource_key("http:shared-endpoint")
        )
        first_task = asyncio.run(
            get_user_task(test_user["id"], first["global_download_id"])
        )
        second_task = asyncio.run(
            get_user_task(test_admin["id"], second["global_download_id"])
        )

        assert first_task is not None
        assert first_task["status"] == "cancelled"
        assert second_task is not None
        assert second_task["status"] == "active"
        assert global_download is not None
        assert global_download["status"] == "active"
        assert global_download["aria2_gid"] == "gid-shared-endpoint"
        cancel_client.force_remove.assert_not_awaited()

    def test_cancel_missing_v0_task_returns_404(
        self,
        authenticated_client: TestClient,
    ) -> None:
        response = authenticated_client.delete("/api/tasks/99999")

        assert response.status_code == 404

    def test_cancel_force_remove_failure_still_cancels_task(
        self,
        authenticated_client: TestClient,
        test_user: dict,
    ) -> None:
        """M3: cleanup RPC failure does not block cancel; task is already cancelled."""
        task, _ = _create_download_for_user(
            user=test_user,
            resource_key="http:cancel-endpoint-failure",
            uri="https://example.com/cancel-endpoint-failure.bin",
            display_name="cancel-endpoint-failure.bin",
            total_bytes=600,
            gid="gid-cancel-endpoint-failure",
        )
        cancel_client = make_aria2_client(force_remove=OSError("aria2 timeout"))

        with patch("app.services.task_service._get_client", return_value=cancel_client):
            response = authenticated_client.delete(f"/api/tasks/{task['id']}")

        assert response.status_code == 200
        assert response.json() == {"ok": True}

        stored_task = asyncio.run(
            get_user_task(test_user["id"], task["global_download_id"])
        )
        usage = asyncio.run(
            get_usage(test_user["id"], quota_bytes=test_user["quota_bytes"])
        )

        assert stored_task is not None
        assert stored_task["status"] == "cancelled"
        assert stored_task["reserved_bytes"] == 0
        assert usage["reserved_bytes"] == 0
        cancel_client.force_remove.assert_awaited()
