"""Tests for auth module."""
import asyncio
import pytest
from unittest.mock import MagicMock
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from app.auth import (
    create_session,
    clear_session,
    clear_user_sessions,
    get_user_by_session,
    set_session_cookie,
)
from app.core.config import settings
from app.core.security import hash_password, verify_password
from app.db.engine import transaction
from app.db.schema import sessions, users
from app.repositories import auth as auth_repo
from tests.helpers_v0 import create_session_v0, now_ms


async def fetch_session(session_id: str) -> dict | None:
    async with transaction() as conn:
        row = (await conn.execute(select(sessions).where(sessions.c.id == session_id))).mappings().first()
    return dict(row) if row else None


async def count_user_sessions(user_id: int) -> int:
    async with transaction() as conn:
        count = (
            await conn.execute(select(func.count()).select_from(sessions).where(sessions.c.user_id == user_id))
        ).scalar_one()
    return int(count)


async def list_user_sessions(user_id: int) -> list[dict]:
    async with transaction() as conn:
        rows = (await conn.execute(select(sessions).where(sessions.c.user_id == user_id))).mappings().all()
    return [dict(row) for row in rows]


async def update_user_row(user_id: int, **values) -> None:
    async with transaction() as conn:
        await conn.execute(update(users).where(users.c.id == user_id).values(**values, updated_at_ms=now_ms()))


@pytest.mark.asyncio
class TestCreateSession:

    async def test_create_session(self, test_user: dict):
        session_id = await create_session(test_user["id"])
        assert session_id is not None
        assert len(session_id) == 32

        session = await fetch_session(session_id)
        assert session is not None
        assert session["user_id"] == test_user["id"]


@pytest.mark.asyncio
class TestClearSession:

    async def test_clear_session(self, test_user: dict):
        session_id = await create_session(test_user["id"])
        session = await fetch_session(session_id)
        assert session is not None

        await clear_session(session_id)
        session = await fetch_session(session_id)
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

        assert await count_user_sessions(test_user["id"]) == 0

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
        await create_session_v0(test_user["id"], "expired_session", expires_at_ms=now_ms() - 60 * 60 * 1000)

        user = await get_user_by_session("expired_session")
        assert user is None

        session = await fetch_session("expired_session")
        assert session is None

    async def test_get_user_by_session_timezone_naive(self, test_user: dict):
        await create_session_v0(test_user["id"], "naive_tz_session", expires_at_ms=now_ms() + 12 * 60 * 60 * 1000)

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


class TestAtomicPasswordChange:
    @pytest.mark.asyncio
    async def test_password_hash_cas_allows_one_concurrent_change(
        self, test_user: dict, user_session: str
    ):
        expected_hash = str(test_user["password_hash"])

        async def change(new_password: str, session_id: str) -> bool:
            return await auth_repo.change_password_and_replace_session(
                user_id=test_user["id"],
                expected_password_hash=expected_hash,
                new_password_hash=hash_password(new_password),
                session_id=session_id,
                expires_at_ms=now_ms() + 60_000,
            )

        results = await asyncio.gather(
            change("new-password-a", "replacement-a"),
            change("new-password-b", "replacement-b"),
        )

        assert sorted(results) == [False, True]
        user = await auth_repo.get_user_by_id(test_user["id"])
        assert user is not None
        assert verify_password("new-password-a", user["password_hash"]) != verify_password(
            "new-password-b", user["password_hash"]
        )
        current_sessions = await list_user_sessions(test_user["id"])
        assert len(current_sessions) == 1
        assert current_sessions[0]["id"] in {"replacement-a", "replacement-b"}

    @pytest.mark.asyncio
    async def test_session_insert_failure_rolls_back_password_and_revocation(
        self,
        test_user: dict,
        user_session: str,
        test_admin: dict,
    ):
        await create_session_v0(test_admin["id"], "conflicting-session")

        with pytest.raises(IntegrityError):
            await auth_repo.change_password_and_replace_session(
                user_id=test_user["id"],
                expected_password_hash=test_user["password_hash"],
                new_password_hash=hash_password("new-password"),
                session_id="conflicting-session",
                expires_at_ms=now_ms() + 60_000,
            )

        user = await auth_repo.get_user_by_id(test_user["id"])
        assert user is not None
        assert user["password_hash"] == test_user["password_hash"]
        assert await fetch_session(user_session) is not None


class TestAuthRouterEndpoints:

    def test_login_success(self, client, test_user: dict):
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
        asyncio.run(update_user_row(test_user["id"], is_initial_password=1))

        response = authenticated_client.post("/api/auth/change-password", json={
            "old_password": "anyvalue",
            "new_password": "newpassword123"
        })
        assert response.status_code == 200
        assert response.json()["ok"] is True

    def test_change_password_wrong_old(self, authenticated_client, test_user: dict):
        asyncio.run(update_user_row(test_user["id"], is_initial_password=0))

        response = authenticated_client.post("/api/auth/change-password", json={
            "old_password": "wrongpassword",
            "new_password": "newpassword123"
        })
        assert response.status_code == 400

    def test_change_password_same_as_old(self, authenticated_client, test_user: dict):
        asyncio.run(
            update_user_row(
                test_user["id"],
                is_initial_password=0,
                password_hash=hash_password("samepassword"),
            )
        )

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
        asyncio.run(
            update_user_row(
                test_user["id"],
                is_initial_password=0,
                password_hash=hash_password("oldpassword"),
            )
        )
        asyncio.run(create_session_v0(test_user["id"], "extra_session_1"))
        asyncio.run(create_session_v0(test_user["id"], "extra_session_2"))

        sessions_before = asyncio.run(list_user_sessions(test_user["id"]))
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

        sessions_after = asyncio.run(list_user_sessions(test_user["id"]))
        assert len(sessions_after) == 1


