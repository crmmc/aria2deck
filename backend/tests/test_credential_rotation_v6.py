from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from app.auth import get_user_by_rpc_secret
from app.core.config import settings
from app.core.security import (
    credential_digest,
    credential_digest_candidates,
    credential_prefix,
)
from app.db.schema import api_tokens
from app.repositories import auth as auth_repo


@pytest.mark.asyncio
async def test_previous_pepper_rpc_auth_atomically_promotes_digest(
    monkeypatch: pytest.MonkeyPatch, test_user: dict
) -> None:
    previous_pepper = "p" * 32
    current_pepper = "c" * 32
    secret = "aria2_rpc_rotation_secret"
    monkeypatch.setattr(settings, "credential_pepper", previous_pepper)
    monkeypatch.setattr(settings, "previous_credential_pepper", "")
    await auth_repo.set_rpc_secret(
        test_user["id"],
        credential_digest("rpc-secret", secret),
        credential_prefix(secret),
        1,
    )

    monkeypatch.setattr(settings, "credential_pepper", current_pepper)
    monkeypatch.setattr(settings, "previous_credential_pepper", previous_pepper)
    results = await asyncio.gather(*(get_user_by_rpc_secret(secret) for _ in range(2)))
    assert [result["id"] for result in results if result is not None] == [test_user["id"]] * 2

    user = await auth_repo.get_user_by_id(test_user["id"])
    assert user is not None
    assert user["rpc_secret_digest"] == credential_digest("rpc-secret", secret)


@pytest.mark.asyncio
async def test_api_token_rotation_and_last_used_writes_are_throttled(
    monkeypatch: pytest.MonkeyPatch, test_user: dict
) -> None:
    previous_pepper = "p" * 32
    current_pepper = "c" * 32
    token = "aria2_api_rotation_token"
    monkeypatch.setattr(settings, "credential_pepper", previous_pepper)
    monkeypatch.setattr(settings, "previous_credential_pepper", "")
    await auth_repo.create_api_token(
        test_user["id"],
        credential_digest("api-token", token),
        credential_prefix(token),
        "rotation",
    )

    monkeypatch.setattr(settings, "credential_pepper", current_pepper)
    monkeypatch.setattr(settings, "previous_credential_pepper", previous_pepper)
    current, previous = credential_digest_candidates("api-token", token)
    timestamp = 1_000_000
    results = await asyncio.gather(
        *(auth_repo.use_api_token_digests(current, previous, timestamp_ms=timestamp) for _ in range(2))
    )
    assert [result["id"] for result in results if result is not None] == [test_user["id"]] * 2

    async def token_row() -> dict:
        from app.db.engine import transaction

        async with transaction() as conn:
            row = (await conn.execute(select(api_tokens))).mappings().one()
        return dict(row)

    rotated = await token_row()
    assert rotated["token_digest"] == current
    assert rotated["last_used_at_ms"] == timestamp

    await auth_repo.use_api_token_digests(current, previous, timestamp_ms=timestamp + 1)
    assert (await token_row())["last_used_at_ms"] == timestamp

    await auth_repo.use_api_token_digests(
        current,
        previous,
        timestamp_ms=timestamp + auth_repo.API_TOKEN_LAST_USED_WRITE_INTERVAL_MS,
    )
    assert (await token_row())["last_used_at_ms"] == (
        timestamp + auth_repo.API_TOKEN_LAST_USED_WRITE_INTERVAL_MS
    )
