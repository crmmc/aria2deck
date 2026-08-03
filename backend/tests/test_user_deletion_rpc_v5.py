from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update

from app.core.config import settings
from app.db.engine import transaction
from app.db.schema import global_downloads, user_tasks, users
from app.repositories import auth as auth_repo
from app.repositories import downloads as downloads_repo
from app.services import deletion_cleanup
from app.services.deletion_cleanup import DeletionCleanupManager
from tests.helpers_v0 import (
    create_global_download_v0,
    create_user_task_v0,
    create_user_v0,
)


@pytest.mark.asyncio
async def test_user_rpc_failure_retries_before_staging_and_db_cleanup(
    test_admin: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await create_user_v0(username="rpc-delete-user")
    download = await create_global_download_v0(
        resource_key="user-delete-rpc",
        resource_kind="http",
        source_uri="https://example.com/retry.bin",
        status="active",
        aria2_gid="delete-generation-gid",
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        display_name="retry.bin",
    )
    staging = Path(settings.download_dir) / "downloading" / str(download["id"])
    staging.mkdir(parents=True)
    (staging / "partial.bin").write_bytes(b"partial")
    await auth_repo.delete_user_as_admin(
        actor_id=test_admin["id"], user_id=user["id"]
    )
    assert await downloads_repo.get_user_task_by_id(
        user["id"], task["id"]
    ) is None
    assert await downloads_repo.get_user_task_by_id(
        user["id"], task["id"], include_pending_user=True
    ) is not None
    assert await downloads_repo.list_user_tasks(user["id"]) == []

    aria2 = AsyncMock()
    aria2.force_remove.side_effect = OSError("rpc unavailable")
    monkeypatch.setattr(deletion_cleanup, "get_aria2_client", lambda: aria2)
    await DeletionCleanupManager.run_once()

    pending = await auth_repo.get_user_by_id_any(user["id"])
    assert pending is not None
    assert pending["delete_attempts"] == 1
    assert pending["delete_lease_token"] is None
    assert pending["delete_error"] == "用户清理失败：RuntimeError"
    assert staging.exists()
    async with transaction() as conn:
        status = (
            await conn.execute(
                select(user_tasks.c.status).where(user_tasks.c.id == task["id"])
            )
        ).scalar_one()
    assert status == "active"

    aria2.force_remove.side_effect = None
    aria2.force_remove.return_value = "delete-generation-gid"
    aria2.remove_download_result.return_value = "OK"
    async with transaction() as conn:
        await conn.execute(
            update(users)
            .where(users.c.id == user["id"])
            .values(delete_next_retry_at_ms=0)
        )
    await DeletionCleanupManager.run_once()

    assert await auth_repo.get_user_by_id_any(user["id"]) is None
    assert not staging.exists()
    assert aria2.force_remove.await_count == 2
    async with transaction() as conn:
        assert (
            await conn.execute(
                select(user_tasks.c.id).where(user_tasks.c.id == task["id"])
            )
        ).first() is None
        global_status = (
            await conn.execute(
                select(global_downloads.c.status).where(
                    global_downloads.c.id == download["id"]
                )
            )
        ).scalar_one()
    assert global_status == "cancelled"
