"""密码变更后 Session 失效测试

测试场景：
1. 修改密码后，旧 session 应该失效
2. 修改密码后，需要重新登录
3. 未修改密码时，session 应保持有效
"""
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.security import hash_password
from app.db import execute


class TestPasswordChangeSessionInvalidation:
    """密码变更后 Session 失效测试套件"""

    def _create_test_user(self, username: str, password: str, quota: int = 107374182400) -> int:
        """直接通过 SQL 创建测试用户（is_initial_password=0）"""
        user_id = execute(
            """
            INSERT INTO users (username, password_hash, is_admin, is_initial_password, created_at, quota)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [username, hash_password(password), 0, 0, datetime.now(timezone.utc).isoformat(), quota]
        )
        return user_id

    def test_password_change_invalidates_session(
        self, client: TestClient, test_admin: dict, admin_session: str, temp_db: str
    ):
        """测试修改密码后旧 session 失效"""
        # 创建一个测试用户
        user_id = self._create_test_user("testuser_session", "oldpassword123")

        # 用测试用户登录
        client.cookies.clear()
        response = client.post(
            "/api/auth/login",
            json={"username": "testuser_session", "password": "oldpassword123"}
        )
        assert response.status_code == 200
        user_session_id = response.cookies.get(settings.session_cookie_name)

        # 确认 session 有效
        client.cookies.set(settings.session_cookie_name, user_session_id)
        response = client.get("/api/auth/me")
        assert response.status_code == 200
        assert response.json()["username"] == "testuser_session"

        # 管理员修改用户密码
        client.cookies.set(settings.session_cookie_name, admin_session)
        response = client.put(
            f"/api/users/{user_id}",
            json={"password": "newpassword123"}
        )
        assert response.status_code == 200

        # 旧 session 应该失效
        client.cookies.set(settings.session_cookie_name, user_session_id)
        response = client.get("/api/auth/me")
        assert response.status_code == 401

    def test_password_change_requires_relogin(
        self, client: TestClient, test_admin: dict, admin_session: str, temp_db: str
    ):
        """测试修改密码后需要重新登录"""
        # 创建测试用户
        user_id = self._create_test_user("testuser_relogin", "oldpassword123")

        # 登录
        client.cookies.clear()
        response = client.post(
            "/api/auth/login",
            json={"username": "testuser_relogin", "password": "oldpassword123"}
        )
        assert response.status_code == 200

        # 管理员修改密码
        client.cookies.set(settings.session_cookie_name, admin_session)
        response = client.put(
            f"/api/users/{user_id}",
            json={"password": "newpassword123"}
        )
        assert response.status_code == 200

        # 旧密码登录会返回 401（密码错误）
        client.cookies.clear()
        response = client.post(
            "/api/auth/login",
            json={"username": "testuser_relogin", "password": "oldpassword123"}
        )
        assert response.status_code == 401

        # 新密码登录成功，但 is_initial_password=True
        response = client.post(
            "/api/auth/login",
            json={"username": "testuser_relogin", "password": "newpassword123"}
        )
        assert response.status_code == 200
        assert response.json()["is_initial_password"] is True

    def test_other_updates_keep_session_valid(
        self, client: TestClient, test_admin: dict, admin_session: str, temp_db: str
    ):
        """测试非密码修改不影响 session"""
        # 创建测试用户
        user_id = self._create_test_user("testuser_keep", "password123", quota=1073741824)

        # 登录
        client.cookies.clear()
        response = client.post(
            "/api/auth/login",
            json={"username": "testuser_keep", "password": "password123"}
        )
        assert response.status_code == 200
        user_session_id = response.cookies.get(settings.session_cookie_name)

        # 确认 session 有效
        client.cookies.set(settings.session_cookie_name, user_session_id)
        response = client.get("/api/auth/me")
        assert response.status_code == 200

        # 修改配额（不修改密码）
        client.cookies.set(settings.session_cookie_name, admin_session)
        response = client.put(
            f"/api/users/{user_id}",
            json={"quota": 2147483648}  # 2GB
        )
        assert response.status_code == 200

        # session 应该仍然有效
        client.cookies.set(settings.session_cookie_name, user_session_id)
        response = client.get("/api/auth/me")
        assert response.status_code == 200

    def test_multiple_sessions_all_invalidated(
        self, client: TestClient, test_admin: dict, admin_session: str, temp_db: str
    ):
        """测试修改密码后所有 session 都失效"""
        # 创建测试用户
        user_id = self._create_test_user("testuser_multi", "password123")

        # 模拟多设备登录（登录3次获取3个不同的 session）
        sessions = []
        for _ in range(3):
            client.cookies.clear()
            response = client.post(
                "/api/auth/login",
                json={"username": "testuser_multi", "password": "password123"}
            )
            assert response.status_code == 200
            sessions.append(response.cookies.get(settings.session_cookie_name))

        # 确认所有 session 都有效
        for session_id in sessions:
            client.cookies.set(settings.session_cookie_name, session_id)
            response = client.get("/api/auth/me")
            assert response.status_code == 200

        # 修改密码
        client.cookies.set(settings.session_cookie_name, admin_session)
        response = client.put(
            f"/api/users/{user_id}",
            json={"password": "newpassword123"}
        )
        assert response.status_code == 200

        # 所有 session 都应该失效
        for session_id in sessions:
            client.cookies.set(settings.session_cookie_name, session_id)
            response = client.get("/api/auth/me")
            assert response.status_code == 401
