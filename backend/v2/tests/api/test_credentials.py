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


def test_rpc_secret_lifecycle_rotation_and_user_isolation(
    client: TestClient, settings: Settings
) -> None:
    create_admin(client)
    login(client)
    create_user(client)
    create_user(client, "other", password=THIRD_PASSWORD)
    login(client, "user", OTHER_PASSWORD)

    disabled = client.get("/api/v2/credentials/rpc")
    assert disabled.json() == {"enabled": False, "secret": None}
    assert client.post("/api/v2/credentials/rpc/refresh").status_code == 409

    enabled = client.put("/api/v2/credentials/rpc", json={"enabled": True}).json()
    first_secret = enabled["secret"]
    assert enabled["enabled"] is True
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
    assert response.json() == {"ok": True, "api_token_count": 0, "rpc_secret_count": 2}
    assert client.get("/api/v2/auth/me").status_code == 200
    assert client.get("/api/v2/credentials/rpc").json()["enabled"] is False

    client.cookies.clear()
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
