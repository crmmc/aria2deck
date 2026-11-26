from __future__ import annotations

import asyncio

import pytest
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
    set_user_status,
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


def _submitted(username: str, password: str) -> str:
    """模拟前端：先在本地派生一次，再把 64 位摘要提交给后端。"""
    from aria2deck.infrastructure.security import CredentialManager

    return CredentialManager(pepper=None, debug=True).derive_password_hash(
        username=username, password=password
    )


def test_status_field_is_always_present_and_freezes_without_side_effects(
    client: TestClient,
) -> None:
    create_admin(client)
    login(client)
    user = create_user(client)
    assert user["status"] == "active"

    frozen = client.patch(f"/api/v2/users/{user['id']}", json={"status": "frozen"})
    assert frozen.status_code == 200
    body = frozen.json()
    assert body["status"] == "frozen"
    assert body["username"] == user["username"]
    assert body["quota_bytes"] == user["quota_bytes"]
    assert body["is_admin"] is False
    assert fetch_user(client, user["id"])["status"] == "frozen"

    unfrozen = client.patch(f"/api/v2/users/{user['id']}", json={"status": "active"})
    assert unfrozen.status_code == 200
    assert unfrozen.json()["status"] == "active"
    client.cookies.clear()
    assert client.post(
        "/api/v2/auth/login", json={"username": "user", "password": OTHER_PASSWORD}
    ).status_code == 200


@pytest.mark.parametrize(
    "payload,expected_status,expected_code",
    [
        ({"status": "locked"}, 422, "invalid_input"),
        ({"status": "unknown"}, 422, "invalid_input"),
        ({"status": "active"}, 409, "invalid_state"),
    ],
)
def test_status_patch_rejects_locked_unknown_and_no_op_values(
    client: TestClient,
    payload: dict[str, str],
    expected_status: int,
    expected_code: str,
) -> None:
    create_admin(client)
    login(client)
    user = create_user(client)

    response = client.patch(f"/api/v2/users/{user['id']}", json=payload)

    assert response.status_code == expected_status
    assert response.json()["error"]["code"] == expected_code
    assert fetch_user(client, user["id"])["status"] == "active"


def test_locked_accounts_cannot_be_edited_back_to_active(client: TestClient, settings) -> None:
    create_admin(client)
    login(client)
    user = create_user(client)
    set_user_status(settings, int(user["id"]), "locked")

    response = client.patch(f"/api/v2/users/{user['id']}", json={"status": "active"})

    assert response.status_code == 409
    assert response.json()["error"]["message"] == "锁定账号需要通过重置密码解锁"
    assert fetch_user(client, user["id"])["status"] == "locked"


def test_admin_cannot_freeze_self_or_use_status_without_permission(client: TestClient) -> None:
    admin = create_admin(client)
    login(client)

    self_freeze = client.patch(f"/api/v2/users/{admin['id']}", json={"status": "frozen"})
    assert self_freeze.status_code == 403
    assert self_freeze.json()["error"]["code"] == "permission_denied"
    assert fetch_user(client, admin["id"])["status"] == "active"

    create_user(client)
    login(client, "user", OTHER_PASSWORD)
    denied = client.patch(f"/api/v2/users/{admin['id']}", json={"status": "frozen"})
    assert denied.status_code == 403
    login(client)
    assert fetch_user(client, admin["id"])["status"] == "active"


def test_password_reset_returns_one_time_password_and_never_persists_it(
    client: TestClient, settings, caplog: pytest.LogCaptureFixture
) -> None:
    create_admin(client)
    login(client)
    user = create_user(client)
    caplog.set_level("DEBUG")

    response = client.post(f"/api/v2/users/{user['id']}/password-reset")

    assert response.status_code == 200
    payload = response.json()
    temporary = payload["temporary_password"]
    assert isinstance(temporary, str)
    assert len(temporary) == 16
    assert payload["user"]["status"] == "active"
    assert payload["user"]["is_initial_password"] is True
    assert temporary not in caplog.text
    assert temporary.encode() not in settings.database_path.read_bytes()

    listed = client.get("/api/v2/users").json()["items"]
    assert [item for item in listed if item["id"] == user["id"]][0]["status"] == "active"
    assert "temporary_password" not in str(listed)

    client.cookies.clear()
    assert client.post(
        "/api/v2/auth/login", json={"username": "user", "password": OTHER_PASSWORD}
    ).status_code == 401
    login(client, "user", _submitted("user", temporary))
    assert client.get("/api/v2/auth/me").json()["is_initial_password"] is True


def test_two_resets_produce_different_passwords_and_only_the_last_works(
    client: TestClient,
) -> None:
    create_admin(client)
    login(client)
    user = create_user(client)

    first = client.post(f"/api/v2/users/{user['id']}/password-reset").json()
    second = client.post(f"/api/v2/users/{user['id']}/password-reset").json()

    assert first["temporary_password"] != second["temporary_password"]
    client.cookies.clear()
    assert client.post(
        "/api/v2/auth/login",
        json={"username": "user", "password": _submitted("user", first["temporary_password"])},
    ).status_code == 401
    assert client.post(
        "/api/v2/auth/login",
        json={"username": "user", "password": _submitted("user", second["temporary_password"])},
    ).status_code == 200


