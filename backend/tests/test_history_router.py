"""Tests for v0 history router endpoints."""
from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert

from app.db.engine import transaction
from app.db.schema import global_downloads, user_tasks
from tests.helpers_v0 import now_ms


async def _create_user_task_row(
    *,
    user_id: int,
    resource_key: str,
    status: str,
    name: str,
    uri: str,
    total_bytes: int = 1024,
    reason: str | None = None,
    created_at_ms: int | None = None,
    finished_at_ms: int | None = None,
) -> dict[str, Any]:
    timestamp = now_ms()
    if created_at_ms is None:
        created_at_ms = timestamp
    if finished_at_ms is None and status in {"completed", "failed", "cancelled"}:
        finished_at_ms = timestamp + 1000

    async with transaction() as conn:
        download = (
            await conn.execute(
                insert(global_downloads)
                .values(
                    resource_key=resource_key,
                    resource_kind="http",
                    source_uri=uri,
                    display_name=name,
                    aria2_gid=None,
                    status=status,
                    total_bytes=total_bytes,
                    completed_bytes=total_bytes if status == "completed" else 0,
                    error_message=reason,
                    created_at_ms=created_at_ms,
                    updated_at_ms=finished_at_ms or created_at_ms,
                    completed_at_ms=finished_at_ms if status == "completed" else None,
                )
                .returning(global_downloads)
            )
        ).mappings().one()
        task = (
            await conn.execute(
                insert(user_tasks)
                .values(
                    user_id=user_id,
                    global_download_id=download["id"],
                    status=status,
                    reserved_bytes=0,
                    display_name=name,
                    error_message=reason,
                    created_at_ms=created_at_ms,
                    updated_at_ms=finished_at_ms or created_at_ms,
                    finished_at_ms=finished_at_ms,
                )
                .returning(user_tasks)
            )
        ).mappings().one()
    return dict(task)


@pytest.fixture
def history_record(test_user: dict, temp_db: str) -> dict:
    return asyncio.run(
        _create_user_task_row(
            user_id=test_user["id"],
            resource_key="http:history-record",
            status="completed",
            name="test_file.zip",
            uri="https://example.com/file.zip",
            total_bytes=1024,
        )
    )


@pytest.fixture
def other_user_history(test_admin: dict, temp_db: str) -> dict:
    return asyncio.run(
        _create_user_task_row(
            user_id=test_admin["id"],
            resource_key="http:admin-history-record",
            status="completed",
            name="admin_file.zip",
            uri="https://example.com/admin.zip",
            total_bytes=2048,
        )
    )


class TestListHistory:
    def test_list_history_empty(self, authenticated_client: TestClient) -> None:
        response = authenticated_client.get("/api/history")
        assert response.status_code == 200
        assert response.json() == []

    def test_list_history_with_terminal_user_tasks(
        self, authenticated_client: TestClient, history_record: dict
    ) -> None:
        response = authenticated_client.get("/api/history")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["id"] == history_record["id"]
        assert data[0]["task_name"] == "test_file.zip"
        assert data[0]["uri"] == "https://example.com/file.zip"
        assert data[0]["total_length"] == 1024
        assert data[0]["result"] == "completed"
        assert isinstance(data[0]["created_at"], str)
        assert isinstance(data[0]["finished_at"], str)

    def test_list_history_includes_failed_reason(
        self, authenticated_client: TestClient, test_user: dict, temp_db: str
    ) -> None:
        asyncio.run(
            _create_user_task_row(
                user_id=test_user["id"],
                resource_key="http:failed-history-record",
                status="failed",
                name="failed.zip",
                uri="https://example.com/failed.zip",
                reason="Connection timeout",
            )
        )

        response = authenticated_client.get("/api/history")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["result"] == "failed"
        assert data[0]["reason"] == "Connection timeout"

    def test_list_history_ignores_active_user_tasks(
        self, authenticated_client: TestClient, test_user: dict, temp_db: str
    ) -> None:
        asyncio.run(
            _create_user_task_row(
                user_id=test_user["id"],
                resource_key="http:active-history-ignore",
                status="active",
                name="active.zip",
                uri="https://example.com/active.zip",
                finished_at_ms=None,
            )
        )

        response = authenticated_client.get("/api/history")

        assert response.status_code == 200
        assert response.json() == []

    def test_list_history_user_isolation(
        self, authenticated_client: TestClient, other_user_history: dict
    ) -> None:
        response = authenticated_client.get("/api/history")
        assert response.status_code == 200
        assert response.json() == []

    def test_list_history_unauthorized(self, client: TestClient) -> None:
        response = client.get("/api/history")
        assert response.status_code == 401


