from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from aria2deck.bootstrap import create_app
from aria2deck.domain.errors import DomainError, ErrorCode
from aria2deck.infrastructure.config import Settings
from aria2deck.infrastructure.sqlite.engine import SQLiteDatabase
from tests.api.conftest import (
    OTHER_PASSWORD,
    PASSWORD,
    THIRD_PASSWORD,
    create_admin,
    create_user,
    login,
)

COOKIE = "aria2deck_v2_session"


def test_first_admin_login_me_and_logout(client: TestClient) -> None:
    created = create_admin(client)
    assert created["is_admin"] is True
    assert created["quota_bytes"] == 100 * 1024**3
    assert client.post(
        "/api/v2/users",
        json={"username": "other", "password": OTHER_PASSWORD, "is_admin": True},
    ).status_code == 401

    wrong = client.post(
        "/api/v2/auth/login", json={"username": "admin", "password": OTHER_PASSWORD}
    )
    assert wrong.status_code == 401
    assert wrong.json() == {
        "error": {"code": "unauthenticated", "message": "用户名或密码错误"}
    }

    response = client.post(
        "/api/v2/auth/login", json={"username": "ADMIN", "password": PASSWORD}
    )
    assert response.status_code == 200
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=lax" in response.headers["set-cookie"]
    assert client.get("/api/v2/auth/me").json()["username"] == "admin"
    assert client.post("/api/v2/auth/logout").json() == {"ok": True}
    assert client.get("/api/v2/auth/me").status_code == 401
    assert client.post("/api/v2/auth/logout").status_code == 401


def test_invalid_bearer_never_falls_back_to_valid_cookie(client: TestClient) -> None:
    create_admin(client)
    login(client)
    for authorization in ("Bearer invalid", "Basic abc", "Bearer"):
        response = client.get(
            "/api/v2/auth/me", headers={"Authorization": authorization}
        )
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthenticated"


def test_new_login_revokes_previous_session(client: TestClient) -> None:
    create_admin(client)
    first = login(client)
    second = login(client)
    client.cookies.clear()
    client.cookies.set(COOKIE, first)
    assert client.get("/api/v2/auth/me").status_code == 401
    client.cookies.clear()
    client.cookies.set(COOKIE, second)
    assert client.get("/api/v2/auth/me").status_code == 200


def test_change_password_rotates_password_and_all_sessions(client: TestClient) -> None:
    create_admin(client)
    old_session = login(client)
    assert client.post(
        "/api/v2/auth/password",
        json={"old_password": OTHER_PASSWORD, "new_password": THIRD_PASSWORD},
    ).status_code == 401
    response = client.post(
        "/api/v2/auth/password",
        json={"old_password": PASSWORD, "new_password": OTHER_PASSWORD},
    )
    assert response.status_code == 200
    new_session = response.cookies[COOKIE]
    client.cookies.clear()
    client.cookies.set(COOKIE, old_session)
    assert client.get("/api/v2/auth/me").status_code == 401
    client.cookies.clear()
    client.cookies.set(COOKIE, new_session)
    assert client.get("/api/v2/auth/me").status_code == 200
    client.cookies.clear()
    assert client.post(
        "/api/v2/auth/login", json={"username": "admin", "password": PASSWORD}
    ).status_code == 401
    login(client, password=OTHER_PASSWORD)


def test_initial_password_can_be_changed_without_old_password(client: TestClient) -> None:
    response = client.post(
        "/api/v2/users",
        json={
            "username": "admin",
            "password": PASSWORD,
            "is_admin": True,
            "is_initial_password": True,
        },
    )
    assert response.status_code == 201
    login(client)
    changed = client.post(
        "/api/v2/auth/password", json={"new_password": OTHER_PASSWORD}
    )
    assert changed.status_code == 200
    assert client.get("/api/v2/auth/me").json()["is_initial_password"] is False


def test_expired_session_is_rejected(client: TestClient, settings: Settings) -> None:
    create_admin(client)
    login(client)

    async def expire() -> None:
        database = SQLiteDatabase(settings.database_path)
        async with database.connection() as connection:
            await connection.execute(text("UPDATE sessions SET expires_at_ms = created_at_ms + 1"))
            await connection.commit()
        await database.close()

    asyncio.run(expire())
    assert client.get("/api/v2/auth/me").status_code == 401


