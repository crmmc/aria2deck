"""Tests for users router endpoints."""
import pytest
from unittest.mock import patch, AsyncMock
from fastapi.testclient import TestClient

from app.core.config import settings
from app.db import execute, fetch_one


@pytest.fixture
def admin_client(client: TestClient, admin_session: str) -> TestClient:
    client.cookies.set(settings.session_cookie_name, admin_session)
    return client


class TestCreateUser:

    def test_create_first_user(self, client: TestClient, temp_db: str):
        response = client.post("/api/users", json={
            "username": "firstuser",
            "password": "password123",
            "is_admin": True
        })
        assert response.status_code == 200
        data = response.json()
        assert data["username"] == "firstuser"
        assert data["is_admin"] is True

    def test_create_user_as_admin(self, admin_client: TestClient, test_admin: dict):
        response = admin_client.post("/api/users", json={
            "username": "newuser",
            "password": "password123",
            "is_admin": False,
            "quota": 50 * 1024 * 1024 * 1024
        })
        assert response.status_code == 200
        data = response.json()
        assert data["username"] == "newuser"
        assert data["is_admin"] is False
        assert data["quota"] == 50 * 1024 * 1024 * 1024

    def test_create_user_duplicate_username(self, admin_client: TestClient, test_admin: dict, test_user: dict):
        response = admin_client.post("/api/users", json={
            "username": "testuser",
            "password": "password123",
            "is_admin": False
        })
        assert response.status_code == 400
        assert "已存在" in response.json()["detail"]

    def test_create_user_non_admin(self, authenticated_client: TestClient, test_user: dict):
        response = authenticated_client.post("/api/users", json={
            "username": "anotheruser",
            "password": "password123",
            "is_admin": False
        })
        assert response.status_code == 403


class TestListUsers:

    def test_list_users_admin(self, admin_client: TestClient, test_admin: dict, test_user: dict):
        response = admin_client.get("/api/users")
        assert response.status_code == 200
        users = response.json()
        assert len(users) >= 2
        usernames = [u["username"] for u in users]
        assert "admin" in usernames
        assert "testuser" in usernames

    def test_list_users_non_admin(self, authenticated_client: TestClient):
        response = authenticated_client.get("/api/users")
        assert response.status_code == 403

    def test_list_users_unauthorized(self, client: TestClient, temp_db: str, test_user: dict):
        response = client.get("/api/users")
        assert response.status_code == 401


class TestGetUser:

    def test_get_user_admin(self, admin_client: TestClient, test_user: dict):
        response = admin_client.get(f"/api/users/{test_user['id']}")
        assert response.status_code == 200
        data = response.json()
        assert data["username"] == "testuser"
        assert data["id"] == test_user["id"]

    def test_get_user_not_found(self, admin_client: TestClient):
        response = admin_client.get("/api/users/99999")
        assert response.status_code == 404

    def test_get_user_non_admin(self, authenticated_client: TestClient, test_admin: dict):
        response = authenticated_client.get(f"/api/users/{test_admin['id']}")
        assert response.status_code == 403


class TestUpdateUser:

    def test_update_user_username(self, admin_client: TestClient, test_user: dict):
        response = admin_client.put(f"/api/users/{test_user['id']}", json={
            "username": "updateduser"
        })
        assert response.status_code == 200
        assert response.json()["username"] == "updateduser"

    def test_update_user_quota(self, admin_client: TestClient, test_user: dict):
        response = admin_client.put(f"/api/users/{test_user['id']}", json={
            "quota": 200 * 1024 * 1024 * 1024
        })
        assert response.status_code == 200
        assert response.json()["quota"] == 200 * 1024 * 1024 * 1024

    def test_update_user_password(self, admin_client: TestClient, test_user: dict):
        response = admin_client.put(f"/api/users/{test_user['id']}", json={
            "password": "newpassword123"
        })
        assert response.status_code == 200

    def test_update_user_duplicate_username(self, admin_client: TestClient, test_user: dict, test_admin: dict):
        response = admin_client.put(f"/api/users/{test_user['id']}", json={
            "username": "admin"
        })
        assert response.status_code == 400
        assert "已被占用" in response.json()["detail"]

    def test_update_user_not_found(self, admin_client: TestClient):
        response = admin_client.put("/api/users/99999", json={"username": "test"})
        assert response.status_code == 404

    def test_cannot_remove_own_admin(self, admin_client: TestClient, test_admin: dict):
        response = admin_client.put(f"/api/users/{test_admin['id']}", json={
            "is_admin": False
        })
        assert response.status_code == 400
        assert "不能取消自己的管理员权限" in response.json()["detail"]

    def test_update_user_non_admin(self, authenticated_client: TestClient, test_user: dict):
        response = authenticated_client.put(f"/api/users/{test_user['id']}", json={
            "username": "hacked"
        })
        assert response.status_code == 403


