from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.db import execute


class TestPackListEndpoints:
    def test_list_pack_tasks_empty(self, authenticated_client: TestClient):
        response = authenticated_client.get("/api/files/pack")
        assert response.status_code == 200
        assert response.json() == []

    def test_list_pack_tasks_unauthorized(self, client: TestClient, temp_db: str):
        response = client.get("/api/files/pack")
        assert response.status_code == 401

    def test_get_pack_task_not_found(self, authenticated_client: TestClient):
        response = authenticated_client.get("/api/files/pack/99999")
        assert response.status_code == 404

    def test_delete_pack_task_not_found(self, authenticated_client: TestClient):
        response = authenticated_client.delete("/api/files/pack/99999")
        assert response.status_code == 404


class TestPackCalculateEndpoints:
    def test_calculate_size_with_file_ids(self, authenticated_client: TestClient, user_file: dict):
        response = authenticated_client.post(
            "/api/files/pack/calculate-size",
            json={"file_ids": [user_file["id"]]},
        )
        assert response.status_code == 200
        assert response.json() == {"total_size": user_file["size"]}

    def test_calculate_size_unauthorized(self, client: TestClient, temp_db: str):
        response = client.post(
            "/api/files/pack/calculate-size",
            json={"file_ids": [1]},
        )
        assert response.status_code == 401

    def test_calculate_size_nonexistent_file_ids(self, authenticated_client: TestClient):
        response = authenticated_client.post(
            "/api/files/pack/calculate-size",
            json={"file_ids": [99999]},
        )
        assert response.status_code == 404

    def test_get_available_space(self, authenticated_client: TestClient):
        response = authenticated_client.get("/api/files/pack/available-space")
        assert response.status_code == 200
        assert set(response.json()) == {"available", "quota", "used"}

    def test_get_available_space_unauthorized(self, client: TestClient, temp_db: str):
        response = client.get("/api/files/pack/available-space")
        assert response.status_code == 401


class TestPackTaskOperations:
    def test_cancel_pack_task_resets_progress(self, authenticated_client: TestClient, test_user: dict):
        now = datetime.now(timezone.utc).isoformat()
        task_id = execute(
            """INSERT INTO pack_tasks
               (owner_id, folder_path, folder_size, reserved_space, status, progress, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [test_user["id"], "[]", 100, 100, "pending", 88, now, now],
        )

        with patch("app.services.pack.PackTaskManager.cancel_pack", new_callable=AsyncMock, return_value=True):
            response = authenticated_client.delete(f"/api/files/pack/{task_id}")
        assert response.status_code == 200

        detail = authenticated_client.get(f"/api/files/pack/{task_id}")
        assert detail.status_code == 200
        assert detail.json()["status"] == "cancelled"
        assert detail.json()["progress"] == 0

    def test_delete_cancelled_pack_uses_cleanup_pack_output(self, authenticated_client: TestClient, test_user: dict):
        now = datetime.now(timezone.utc).isoformat()
        output_path = Path(settings.download_dir) / str(test_user["id"]) / "cancelled_partial.zip"
        task_id = execute(
            """INSERT INTO pack_tasks
               (owner_id, folder_path, folder_size, reserved_space, output_path, status, progress, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [test_user["id"], "[]", 100, 0, str(output_path), "cancelled", 0, now, now],
        )

        with patch("app.routers.files.cleanup_pack_output", return_value=True) as mock_cleanup:
            response = authenticated_client.delete(f"/api/files/pack/{task_id}")

        assert response.status_code == 200
        mock_cleanup.assert_called_once_with(output_path)


@pytest.mark.parametrize(
    ("payload", "status"),
    [
        ({}, 422),
        ({"file_ids": [], "output_name": "test.7z"}, 422),
    ],
    ids=["missing-file-ids", "empty-file-ids"],
)
def test_create_pack_task_rejects_invalid_payload_shape(
    authenticated_client: TestClient,
    payload: dict,
    status: int,
):
    response = authenticated_client.post("/api/files/pack", json=payload)
    assert response.status_code == status


class TestPackTaskCreate:
    def test_create_pack_task_nonexistent_file_ids(self, authenticated_client: TestClient):
        response = authenticated_client.post(
            "/api/files/pack",
            json={"file_ids": [99999], "output_name": "test.7z"},
        )
        assert response.status_code == 404

    def test_create_pack_task_default_output_name_uses_display_name(
        self, authenticated_client: TestClient, user_file: dict
    ):
        response = authenticated_client.post(
            "/api/files/pack",
            json={"file_ids": [user_file["id"]]},
        )

        assert response.status_code == 201
        assert response.json()["output_name"] == "test_file"

    @pytest.mark.parametrize(
        ("output_name", "detail"),
        [
            ("a" * 201, "输出文件名不能超过 200 个字符"),
            ("bad:name", "输出文件名包含非法字符"),
        ],
        ids=["too-long", "invalid-char"],
    )
    def test_create_pack_task_rejects_invalid_output_name(
        self,
        authenticated_client: TestClient,
        user_file: dict,
        output_name: str,
        detail: str,
    ):
        response = authenticated_client.post(
            "/api/files/pack",
            json={"file_ids": [user_file["id"]], "output_name": output_name},
        )

        assert response.status_code == 400
        assert response.json()["detail"] == detail
