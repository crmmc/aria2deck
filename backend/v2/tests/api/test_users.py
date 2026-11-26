from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient
from sqlalchemy import text

from aria2deck.infrastructure.config import Settings
from aria2deck.infrastructure.sqlite.engine import SQLiteDatabase
from tests.api.conftest import (
    OTHER_PASSWORD,
    PASSWORD,
    THIRD_PASSWORD,
    create_admin,
    create_user,
    fetch_user,
    login,
)

COOKIE = "aria2deck_v2_session"


def test_admin_lists_and_reads_users_with_real_pagination(client: TestClient) -> None:
    admin = create_admin(client)
    login(client)
    user = create_user(client)

    first_page = client.get("/api/v2/users", params={"page_size": 1})
    second_page = client.get("/api/v2/users", params={"page": 2, "page_size": 1})
    beyond_last = client.get("/api/v2/users", params={"page": 3, "page_size": 1})

    assert first_page.status_code == 200
    assert first_page.json()["total"] == 2
    assert [item["id"] for item in first_page.json()["items"]] == [admin["id"]]
    assert [item["id"] for item in second_page.json()["items"]] == [user["id"]]
    assert beyond_last.json() == {
        "items": [],
        "total": 2,
        "page": 3,
        "page_size": 1,
    }
    detail = fetch_user(client, user["id"])
    assert detail == user
    assert detail["available_bytes"] == user["quota_bytes"]
    assert "password_hash" not in str(detail)
    assert "rpc_secret" not in str(detail)
    assert client.get("/api/v2/users", params={"page_size": 101}).status_code == 422


def test_regular_user_cannot_manage_users(client: TestClient) -> None:
    admin = create_admin(client)
    login(client)
    create_user(client)
    login(client, "user", OTHER_PASSWORD)

    assert client.get("/api/v2/users").status_code == 403
    assert client.patch(f"/api/v2/users/{admin['id']}", json={}).status_code == 403
    assert client.delete(f"/api/v2/users/{admin['id']}").status_code == 403
    assert client.post(
        "/api/v2/users", json={"username": "third", "password": PASSWORD}
    ).status_code == 403


def test_admin_user_update_rotates_password_and_sessions_atomically(
    client: TestClient,
) -> None:
    create_admin(client)
    login(client)
    user = create_user(client)
    login(client, "user", OTHER_PASSWORD)
    old_user_session = client.cookies.get(COOKIE)
    login(client)

    missing_password = client.patch(
        f"/api/v2/users/{user['id']}", json={"username": "renamed"}
    )
    assert missing_password.status_code == 422

    updated = client.patch(
        f"/api/v2/users/{user['id']}",
        json={
            "username": "renamed",
            "password": THIRD_PASSWORD,
            "is_admin": True,
            "quota_bytes": 2048,
        },
    )
    assert updated.status_code == 200
    assert updated.json()["username"] == "renamed"
    assert updated.json()["is_admin"] is True
    assert updated.json()["quota_bytes"] == 2048
    assert updated.json()["is_initial_password"] is True

    client.cookies.clear()
    client.cookies.set(COOKIE, old_user_session)
    assert client.get("/api/v2/auth/me").status_code == 401
    client.cookies.clear()
    assert client.post(
        "/api/v2/auth/login", json={"username": "user", "password": OTHER_PASSWORD}
    ).status_code == 401
    login(client, "renamed", THIRD_PASSWORD)
    assert client.post(
        "/api/v2/auth/password", json={"new_password": PASSWORD}
    ).status_code == 200


def test_duplicate_rename_rolls_back_every_submitted_change(client: TestClient) -> None:
    create_admin(client)
    login(client)
    user = create_user(client)

    response = client.patch(
        f"/api/v2/users/{user['id']}",
        json={"username": "ADMIN", "password": THIRD_PASSWORD, "quota_bytes": 1},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "duplicate_resource"
    unchanged = fetch_user(client, user["id"])
    assert unchanged["username"] == "user"
    assert unchanged["quota_bytes"] == user["quota_bytes"]
    login(client, "user", OTHER_PASSWORD)


def test_user_update_enforces_quota_and_role_guards(
    client: TestClient, settings: Settings
) -> None:
    admin = create_admin(client)
    login(client)
    user = create_user(client)
    user_id = int(user["id"])

    async def set_usage() -> None:
        database = SQLiteDatabase(settings.database_path)
        async with database.connection() as connection:
            await connection.execute(
                text("UPDATE quota_accounts SET used_bytes = 50, reserved_bytes = 25")
            )
            await connection.commit()
        await database.close()

    asyncio.run(set_usage())
    assert client.patch(
        f"/api/v2/users/{user_id}", json={"quota_bytes": 74}
    ).status_code == 409
    at_limit = client.patch(f"/api/v2/users/{user_id}", json={"quota_bytes": 75})
    assert at_limit.status_code == 200
    assert at_limit.json()["available_bytes"] == 0
    empty_patch = client.patch(f"/api/v2/users/{user_id}", json={})
    assert empty_patch.status_code == 200
    assert empty_patch.json()["quota_bytes"] == 75
    assert client.patch(
        f"/api/v2/users/{admin['id']}", json={"is_admin": False}
    ).status_code == 403
    assert client.patch(f"/api/v2/users/{user_id}", json={"quota_bytes": -1}).status_code == 422


def test_user_delete_is_accepted_and_revokes_session(client: TestClient) -> None:
    admin = create_admin(client)
    login(client)
    user = create_user(client)
    login(client, "user", OTHER_PASSWORD)
    user_session = client.cookies.get(COOKIE)
    login(client)

    response = client.delete(f"/api/v2/users/{user['id']}")

    assert response.status_code == 202
    assert response.json() == {"ok": True, "state": "pending", "accepted": True}
    client.cookies.clear()
    client.cookies.set(COOKIE, user_session)
    assert client.get("/api/v2/auth/me").status_code == 401
    login(client)
    assert client.delete(f"/api/v2/users/{admin['id']}").status_code == 409
    assert client.delete("/api/v2/users/999").status_code == 404


def test_initial_password_flag_requires_a_new_password(client: TestClient) -> None:
    create_admin(client)
    login(client)
    user = create_user(client)

    response = client.patch(
        f"/api/v2/users/{user['id']}", json={"is_initial_password": True}
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_input"
    assert fetch_user(client, user["id"])["is_initial_password"] is False
