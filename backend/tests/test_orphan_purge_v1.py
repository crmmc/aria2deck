"""孤儿 aria2 下载对账：受管目录内无 live DB 归属的僵尸在启动时清除。"""

from __future__ import annotations

from typing import Any

import pytest

from app.services.repair import purge_orphan_aria2_downloads
from tests.helpers_v0 import (
    create_global_download_v0,
    create_user_task_v0,
    create_user_v0,
)

from app.services.storage import get_downloading_dir


def _root() -> str:
    return str(get_downloading_dir().resolve())


class FakeBackend:
    def __init__(self, active: list[dict], waiting: list[dict]):
        self._active = active
        self._waiting = waiting
        self.removed: list[str] = []

    async def tell_active(self) -> list[dict[str, Any]]:
        return self._active

    async def tell_waiting(self, offset: int = 0, num: int = 1000) -> list[dict]:
        return self._waiting

    async def force_remove_gid(self, gid: str) -> None:
        self.removed.append(gid)


@pytest.mark.asyncio
async def test_removes_orphan_in_managed_dir_only(temp_db: str) -> None:
    user = await create_user_v0(username="orphan-user")
    live = await create_global_download_v0(
        resource_key="rk-orphan-live",
        source_uri="http://example.com/live.bin",
        resource_kind="http",
        status="active",
        aria2_gid="gid-live",
        total_bytes=1024,
        size_known=True,
    )
    await create_user_task_v0(
        user_id=user["id"], global_download_id=int(live["id"]), status="active"
    )

    backend = FakeBackend(
        active=[
            # live 任务自己的 gid：不动
            {"gid": "gid-live", "dir": f"{_root()}/{live['id']}"},
            # 受管目录内的僵尸（DB 无 live 归属）：移除
            {"gid": "gid-zombie", "dir": f"{_root()}/192"},
            # 受管目录外的下载（用户自己的）：绝不碰
            {"gid": "gid-foreign", "dir": "/tmp/definitely-foreign"},
        ],
        waiting=[
            # paused 僵尸（metadata 残骸）：移除
            {"gid": "gid-meta-zombie", "dir": f"{_root()}/289"},
        ],
    )

    result = await purge_orphan_aria2_downloads(backend)  # type: ignore[arg-type]

    assert result == {"found": 2, "removed": 2, "failed": 0}
    assert sorted(backend.removed) == ["gid-meta-zombie", "gid-zombie"]


@pytest.mark.asyncio
async def test_terminal_db_gid_not_live_still_orphan(temp_db: str) -> None:
    """DB 终态任务的残留 gid 同样算孤儿（list_v0_tracked 只列 live）。"""
    user = await create_user_v0(username="orphan-user2")
    dead = await create_global_download_v0(
        resource_key="rk-orphan-dead",
        source_uri="http://example.com/dead.bin",
        resource_kind="http",
        status="failed",
        aria2_gid="gid-dead",
    )
    await create_user_task_v0(
        user_id=user["id"], global_download_id=int(dead["id"]), status="failed"
    )

    backend = FakeBackend(
        active=[{"gid": "gid-dead", "dir": f"{_root()}/{dead['id']}"}],
        waiting=[],
    )
    result = await purge_orphan_aria2_downloads(backend)  # type: ignore[arg-type]

    assert result["removed"] == 1
    assert backend.removed == ["gid-dead"]


@pytest.mark.asyncio
async def test_remove_failure_is_counted_not_raised(temp_db: str) -> None:
    class FailingBackend(FakeBackend):
        async def force_remove_gid(self, gid: str) -> None:
            raise RuntimeError("rpc down")

    backend = FailingBackend(
        active=[{"gid": "gid-zombie", "dir": f"{_root()}/1"}], waiting=[]
    )
    result = await purge_orphan_aria2_downloads(backend)  # type: ignore[arg-type]

    assert result == {"found": 1, "removed": 0, "failed": 1}