class TestDeleteUser:

    def test_delete_user_admin(self, admin_client: TestClient, test_user: dict):
        with patch("app.services.storage.delete_user_file_reference", new_callable=AsyncMock):
            response = admin_client.delete(f"/api/users/{test_user['id']}")
        assert response.status_code == 200
        assert response.json()["ok"] is True

    def test_delete_user_not_found(self, admin_client: TestClient):
        response = admin_client.delete("/api/users/99999")
        assert response.status_code == 404

    def test_cannot_delete_self(self, admin_client: TestClient, test_admin: dict):
        response = admin_client.delete(f"/api/users/{test_admin['id']}")
        assert response.status_code == 400
        assert "不能删除自己" in response.json()["detail"]

    def test_delete_user_non_admin(self, authenticated_client: TestClient, test_admin: dict):
        response = authenticated_client.delete(f"/api/users/{test_admin['id']}")
        assert response.status_code == 403


class TestRpcAccess:

    def test_get_rpc_access_disabled(self, authenticated_client: TestClient):
        response = authenticated_client.get("/api/users/me/rpc-access")
        assert response.status_code == 200
        data = response.json()
        assert data["enabled"] is False
        assert data["secret"] is None

    def test_enable_rpc_access(self, authenticated_client: TestClient):
        response = authenticated_client.put("/api/users/me/rpc-access", json={"enabled": True})
        assert response.status_code == 200
        data = response.json()
        assert data["enabled"] is True
        assert data["secret"] is not None
        assert data["secret"].startswith("aria2_")

    def test_disable_rpc_access(self, authenticated_client: TestClient):
        authenticated_client.put("/api/users/me/rpc-access", json={"enabled": True})
        response = authenticated_client.put("/api/users/me/rpc-access", json={"enabled": False})
        assert response.status_code == 200
        data = response.json()
        assert data["enabled"] is False
        assert data["secret"] is None

    def test_refresh_rpc_secret(self, authenticated_client: TestClient):
        enable_response = authenticated_client.put("/api/users/me/rpc-access", json={"enabled": True})
        old_secret = enable_response.json()["secret"]

        response = authenticated_client.post("/api/users/me/rpc-access/refresh")
        assert response.status_code == 200
        data = response.json()
        assert data["enabled"] is True
        assert data["secret"] != old_secret
        assert data["secret"].startswith("aria2_")

    def test_refresh_rpc_secret_not_enabled(self, authenticated_client: TestClient):
        response = authenticated_client.post("/api/users/me/rpc-access/refresh")
        assert response.status_code == 400
        assert "未开启" in response.json()["detail"]

    def test_rpc_access_unauthorized(self, client: TestClient, temp_db: str, test_user: dict):
        response = client.get("/api/users/me/rpc-access")
        assert response.status_code == 401


class TestCreateUserFirstUser:
    """Tests for first user creation flow."""

    def test_create_first_user_with_default_quota(self, client: TestClient, temp_db: str):
        """First user gets default 100GB quota."""
        response = client.post("/api/users", json={
            "username": "firstadmin",
            "password": "password123",
            "is_admin": True
        })
        assert response.status_code == 200
        data = response.json()
        assert data["quota"] == 107374182400

    def test_create_first_user_with_custom_quota(self, client: TestClient, temp_db: str):
        """First user can have custom quota."""
        response = client.post("/api/users", json={
            "username": "firstadmin",
            "password": "password123",
            "is_admin": True,
            "quota": 50 * 1024 * 1024 * 1024
        })
        assert response.status_code == 200
        data = response.json()
        assert data["quota"] == 50 * 1024 * 1024 * 1024

    def test_create_second_user_requires_admin(self, client: TestClient, temp_db: str):
        """After first user, admin auth is required."""
        client.post("/api/users", json={
            "username": "firstadmin",
            "password": "password123",
            "is_admin": True
        })

        response = client.post("/api/users", json={
            "username": "seconduser",
            "password": "password123",
            "is_admin": False
        })
        assert response.status_code == 401


