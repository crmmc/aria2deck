"""密码变更后 Session 失效测试。"""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.helpers_v0 import create_user_v0


class TestPasswordChangeSessionInvalidation:
    """密码变更后 Session 失效测试套件。"""

    def _create_test_user(
        self, username: str, password: str, quota: int = 107374182400
    ) -> int:
        user = asyncio.run(
            create_user_v0(
                username=username,
                password=password,
                quota_bytes=quota,
            )
        )
        return int(user["id"])

    def test_password_change_invalidates_session(
        self, client: TestClient, test_admin: dict, admin_session: str, temp_db: str
    ) -> None:
        user_id = self._create_test_user("testuser_session", "oldpassword123")

        client.cookies.clear()
        response = client.post(
            "/api/auth/login",
            json={"username": "testuser_session", "password": "oldpassword123"},
        )
        assert response.status_code == 200
        user_session_id = response.cookies.get(settings.session_cookie_name)
        assert user_session_id is not None

        client.cookies.set(settings.session_cookie_name, user_session_id)
        response = client.get("/api/auth/me")
        assert response.status_code == 200
        assert response.json()["username"] == "testuser_session"

        client.cookies.set(settings.session_cookie_name, admin_session)
        response = client.put(
            f"/api/users/{user_id}",
            json={"password": "newpassword123"},
        )
        assert response.status_code == 200

        client.cookies.set(settings.session_cookie_name, user_session_id)
        response = client.get("/api/auth/me")
        assert response.status_code == 401

    def test_password_change_requires_relogin(
        self, client: TestClient, test_admin: dict, admin_session: str, temp_db: str
    ) -> None:
        user_id = self._create_test_user("testuser_relogin", "oldpassword123")

        client.cookies.clear()
        response = client.post(
            "/api/auth/login",
            json={"username": "testuser_relogin", "password": "oldpassword123"},
        )
        assert response.status_code == 200

        client.cookies.set(settings.session_cookie_name, admin_session)
        response = client.put(
            f"/api/users/{user_id}",
            json={"password": "newpassword123"},
        )
        assert response.status_code == 200

        client.cookies.clear()
        response = client.post(
            "/api/auth/login",
            json={"username": "testuser_relogin", "password": "oldpassword123"},
        )
        assert response.status_code == 401

        response = client.post(
            "/api/auth/login",
            json={"username": "testuser_relogin", "password": "newpassword123"},
        )
        assert response.status_code == 200
        assert response.json()["is_initial_password"] is True

    def test_other_updates_keep_session_valid(
        self, client: TestClient, test_admin: dict, admin_session: str, temp_db: str
    ) -> None:
        user_id = self._create_test_user(
            "testuser_keep", "password123", quota=1073741824
        )

        client.cookies.clear()
        response = client.post(
            "/api/auth/login",
            json={"username": "testuser_keep", "password": "password123"},
        )
        assert response.status_code == 200
        user_session_id = response.cookies.get(settings.session_cookie_name)
        assert user_session_id is not None

        client.cookies.set(settings.session_cookie_name, user_session_id)
        response = client.get("/api/auth/me")
        assert response.status_code == 200

        client.cookies.set(settings.session_cookie_name, admin_session)
        response = client.put(
            f"/api/users/{user_id}",
            json={"quota": 2147483648},
        )
        assert response.status_code == 200

        client.cookies.set(settings.session_cookie_name, user_session_id)
        response = client.get("/api/auth/me")
        assert response.status_code == 200

    def test_multiple_sessions_all_invalidated(
        self, client: TestClient, test_admin: dict, admin_session: str, temp_db: str
    ) -> None:
        user_id = self._create_test_user("testuser_multi", "password123")

        sessions: list[str] = []
        for _ in range(3):
            client.cookies.clear()
            response = client.post(
                "/api/auth/login",
                json={"username": "testuser_multi", "password": "password123"},
            )
            assert response.status_code == 200
            session_id = response.cookies.get(settings.session_cookie_name)
            assert session_id is not None
            sessions.append(session_id)

        for session_id in sessions:
            client.cookies.set(settings.session_cookie_name, session_id)
            response = client.get("/api/auth/me")
            assert response.status_code == 200

        client.cookies.set(settings.session_cookie_name, admin_session)
        response = client.put(
            f"/api/users/{user_id}",
            json={"password": "newpassword123"},
        )
        assert response.status_code == 200

        for session_id in sessions:
            client.cookies.set(settings.session_cookie_name, session_id)
            response = client.get("/api/auth/me")
            assert response.status_code == 401
