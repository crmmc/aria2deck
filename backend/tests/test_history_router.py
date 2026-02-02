"""Tests for history router endpoints."""
import pytest
from datetime import datetime, timezone
from fastapi.testclient import TestClient

from app.db import execute
from app.core.config import settings


@pytest.fixture
def history_record(test_user: dict, temp_db: str) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    record_id = execute(
        """
        INSERT INTO task_history (owner_id, task_name, uri, total_length, result, reason, created_at, finished_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [test_user["id"], "test_file.zip", "https://example.com/file.zip", 1024, "success", None, now, now]
    )
    return {"id": record_id, "owner_id": test_user["id"], "task_name": "test_file.zip"}


@pytest.fixture
def other_user_history(test_admin: dict, temp_db: str) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    record_id = execute(
        """
        INSERT INTO task_history (owner_id, task_name, uri, total_length, result, reason, created_at, finished_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [test_admin["id"], "admin_file.zip", "https://example.com/admin.zip", 2048, "success", None, now, now]
    )
    return {"id": record_id, "owner_id": test_admin["id"], "task_name": "admin_file.zip"}


class TestListHistory:

    def test_list_history_empty(self, authenticated_client: TestClient):
        response = authenticated_client.get("/api/history")
        assert response.status_code == 200
        assert response.json() == []

    def test_list_history_with_records(self, authenticated_client: TestClient, history_record: dict):
        response = authenticated_client.get("/api/history")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["task_name"] == "test_file.zip"
        assert data[0]["uri"] == "https://example.com/file.zip"
        assert data[0]["total_length"] == 1024
        assert data[0]["result"] == "success"

    def test_list_history_user_isolation(self, authenticated_client: TestClient, other_user_history: dict):
        response = authenticated_client.get("/api/history")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 0

    def test_list_history_unauthorized(self, client: TestClient):
        response = client.get("/api/history")
        assert response.status_code == 401


class TestDeleteSingleHistory:

    def test_delete_history_success(self, authenticated_client: TestClient, history_record: dict):
        response = authenticated_client.delete(f"/api/history/{history_record['id']}")
        assert response.status_code == 200
        assert response.json()["ok"] is True

        verify_response = authenticated_client.get("/api/history")
        assert len(verify_response.json()) == 0

    def test_delete_history_not_found(self, authenticated_client: TestClient):
        response = authenticated_client.delete("/api/history/99999")
        assert response.status_code == 404

    def test_delete_other_user_history(self, authenticated_client: TestClient, other_user_history: dict):
        response = authenticated_client.delete(f"/api/history/{other_user_history['id']}")
        assert response.status_code == 404

    def test_delete_history_unauthorized(self, client: TestClient, history_record: dict):
        response = client.delete(f"/api/history/{history_record['id']}")
        assert response.status_code == 401


class TestClearHistory:

    def test_clear_history_success(self, authenticated_client: TestClient, history_record: dict):
        response = authenticated_client.delete("/api/history")
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert data["count"] == 1

        verify_response = authenticated_client.get("/api/history")
        assert len(verify_response.json()) == 0

    def test_clear_history_empty(self, authenticated_client: TestClient):
        response = authenticated_client.delete("/api/history")
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert data["count"] == 0

    def test_clear_history_user_isolation(self, authenticated_client: TestClient, other_user_history: dict):
        response = authenticated_client.delete("/api/history")
        assert response.status_code == 200
        assert response.json()["count"] == 0

    def test_clear_history_unauthorized(self, client: TestClient):
        response = client.delete("/api/history")
        assert response.status_code == 401

    def test_clear_history_multiple_records(self, authenticated_client: TestClient, test_user: dict, temp_db: str):
        """Test clearing multiple history records - covers lines 74-78."""
        now = datetime.now(timezone.utc).isoformat()
        for i in range(5):
            execute(
                """
                INSERT INTO task_history (owner_id, task_name, uri, total_length, result, reason, created_at, finished_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [test_user["id"], f"file_{i}.zip", f"https://example.com/file_{i}.zip", 1024 * (i + 1), "success", None, now, now]
            )

        # Verify records exist
        list_response = authenticated_client.get("/api/history")
        assert len(list_response.json()) == 5

        # Clear all
        response = authenticated_client.delete("/api/history")
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert data["count"] == 5

        # Verify all cleared
        verify_response = authenticated_client.get("/api/history")
        assert len(verify_response.json()) == 0


class TestHistoryListOrdering:
    """Test history list ordering - covers line 24."""

    def test_list_history_ordered_by_id_desc(self, authenticated_client: TestClient, test_user: dict, temp_db: str):
        """Test that history records are returned in descending order by id."""
        now = datetime.now(timezone.utc).isoformat()
        for i in range(3):
            execute(
                """
                INSERT INTO task_history (owner_id, task_name, uri, total_length, result, reason, created_at, finished_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [test_user["id"], f"file_{i}.zip", f"https://example.com/file_{i}.zip", 1024, "success", None, now, now]
            )

        response = authenticated_client.get("/api/history")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 3
        # Should be in descending order by id
        assert data[0]["task_name"] == "file_2.zip"
        assert data[1]["task_name"] == "file_1.zip"
        assert data[2]["task_name"] == "file_0.zip"
