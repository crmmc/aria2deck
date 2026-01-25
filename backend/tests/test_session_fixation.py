"""会话固定防护测试

测试场景：
1. 登录后 session ID 应该轮换
2. 旧 session 在登录后应该失效
3. 重复登录每次都获得新 session
"""
from fastapi.testclient import TestClient

from app.core.config import settings


class TestSessionFixationProtection:
    """会话固定防护测试套件"""

    def test_login_regenerates_session_id(
        self, client: TestClient, test_user: dict
    ):
        """测试登录后 session ID 轮换"""
        # 第一次登录
        response = client.post(
            "/api/auth/login",
            json={"username": "testuser", "password": "testpass"}
        )
        assert response.status_code == 200
        first_session = response.cookies.get(settings.session_cookie_name)
        assert first_session is not None

        # 登出
        client.cookies.set(settings.session_cookie_name, first_session)
        response = client.post("/api/auth/logout")
        assert response.status_code == 200

        # 第二次登录应该获得不同的 session ID
        client.cookies.clear()
        response = client.post(
            "/api/auth/login",
            json={"username": "testuser", "password": "testpass"}
        )
        assert response.status_code == 200
        second_session = response.cookies.get(settings.session_cookie_name)
        assert second_session is not None
        assert second_session != first_session

    def test_old_session_invalidated_on_login(
        self, client: TestClient, test_user: dict
    ):
        """测试登录时旧 session 被清除"""
        # 第一次登录
        response = client.post(
            "/api/auth/login",
            json={"username": "testuser", "password": "testpass"}
        )
        assert response.status_code == 200
        first_session = response.cookies.get(settings.session_cookie_name)

        # 确认第一个 session 有效
        client.cookies.set(settings.session_cookie_name, first_session)
        response = client.get("/api/auth/me")
        assert response.status_code == 200

        # 携带旧 session 再次登录
        response = client.post(
            "/api/auth/login",
            json={"username": "testuser", "password": "testpass"}
        )
        assert response.status_code == 200
        second_session = response.cookies.get(settings.session_cookie_name)

        # 新 session 应该有效
        client.cookies.set(settings.session_cookie_name, second_session)
        response = client.get("/api/auth/me")
        assert response.status_code == 200

        # 旧 session 应该失效
        client.cookies.set(settings.session_cookie_name, first_session)
        response = client.get("/api/auth/me")
        assert response.status_code == 401

    def test_multiple_logins_always_new_session(
        self, client: TestClient, test_user: dict
    ):
        """测试多次登录每次都获得新 session"""
        sessions = []

        for i in range(5):
            # 如果有旧 session，携带它登录
            if sessions:
                client.cookies.set(settings.session_cookie_name, sessions[-1])
            else:
                client.cookies.clear()

            response = client.post(
                "/api/auth/login",
                json={"username": "testuser", "password": "testpass"}
            )
            assert response.status_code == 200
            new_session = response.cookies.get(settings.session_cookie_name)
            assert new_session is not None

            # 每次登录的 session 应该都不同
            assert new_session not in sessions
            sessions.append(new_session)

        # 只有最后一个 session 有效
        for i, session in enumerate(sessions):
            client.cookies.set(settings.session_cookie_name, session)
            response = client.get("/api/auth/me")
            if i == len(sessions) - 1:
                assert response.status_code == 200
            else:
                assert response.status_code == 401
