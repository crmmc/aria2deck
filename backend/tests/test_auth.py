"""Tests for auth module."""
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
from fastapi.testclient import TestClient

from app.auth import (
    create_session,
    clear_session,
    clear_user_sessions,
    get_user_by_session,
    set_session_cookie,
)
from app.core.config import settings
from app.db import execute, fetch_one


@pytest.mark.asyncio
class TestCreateSession:

    async def test_create_session(self, test_user: dict):
        session_id = await create_session(test_user["id"])
        assert session_id is not None
        assert len(session_id) == 32

        session = fetch_one("SELECT * FROM sessions WHERE id = ?", [session_id])
        assert session is not None
        assert session["user_id"] == test_user["id"]


@pytest.mark.asyncio
class TestClearSession:

    async def test_clear_session(self, test_user: dict):
        session_id = await create_session(test_user["id"])
        session = fetch_one("SELECT * FROM sessions WHERE id = ?", [session_id])
        assert session is not None

        await clear_session(session_id)
        session = fetch_one("SELECT * FROM sessions WHERE id = ?", [session_id])
        assert session is None

    async def test_clear_nonexistent_session(self, temp_db: str):
        await clear_session("nonexistent_session_id")


@pytest.mark.asyncio
class TestClearUserSessions:

    async def test_clear_user_sessions(self, test_user: dict):
        await create_session(test_user["id"])
        await create_session(test_user["id"])
        await create_session(test_user["id"])

        count = await clear_user_sessions(test_user["id"])
        assert count == 3

        sessions = fetch_one("SELECT COUNT(*) as cnt FROM sessions WHERE user_id = ?", [test_user["id"]])
        assert sessions["cnt"] == 0

    async def test_clear_user_sessions_no_sessions(self, test_user: dict):
        count = await clear_user_sessions(test_user["id"])
        assert count == 0


@pytest.mark.asyncio
class TestGetUserBySession:

    async def test_get_user_by_session_valid(self, test_user: dict):
        session_id = await create_session(test_user["id"])
        user = await get_user_by_session(session_id)
        assert user is not None
        assert user.id == test_user["id"]
        assert user.username == test_user["username"]

    async def test_get_user_by_session_none(self, temp_db: str):
        user = await get_user_by_session(None)
        assert user is None

    async def test_get_user_by_session_invalid(self, temp_db: str):
        user = await get_user_by_session("invalid_session_id")
        assert user is None

    async def test_get_user_by_session_expired(self, test_user: dict):
        expired_at = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        execute(
            "INSERT INTO sessions (id, user_id, expires_at) VALUES (?, ?, ?)",
            ["expired_session", test_user["id"], expired_at]
        )

        user = await get_user_by_session("expired_session")
        assert user is None

        session = fetch_one("SELECT * FROM sessions WHERE id = ?", ["expired_session"])
        assert session is None

    async def test_get_user_by_session_timezone_naive(self, test_user: dict):
        future_at = (datetime.now() + timedelta(hours=12)).strftime("%Y-%m-%dT%H:%M:%S")
        execute(
            "INSERT INTO sessions (id, user_id, expires_at) VALUES (?, ?, ?)",
            ["naive_tz_session", test_user["id"], future_at]
        )

        user = await get_user_by_session("naive_tz_session")
        assert user is not None
        assert user.id == test_user["id"]


class TestSetSessionCookie:

    def test_set_session_cookie(self):
        response = MagicMock()
        set_session_cookie(response, "test_session_id")

        response.set_cookie.assert_called_once_with(
            settings.session_cookie_name,
            "test_session_id",
            httponly=True,
            secure=not settings.debug,
            samesite="lax",
            max_age=settings.session_ttl_seconds,
        )


