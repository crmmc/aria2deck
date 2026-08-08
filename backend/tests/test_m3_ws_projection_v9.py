"""M3 T10: WebSocket 广播去实时 aria2。

验证 ``broadcast_task_update_to_subscribers``：
- 广播 payload 的速度/进度来自 ``task_backend_snapshots``（row 内
  ``backend_snapshot``），不调用实时 aria2 RPC
- aria2 不可用时广播不报错（不引用 aria2 client）
- WS 消息结构保持 ``{"type": "task_update", "task": payload}``
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.repositories.backend_snapshots import upsert_snapshot
from app.services.task_broadcast import (
    broadcast_task_update_to_subscribers,
    clear_connections,
    set_connections_for_user,
)
from tests.helpers_v0 import (
    create_global_download_v0,
    create_user_task_v0,
    create_user_v0,
)


class _FakeWebSocket:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def send_json(self, payload: dict) -> None:
        self.messages.append(payload)

    async def close(self, code: int = 1000) -> None:  # noqa: ARG002
        pass


async def _setup_task(
    username: str, resource_key: str, *, gid: str
) -> tuple[int, int]:
    user = await create_user_v0(username=username)
    gd = await create_global_download_v0(
        resource_key=resource_key,
        source_uri="http://example.com/file.bin",
        resource_kind="http",
        status="active",
        aria2_gid=gid,
        display_name="file.bin",
        total_bytes=1000,
        size_known=True,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=gd["id"],
        status="active",
        display_name="file.bin",
    )
    return int(user["id"]), int(gd["id"])


async def _upsert_snapshot(tid: int) -> None:
    raw = {
        "gid": "gid-ws",
        "status": "active",
        "totalLength": "1000",
        "completedLength": "400",
        "downloadSpeed": "54321",
        "uploadSpeed": "98",
    }
    await upsert_snapshot(
        global_download_id=tid,
        download_speed=54321,
        upload_speed=98,
        total_length=1000,
        completed_length=400,
        status="active",
        files_json=json.dumps(
            [{"index": "1", "path": "file.bin", "length": "1000"}]
        ),
        raw_json=json.dumps(raw),
        updated_at_ms=999,
    )


@pytest.mark.asyncio
async def test_broadcast_payload_speed_comes_from_snapshot(
    temp_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_id, tid = await _setup_task(
        "ws_snapshot", "http://example.com/ws-snapshot.bin", gid="gid-ws"
    )
    await _upsert_snapshot(tid)

    async def _boom(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("broadcast path must not call live aria2 RPC")

    monkeypatch.setattr("app.aria2.client.Aria2Client.tell_active", _boom)
    monkeypatch.setattr("app.aria2.client.Aria2Client.tell_status", _boom)

    websocket = _FakeWebSocket()
    await clear_connections()
    await set_connections_for_user(user_id, {websocket})

    await broadcast_task_update_to_subscribers(tid)

    assert len(websocket.messages) == 1
    message = websocket.messages[0]
    assert message["type"] == "task_update"
    task = message["task"]
    assert task["download_speed"] == 54321
    assert task["upload_speed"] == 98
    assert task["total_length"] == 1000
    assert task["completed_length"] == 400


@pytest.mark.asyncio
async def test_broadcast_without_aria2_does_not_fail(temp_db: str) -> None:
    user_id, tid = await _setup_task(
        "ws_noaria2", "http://example.com/ws-noaria2.bin", gid="gid-ws"
    )
    await _upsert_snapshot(tid)

    websocket = _FakeWebSocket()
    await clear_connections()
    await set_connections_for_user(user_id, {websocket})

    # 不注入 aria2 client、无快照实时回退：广播必须成功完成。
    await broadcast_task_update_to_subscribers(tid)

    assert len(websocket.messages) == 1
    assert websocket.messages[0]["task"]["download_speed"] == 54321