def test_reset_applies_the_status_table(client: TestClient, settings) -> None:
    create_admin(client)
    login(client)
    user = create_user(client)

    set_user_status(settings, int(user["id"]), "locked")
    unlocked = client.post(f"/api/v2/users/{user['id']}/password-reset")
    assert unlocked.status_code == 200
    assert unlocked.json()["user"]["status"] == "active"
    login(client, "user", _submitted("user", unlocked.json()["temporary_password"]))
    assert client.get("/api/v2/auth/me").json()["status"] == "active"

    login(client)
    set_user_status(settings, int(user["id"]), "frozen")
    frozen = client.post(f"/api/v2/users/{user['id']}/password-reset")
    assert frozen.status_code == 200
    assert frozen.json()["user"]["status"] == "frozen"
    client.cookies.clear()
    blocked = client.post(
        "/api/v2/auth/login",
        json={"username": "user", "password": _submitted("user", frozen.json()["temporary_password"])},
    )
    assert blocked.status_code == 403
    assert blocked.json()["error"]["code"] == "account_frozen"


def test_manual_password_edit_matches_reset_transitions(client: TestClient, settings) -> None:
    create_admin(client)
    login(client)
    user = create_user(client)
    login(client, "user", OTHER_PASSWORD)
    old_session = client.cookies.get(COOKIE)
    login(client)

    set_user_status(settings, int(user["id"]), "locked")
    unlocked = client.patch(
        f"/api/v2/users/{user['id']}", json={"password": THIRD_PASSWORD}
    )
    assert unlocked.status_code == 200
    assert unlocked.json()["status"] == "active"
    assert unlocked.json()["is_initial_password"] is True

    client.cookies.clear()
    client.cookies.set(COOKIE, old_session)
    assert client.get("/api/v2/auth/me").status_code == 401
    client.cookies.clear()
    assert client.post(
        "/api/v2/auth/login", json={"username": "user", "password": OTHER_PASSWORD}
    ).status_code == 401
    login(client, "user", THIRD_PASSWORD)

    login(client)
    set_user_status(settings, int(user["id"]), "frozen")
    still_frozen = client.patch(
        f"/api/v2/users/{user['id']}", json={"password": OTHER_PASSWORD}
    )
    assert still_frozen.status_code == 200
    assert still_frozen.json()["status"] == "frozen"
    client.cookies.clear()
    assert client.post(
        "/api/v2/auth/login", json={"username": "user", "password": OTHER_PASSWORD}
    ).status_code == 403


def test_reset_rejects_non_admins_missing_and_pending_targets(client: TestClient) -> None:
    admin = create_admin(client)
    login(client)
    user = create_user(client)

    login(client, "user", OTHER_PASSWORD)
    assert client.post(f"/api/v2/users/{admin['id']}/password-reset").status_code == 403

    login(client)
    assert client.post("/api/v2/users/999/password-reset").status_code == 404
    assert client.delete(f"/api/v2/users/{user['id']}").status_code == 202
    pending = client.post(f"/api/v2/users/{user['id']}/password-reset")
    assert pending.status_code == 409
    assert pending.json()["error"]["code"] == "invalid_state"


def test_freeze_keeps_files_public_shares_and_running_tasks(client: TestClient, settings) -> None:
    """AC3：状态操作不删文件、不撤分享、不取消已运行任务。"""
    from tests.api.test_downloads import seed_task
    from tests.api.test_files import seed_file

    create_admin(client)
    login(client)
    user = create_user(client)
    user_id = int(user["id"])
    login(client, "user", OTHER_PASSWORD)
    _, file_id = seed_file(settings, user_id=user_id, index=1, payload=b"data")
    share = client.post(
        "/api/v2/shares", json={"user_file_id": file_id, "expires_in_seconds": 3600}
    )
    assert share.status_code == 201
    code = share.json()["code"]
    task_id, _attempt_id = seed_task(settings, user_id=user_id, name="running.bin")
    assert client.get(f"/api/s/{code}/info").status_code == 200
    assert client.get("/api/v2/tasks").json()["total"] == 1

    login(client)
    assert client.patch(
        f"/api/v2/users/{user_id}", json={"status": "frozen"}
    ).status_code == 200

    assert client.get(f"/api/s/{code}/info").status_code == 200
    rows = client.get("/api/v2/users").json()["items"]
    assert [item for item in rows if item["id"] == user_id][0]["status"] == "frozen"

    async def read_counts() -> tuple[int, int, str]:
        database = SQLiteDatabase(settings.database_path)
        try:
            async with database.connection() as connection:
                files = (
                    await connection.execute(
                        text("SELECT COUNT(*) FROM user_files WHERE user_id = :uid"),
                        {"uid": user_id},
                    )
                ).scalar_one()
                shares = (
                    await connection.execute(
                        text(
                            "SELECT COUNT(*) FROM share_links WHERE owner_id = :uid "
                            "AND revoked_at_ms IS NULL"
                        ),
                        {"uid": user_id},
                    )
                ).scalar_one()
                status = (
                    await connection.execute(
                        text("SELECT status FROM task_subscriptions WHERE id = :task_id"),
                        {"task_id": task_id},
                    )
                ).scalar_one()
        finally:
            await database.close()
        return int(files), int(shares), str(status)

    files, shares, task_status = asyncio.run(read_counts())
    assert (files, shares, task_status) == (1, 1, "active")
