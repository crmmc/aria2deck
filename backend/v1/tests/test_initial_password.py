"""Tests for initial password (zero-knowledge password) functionality."""

from __future__ import annotations

import asyncio
import hashlib

import pytest
from fastapi.testclient import TestClient

from app.core.config import INITIAL_ADMIN_PASSWORD_ENV, settings
from app.core.security import hash_password
from app.db.engine import transaction
from app.db.schema import users
from app.main import ensure_default_admin_v0, reset_admin_password_for_dev_v0
from app.repositories.auth import get_user_by_username
from tests.helpers_v0 import create_session_v0, create_user_v0, get_user_v0


def _client_hash(password: str, username: str) -> str:
    salt = hashlib.sha256(username.lower().encode("utf-8")).digest()
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 10000).hex()


def _create_user(
    *,
    username: str,
    password: str,
    is_admin: bool = False,
    is_initial_password: bool = False,
) -> dict:
    return asyncio.run(
        create_user_v0(
            username=username,
            password=password,
            is_admin=is_admin,
            is_initial_password=is_initial_password,
        )
    )


def _create_session(user_id: int, session_id: str) -> None:
    asyncio.run(create_session_v0(user_id, session_id))


def _get_user(user_id: int) -> dict:
    user = asyncio.run(get_user_v0(user_id))
    assert user is not None
    return user


def _set_password(username: str, password: str, *, is_initial_password: bool) -> None:
    async def update_password() -> None:
        async with transaction() as conn:
            await conn.execute(
                users.update()
                .where(users.c.username == username)
                .values(
                    password_hash=hash_password(password),
                    is_initial_password=1 if is_initial_password else 0,
                )
            )

    asyncio.run(update_password())


class TestInitialPasswordLogin:
    """Test login behavior for users with initial password state."""

    def test_initial_password_user_can_login_with_correct_password(
        self, client: TestClient, temp_db: str
    ) -> None:
        client_hash = "a" * 64
        _create_user(
            username="newuser",
            password=client_hash,
            is_initial_password=True,
        )

        response = client.post(
            "/api/auth/login",
            json={"username": "newuser", "password": client_hash},
        )

        assert response.status_code == 200
        assert response.json()["username"] == "newuser"
        assert response.json()["is_initial_password"] is True

    def test_initial_password_user_cannot_login_with_wrong_password(
        self, client: TestClient, temp_db: str
    ) -> None:
        _create_user(
            username="newuser",
            password="a" * 64,
            is_initial_password=True,
        )

        response = client.post(
            "/api/auth/login",
            json={"username": "newuser", "password": "wrongpassword"},
        )

        assert response.status_code == 401
        assert "用户名或密码错误" in response.json()["detail"]

    def test_normal_user_can_login(self, client: TestClient, temp_db: str) -> None:
        client_hash = "a" * 64
        _create_user(username="normaluser", password=client_hash)

        response = client.post(
            "/api/auth/login",
            json={"username": "normaluser", "password": client_hash},
        )

        assert response.status_code == 200
        assert response.json()["username"] == "normaluser"
        assert response.json()["is_initial_password"] is False


class TestAdminResetUserPassword:
    """Test admin resetting user password."""

    def test_admin_reset_user_password_sets_initial_flag(
        self, client: TestClient, temp_db: str
    ) -> None:
        admin = _create_user(username="admin", password="g" * 64, is_admin=True)
        target = _create_user(username="targetuser", password="h" * 64)
        _create_session(admin["id"], "admin_session_test")

        client.cookies.set(settings.session_cookie_name, "admin_session_test")
        response = client.put(f"/api/users/{target['id']}", json={"password": "i" * 64})

        assert response.status_code == 200
        assert _get_user(target["id"])["is_initial_password"] == 1

    def test_admin_update_own_password_keeps_non_initial_flag(
        self, client: TestClient, temp_db: str
    ) -> None:
        old_hash = "g" * 64
        admin = _create_user(
            username="admin",
            password=old_hash,
            is_admin=True,
            is_initial_password=False,
        )
        _create_session(admin["id"], "admin_self_update_session")

        new_hash = "z" * 64
        client.cookies.set(settings.session_cookie_name, "admin_self_update_session")
        response = client.put(f"/api/users/{admin['id']}", json={"password": new_hash})

        assert response.status_code == 200
        assert _get_user(admin["id"])["is_initial_password"] == 0

        client.cookies.clear()
        login_response = client.post(
            "/api/auth/login", json={"username": "admin", "password": new_hash}
        )
        assert login_response.status_code == 200
        assert login_response.json()["is_initial_password"] is False


