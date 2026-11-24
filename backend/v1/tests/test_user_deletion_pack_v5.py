from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.engine import transaction
from app.db.schema import pack_tasks
from app.repositories import auth as auth_repo
from app.services.deletion_cleanup import DeletionCleanupManager
from app.modules.pack import PackTaskManager
from app.services.storage import get_downloading_dir
from tests.helpers_v0 import create_pack_task_v0, create_user_v0


@pytest.mark.asyncio
async def test_user_delete_cancels_pack_and_removes_staging(test_admin: dict) -> None:
    user = await create_user_v0(username="pack-delete-user")
    pack = await create_pack_task_v0(
        user_id=user["id"],
        source_user_file_ids=[],
        source_size_bytes=0,
        reserved_bytes=0,
        status="packing",
        output_name="pending.zip",
    )
    staging = get_downloading_dir() / f"pack_{pack['id']}"
    staging.mkdir(parents=True)
    (staging / "partial.zip").write_bytes(b"partial")

    await auth_repo.delete_user_as_admin(
        actor_id=test_admin["id"], user_id=user["id"]
    )
    await DeletionCleanupManager.run_once()

    assert await auth_repo.get_user_by_id_any(user["id"]) is None
    assert not staging.exists()
    assert user["id"] not in PackTaskManager._blocked_user_ids
    async with transaction() as conn:
        row = (
            await conn.execute(
                select(pack_tasks.c.id).where(pack_tasks.c.id == pack["id"])
            )
        ).first()
    assert row is None