class TestLoginRateLimit:

    @pytest.mark.asyncio
    async def test_login_blocked_after_many_failures(self, client, temp_db: str, test_user: dict):
        from app.core.rate_limit import login_limiter
        from app.core.rate_limit_config import rate_limit_config
        from app.services.rate_limit_service import login_account_key

        key = login_account_key("testuser")
        assert key == "login:account:testuser"
        for _ in range(rate_limit_config.account_security):
            await login_limiter.record_failure(key)

        response = client.post(
            "/api/auth/login",
            json={"username": "testuser", "password": "testpass"},
        )
        assert response.status_code == 429
        assert response.json()["detail"] == "登录尝试次数过多，请稍后再试"

    def test_login_bucket_normalizes_username(
        self, client, temp_db: str, test_user: dict
    ):
        from app.core.rate_limit_config import rate_limit_config

        for _ in range(rate_limit_config.account_security):
            response = client.post(
                "/api/auth/login",
                json={"username": "TESTUSER", "password": "wrong"},
            )
            assert response.status_code == 401

        response = client.post(
            "/api/auth/login",
            json={"username": "testuser", "password": "testpass"},
        )
        assert response.status_code == 429

    @pytest.mark.asyncio
    async def test_account_bucket_blocks_rotating_ips(self, temp_db: str):
        from app.core.rate_limit_config import rate_limit_config
        from app.domain.errors import TooManyRequestsError
        from app.services.rate_limit_service import (
            ensure_login_allowed,
            record_login_failure,
        )

        for index in range(rate_limit_config.account_security):
            await record_login_failure("TestUser", f"192.0.2.{index}")

        with pytest.raises(TooManyRequestsError):
            await ensure_login_allowed("testuser", "198.51.100.1")

    @pytest.mark.asyncio
    async def test_disabled_login_limit_leaves_no_keys(self, temp_db: str):
        from app.core.rate_limit import login_limiter
        from app.core.rate_limit_config import rate_limit_config
        from app.services.rate_limit_service import (
            clear_login_failures,
            ensure_account_security_allowed,
            ensure_login_allowed,
            record_login_failure,
        )

        original_limit = rate_limit_config.account_security
        rate_limit_config.account_security = 0
        try:
            await ensure_login_allowed("testuser", "192.0.2.1")
            await record_login_failure("testuser", "192.0.2.1")
            await clear_login_failures("testuser")
            await ensure_account_security_allowed("192.0.2.1")
            assert not login_limiter._requests
        finally:
            rate_limit_config.account_security = original_limit

    def test_success_only_clears_current_account_bucket(
        self, client, test_user: dict, test_admin: dict
    ):
        from app.core.rate_limit_config import rate_limit_config

        response = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "wrong"},
        )
        assert response.status_code == 401

        response = client.post(
            "/api/auth/login",
            json={"username": "testuser", "password": "testpass"},
        )
        assert response.status_code == 200

        for index in range(rate_limit_config.account_security - 1):
            response = client.post(
                "/api/auth/login",
                json={"username": f"missing_{index}", "password": "wrong"},
            )
            assert response.status_code == 401

        response = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "adminpass"},
        )
        assert response.status_code == 429


class TestChangePasswordRateLimit:

    @pytest.mark.asyncio
    async def test_change_password_rate_limited(self, authenticated_client, test_user: dict):
        from app.core.rate_limit import api_limiter
        from app.core.rate_limit_config import rate_limit_config

        await update_user_row(
            test_user["id"],
            is_initial_password=0,
            password_hash=hash_password("oldpassword"),
        )
        limit = rate_limit_config.account_security
        for _ in range(limit):
            await api_limiter.is_allowed(test_user["id"], "account_security", limit=limit, window_seconds=300)

        response = authenticated_client.post("/api/auth/change-password", json={
            "old_password": "oldpassword",
            "new_password": "newpassword123"
        })
        assert response.status_code == 429
        assert response.json()["detail"] == "操作过于频繁，请稍后再试"


class TestLoginWithExistingSession:

    def test_login_clears_old_session(self, client, test_user: dict, temp_db: str):
        old_session_id = "old_session_to_clear"
        asyncio.run(create_session_v0(test_user["id"], old_session_id))

        client.cookies.set(settings.session_cookie_name, old_session_id)

        response = client.post("/api/auth/login", json={
            "username": "testuser",
            "password": "testpass"
        })
        assert response.status_code == 200

        old_session = asyncio.run(fetch_session(old_session_id))
        assert old_session is None


@pytest.fixture
def authenticated_client(client, user_session: str):
    client.cookies.set(settings.session_cookie_name, user_session)
    return client
