from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient
from sqlalchemy import text

from aria2deck.infrastructure.config import Settings
from aria2deck.infrastructure.sqlite.engine import SQLiteDatabase
from tests.api.conftest import (
    OTHER_PASSWORD,
    THIRD_PASSWORD,
    create_admin,
    create_user,
    login,
)

COOKIE = "aria2deck_v2_session"


def test_api_token_raw_value_is_shown_once_and_usage_is_tracked(
    client: TestClient, settings: Settings
) -> None:
    create_admin(client)
    login(client)
    create_user(client)
    login(client, "user", OTHER_PASSWORD)

    created_response = client.post("/api/v2/credentials/tokens", json={"name": "  CLI  "})
    assert created_response.status_code == 200
    created = created_response.json()
    raw_token = created["token"]
    assert raw_token.startswith("aria2_")
    assert created["name"] == "CLI"
    assert created["prefix"] == raw_token[:12]
    assert created["last_used_at_ms"] is None

    listed = client.get("/api/v2/credentials/tokens").json()
    assert listed["total"] == 1
    assert listed["items"][0]["name"] == "CLI"
    assert "token" not in listed["items"][0]
    assert raw_token not in str(listed)

    response = client.get(
        "/api/v2/auth/me", headers={"Authorization": f"Bearer {raw_token}"}
    )
    assert response.status_code == 200
    assert response.json()["username"] == "user"
    used = client.get("/api/v2/credentials/tokens").json()["items"][0]
    assert used["last_used_at_ms"] is not None

    async def inspect_database() -> str:
        database = SQLiteDatabase(settings.database_path)
        async with database.connection() as connection:
            stored = (
                await connection.execute(text("SELECT token_digest FROM api_tokens"))
            ).scalar_one()
        await database.close()
        return stored

    stored_digest = asyncio.run(inspect_database())
    assert raw_token not in stored_digest
    assert stored_digest != raw_token


def test_api_token_cannot_call_session_only_endpoints(client: TestClient) -> None:
    create_admin(client)
    login(client)
    create_user(client)
    login(client, "user", OTHER_PASSWORD)
    raw_token = client.post("/api/v2/credentials/tokens", json={}).json()["token"]
    headers = {"Authorization": f"Bearer {raw_token}"}

    assert client.post("/api/v2/auth/logout", headers=headers).status_code == 403
    assert client.post(
        "/api/v2/auth/password",
        headers=headers,
        json={"old_password": OTHER_PASSWORD, "new_password": THIRD_PASSWORD},
    ).status_code == 403
    assert client.get("/api/v2/credentials/tokens", headers=headers).status_code == 403
    assert client.get("/api/v2/credentials/rpc", headers=headers).status_code == 403
    assert client.get("/api/v2/users", headers=headers).status_code == 403
    assert client.get("/api/v2/auth/me").status_code == 200


def test_api_token_delete_enforces_ownership_and_is_not_idempotent_success(
    client: TestClient,
) -> None:
    create_admin(client)
    login(client)
    user = create_user(client)
    create_user(client, "other", password=THIRD_PASSWORD)
    login(client, "user", OTHER_PASSWORD)
    token = client.post("/api/v2/credentials/tokens", json={}).json()
    login(client, "other", THIRD_PASSWORD)

    other_delete = client.delete(f"/api/v2/credentials/tokens/{token['id']}")
    assert other_delete.status_code == 404
    assert client.get(
        "/api/v2/auth/me", headers={"Authorization": f"Bearer {token['token']}"}
    ).status_code == 200

    login(client, "user", OTHER_PASSWORD)
    assert client.delete(f"/api/v2/credentials/tokens/{token['id']}").json() == {"ok": True}
    assert client.delete(f"/api/v2/credentials/tokens/{token['id']}").status_code == 404
    assert client.get(
        "/api/v2/auth/me", headers={"Authorization": f"Bearer {token['token']}"}
    ).status_code == 401
    assert client.get("/api/v2/credentials/tokens").json()["total"] == 0
    assert user["username"] == "user"


