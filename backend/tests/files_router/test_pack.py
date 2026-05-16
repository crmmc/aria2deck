from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert

from app.db.engine import transaction
from app.db.schema import pack_tasks, stored_files


def _now_ms() -> int:
    import time

    return int(time.time() * 1000)


def _insert_pack_task_v0(
    *,
    user_id: int,
    status: str,
    source_size_bytes: int = 100,
    reserved_bytes: int = 0,
    progress: int = 0,
    output_stored_file_id: int | None = None,
) -> int:
    import asyncio

    async def seed() -> int:
        timestamp = _now_ms()
        async with transaction() as conn:
            row = (
                await conn.execute(
                    insert(pack_tasks)
                    .values(
                        user_id=user_id,
                        source_user_file_ids_json="[]",
                        source_size_bytes=source_size_bytes,
                        reserved_bytes=reserved_bytes,
                        output_stored_file_id=output_stored_file_id,
                        status=status,
                        progress=progress,
                        created_at_ms=timestamp,
                        updated_at_ms=timestamp,
                    )
                    .returning(pack_tasks.c.id)
                )
            ).one()
        return int(row[0])

    return asyncio.run(seed())


def _insert_stored_file_v0(*, size_bytes: int = 123) -> int:
    import asyncio

    async def seed() -> int:
        timestamp = _now_ms()
        async with transaction() as conn:
            row = (
                await conn.execute(
                    insert(stored_files)
                    .values(
                        content_hash=f"pack_output_{timestamp}",
                        real_path=f"/tmp/pack_output_{timestamp}.zip",
                        size_bytes=size_bytes,
                        is_directory=0,
                        original_name="packed.zip",
                        created_at_ms=timestamp,
                    )
                    .returning(stored_files.c.id)
                )
            ).one()
        return int(row[0])

    return asyncio.run(seed())


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

    def test_completed_pack_task_response_keeps_frontend_shape(
        self,
        authenticated_client: TestClient,
        test_user: dict,
    ):
        stored_file_id = _insert_stored_file_v0(size_bytes=321)
        task_id = _insert_pack_task_v0(
            user_id=test_user["id"],
            status="completed",
            source_size_bytes=100,
            reserved_bytes=0,
            progress=100,
            output_stored_file_id=stored_file_id,
        )

        response = authenticated_client.get(f"/api/files/pack/{task_id}")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "done"
        assert data["output_size"] == 321


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
        task_id = _insert_pack_task_v0(
            user_id=test_user["id"],
            status="pending",
            source_size_bytes=100,
            reserved_bytes=100,
            progress=88,
        )

        with patch("app.services.pack.PackTaskManager.cancel_pack", new_callable=AsyncMock, return_value=True):
            response = authenticated_client.delete(f"/api/files/pack/{task_id}")
        assert response.status_code == 200

        detail = authenticated_client.get(f"/api/files/pack/{task_id}")
        assert detail.status_code == 200
        assert detail.json()["status"] == "cancelled"
        assert detail.json()["progress"] == 0

    def test_delete_cancelled_pack_removes_task(self, authenticated_client: TestClient, test_user: dict):
        task_id = _insert_pack_task_v0(
            user_id=test_user["id"],
            status="cancelled",
            source_size_bytes=100,
            reserved_bytes=0,
            progress=0,
        )

        response = authenticated_client.delete(f"/api/files/pack/{task_id}")

        assert response.status_code == 200
        assert authenticated_client.get(f"/api/files/pack/{task_id}").status_code == 404


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
