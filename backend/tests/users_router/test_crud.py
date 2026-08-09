import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.security import verify_password
from app.db.engine import transaction
from app.db.schema import sessions
from app.repositories import auth as auth_repo
from app.services.deletion_cleanup import DeletionCleanupManager
from app.services.usage_service import reserve_bytes
from tests.helpers_v0 import create_user_v0


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
        response = admin_client.put(
            f"/api/users/{test_user['id']}",
            json={"username": "updateduser", "password": "updated-password"},
        )
        assert response.status_code == 200
        assert response.json()["username"] == "updateduser"

        admin_client.cookies.clear()
        response = admin_client.post(
            "/api/auth/login",
            json={"username": "updateduser", "password": "updated-password"},
        )
        assert response.status_code == 200

    def test_update_username_requires_new_password(
        self, admin_client: TestClient, test_user: dict
    ):
        response = admin_client.put(
            f"/api/users/{test_user['id']}", json={"username": "updateduser"}
        )
        assert response.status_code == 400
        assert "按新用户名派生的密码" in response.json()["detail"]

    def test_case_only_username_change_keeps_password(
        self, admin_client: TestClient, test_user: dict
    ):
        response = admin_client.put(
            f"/api/users/{test_user['id']}", json={"username": "TestUser"}
        )
        assert response.status_code == 200
        assert response.json()["username"] == "TestUser"

    def test_update_user_quota(self, admin_client: TestClient, test_user: dict):
        response = admin_client.put(
            f"/api/users/{test_user['id']}",
            json={"quota": 200 * 1024 * 1024 * 1024},
        )
        assert response.status_code == 200
        assert response.json()["quota"] == 200 * 1024 * 1024 * 1024

    def test_quota_cannot_drop_below_used_and_reserved(
        self, admin_client: TestClient, test_user: dict
    ) -> None:
        asyncio.run(
            reserve_bytes(
                test_user["id"], 300, quota_bytes=test_user["quota_bytes"]
            )
        )
        response = admin_client.put(
            f"/api/users/{test_user['id']}", json={"quota": 299}
        )
        assert response.status_code == 400
        assert "已用空间与冻结空间" in response.json()["detail"]

    def test_update_user_password(self, admin_client: TestClient, test_user: dict):
        response = admin_client.put(f"/api/users/{test_user['id']}", json={"password": "newpassword123"})
        assert response.status_code == 200

    def test_update_user_duplicate_username(self, admin_client: TestClient, test_user: dict, test_admin: dict):
        response = admin_client.put(
            f"/api/users/{test_user['id']}",
            json={"username": "admin", "password": "new-password"},
        )
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
        response = admin_client.delete(f"/api/users/{test_user['id']}")
        assert response.status_code == 202
        assert response.json() == {
            "ok": True,
            "state": "pending",
            "accepted": True,
        }

    def test_delete_user_waits_for_pack_jobs(
        self, admin_client: TestClient, test_user: dict
    ):
        with (
            patch(
                "app.modules.pack.PackTaskManager.cancel_user_jobs",
                new_callable=AsyncMock,
            ) as cancel_jobs,
            patch(
                "app.modules.pack.PackTaskManager.unblock_user",
                new_callable=AsyncMock,
            ) as unblock_user,
        ):
            response = admin_client.delete(f"/api/users/{test_user['id']}")
            cancel_jobs.assert_not_awaited()
            asyncio.run(DeletionCleanupManager.run_once())
        assert response.status_code == 202
        cancel_jobs.assert_awaited_once_with(test_user["id"])
        unblock_user.assert_awaited_once_with(test_user["id"])

    def test_delete_user_not_found(self, admin_client: TestClient):
        response = admin_client.delete("/api/users/99999")
        assert response.status_code == 404

    def test_cannot_delete_self(self, admin_client: TestClient, test_admin: dict):
        response = admin_client.delete(f"/api/users/{test_admin['id']}")
        assert response.status_code == 400
        assert "不能删除自己" in response.json()["detail"]


class TestAtomicAdminMutations:
    @pytest.mark.asyncio
    async def test_concurrent_cross_demotion_preserves_an_admin(
        self, test_admin: dict, temp_db: str
    ):
        other = await create_user_v0(
            username="other-admin", password="password", is_admin=True
        )

        results = await asyncio.gather(
            auth_repo.update_user_as_admin(
                actor_id=test_admin["id"],
                user_id=other["id"],
                expected_username=other["username"],
                is_admin=False,
            ),
            auth_repo.update_user_as_admin(
                actor_id=other["id"],
                user_id=test_admin["id"],
                expected_username=test_admin["username"],
                is_admin=False,
            ),
            return_exceptions=True,
        )

        assert sum(isinstance(result, dict) for result in results) == 1
        failures = [result for result in results if isinstance(result, Exception)]
        assert len(failures) == 1
        assert isinstance(failures[0], auth_repo.AdminActorInvalidError)
        assert await auth_repo.count_admins() == 1

    @pytest.mark.asyncio
    async def test_concurrent_cross_delete_preserves_an_admin(
        self, test_admin: dict, temp_db: str
    ):
        other = await create_user_v0(
            username="other-admin", password="password", is_admin=True
        )

        results = await asyncio.gather(
            auth_repo.delete_user_as_admin(
                actor_id=test_admin["id"], user_id=other["id"]
            ),
            auth_repo.delete_user_as_admin(
                actor_id=other["id"], user_id=test_admin["id"]
            ),
            return_exceptions=True,
        )

        assert sum(isinstance(result, dict) for result in results) == 1
        failures = [result for result in results if isinstance(result, Exception)]
        assert len(failures) == 1
        assert isinstance(failures[0], auth_repo.AdminActorInvalidError)
        assert await auth_repo.count_admins() == 1

    @pytest.mark.asyncio
    async def test_admin_password_reset_failure_rolls_back_sessions(
        self,
        test_admin: dict,
        test_user: dict,
        user_session: str,
        monkeypatch: pytest.MonkeyPatch,
    ):
        original_delete = auth_repo.delete

        def fail_session_delete(table):
            if table is sessions:
                raise RuntimeError("session deletion failed")
            return original_delete(table)

        monkeypatch.setattr(auth_repo, "delete", fail_session_delete)
        with pytest.raises(RuntimeError, match="session deletion failed"):
            await auth_repo.update_user_as_admin(
                actor_id=test_admin["id"],
                user_id=test_user["id"],
                expected_username=test_user["username"],
                password_hash="replacement-hash",
            )

        user = await auth_repo.get_user_by_id(test_user["id"])
        assert user is not None
        assert verify_password("testpass", user["password_hash"])
        async with transaction() as conn:
            session = (
                await conn.execute(
                    select(sessions.c.id).where(sessions.c.id == user_session)
                )
            ).first()
        assert session is not None


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
