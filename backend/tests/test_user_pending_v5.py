from __future__ import annotations

import pytest
from sqlalchemy import insert, select

from app.core.security import credential_digest, credential_prefix
from app.db.engine import transaction
from app.db.schema import share_links
from app.repositories import auth as auth_repo
from app.repositories import files as files_repo
from tests.helpers_v0 import now_ms


@pytest.mark.asyncio
async def test_user_enqueue_immediately_invalidates_all_access(
    test_admin: dict,
    test_user: dict,
    user_session: str,
    user_file: dict,
) -> None:
    pending_token = "pending-token"
    token = await auth_repo.create_api_token(
        test_user["id"],
        credential_digest("api-token", pending_token),
        credential_prefix(pending_token),
        None,
    )
    pending_secret = "pending-rpc-secret"
    await auth_repo.set_rpc_secret(
        test_user["id"],
        credential_digest("rpc-secret", pending_secret),
        credential_prefix(pending_secret),
        now_ms(),
    )
    async with transaction() as conn:
        share_id = (
            await conn.execute(
                insert(share_links)
                .values(
                    share_code="pending-user-share",
                    owner_id=test_user["id"],
                    user_file_id=user_file["id"],
                    status="active",
                    download_count=0,
                    created_at_ms=now_ms(),
                )
                .returning(share_links.c.id)
            )
        ).scalar_one()

    queued = await auth_repo.delete_user_as_admin(
        actor_id=test_admin["id"], user_id=test_user["id"]
    )
    assert queued is not None
    assert queued["pending_delete"] == 1
    assert queued["rpc_secret_digest"] is None
    assert queued["rpc_secret_prefix"] is None
    assert await auth_repo.get_user_by_id(test_user["id"]) is None
    assert await auth_repo.get_session_user(user_session) is None
    assert await auth_repo.list_api_tokens(test_user["id"]) == []
    assert await files_repo.get_user_file_by_hash(
        test_user["id"], user_file["content_hash"]
    ) is None
    assert all(
        row["id"] != test_user["id"] for row in await auth_repo.list_users()
    )

    async with transaction() as conn:
        status = (
            await conn.execute(
                select(share_links.c.status).where(share_links.c.id == share_id)
            )
        ).scalar_one()
    assert status == "revoked"
    assert token["token_digest"] != "pending-token"

    repeated = await auth_repo.delete_user_as_admin(
        actor_id=test_admin["id"], user_id=test_user["id"]
    )
    assert repeated is not None
    assert repeated["pending_delete"] == 1
    with pytest.raises(ValueError, match="用户不可用"):
        await auth_repo.create_session("resurrected", test_user["id"], now_ms() + 1000)
    with pytest.raises(ValueError, match="用户不可用"):
        await auth_repo.create_api_token(
            test_user["id"],
            credential_digest("api-token", "resurrected"),
            credential_prefix("resurrected"),
            None,
        )
