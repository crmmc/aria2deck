from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.db import execute, fetch_one


class TestDeleteUserCleanup:
    def test_delete_user_clears_sessions(
        self, admin_client: TestClient, test_user: dict, user_session: str
    ):
        with patch("app.services.storage.delete_user_file_reference", new_callable=AsyncMock):
            response = admin_client.delete(f"/api/users/{test_user['id']}")
        assert response.status_code == 200
        assert fetch_one("SELECT * FROM sessions WHERE user_id = ?", [test_user["id"]]) is None

    def test_delete_user_clears_tasks(
        self, admin_client: TestClient, test_user: dict, temp_db: str
    ):
        from app.db import utc_now

        execute(
            """INSERT INTO tasks (owner_id, gid, uri, name, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [test_user["id"], "abc123", "http://example.com/file.zip", "file.zip", "active", utc_now(), utc_now()],
        )

        with patch("app.services.storage.delete_user_file_reference", new_callable=AsyncMock):
            response = admin_client.delete(f"/api/users/{test_user['id']}")
        assert response.status_code == 200
        assert fetch_one("SELECT * FROM tasks WHERE owner_id = ?", [test_user["id"]]) is None

    def test_delete_user_clears_pack_tasks(
        self, admin_client: TestClient, test_user: dict, temp_db: str
    ):
        from app.db import utc_now

        execute(
            """INSERT INTO pack_tasks
               (owner_id, folder_path, folder_size, reserved_space, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [test_user["id"], "test_folder", 1000, 1000, "pending", utc_now(), utc_now()],
        )

        with patch("app.services.storage.delete_user_file_reference", new_callable=AsyncMock):
            response = admin_client.delete(f"/api/users/{test_user['id']}")
        assert response.status_code == 200
        assert fetch_one("SELECT * FROM pack_tasks WHERE owner_id = ?", [test_user["id"]]) is None

    def test_delete_user_with_user_files(
        self, admin_client: TestClient, test_user: dict, temp_db: str
    ):
        from app.db import utc_now

        execute(
            """INSERT INTO stored_files (content_hash, real_path, size, ref_count, is_directory, original_name, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ["abc123hash", "/store/abc123", 1000, 1, 0, "test_file.txt", utc_now()],
        )
        stored_file = fetch_one("SELECT id FROM stored_files WHERE content_hash = ?", ["abc123hash"])

        execute(
            """INSERT INTO user_files (owner_id, stored_file_id, display_name, created_at)
               VALUES (?, ?, ?, ?)""",
            [test_user["id"], stored_file["id"], "test_file.txt", utc_now()],
        )

        with patch("app.services.storage.delete_user_file_reference", new_callable=AsyncMock) as mock_delete:
            response = admin_client.delete(f"/api/users/{test_user['id']}")

        assert response.status_code == 200
        assert mock_delete.called
