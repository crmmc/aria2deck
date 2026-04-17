from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient


class TestCreateUser:
    def test_create_user_as_admin(self, admin_client: TestClient, test_admin: dict):
        response = admin_client.post("/api/users", json={
            "username": "newuser",
            "password": "password123",
            "is_admin": False,
            "quota": 50 * 1024 * 1024 * 1024,
        })
        assert response.status_code == 200
        assert response.json()["username"] == "newuser"
        assert response.json()["is_admin"] is False
        assert response.json()["quota"] == 50 * 1024 * 1024 * 1024

    def test_create_user_duplicate_username(self, admin_client: TestClient, test_admin: dict, test_user: dict):
        response = admin_client.post("/api/users", json={
            "username": "testuser",
            "password": "password123",
            "is_admin": False,
        })
        assert response.status_code == 400
        assert "已存在" in response.json()["detail"]

    def test_create_user_non_admin(self, authenticated_client: TestClient, test_user: dict):
        response = authenticated_client.post("/api/users", json={
            "username": "anotheruser",
            "password": "password123",
            "is_admin": False,
        })
        assert response.status_code == 403


class TestListUsers:
    def test_list_users_admin(self, admin_client: TestClient, test_admin: dict, test_user: dict):
        response = admin_client.get("/api/users")
        assert response.status_code == 200
        users = response.json()
        assert len(users) == 2
        assert {user["username"] for user in users} == {"admin", "testuser"}


class TestGetUser:
    def test_get_user_admin(self, admin_client: TestClient, test_user: dict):
        response = admin_client.get(f"/api/users/{test_user['id']}")
        assert response.status_code == 200
        assert response.json()["username"] == "testuser"
        assert response.json()["id"] == test_user["id"]

    def test_get_user_not_found(self, admin_client: TestClient):
        response = admin_client.get("/api/users/99999")
        assert response.status_code == 404


class TestUpdateUser:
    def test_update_user_username(self, admin_client: TestClient, test_user: dict):
        response = admin_client.put(f"/api/users/{test_user['id']}", json={"username": "updateduser"})
        assert response.status_code == 200
        assert response.json()["username"] == "updateduser"

    def test_update_user_quota(self, admin_client: TestClient, test_user: dict):
        response = admin_client.put(
            f"/api/users/{test_user['id']}",
            json={"quota": 200 * 1024 * 1024 * 1024},
        )
        assert response.status_code == 200
        assert response.json()["quota"] == 200 * 1024 * 1024 * 1024

    def test_update_user_password(self, admin_client: TestClient, test_user: dict):
        response = admin_client.put(f"/api/users/{test_user['id']}", json={"password": "newpassword123"})
        assert response.status_code == 200

    def test_update_user_duplicate_username(self, admin_client: TestClient, test_user: dict, test_admin: dict):
        response = admin_client.put(f"/api/users/{test_user['id']}", json={"username": "admin"})
        assert response.status_code == 400
        assert "已被占用" in response.json()["detail"]

    def test_update_user_not_found(self, admin_client: TestClient):
        response = admin_client.put("/api/users/99999", json={"username": "test"})
        assert response.status_code == 404

    def test_cannot_remove_own_admin(self, admin_client: TestClient, test_admin: dict):
        response = admin_client.put(f"/api/users/{test_admin['id']}", json={"is_admin": False})
        assert response.status_code == 400
        assert "不能取消自己的管理员权限" in response.json()["detail"]

    def test_update_user_make_admin(self, admin_client: TestClient, test_user: dict):
        response = admin_client.put(f"/api/users/{test_user['id']}", json={"is_admin": True})
        assert response.status_code == 200
        assert response.json()["is_admin"] is True

    def test_update_user_remove_admin(self, admin_client: TestClient, test_user: dict):
        admin_client.put(f"/api/users/{test_user['id']}", json={"is_admin": True})
        response = admin_client.put(f"/api/users/{test_user['id']}", json={"is_admin": False})
        assert response.status_code == 200
        assert response.json()["is_admin"] is False


class TestDeleteUser:
    def test_delete_user_admin(self, admin_client: TestClient, test_user: dict):
        with patch("app.services.storage.delete_user_file_reference", new_callable=AsyncMock):
            response = admin_client.delete(f"/api/users/{test_user['id']}")
        assert response.status_code == 200
        assert response.json() == {"ok": True}

    def test_delete_user_not_found(self, admin_client: TestClient):
        response = admin_client.delete("/api/users/99999")
        assert response.status_code == 404

    def test_cannot_delete_self(self, admin_client: TestClient, test_admin: dict):
        response = admin_client.delete(f"/api/users/{test_admin['id']}")
        assert response.status_code == 400
        assert "不能删除自己" in response.json()["detail"]


@pytest.mark.parametrize(
    ("method", "path_template", "payload", "cookie_mode", "status"),
    [
        ("get", "/api/users", None, "user", 403),
        ("get", "/api/users", None, "anonymous", 401),
        ("get", "/api/users/{admin_id}", None, "user", 403),
        ("get", "/api/users/{admin_id}", None, "anonymous", 401),
        ("put", "/api/users/{user_id}", {"username": "hacked"}, "user", 403),
        ("put", "/api/users/{user_id}", {"username": "hacked"}, "anonymous", 401),
        ("delete", "/api/users/{admin_id}", None, "user", 403),
        ("delete", "/api/users/{admin_id}", None, "anonymous", 401),
    ],
    ids=[
        "list-users-non-admin",
        "list-users-anonymous",
        "get-user-non-admin",
        "get-user-anonymous",
        "update-user-non-admin",
        "update-user-anonymous",
        "delete-user-non-admin",
        "delete-user-anonymous",
    ],
)
def test_admin_only_endpoints_enforce_authz(
    client: TestClient,
    authenticated_client: TestClient,
    test_user: dict,
    test_admin: dict,
    method: str,
    path_template: str,
    payload: dict | None,
    cookie_mode: str,
    status: int,
):
    target_client = authenticated_client if cookie_mode == "user" else client
    if cookie_mode == "anonymous":
        target_client.cookies.clear()
    path = path_template.format(user_id=test_user["id"], admin_id=test_admin["id"])
    kwargs = {"json": payload} if payload is not None else {}
    response = getattr(target_client, method)(path, **kwargs)
    assert response.status_code == status