def test_admin_and_pending_user_api_tokens_never_authenticate(client: TestClient) -> None:
    create_admin(client)
    login(client)
    admin_token = client.post("/api/v2/credentials/tokens", json={}).json()["token"]

    response = client.get(
        "/api/v2/auth/me", headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


def test_rpc_secret_lifecycle_rotation_and_user_isolation(
    client: TestClient, settings: Settings
) -> None:
    create_admin(client)
    login(client)
    create_user(client)
    create_user(client, "other", password=THIRD_PASSWORD)
    login(client, "user", OTHER_PASSWORD)

    disabled = client.get("/api/v2/credentials/rpc")
    assert disabled.json() == {"enabled": False, "secret_prefix": None, "secret": None}
    assert client.post("/api/v2/credentials/rpc/refresh").status_code == 409

    enabled = client.put("/api/v2/credentials/rpc", json={"enabled": True}).json()
    first_secret = enabled["secret"]
    assert enabled["enabled"] is True
    assert enabled["secret_prefix"] == first_secret[:12]
    assert client.put("/api/v2/credentials/rpc", json={"enabled": True}).json() == enabled
    assert client.get("/api/v2/credentials/rpc").json() == enabled

    login(client, "other", THIRD_PASSWORD)
    other_enabled = client.put("/api/v2/credentials/rpc", json={"enabled": True}).json()
    other_secret = other_enabled["secret"]
    login(client, "user", OTHER_PASSWORD)
    rotated = client.post("/api/v2/credentials/rpc/refresh").json()
    assert rotated["secret"] != first_secret
    assert client.get("/api/v2/credentials/rpc").json() == rotated
    login(client, "other", THIRD_PASSWORD)
    assert client.get("/api/v2/credentials/rpc").json()["secret"] == other_secret

    async def ciphertext() -> str:
        database = SQLiteDatabase(settings.database_path)
        async with database.connection() as connection:
            value = (
                await connection.execute(
                    text("SELECT rpc_secret_ciphertext FROM users WHERE username = 'user'")
                )
            ).scalar_one()
        await database.close()
        return value

    stored = asyncio.run(ciphertext())
    assert first_secret not in stored
    assert rotated["secret"] not in stored

    login(client, "user", OTHER_PASSWORD)
    assert client.put("/api/v2/credentials/rpc", json={"enabled": False}).json() == {
        "enabled": False,
        "secret_prefix": None,
        "secret": None,
    }
    assert client.put("/api/v2/credentials/rpc", json={"enabled": False}).status_code == 200
    assert client.get("/api/v2/credentials/rpc").json()["enabled"] is False


def test_global_credential_invalidation_preserves_password_and_sessions(
    client: TestClient,
) -> None:
    create_admin(client)
    login(client)
    create_user(client)
    login(client, "user", OTHER_PASSWORD)
    user_session = client.cookies.get(COOKIE)
    raw_token = client.post("/api/v2/credentials/tokens", json={}).json()["token"]
    client.put("/api/v2/credentials/rpc", json={"enabled": True})
    regular_forbidden = client.post(
        "/api/v2/credentials/invalidate",
        json={"confirmation": "INVALIDATE_ALL_CREDENTIALS"},
    )
    assert regular_forbidden.status_code == 403

    login(client)
    client.put("/api/v2/credentials/rpc", json={"enabled": True})
    wrong_confirmation = client.post(
        "/api/v2/credentials/invalidate", json={"confirmation": "yes"}
    )
    assert wrong_confirmation.status_code == 422
    assert wrong_confirmation.json()["error"]["code"] == "invalid_input"

    response = client.post(
        "/api/v2/credentials/invalidate",
        json={"confirmation": "INVALIDATE_ALL_CREDENTIALS"},
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True, "api_token_count": 1, "rpc_secret_count": 2}
    assert client.get("/api/v2/auth/me").status_code == 200
    assert client.get("/api/v2/credentials/rpc").json()["enabled"] is False

    client.cookies.clear()
    assert client.get(
        "/api/v2/auth/me", headers={"Authorization": f"Bearer {raw_token}"}
    ).status_code == 401
    client.cookies.set(COOKIE, user_session)
    assert client.get("/api/v2/auth/me").status_code == 200
    assert client.get("/api/v2/credentials/rpc").json()["enabled"] is False

    login(client)
    repeated = client.post(
        "/api/v2/credentials/invalidate",
        json={"confirmation": "INVALIDATE_ALL_CREDENTIALS"},
    )
    assert repeated.json()["api_token_count"] == 0
    assert repeated.json()["rpc_secret_count"] == 0
    login(client, "user", OTHER_PASSWORD)
    assert client.get("/api/v2/auth/me").json()["username"] == "user"
