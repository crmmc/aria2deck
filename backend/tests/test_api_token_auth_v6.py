from __future__ import annotations

import asyncio

from typing import TypedDict, cast

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.engine import transaction
from app.db.schema import api_tokens
from app.repositories import auth as auth_repo


class IssuedToken(TypedDict):
    id: int
    token: str


def _issue_token(client: TestClient) -> IssuedToken:
    response = client.post("/api/config/tokens", json={"name": "automation"})
    assert response.status_code == 200
    return cast(IssuedToken, response.json())


def test_bearer_updates_last_used_without_persisting_plaintext(
    authenticated_client: TestClient, test_user: dict
) -> None:
    issued = _issue_token(authenticated_client)
    before = asyncio.run(auth_repo.list_api_tokens(test_user["id"]))
    assert before[0]["last_used_at_ms"] is None

    authenticated_client.cookies.clear()
    response = authenticated_client.get(
        "/api/tasks", headers={"Authorization": f"Bearer {issued['token']}"}
    )
    assert response.status_code == 200

    after = asyncio.run(auth_repo.list_api_tokens(test_user["id"]))
    assert after[0]["last_used_at_ms"] is not None

    async def load_token() -> dict:
        async with transaction() as conn:
            row = (await conn.execute(select(api_tokens))).mappings().one()
        return dict(row)

    stored = asyncio.run(load_token())
    assert stored["token_digest"] != issued["token"]
    assert stored["token_prefix"] == issued["token"][:16]


def test_invalid_bearer_never_falls_back_to_cookie(
    authenticated_client: TestClient,
) -> None:
    response = authenticated_client.get(
        "/api/tasks", headers={"Authorization": "Bearer invalid-token"}
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "API Token 无效"
    malformed = authenticated_client.get(
        "/api/tasks", headers={"Authorization": "Token invalid-token"}
    )
    assert malformed.status_code == 401


def test_bearer_cannot_access_session_only_endpoints(
    authenticated_client: TestClient, user_file: dict
) -> None:
    issued = _issue_token(authenticated_client)
    authenticated_client.cookies.clear()
    headers = {"Authorization": f"Bearer {issued['token']}"}

    assert authenticated_client.post("/api/auth/logout", headers=headers).status_code == 401
    assert authenticated_client.get("/api/config/tokens", headers=headers).status_code == 401
    assert authenticated_client.get("/api/users/me/rpc-access", headers=headers).status_code == 401
    assert authenticated_client.get(
        f"/api/files/{user_file['content_hash']}/download", headers=headers
    ).status_code == 401


def test_bearer_rejects_pending_and_administrator_users(
    authenticated_client: TestClient,
    client: TestClient,
    admin_session: str,
    test_admin: dict,
    test_user: dict,
) -> None:
    issued = _issue_token(authenticated_client)
    asyncio.run(auth_repo.update_user(test_user["id"], pending_delete=1))
    authenticated_client.cookies.clear()
    assert authenticated_client.get(
        "/api/tasks", headers={"Authorization": f"Bearer {issued['token']}"}
    ).status_code == 401

    client.cookies.set("aria2_session", admin_session)
    admin_issued = _issue_token(client)
    client.cookies.clear()
    headers = {"Authorization": f"Bearer {admin_issued['token']}"}
    assert client.get("/api/tasks", headers=headers).status_code == 401
    assert client.get("/api/config", headers=headers).status_code == 401
    assert test_admin["is_admin"] == 1


def test_token_list_never_repeats_issued_token(authenticated_client: TestClient) -> None:
    issued = _issue_token(authenticated_client)
    response = authenticated_client.get("/api/config/tokens")
    assert response.status_code == 200
    listed = response.json()[0]
    assert listed["id"] == issued["id"]
    assert listed["prefix"] == issued["token"][:16]
    assert "token" not in listed and "token_digest" not in listed
