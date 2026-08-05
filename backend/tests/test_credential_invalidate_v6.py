from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import settings
from app.core.security import credential_digest, credential_prefix
from app.db.engine import transaction
from app.db.schema import api_tokens
from app.repositories import auth as auth_repo
from app.services import token_service


async def test_invalidate_all_credential_digests_clears_tokens_and_rpc(
    test_user: dict,
    test_admin: dict,
) -> None:
    secret = "rpc-secret-to-invalidate"
    token = "aria2_token_to_invalidate"
    await auth_repo.set_rpc_secret(
        test_user["id"],
        credential_digest("rpc-secret", secret),
        credential_prefix(secret),
        1,
    )
    await auth_repo.create_api_token(
        test_user["id"],
        credential_digest("api-token", token),
        credential_prefix(token),
        "invalidate-me",
    )

    result = await token_service.invalidate_all_credential_digests(
        actor_id=test_admin["id"]
    )
    assert result == {
        "ok": True,
        "api_token_count": 1,
        "rpc_secret_count": 1,
    }

    assert await auth_repo.list_api_tokens(test_user["id"]) == []
    user = await auth_repo.get_user_by_id(test_user["id"])
    assert user is not None
    assert user["rpc_secret_digest"] is None
    assert user["rpc_secret_prefix"] is None
    assert user["rpc_secret_created_at_ms"] is None


def test_admin_endpoint_invalidates_all_credentials(
    client: TestClient,
    admin_session: str,
    authenticated_client: TestClient,
    test_user: dict,
) -> None:
    issued = authenticated_client.post(
        "/api/config/tokens", json={"name": "cutover"}
    )
    assert issued.status_code == 200
    token = issued.json()["token"]

    secret = "rpc-secret-endpoint"
    asyncio.run(
        auth_repo.set_rpc_secret(
            test_user["id"],
            credential_digest("rpc-secret", secret),
            credential_prefix(secret),
            1,
        )
    )

    assert (
        authenticated_client.post(
            "/api/config/credentials/invalidate",
            json={"confirm": "INVALIDATE_ALL_CREDENTIALS"},
        ).status_code
        == 403
    )

    client.cookies.set(settings.session_cookie_name, admin_session)
    bad_confirm = client.post(
        "/api/config/credentials/invalidate",
        json={"confirm": "yes"},
    )
    assert bad_confirm.status_code == 400
    assert bad_confirm.json()["detail"] == "confirm 必须为 INVALIDATE_ALL_CREDENTIALS"

    response = client.post(
        "/api/config/credentials/invalidate",
        json={"confirm": "INVALIDATE_ALL_CREDENTIALS"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["api_token_count"] >= 1
    assert body["rpc_secret_count"] >= 1

    client.cookies.clear()
    assert (
        client.get(
            "/api/tasks", headers={"Authorization": f"Bearer {token}"}
        ).status_code
        == 401
    )
    user = asyncio.run(auth_repo.get_user_by_id(test_user["id"]))
    assert user is not None
    assert user["rpc_secret_digest"] is None


async def test_api_token_last_used_writes_are_throttled(test_user: dict) -> None:
    token = "aria2_api_throttle_token"
    digest = credential_digest("api-token", token)
    await auth_repo.create_api_token(
        test_user["id"],
        digest,
        credential_prefix(token),
        "throttle",
    )

    timestamp = 1_000_000
    first = await auth_repo.use_api_token_digest(digest, timestamp_ms=timestamp)
    assert first is not None

    async def token_row() -> dict:
        async with transaction() as conn:
            row = (await conn.execute(select(api_tokens))).mappings().one()
        return dict(row)

    assert (await token_row())["last_used_at_ms"] == timestamp

    await auth_repo.use_api_token_digest(digest, timestamp_ms=timestamp + 1)
    assert (await token_row())["last_used_at_ms"] == timestamp

    await auth_repo.use_api_token_digest(
        digest,
        timestamp_ms=timestamp + auth_repo.API_TOKEN_LAST_USED_WRITE_INTERVAL_MS,
    )
    assert (await token_row())["last_used_at_ms"] == (
        timestamp + auth_repo.API_TOKEN_LAST_USED_WRITE_INTERVAL_MS
    )