class TestDeleteUserWithFiles:
    """Tests for delete user with file cleanup."""

    def test_delete_user_with_delete_files_flag(
        self, admin_client: TestClient, test_user: dict, user_download_dir
    ):
        """Delete user with delete_files=true removes download directory."""
        with patch("app.services.storage.delete_user_file_reference", new_callable=AsyncMock):
            response = admin_client.delete(
                f"/api/users/{test_user['id']}?delete_files=true"
            )
        assert response.status_code == 200
        assert response.json()["ok"] is True

    def test_delete_user_clears_sessions(
        self, admin_client: TestClient, test_user: dict, user_session: str
    ):
        """Delete user removes all user sessions."""
        with patch("app.services.storage.delete_user_file_reference", new_callable=AsyncMock):
            response = admin_client.delete(f"/api/users/{test_user['id']}")
        assert response.status_code == 200

        from app.db import fetch_one
        session = fetch_one(
            "SELECT * FROM sessions WHERE user_id = ?",
            [test_user["id"]]
        )
        assert session is None

    def test_delete_user_clears_tasks(
        self, admin_client: TestClient, test_user: dict, temp_db: str
    ):
        """Delete user removes all user tasks."""
        from app.db import execute, fetch_one, utc_now

        execute(
            """INSERT INTO tasks (owner_id, gid, uri, name, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [test_user["id"], "abc123", "http://example.com/file.zip", "file.zip",
             "active", utc_now(), utc_now()]
        )

        with patch("app.services.storage.delete_user_file_reference", new_callable=AsyncMock):
            response = admin_client.delete(f"/api/users/{test_user['id']}")
        assert response.status_code == 200

        task = fetch_one("SELECT * FROM tasks WHERE owner_id = ?", [test_user["id"]])
        assert task is None


class TestUpdateUserIsAdmin:
    """Tests for updating user admin status."""

    def test_update_user_make_admin(self, admin_client: TestClient, test_user: dict):
        """Admin can promote user to admin."""
        response = admin_client.put(f"/api/users/{test_user['id']}", json={
            "is_admin": True
        })
        assert response.status_code == 200
        assert response.json()["is_admin"] is True

    def test_update_user_remove_admin(self, admin_client: TestClient, test_user: dict):
        """Admin can demote another admin."""
        admin_client.put(f"/api/users/{test_user['id']}", json={"is_admin": True})

        response = admin_client.put(f"/api/users/{test_user['id']}", json={
            "is_admin": False
        })
        assert response.status_code == 200
        assert response.json()["is_admin"] is False


@pytest.fixture
def user_download_dir(test_user: dict, temp_db: str):
    """Create user download directory."""
    from pathlib import Path
    user_dir = Path(settings.download_dir) / str(test_user["id"])
    user_dir.mkdir(parents=True, exist_ok=True)
    return user_dir


class TestFirstUserRateLimiting:

    def test_first_user_creation_blocked_after_many_attempts(self, client: TestClient, temp_db: str):
        from app.core.rate_limit import login_limiter

        for i in range(6):
            response = client.post("/api/users", json={
                "username": f"user{i}",
                "password": "password123",
                "is_admin": True
            })
            if i == 0:
                assert response.status_code == 200
            else:
                assert response.status_code in [401, 429]


class TestDeleteUserWithPackTasks:

    def test_delete_user_clears_pack_tasks(
        self, admin_client: TestClient, test_user: dict, temp_db: str
    ):
        from app.db import execute, fetch_one, utc_now

        execute(
            """INSERT INTO pack_tasks
               (owner_id, folder_path, folder_size, reserved_space, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [test_user["id"], "test_folder", 1000, 1000, "pending", utc_now(), utc_now()]
        )

        with patch("app.services.storage.delete_user_file_reference", new_callable=AsyncMock):
            response = admin_client.delete(f"/api/users/{test_user['id']}")
        assert response.status_code == 200

        pack_task = fetch_one("SELECT * FROM pack_tasks WHERE owner_id = ?", [test_user["id"]])
        assert pack_task is None


class TestUpdateUserEdgeCases:

    def test_update_user_integrity_error_on_commit(
        self, admin_client: TestClient, test_user: dict, test_admin: dict, temp_db: str
    ):
        from app.db import execute
        execute(
            "INSERT INTO users (username, password_hash, is_admin, created_at, quota) VALUES (?, ?, ?, ?, ?)",
            ["existinguser", "hash", 0, "2024-01-01", 100]
        )

        response = admin_client.put(f"/api/users/{test_user['id']}", json={
            "username": "existinguser"
        })
        assert response.status_code == 400
        assert "已被占用" in response.json()["detail"]


class TestCreateUserRaceCondition:

    def test_first_user_race_condition_second_request_rejected(
        self, client: TestClient, temp_db: str
    ):
        response1 = client.post("/api/users", json={
            "username": "firstuser",
            "password": "password123",
            "is_admin": True
        })
        assert response1.status_code == 200

        response2 = client.post("/api/users", json={
            "username": "seconduser",
            "password": "password123",
            "is_admin": False
        })
        assert response2.status_code == 401

    def test_create_user_integrity_error_on_commit_as_admin(
        self, admin_client: TestClient, test_admin: dict, temp_db: str
    ):
        response1 = admin_client.post("/api/users", json={
            "username": "uniqueuser",
            "password": "password123",
            "is_admin": False
        })
        assert response1.status_code == 200

        response2 = admin_client.post("/api/users", json={
            "username": "uniqueuser",
            "password": "password123",
            "is_admin": False
        })
        assert response2.status_code == 400
        assert "已存在" in response2.json()["detail"]


class TestDeleteUserWithUserFiles:

    def test_delete_user_with_user_files(
        self, admin_client: TestClient, test_user: dict, temp_db: str
    ):
        from app.db import execute, fetch_one, utc_now

        execute(
            """INSERT INTO stored_files (content_hash, real_path, size, ref_count, is_directory, original_name, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ["abc123hash", "/store/abc123", 1000, 1, 0, "test_file.txt", utc_now()]
        )
        stored_file = fetch_one("SELECT id FROM stored_files WHERE content_hash = ?", ["abc123hash"])

        execute(
            """INSERT INTO user_files (owner_id, stored_file_id, display_name, created_at)
               VALUES (?, ?, ?, ?)""",
            [test_user["id"], stored_file["id"], "test_file.txt", utc_now()]
        )

        with patch("app.services.storage.delete_user_file_reference", new_callable=AsyncMock) as mock_delete:
            response = admin_client.delete(f"/api/users/{test_user['id']}")

        assert response.status_code == 200
        assert mock_delete.called


class TestListUsersExtended:

    def test_list_users_as_admin(
        self, admin_client: TestClient, test_admin: dict, temp_db: str
    ):
        response = admin_client.get("/api/users")
        assert response.status_code == 200
        data = response.json()
        assert len(data) >= 1
        assert any(u["username"] == "admin" for u in data)

    def test_list_users_as_non_admin(
        self, authenticated_client: TestClient, test_user: dict, temp_db: str
    ):
        response = authenticated_client.get("/api/users")
        assert response.status_code == 403

    def test_list_users_unauthorized(self, client: TestClient, temp_db: str):
        response = client.get("/api/users")
        assert response.status_code == 401


class TestGetUserExtended:

    def test_get_user_as_admin(
        self, admin_client: TestClient, test_user: dict, temp_db: str
    ):
        response = admin_client.get(f"/api/users/{test_user['id']}")
        assert response.status_code == 200
        data = response.json()
        assert data["username"] == "testuser"

    def test_get_user_not_found(
        self, admin_client: TestClient, temp_db: str
    ):
        response = admin_client.get("/api/users/99999")
        assert response.status_code == 404

    def test_get_user_as_non_admin(
        self, authenticated_client: TestClient, test_user: dict, temp_db: str
    ):
        response = authenticated_client.get(f"/api/users/{test_user['id']}")
        assert response.status_code == 403


class TestUpdateUserExtended:

    def test_update_user_quota(
        self, admin_client: TestClient, test_user: dict, temp_db: str
    ):
        response = admin_client.put(f"/api/users/{test_user['id']}", json={
            "quota": 200 * 1024 * 1024 * 1024
        })
        assert response.status_code == 200
        data = response.json()
        assert data["quota"] == 200 * 1024 * 1024 * 1024

    def test_update_user_not_found(
        self, admin_client: TestClient, temp_db: str
    ):
        response = admin_client.put("/api/users/99999", json={
            "quota": 100 * 1024 * 1024 * 1024
        })
        assert response.status_code == 404

    def test_update_user_as_non_admin(
        self, authenticated_client: TestClient, test_user: dict, temp_db: str
    ):
        response = authenticated_client.put(f"/api/users/{test_user['id']}", json={
            "quota": 200 * 1024 * 1024 * 1024
        })
        assert response.status_code == 403
