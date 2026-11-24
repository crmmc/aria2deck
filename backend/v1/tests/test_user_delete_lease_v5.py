from __future__ import annotations

import pytest
from sqlalchemy import update

from app.db.engine import transaction
from app.db.schema import users
from app.repositories import auth as auth_repo
from app.services.deletion_cleanup import DeletionCleanupManager
from tests.helpers_v0 import create_user_v0, now_ms


@pytest.mark.asyncio
async def test_expired_user_delete_lease_is_reclaimed(test_admin: dict) -> None:
    user = await create_user_v0(username="lease-delete-user")
    await auth_repo.delete_user_as_admin(
        actor_id=test_admin["id"], user_id=user["id"]
    )
    timestamp = now_ms()
    first = await auth_repo.claim_due_users(
        lease_token="crashed-user-worker",
        timestamp_ms=timestamp,
        lease_expires_at_ms=timestamp + 60_000,
        limit=1,
    )
    assert [row["id"] for row in first] == [user["id"]]
    assert first[0]["delete_attempts"] == 1
    assert await auth_repo.claim_due_users(
        lease_token="early-worker",
        timestamp_ms=timestamp + 1,
        lease_expires_at_ms=timestamp + 60_001,
        limit=1,
    ) == []

    async with transaction() as conn:
        await conn.execute(
            update(users)
            .where(users.c.id == user["id"])
            .values(delete_lease_expires_at_ms=0)
        )
    reclaimed = await auth_repo.claim_due_users(
        lease_token="reclaimer",
        timestamp_ms=timestamp + 2,
        lease_expires_at_ms=timestamp + 60_002,
        limit=1,
    )
    assert reclaimed[0]["delete_attempts"] == 2
    await auth_repo.retry_claimed_user_delete(
        user_id=user["id"],
        lease_token="reclaimer",
        next_retry_at_ms=0,
        error=None,
    )
    await DeletionCleanupManager.run_once()
    assert await auth_repo.get_user_by_id_any(user["id"]) is None
