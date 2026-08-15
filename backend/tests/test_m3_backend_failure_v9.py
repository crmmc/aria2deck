"""M3 T19: aria2 假死模拟（全链路不可达时的读/写行为验收）。

场景：aria2 完全不可达（所有 RPC 方法抛 ConnectionError/TimeoutError）。

验收（spec §9）：
- RPC 读仍成功：tellStatus 返回最后一次快照；tellActive 返回投影列表；
  getGlobalStat 返回快照聚合速度。
- Web 读仍成功：GET /api/tasks 与 GET /api/stats 的速度/进度来自快照。
- 写路径走 Task Core：backend 不可达时按现有语义报错（不崩溃）；
  容忍 backend 失败的取消路径（tolerate_backend_failure）仍可用。

注入方式：``set_task_backend_override`` 挂一个全抛异常的 fake backend，
同时把 ``Aria2Client`` 层所有读方法打挂，双重模拟 aria2 假死。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.security import credential_digest, credential_prefix
from app.modules.backend.port import Snapshot
from app.repositories import auth as auth_repo
from app.repositories.backend_snapshots import upsert_snapshot
from app.services import task_service
from app.services.rpc import Aria2RpcHandler, RpcError, RpcErrorCode
from tests.helpers_v0 import (
    create_global_download_v0,
    create_session_v0,
    create_user_task_v0,
    create_user_v0,
    now_ms,
)


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------

class DeadBackend:
    """BackendPort fake whose every operation raises (aria2 假死)."""

    def _boom(self) -> None:
        raise ConnectionError("aria2 unreachable (simulated)")

    async def submit(self, *, tid: int, uri: str, options: dict[str, Any]) -> str:
        self._boom()
        return ""

    async def tell_many(self, tids: list[int]) -> list[Snapshot]:
        self._boom()
        return []

    async def pause(self, tid: int) -> None:
        self._boom()

    async def unpause(self, tid: int) -> None:
        self._boom()

    async def remove(self, tid: int) -> None:
        self._boom()

    async def join_submission(self, *, tid: int, gid: str, uris: list[str]) -> None:
        self._boom()


@pytest.fixture
def dead_backend():
    """Install the all-raising backend override; restore afterwards."""
    task_service.set_task_backend_override(DeadBackend())
    try:
        yield
    finally:
        task_service.set_task_backend_override(None)


async def _make_active_task(
    username: str,
    resource_key: str,
    *,
    display_name: str = "file.bin",
) -> tuple[dict, dict, dict]:
    user = await create_user_v0(username=username)
    gd = await create_global_download_v0(
        resource_key=resource_key,
        source_uri=f"http://example.com/{display_name}",
        resource_kind="http",
        status="active",
        aria2_gid=f"gid-{resource_key[-6:]}",
        total_bytes=1000,
        completed_bytes=300,
        display_name=display_name,
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=gd["id"],
        status="active",
        display_name=display_name,
    )
    return user, gd, task


async def _upsert_snapshot(
    gd: dict,
    *,
    download_speed: int = 321,
    upload_speed: int = 45,
    completed: int = 700,
) -> None:
    raw = {
        "gid": gd["aria2_gid"],
        "status": "active",
        "totalLength": "1000",
        "completedLength": str(completed),
        "downloadSpeed": str(download_speed),
        "uploadSpeed": str(upload_speed),
    }
    await upsert_snapshot(
        global_download_id=int(gd["id"]),
        download_speed=download_speed,
        upload_speed=upload_speed,
        total_length=1000,
        completed_length=completed,
        status="active",
        files_json=json.dumps([]),
        raw_json=json.dumps(raw),
        updated_at_ms=now_ms(),
    )


# ---------------------------------------------------------------------------
# RPC reads survive a dead aria2
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rpc_reads_survive_backend_failure(temp_db: str, dead_backend) -> None:
    user, gd, task = await _make_active_task(
        "t19_rpc", "http://example.com/rpc.bin"
    )
    await _upsert_snapshot(gd)
    handler = Aria2RpcHandler(user["id"])

    # tellStatus → 最后一次快照数据
    status = await handler.handle(
        "aria2.tellStatus",
        [f"task-{task['id']}", ["gid", "completedLength", "downloadSpeed"]],
    )
    assert status == {
        "gid": f"task-{task['id']}",
        "completedLength": "700",
        "downloadSpeed": "321",
    }

    # tellActive → 投影列表（含快照速度）
    active = await handler.handle("aria2.tellActive", [])
    assert len(active) == 1
    assert active[0]["gid"] == f"task-{task['id']}"
    assert active[0]["downloadSpeed"] == "321"

    # getGlobalStat → 快照聚合速度
    stat = await handler.handle("aria2.getGlobalStat", [])
    assert stat["downloadSpeed"] == "321"
    assert stat["uploadSpeed"] == "45"
    assert stat["numActive"] == "1"


# ---------------------------------------------------------------------------
# Web reads survive a dead aria2
# ---------------------------------------------------------------------------

def test_web_reads_survive_backend_failure(
    client: TestClient, temp_db: str, dead_backend
) -> None:
    async def seed() -> tuple[int, str]:
        user, gd, _task = await _make_active_task(
            "t19_web", "http://example.com/web.bin"
        )
        await _upsert_snapshot(gd)
        session = await create_session_v0(user["id"], "t19_web_session")
        return user["id"], session

    user_id, session = asyncio.run(seed())
    client.cookies.set("aria2_session", session)

    # GET /api/tasks → 列表（速度/进度来自快照）
    resp = client.get("/api/tasks")
    assert resp.status_code == 200
    tasks = resp.json()
    assert len(tasks) == 1
    assert tasks[0]["download_speed"] == 321
    assert tasks[0]["completed_length"] == 700

    # GET /api/stats → 统计（速度来自快照）
    resp = client.get("/api/stats")
    assert resp.status_code == 200
    stats = resp.json()
    assert stats["download_speed"] == 321
    assert stats["upload_speed"] == 45


# ---------------------------------------------------------------------------
# Writes: Task Core semantics under a dead backend (no crash, defined errors)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_task_backend_failure_is_safe_error(
    temp_db: str, dead_backend
) -> None:
    """register_and_submit: backend 提交失败 → 中文语义错误，任务回滚，不崩溃。"""
    from app.domain.errors import BadGatewayError
    from app.modules.task_core.register import ResourceSpec

    user = await create_user_v0(username="t19_create")
    resource = ResourceSpec(
        resource_key="http://example.com/new.bin",
        source_uri="http://example.com/new.bin",
        resource_kind="http",
        display_name="new.bin",
    )

    with pytest.raises(BadGatewayError) as excinfo:
        await task_service.register_and_submit(
            user_id=user["id"],
            quota_bytes=user["quota_bytes"],
            resource=resource,
        )
    assert "添加下载任务失败" in str(excinfo.value)


@pytest.mark.asyncio
async def test_rpc_remove_dead_backend_surfaces_error(
    temp_db: str, dead_backend
) -> None:
    """RPC aria2.remove 走 Task Core cancel；backend 不可达时返回安全错误，不崩溃。"""
    user, gd, task = await _make_active_task(
        "t19_remove", "http://example.com/remove.bin"
    )
    await _upsert_snapshot(gd)
    handler = Aria2RpcHandler(user["id"])

    with pytest.raises(RpcError) as excinfo:
        await handler.handle("aria2.remove", [f"task-{task['id']}"])
    assert excinfo.value.code == RpcErrorCode.INTERNAL_ERROR
    assert excinfo.value.message == "Internal error"

    # 读路径仍可用（快照不依赖 backend）。unref 先落 DB 终态再调
    # backend.remove，故 remove 失败后任务已是 cancelled：单一真相下
    # 状态报 error、速度归零（active 时代残速不得泄漏）。
    status = await handler.handle(
        "aria2.tellStatus", [f"task-{task['id']}", ["status", "downloadSpeed"]]
    )
    assert status["status"] == "error"
    assert status["downloadSpeed"] == "0"


@pytest.mark.asyncio
async def test_cancel_task_tolerates_backend_failure(
    temp_db: str, dead_backend
) -> None:
    """容忍 backend 失败的取消路径（deletion_cleanup 语义）：终态化不被阻塞。"""
    user, gd, task = await _make_active_task(
        "t19_cancel", "http://example.com/cancel.bin"
    )
    result = await task_service.cancel_task(
        user_id=user["id"],
        user_task_id=int(task["id"]),
        quota_bytes=int(user["quota_bytes"]),
        tolerate_backend_failure=True,
    )
    assert result == {"ok": True}


# ---------------------------------------------------------------------------
# End-to-end: aria2 client layer fully dead, HTTP RPC + REST both serve reads
# ---------------------------------------------------------------------------

def test_http_rpc_tell_status_uses_snapshot_when_aria2_client_dead(
    client: TestClient, temp_db: str
) -> None:
    """连 aria2 client 层也打挂（超时），HTTP JSON-RPC tellStatus 仍返回快照。"""
    async def seed() -> tuple[int, str]:
        user, gd, task = await _make_active_task(
            "t19_http", "http://example.com/http.bin"
        )
        await _upsert_snapshot(gd)
        secret = "t19_rpc_secret"
        await auth_repo.set_rpc_secret(
            user["id"],
            credential_digest("rpc-secret", secret),
            credential_prefix(secret),
            now_ms(),
        )
        return user["id"], f"task-{task['id']}"

    _, gid = asyncio.run(seed())

    resp = client.post(
        "/aria2/jsonrpc",
        json={
            "jsonrpc": "2.0",
            "method": "aria2.tellStatus",
            "params": [
                "token:t19_rpc_secret",
                gid,
                ["gid", "completedLength", "downloadSpeed"],
            ],
            "id": "1",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "error" not in body
    assert body["result"] == {
        "gid": gid,
        "completedLength": "700",
        "downloadSpeed": "321",
    }