async def test_concurrent_first_user_creation_has_one_admin(app: FastAPI, settings: Settings) -> None:
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            responses = await asyncio.gather(
                *(
                    client.post(
                        "/api/v2/users",
                        json={
                            "username": f"admin-{index}",
                            "password": PASSWORD,
                            "is_admin": True,
                        },
                    )
                    for index in range(6)
                )
            )
    assert [response.status_code for response in responses].count(201) == 1
    assert all(
        response.status_code in {201, 401, 409} for response in responses
    )
    database = SQLiteDatabase(settings.database_path)
    async with database.connection() as connection:
        count = (await connection.execute(text("SELECT COUNT(*) FROM users"))).scalar_one()
        admins = (
            await connection.execute(text("SELECT COUNT(*) FROM users WHERE is_admin = 1"))
        ).scalar_one()
    await database.close()
    assert (count, admins) == (1, 1)


async def test_concurrent_password_changes_have_one_winning_generation(client: TestClient) -> None:
    create_admin(client)
    login(client)
    old_cookie = client.cookies.get(COOKIE)
    transport = ASGITransport(app=client.app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        async_client.cookies.set(COOKIE, old_cookie)
        responses = await asyncio.gather(
            async_client.post(
                "/api/v2/auth/password",
                json={"old_password": PASSWORD, "new_password": OTHER_PASSWORD},
            ),
            async_client.post(
                "/api/v2/auth/password",
                json={"old_password": PASSWORD, "new_password": THIRD_PASSWORD},
            ),
        )
    assert [response.status_code for response in responses].count(200) == 1
    assert [response.status_code for response in responses].count(200) + [
        response.status_code in {401, 409} for response in responses
    ].count(True) == 2


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"username": "admin", "password": "bad", "is_admin": True}, 422),
        ({"username": "admin", "password": PASSWORD, "is_admin": False}, 403),
        ({"username": "admin", "password": PASSWORD, "is_admin": True, "extra": 1}, 422),
    ],
)
def test_first_user_input_boundaries(
    client: TestClient, payload: dict[str, object], expected: int
) -> None:
    assert client.post("/api/v2/users", json=payload).status_code == expected


def test_duplicate_username_is_conflict_without_partial_user(client: TestClient) -> None:
    create_admin(client)
    login(client)
    duplicate = client.post(
        "/api/v2/users", json={"username": "ADMIN", "password": OTHER_PASSWORD}
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "duplicate_resource"
    assert client.get("/api/v2/users").json()["total"] == 1


def test_user_fixture_helper_creates_regular_user(client: TestClient) -> None:
    create_admin(client)
    login(client)
    created = create_user(client)
    assert created["is_admin"] is False


def test_production_session_cookie_is_secure(tmp_path: Path) -> None:
    settings = Settings(
        database_path=tmp_path / "data" / "v2.db",
        download_directory=tmp_path / "downloads",
        static_directory=tmp_path / "static",
        debug=False,
        share_jwt_secret="share-" + "s" * 32,
        credential_pepper="pepper-" + "p" * 32,
        initial_admin_password=INITIAL_ADMIN_PASSWORD,
    )
    with TestClient(create_app(settings=settings), base_url="https://test") as client:
        response = client.post(
            "/api/v2/auth/login",
            json={"username": "admin", "password": initial_admin_hash(INITIAL_ADMIN_PASSWORD)},
        )

    assert response.status_code == 200
    assert "Secure" in response.headers["set-cookie"]
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=lax" in response.headers["set-cookie"]



INITIAL_ADMIN_PASSWORD = "test-initial-admin-32-characters"  # noqa: S105  # 本地测试凭证


def initial_admin_hash(password: str, username: str = "admin") -> str:
    # 脚本/UI 客户端共同形态：盐 = sha256(username.strip().lower()) 的 hex ASCII 字节。
    salt = hashlib.sha256(username.strip().lower().encode()).hexdigest().encode("ascii")
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 10000).hex()


