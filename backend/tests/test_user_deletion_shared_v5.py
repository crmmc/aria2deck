from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.db.engine import transaction
from app.db.schema import global_downloads, user_tasks
from app.repositories import auth as auth_repo
from app.services import deletion_cleanup
from app.services.deletion_cleanup import DeletionCleanupManager
from tests.helpers_v0 import (
    create_global_download_v0,
    create_user_task_v0,
    create_user_v0,
)


@pytest.mark.asyncio
async def test_deleting_shared_subscriber_keeps_writer_for_active_user(
    test_admin: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deleting = await create_user_v0(username="shared-delete")
    survivor = await create_user_v0(username="shared-survivor")
    download = await create_global_download_v0(
        resource_key="shared-user-delete",
        resource_kind="http",
        source_uri="https://example.com/shared.bin",
        status="active",
        aria2_gid="shared-writer-gid",
    )
    deleting_task = await create_user_task_v0(
        user_id=deleting["id"],
        global_download_id=download["id"],
        status="active",
    )
    survivor_task = await create_user_task_v0(
        user_id=survivor["id"],
        global_download_id=download["id"],
        status="active",
    )
    aria2 = AsyncMock()
    monkeypatch.setattr(deletion_cleanup, "get_aria2_client", lambda: aria2)

    await auth_repo.delete_user_as_admin(
        actor_id=test_admin["id"], user_id=deleting["id"]
    )
    await DeletionCleanupManager.run_once()

    assert await auth_repo.get_user_by_id_any(deleting["id"]) is None
    aria2.force_remove.assert_not_awaited()
    async with transaction() as conn:
        removed = (
            await conn.execute(
                select(user_tasks.c.id).where(user_tasks.c.id == deleting_task["id"])
            )
        ).first()
        survivor_status = (
            await conn.execute(
                select(user_tasks.c.status).where(
                    user_tasks.c.id == survivor_task["id"]
                )
            )
        ).scalar_one()
        global_status = (
            await conn.execute(
                select(global_downloads.c.status).where(
                    global_downloads.c.id == download["id"]
                )
            )
        ).scalar_one()
    assert removed is None
    assert survivor_status == "active"
    assert global_status == "active"
