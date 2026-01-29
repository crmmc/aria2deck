"""Tests for initial password (zero-knowledge password) functionality."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.security import hash_password
from app.db import execute, fetch_one


class TestInitialPasswordLogin:
    """Test login behavior for users with initial password state."""

    def test_initial_password_user_can_login_with_correct_password(self, client: TestClient, temp_db: str):
        """Initial password users can login with correct password, returns is_initial_password=True."""
        client_hash = "a" * 64
        execute(
            """
            INSERT INTO users (username, password_hash, is_admin, is_initial_password, created_at, quota)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ["newuser", hash_password(client_hash), 0, 1, datetime.now(timezone.utc).isoformat(), 100 * 1024 * 1024 * 1024]
        )

        response = client.post("/api/auth/login", json={
            "username": "newuser",
            "password": client_hash
        })

        assert response.status_code == 200
        assert response.json()["username"] == "newuser"
        assert response.json()["is_initial_password"] is True

    def test_initial_password_user_cannot_login_with_wrong_password(self, client: TestClient, temp_db: str):
        """Initial password users cannot login with wrong password."""
        client_hash = "a" * 64
        execute(
            """
            INSERT INTO users (username, password_hash, is_admin, is_initial_password, created_at, quota)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ["newuser", hash_password(client_hash), 0, 1, datetime.now(timezone.utc).isoformat(), 100 * 1024 * 1024 * 1024]
        )

        response = client.post("/api/auth/login", json={
            "username": "newuser",
            "password": "wrongpassword"
        })

        assert response.status_code == 401
        assert "用户名或密码错误" in response.json()["detail"]

    def test_normal_user_can_login(self, client: TestClient, temp_db: str):
        """Normal users (is_initial_password=0) should be able to login."""
        # Create normal user with valid password hash
        client_hash = "a" * 64  # Simulated client hash
        execute(
            """
            INSERT INTO users (username, password_hash, is_admin, is_initial_password, created_at, quota)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ["normaluser", hash_password(client_hash), 0, 0, datetime.now(timezone.utc).isoformat(), 100 * 1024 * 1024 * 1024]
        )

        response = client.post("/api/auth/login", json={
            "username": "normaluser",
            "password": client_hash
        })

        assert response.status_code == 200
        assert response.json()["username"] == "normaluser"
        assert response.json()["is_initial_password"] is False


class TestAdminResetUserPassword:
    """Test admin resetting user password."""

    def test_admin_reset_user_password_sets_initial_flag(self, client: TestClient, temp_db: str):
        """When admin resets user password, is_initial_password should be set to true."""
        # Create admin
        admin_id = execute(
            """
            INSERT INTO users (username, password_hash, is_admin, is_initial_password, created_at, quota)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ["admin", hash_password("g" * 64), 1, 0, datetime.now(timezone.utc).isoformat(), 100 * 1024 * 1024 * 1024]
        )

        # Create admin session
        session_id = "admin_session_test"
        expires_at = (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
        execute(
            "INSERT INTO sessions (id, user_id, expires_at) VALUES (?, ?, ?)",
            [session_id, admin_id, expires_at]
        )

        # Create target user (normal, not initial)
        target_id = execute(
            """
            INSERT INTO users (username, password_hash, is_admin, is_initial_password, created_at, quota)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ["targetuser", hash_password("h" * 64), 0, 0, datetime.now(timezone.utc).isoformat(), 100 * 1024 * 1024 * 1024]
        )

        # Admin resets target user's password
        client.cookies.set(settings.session_cookie_name, session_id)
        response = client.put(f"/api/users/{target_id}", json={
            "password": "i" * 64  # New password hash
        })

        assert response.status_code == 200

        # Verify is_initial_password is now 1
        user = fetch_one("SELECT * FROM users WHERE id = ?", [target_id])
        assert user["is_initial_password"] == 1


class TestChangePassword:
    """Test user changing their own password."""

    def test_change_password_clears_initial_flag(self, client: TestClient, temp_db: str):
        """Changing password should clear is_initial_password flag."""
        # Create user with initial password (but with a valid hash for testing)
        old_hash = "j" * 64
        user_id = execute(
            """
            INSERT INTO users (username, password_hash, is_admin, is_initial_password, created_at, quota)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ["changeuser", hash_password(old_hash), 0, 1, datetime.now(timezone.utc).isoformat(), 100 * 1024 * 1024 * 1024]
        )

        # Create session
        session_id = "change_session_test"
        expires_at = (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
        execute(
            "INSERT INTO sessions (id, user_id, expires_at) VALUES (?, ?, ?)",
            [session_id, user_id, expires_at]
        )

        # Change password
        client.cookies.set(settings.session_cookie_name, session_id)
        new_hash = "k" * 64
        response = client.post("/api/auth/change-password", json={
            "old_password": old_hash,
            "new_password": new_hash
        })

        assert response.status_code == 200

        # Verify is_initial_password is now 0
        user = fetch_one("SELECT * FROM users WHERE id = ?", [user_id])
        assert user["is_initial_password"] == 0

    def test_initial_user_can_change_without_old_password(self, client: TestClient, temp_db: str):
        """Initial password users should be able to change password without validating old password."""
        # Create user with initial password state (empty password hash)
        user_id = execute(
            """
            INSERT INTO users (username, password_hash, is_admin, is_initial_password, created_at, quota)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ["inituser", hash_password("anyoldhash"), 0, 1, datetime.now(timezone.utc).isoformat(), 100 * 1024 * 1024 * 1024]
        )

        # Create session
        session_id = "init_session_test"
        expires_at = (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
        execute(
            "INSERT INTO sessions (id, user_id, expires_at) VALUES (?, ?, ?)",
            [session_id, user_id, expires_at]
        )

        # Change password with wrong old password (should still work for initial users)
        client.cookies.set(settings.session_cookie_name, session_id)
        response = client.post("/api/auth/change-password", json={
            "old_password": "wrongoldhash",
            "new_password": "l" * 64
        })

        assert response.status_code == 200

        # Verify is_initial_password is cleared
        user = fetch_one("SELECT * FROM users WHERE id = ?", [user_id])
        assert user["is_initial_password"] == 0


class TestMeEndpoint:
    """Test /me endpoint returns is_initial_password correctly."""

    def test_me_returns_initial_password_flag(self, client: TestClient, temp_db: str):
        """The /me endpoint should return is_initial_password flag."""
        # Create user
        user_id = execute(
            """
            INSERT INTO users (username, password_hash, is_admin, is_initial_password, created_at, quota)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ["meuser", hash_password("m" * 64), 0, 1, datetime.now(timezone.utc).isoformat(), 100 * 1024 * 1024 * 1024]
        )

        # Create session
        session_id = "me_session_test"
        expires_at = (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
        execute(
            "INSERT INTO sessions (id, user_id, expires_at) VALUES (?, ?, ?)",
            [session_id, user_id, expires_at]
        )

        client.cookies.set(settings.session_cookie_name, session_id)
        response = client.get("/api/auth/me")

        assert response.status_code == 200
        assert response.json()["is_initial_password"] is True
