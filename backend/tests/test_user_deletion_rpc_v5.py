from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update

from app.core.config import settings
from app.db.engine import transaction
from app.db.schema import global_downloads, user_tasks, users
from app.repositories import auth as auth_repo
from app.repositories.task import user_tasks as downloads_repo
from app.services import task_service
from app.services.deletion_cleanup import DeletionCleanupManager
from tests.helpers_v0 import (
    create_global_download_v0,
    create_user_task_v0,
    create_user_v0,
)


@pytest.mark.asyncio
async def test_user_rpc_failure_does_not_block_terminal_user_cleanup(
    test_admin: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M3: force_remove failure no longer blocks user deletion cleanup.

    Cancel terminalizes the attempt first; physical Aria2 cleanup may fail
    without leaving the user in a permanent pending-delete retry loop.
    """
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
    monkeypatch.setattr(
        task_service, "_get_backend", lambda: task_service.Aria2BackendAdapter(aria2)
    )
    await DeletionCleanupManager.run_once()

    assert await auth_repo.get_user_by_id_any(user["id"]) is None
    # backend remove 失败保留下载目录（writer 未确认停止），但用户行与
    # user_task 已完成终态清理。
    assert staging.exists()
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
    aria2.remove.assert_awaited()