class TestDeleteSingleHistory:
    def test_delete_history_success(
        self, authenticated_client: TestClient, history_record: dict
    ) -> None:
        response = authenticated_client.delete(f"/api/history/{history_record['id']}")
        assert response.status_code == 200
        assert response.json()["ok"] is True

        verify_response = authenticated_client.get("/api/history")
        assert verify_response.json() == []

    def test_delete_history_not_found(self, authenticated_client: TestClient) -> None:
        response = authenticated_client.delete("/api/history/99999")
        assert response.status_code == 404

    def test_delete_other_user_history(
        self, authenticated_client: TestClient, other_user_history: dict
    ) -> None:
        response = authenticated_client.delete(f"/api/history/{other_user_history['id']}")
        assert response.status_code == 404

    def test_delete_active_user_task_not_history(
        self, authenticated_client: TestClient, test_user: dict, temp_db: str
    ) -> None:
        task = asyncio.run(
            _create_user_task_row(
                user_id=test_user["id"],
                resource_key="http:active-delete-ignore",
                status="active",
                name="active.zip",
                uri="https://example.com/active.zip",
                finished_at_ms=None,
            )
        )

        response = authenticated_client.delete(f"/api/history/{task['id']}")

        assert response.status_code == 404

    def test_delete_history_unauthorized(
        self, client: TestClient, history_record: dict
    ) -> None:
        response = client.delete(f"/api/history/{history_record['id']}")
        assert response.status_code == 401


class TestClearHistory:
    def test_clear_history_success(
        self, authenticated_client: TestClient, history_record: dict
    ) -> None:
        response = authenticated_client.delete("/api/history")
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert data["count"] == 1

        verify_response = authenticated_client.get("/api/history")
        assert verify_response.json() == []

    def test_clear_history_empty(self, authenticated_client: TestClient) -> None:
        response = authenticated_client.delete("/api/history")
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert data["count"] == 0

    def test_clear_history_user_isolation(
        self, authenticated_client: TestClient, other_user_history: dict
    ) -> None:
        response = authenticated_client.delete("/api/history")
        assert response.status_code == 200
        assert response.json()["count"] == 0

    def test_clear_history_unauthorized(self, client: TestClient) -> None:
        response = client.delete("/api/history")
        assert response.status_code == 401

    def test_clear_history_multiple_terminal_records(
        self, authenticated_client: TestClient, test_user: dict, temp_db: str
    ) -> None:
        for index in range(5):
            asyncio.run(
                _create_user_task_row(
                    user_id=test_user["id"],
                    resource_key=f"http:clear-history-{index}",
                    status="completed",
                    name=f"file_{index}.zip",
                    uri=f"https://example.com/file_{index}.zip",
                    total_bytes=1024 * (index + 1),
                )
            )

        list_response = authenticated_client.get("/api/history")
        assert len(list_response.json()) == 5

        response = authenticated_client.delete("/api/history")
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert data["count"] == 5

        verify_response = authenticated_client.get("/api/history")
        assert verify_response.json() == []


class TestHistoryListOrdering:
    def test_list_history_ordered_by_id_desc(
        self, authenticated_client: TestClient, test_user: dict, temp_db: str
    ) -> None:
        for index in range(3):
            asyncio.run(
                _create_user_task_row(
                    user_id=test_user["id"],
                    resource_key=f"http:ordered-history-{index}",
                    status="completed",
                    name=f"file_{index}.zip",
                    uri=f"https://example.com/file_{index}.zip",
                )
            )

        response = authenticated_client.get("/api/history")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 3
        assert data[0]["task_name"] == "file_2.zip"
        assert data[1]["task_name"] == "file_1.zip"
        assert data[2]["task_name"] == "file_0.zip"


class TestV2HistoryPagination:
    def test_v2_history_is_stable_paginated_and_keeps_old_array(
        self,
        authenticated_client: TestClient,
        test_user: dict,
        other_user_history: dict,
        temp_db: str,
    ) -> None:
        records = []
        for index, created_at_ms in enumerate((100, 300, 200)):
            records.append(
                asyncio.run(
                    _create_user_task_row(
                        user_id=test_user["id"],
                        resource_key=f"v2-history-{index}",
                        status="completed",
                        name=f"v2-{index}.zip",
                        uri=f"https://example.com/v2-{index}.zip",
                        created_at_ms=created_at_ms,
                        finished_at_ms=created_at_ms + 1,
                    )
                )
            )

        old = authenticated_client.get("/api/history")
        first = authenticated_client.get("/api/v2/history?page=1&page_size=2")
        second = authenticated_client.get("/api/v2/history?page=2&page_size=2")
        empty = authenticated_client.get("/api/v2/history?page=9&page_size=2")

        assert isinstance(old.json(), list)
        assert [item["id"] for item in first.json()["items"]] == [
            records[1]["id"],
            records[2]["id"],
        ]
        assert first.json()["total"] == 3
        assert first.json()["page"] == 1
        assert second.json()["items"][0]["id"] == records[0]["id"]
        assert empty.json() == {"items": [], "total": 3, "page": 9, "page_size": 2}
        assert authenticated_client.get("/api/v2/history?page=0").status_code == 422
