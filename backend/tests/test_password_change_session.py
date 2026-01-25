"""密码变更后 Session 失效测试

测试场景：
1. 修改密码后，旧 session 应该失效
2. 修改密码后，需要重新登录
3. 未修改密码时，session 应保持有效
"""
import pytest
from fastapi.testclient import TestClient

from app.core.config import settings


class TestPasswordChangeSessionInvalidation:
    """密码变更后 Session 失效测试套件"""

    def test_password_change_invalidates_session(
        self, client: TestClient, test_admin: dict, admin_session: str
    ):
        """测试修改密码后旧 session 失效"""
        # 创建一个测试用户
        client.cookies.set(settings.session_cookie_name, admin_session)
        response = client.post(
            "/api/users",
            json={
                "username": "testuser_session",
                "password": "oldpassword123",
                "is_admin": False
            }
        )
        assert response.status_code == 200
        user_id = response.json()["id"]

        # 清除管理员 cookie，用测试用户登录
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
        self, client: TestClient, test_admin: dict, admin_session: str
    ):
        """测试修改密码后需要重新登录"""
        # 创建测试用户
        client.cookies.set(settings.session_cookie_name, admin_session)
        response = client.post(
            "/api/users",
            json={
                "username": "testuser_relogin",
                "password": "oldpassword123",
                "is_admin": False
            }
        )
        assert response.status_code == 200
        user_id = response.json()["id"]

        # 登录
        client.cookies.clear()
        response = client.post(
            "/api/auth/login",
            json={"username": "testuser_relogin", "password": "oldpassword123"}
        )
        assert response.status_code == 200

        # 修改密码
        client.cookies.set(settings.session_cookie_name, admin_session)
        response = client.put(
            f"/api/users/{user_id}",
            json={"password": "newpassword123"}
        )
        assert response.status_code == 200

        # 旧密码登录应该失败
        client.cookies.clear()
        response = client.post(
            "/api/auth/login",
            json={"username": "testuser_relogin", "password": "oldpassword123"}
        )
        assert response.status_code == 401

        # 新密码登录应该成功
        response = client.post(
            "/api/auth/login",
            json={"username": "testuser_relogin", "password": "newpassword123"}
        )
        assert response.status_code == 200

    def test_other_updates_keep_session_valid(
        self, client: TestClient, test_admin: dict, admin_session: str
    ):
        """测试非密码修改不影响 session"""
        # 创建测试用户
        client.cookies.set(settings.session_cookie_name, admin_session)
        response = client.post(
            "/api/users",
            json={
                "username": "testuser_keep",
                "password": "password123",
                "is_admin": False,
                "quota": 1073741824  # 1GB
            }
        )
        assert response.status_code == 200
        user_id = response.json()["id"]

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
        self, client: TestClient, test_admin: dict, admin_session: str
    ):
        """测试修改密码后所有 session 都失效"""
        # 创建测试用户
        client.cookies.set(settings.session_cookie_name, admin_session)
        response = client.post(
            "/api/users",
            json={
                "username": "testuser_multi",
                "password": "password123",
                "is_admin": False
            }
        )
        assert response.status_code == 200
        user_id = response.json()["id"]

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