class TestChangePassword:
    """Test user changing their own password."""

    def test_change_password_clears_initial_flag(
        self, client: TestClient, temp_db: str
    ) -> None:
        old_hash = "j" * 64
        user = _create_user(
            username="changeuser",
            password=old_hash,
            is_initial_password=True,
        )
        _create_session(user["id"], "change_session_test")

        client.cookies.set(settings.session_cookie_name, "change_session_test")
        response = client.post(
            "/api/auth/change-password",
            json={"old_password": old_hash, "new_password": "k" * 64},
        )

        assert response.status_code == 200
        assert _get_user(user["id"])["is_initial_password"] == 0

    def test_initial_user_can_change_without_old_password(
        self, client: TestClient, temp_db: str
    ) -> None:
        user = _create_user(
            username="inituser",
            password="anyoldhash",
            is_initial_password=True,
        )
        _create_session(user["id"], "init_session_test")

        client.cookies.set(settings.session_cookie_name, "init_session_test")
        response = client.post(
            "/api/auth/change-password",
            json={"old_password": "wrongoldhash", "new_password": "l" * 64},
        )

        assert response.status_code == 200
        assert _get_user(user["id"])["is_initial_password"] == 0


class TestMeEndpoint:
    """Test /me endpoint returns is_initial_password correctly."""

    def test_me_returns_initial_password_flag(
        self, client: TestClient, temp_db: str
    ) -> None:
        user = _create_user(
            username="meuser",
            password="m" * 64,
            is_initial_password=True,
        )
        _create_session(user["id"], "me_session_test")

        client.cookies.set(settings.session_cookie_name, "me_session_test")
        response = client.get("/api/auth/me")

        assert response.status_code == 200
        assert response.json()["is_initial_password"] is True


class TestDefaultAdminBootstrap:
    def test_missing_initial_admin_password_fails_closed(
        self, temp_db: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "initial_admin_password", "")

        with pytest.raises(RuntimeError, match=INITIAL_ADMIN_PASSWORD_ENV):
            asyncio.run(ensure_default_admin_v0())

        assert asyncio.run(get_user_by_username("admin")) is None

    def test_weak_initial_admin_password_fails_closed(
        self, temp_db: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "initial_admin_password", "123456")

        with pytest.raises(RuntimeError, match="长度不足"):
            asyncio.run(ensure_default_admin_v0())

    def test_existing_admin_does_not_require_initial_password(
        self, temp_db: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _create_user(username="existing-admin", password="stored-hash", is_admin=True)
        monkeypatch.setattr(settings, "initial_admin_password", "")

        asyncio.run(ensure_default_admin_v0())

        assert asyncio.run(get_user_by_username("admin")) is None

    def test_configured_admin_can_login_with_frontend_hash(
        self,
        client: TestClient,
        temp_db: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        initial_password = "correct horse battery staple"
        monkeypatch.setattr(settings, "initial_admin_password", initial_password)
        asyncio.run(ensure_default_admin_v0())

        response = client.post(
            "/api/auth/login",
            json={
                "username": "admin",
                "password": _client_hash(initial_password, "admin"),
            },
        )

        assert response.status_code == 200
        assert response.json()["username"] == "admin"
        assert response.json()["is_initial_password"] is True

    def test_dev_reset_admin_password_takes_effect_immediately(
        self,
        client: TestClient,
        temp_db: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        initial_password = "local development admin password"
        monkeypatch.setattr(settings, "initial_admin_password", initial_password)
        asyncio.run(ensure_default_admin_v0())
        _set_password(
            "admin",
            "not-default-client-hash",
            is_initial_password=False,
        )

        assert asyncio.run(reset_admin_password_for_dev_v0()) is True

        response = client.post(
            "/api/auth/login",
            json={
                "username": "admin",
                "password": _client_hash(initial_password, "admin"),
            },
        )

        assert response.status_code == 200
        assert response.json()["username"] == "admin"
        assert response.json()["is_initial_password"] is True
