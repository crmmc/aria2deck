"""T15: deletion_cleanup 用户任务取消切换到 unref（M2 kill-list P0）。

验证目标：
1. Spy：用户持久清理取消活跃任务时走 ``task_service.cancel_task``（内部
   经由 ``task_core.unref`` + BackendPort），不再经过
   ``download_service.cancel_user_task``。
2. AST：``app/services/deletion_cleanup.py`` 不 import
   ``cancel_user_task`` / ``get_aria2_client``。
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from app.db.engine import transaction
from app.db.schema import global_downloads, user_tasks
from app.repositories import auth as auth_repo
from app.services import deletion_cleanup, task_service
from app.services.deletion_cleanup import DeletionCleanupManager
from tests.helpers_v0 import (
    create_global_download_v0,
    create_user_task_v0,
    create_user_v0,
)

_SRC = (
    Path(__file__).resolve().parent.parent
    / "app"
    / "services"
    / "deletion_cleanup.py"
)


# ---------------------------------------------------------------------------
# 1. AST: 不得 import cancel_user_task / get_aria2_client
# ---------------------------------------------------------------------------


def test_deletion_cleanup_does_not_import_legacy_symbols() -> None:
    tree = ast.parse(_SRC.read_text(encoding="utf-8"), filename=str(_SRC))

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                imported.add(alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name)

    assert "cancel_user_task" not in imported
    assert "get_aria2_client" not in imported


# ---------------------------------------------------------------------------
# 2. Spy: 用户清理走 task_service.cancel_task（unref 路径）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_cleanup_cancels_via_task_service_cancel_task(
    test_admin: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await create_user_v0(username="m3-dc-cancel")
    download = await create_global_download_v0(
        resource_key="http:m3-dc-cancel",
        resource_kind="http",
        source_uri="https://example.com/m3-dc.bin",
        status="active",
        aria2_gid="m3-dc-gid",
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        display_name="m3-dc.bin",
    )

    cancel_calls: list[dict[str, Any]] = []
    original = task_service.cancel_task

    async def spy(**kwargs: Any) -> dict:
        cancel_calls.append(kwargs)
        return await original(**kwargs)

    monkeypatch.setattr(deletion_cleanup, "cancel_task", spy)

    backend = _RecordingBackend()
    monkeypatch.setattr(task_service, "_get_backend", lambda: backend)

    await auth_repo.delete_user_as_admin(
        actor_id=test_admin["id"], user_id=user["id"]
    )
    await DeletionCleanupManager.run_once()

    assert await auth_repo.get_user_by_id_any(user["id"]) is None
    assert len(cancel_calls) == 1
    assert cancel_calls[0]["user_id"] == user["id"]
    assert cancel_calls[0]["user_task_id"] == task["id"]

    # 最后一个订阅者 unref 后，tid 终态化且 backend.remove 被调用。
    async with transaction() as conn:
        assert (
            await conn.execute(
                select(user_tasks.c.id).where(user_tasks.c.id == task["id"])
            )
        ).first() is None
        global_row = (
            await conn.execute(
                select(
                    global_downloads.c.status,
                    global_downloads.c.aria2_gid,
                ).where(global_downloads.c.id == download["id"])
            )
        ).one()
    assert global_row.status == "cancelled", (
        f"global status={global_row.status} gid={global_row.aria2_gid} "
        f"removed={backend.removed}"
    )
    assert backend.removed == [download["id"]]


# ---------------------------------------------------------------------------
# 3. Spy: cancel_task 抛错时不阻塞用户持久清理（任务已终态化）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_cleanup_survives_backend_failure(
    test_admin: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await create_user_v0(username="m3-dc-rpcfail")
    download = await create_global_download_v0(
        resource_key="http:m3-dc-rpcfail",
        resource_kind="http",
        source_uri="https://example.com/m3-dc-fail.bin",
        status="active",
        aria2_gid="m3-dc-fail-gid",
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
    )

    class _FailingBackend:
        async def remove(self, tid: int) -> None:
            raise OSError("rpc unavailable")

    monkeypatch.setattr(task_service, "_get_backend", lambda: _FailingBackend())

    await auth_repo.delete_user_as_admin(
        actor_id=test_admin["id"], user_id=user["id"]
    )
    await DeletionCleanupManager.run_once()

    # backend.remove 失败不得把用户卡在 pending-delete 重试循环里。
    assert await auth_repo.get_user_by_id_any(user["id"]) is None


class _RecordingBackend:
    def __init__(self) -> None:
        self.removed: list[int] = []

    async def remove(self, tid: int) -> None:
        self.removed.append(tid)