def production_settings(tmp_path: Path, initial_password: str | None) -> Settings:
    return Settings(
        database_path=tmp_path / "data" / "v2.db",
        download_directory=tmp_path / "downloads",
        static_directory=tmp_path / "static",
        debug=False,
        share_jwt_secret="share-" + "s" * 32,
        credential_pepper="pepper-" + "p" * 32,
        initial_admin_password=initial_password,
    )


def test_lifespan_creates_initial_admin_once_and_allows_restart_without_password(
    tmp_path: Path,
) -> None:
    settings = production_settings(tmp_path, INITIAL_ADMIN_PASSWORD)
    with TestClient(create_app(settings=settings)) as client:
        response = client.post(
            "/api/v2/auth/login",
            json={"username": "admin", "password": initial_admin_hash(INITIAL_ADMIN_PASSWORD)},
        )
        assert response.status_code == 200
        assert response.json()["is_admin"] is True
        assert response.json()["is_initial_password"] is True

    restarted = replace(settings, initial_admin_password=None)
    with TestClient(create_app(settings=restarted), base_url="https://test") as client:
        response = client.post(
            "/api/v2/auth/login",
            json={"username": "admin", "password": initial_admin_hash(INITIAL_ADMIN_PASSWORD)},
        )
        assert response.status_code == 200
        assert client.get("/api/v2/users").json()["total"] == 1


def test_initial_admin_login_accepts_ui_normalized_username_derivation(
    tmp_path: Path,
) -> None:
    # 回归（跨端哈希形态分叉）：UI 客户端按 username.strip().lower() 派生 wire，
    # 用户名输入含大小写/空白时也必须与 ensure_initial_admin 的存储派生同形可登录。
    settings = production_settings(tmp_path, INITIAL_ADMIN_PASSWORD)
    with TestClient(create_app(settings=settings)) as client:
        response = client.post(
            "/api/v2/auth/login",
            json={
                "username": " Admin ",
                "password": initial_admin_hash(INITIAL_ADMIN_PASSWORD, " adMIN "),
            },
        )
        assert response.status_code == 200
        assert response.json()["is_initial_password"] is True


def test_production_lifespan_rejects_missing_initial_password_for_empty_database(
    tmp_path: Path,
) -> None:
    with pytest.raises(DomainError) as caught:
        with TestClient(create_app(settings=production_settings(tmp_path, None))):
            pass
    assert caught.value.code is ErrorCode.CONFIGURATION_INVALID


@pytest.mark.parametrize("debug", [True, False])
def test_lifespan_rejects_short_initial_password(tmp_path: Path, debug: bool) -> None:
    settings = replace(production_settings(tmp_path, "short"), debug=debug)
    with pytest.raises(DomainError) as caught:
        with TestClient(create_app(settings=settings)):
            pass
    assert caught.value.code is ErrorCode.CONFIGURATION_INVALID



def test_login_rate_limit_returns_uniform_429_and_retry_after(client: TestClient) -> None:
    create_admin(client)
    login(client)
    assert client.patch(
        "/api/v2/settings", json={"account_security_rate_limit": 2}
    ).status_code == 200
    client.cookies.clear()

    first = client.post(
        "/api/v2/auth/login", json={"username": "admin", "password": PASSWORD}
    )
    second = client.post(
        "/api/v2/auth/login", json={"username": "admin", "password": PASSWORD}
    )

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.headers["Retry-After"] == "300"
    assert second.json() == {
        "error": {"code": "rate_limited", "message": "请求过于频繁，请稍后重试"}
    }


def test_authenticated_api_rate_limit_shares_one_bucket_across_endpoints(
    client: TestClient,
) -> None:
    create_admin(client)
    login(client)
    assert client.patch(
        "/api/v2/settings", json={"authenticated_api_rate_limit": 2}
    ).status_code == 200

    assert client.get("/api/v2/auth/me").status_code == 200
    limited = client.get("/api/v2/users")
    assert limited.status_code == 429
    assert limited.headers["Retry-After"] == "60"
    assert client.get("/api/v2/public/site-info").status_code == 200