class TestAuthRouterEndpoints:

    def test_login_success(self, client, test_user: dict):
        from app.core.security import hash_password
        from app.db import execute
        execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            [hash_password("testpass"), test_user["id"]]
        )

        response = client.post("/api/auth/login", json={
            "username": "testuser",
            "password": "testpass"
        })
        assert response.status_code == 200
        data = response.json()
        assert data["username"] == "testuser"

    def test_login_wrong_password(self, client, test_user: dict):
        response = client.post("/api/auth/login", json={
            "username": "testuser",
            "password": "wrongpassword"
        })
        assert response.status_code == 401

    def test_login_nonexistent_user(self, client, temp_db: str):
        response = client.post("/api/auth/login", json={
            "username": "nonexistent",
            "password": "password"
        })
        assert response.status_code == 401

    def test_logout_success(self, authenticated_client):
        response = authenticated_client.post("/api/auth/logout")
        assert response.status_code == 200
        assert response.json()["ok"] is True

    def test_logout_unauthorized(self, client, temp_db: str):
        response = client.post("/api/auth/logout")
        assert response.status_code == 401

    def test_me_success(self, authenticated_client, test_user: dict):
        response = authenticated_client.get("/api/auth/me")
        assert response.status_code == 200
        data = response.json()
        assert data["username"] == "testuser"
        assert data["id"] == test_user["id"]

    def test_me_unauthorized(self, client, temp_db: str):
        response = client.get("/api/auth/me")
        assert response.status_code == 401

    def test_change_password_initial(self, authenticated_client, test_user: dict):
        from app.db import execute
        execute("UPDATE users SET is_initial_password = 1 WHERE id = ?", [test_user["id"]])

        response = authenticated_client.post("/api/auth/change-password", json={
            "old_password": "anyvalue",
            "new_password": "newpassword123"
        })
        assert response.status_code == 200
        assert response.json()["ok"] is True

    def test_change_password_wrong_old(self, authenticated_client, test_user: dict):
        from app.db import execute
        execute("UPDATE users SET is_initial_password = 0 WHERE id = ?", [test_user["id"]])

        response = authenticated_client.post("/api/auth/change-password", json={
            "old_password": "wrongpassword",
            "new_password": "newpassword123"
        })
        assert response.status_code == 400

    def test_change_password_same_as_old(self, authenticated_client, test_user: dict):
        from app.db import execute
        from app.core.security import hash_password
        execute("UPDATE users SET is_initial_password = 0, password_hash = ? WHERE id = ?",
                [hash_password("samepassword"), test_user["id"]])

        response = authenticated_client.post("/api/auth/change-password", json={
            "old_password": "samepassword",
            "new_password": "samepassword"
        })
        assert response.status_code == 400

    def test_change_password_unauthorized(self, client, temp_db: str):
        response = client.post("/api/auth/change-password", json={
            "old_password": "old",
            "new_password": "new"
        })
        assert response.status_code == 401

    def test_change_password_success_invalidates_sessions(
        self, authenticated_client, test_user: dict
    ):
        from app.db import execute, fetch_all
        from app.core.security import hash_password

        execute("UPDATE users SET is_initial_password = 0, password_hash = ? WHERE id = ?",
                [hash_password("oldpassword"), test_user["id"]])

        execute(
            "INSERT INTO sessions (id, user_id, expires_at) VALUES (?, ?, ?)",
            ["extra_session_1", test_user["id"], "2099-01-01"]
        )
        execute(
            "INSERT INTO sessions (id, user_id, expires_at) VALUES (?, ?, ?)",
            ["extra_session_2", test_user["id"], "2099-01-01"]
        )

        sessions_before = fetch_all("SELECT * FROM sessions WHERE user_id = ?", [test_user["id"]])
        assert {session["id"] for session in sessions_before} == {
            "test_session_123",
            "extra_session_1",
            "extra_session_2",
        }

        response = authenticated_client.post("/api/auth/change-password", json={
            "old_password": "oldpassword",
            "new_password": "newpassword123"
        })
        assert response.status_code == 200

        sessions_after = fetch_all("SELECT * FROM sessions WHERE user_id = ?", [test_user["id"]])
        assert len(sessions_after) == 1


class TestLoginRateLimit:

    @pytest.mark.asyncio
    async def test_login_blocked_after_many_failures(self, client, temp_db: str, test_user: dict):
        from app.core.security import hash_password
        from app.core.rate_limit import login_limiter
        from app.db import execute

        execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            [hash_password("testpass"), test_user["id"]],
        )
        for _ in range(5):
            await login_limiter.record_failure("testclient")

        response = client.post(
            "/api/auth/login",
            json={"username": "testuser", "password": "testpass"},
        )
        assert response.status_code == 429
        assert response.json()["detail"] == "登录尝试次数过多，请稍后再试"


class TestChangePasswordRateLimit:

    @pytest.mark.asyncio
    async def test_change_password_rate_limited(self, authenticated_client, test_user: dict):
        from app.core.security import hash_password
        from app.core.rate_limit import api_limiter
        from app.db import execute

        execute(
            "UPDATE users SET is_initial_password = 0, password_hash = ? WHERE id = ?",
            [hash_password("oldpassword"), test_user["id"]],
        )
        for _ in range(5):
            await api_limiter.is_allowed(test_user["id"], "change_password", limit=5, window_seconds=300)

        response = authenticated_client.post("/api/auth/change-password", json={
            "old_password": "oldpassword",
            "new_password": "newpassword123"
        })
        assert response.status_code == 429
        assert response.json()["detail"] == "操作过于频繁，请稍后再试"


class TestLoginWithExistingSession:

    def test_login_clears_old_session(self, client, test_user: dict, temp_db: str):
        from app.core.security import hash_password
        from app.db import fetch_one

        execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            [hash_password("testpass"), test_user["id"]]
        )

        old_session_id = "old_session_to_clear"
        expires_at = (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
        execute(
            "INSERT INTO sessions (id, user_id, expires_at) VALUES (?, ?, ?)",
            [old_session_id, test_user["id"], expires_at]
        )

        client.cookies.set(settings.session_cookie_name, old_session_id)

        response = client.post("/api/auth/login", json={
            "username": "testuser",
            "password": "testpass"
        })
        assert response.status_code == 200

        old_session = fetch_one("SELECT * FROM sessions WHERE id = ?", [old_session_id])
        assert old_session is None


@pytest.fixture
def authenticated_client(client, user_session: str):
    client.cookies.set(settings.session_cookie_name, user_session)
    return client
