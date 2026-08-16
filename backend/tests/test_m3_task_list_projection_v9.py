"""M3 T08: HTTP 任务列表去实时 aria2。

验证 ``list_tasks`` / ``list_tasks_page``：
- 数据源为 ``list_user_task_projections``（row 内 snapshot），不调用
  ``fetch_active_live_statuses_by_gid``（即使 aria2 client 不可用也返回）
- 速度/进度字段来自 ``backend_snapshot``
- 分页路径同样不触碰实时 RPC
"""

from __future__ import annotations

from typing import Any

import pytest

import app.services.task_service as task_service
from app.modules.task_core.sync import record_observed_snapshot
from tests.helpers_v0 import (
    create_global_download_v0,
    create_user_task_v0,
    create_user_v0,
)


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
        total_bytes=1000,
        size_known=True,
    )
    await create_user_task_v0(
        user_id=user["id"], global_download_id=gd["id"], status="active"
    )
    return int(user["id"]), int(gd["id"])


async def _upsert_snapshot(tid: int) -> dict[str, Any]:
    raw = {
        "gid": "gid-list",
        "status": "active",
        "totalLength": "1000",
        "completedLength": "400",
        "downloadSpeed": "12345",
        "uploadSpeed": "67",
        "files": [{"index": "1", "path": "file.bin", "length": "1000"}],
    }
    await record_observed_snapshot(tid=tid, observed_status=raw)
    return raw


@pytest.mark.asyncio
async def test_list_tasks_never_calls_live_fetch(
    temp_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_id, tid = await _setup_task(
        "list_nofetch", "http://example.com/nofetch.bin", gid="gid-list"
    )
    await _upsert_snapshot(tid)

    async def _boom(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("live aria2 RPC must not be called in list path")

    monkeypatch.setattr("app.aria2.client.Aria2Client.tell_active", _boom)
    monkeypatch.setattr("app.aria2.client.Aria2Client.tell_status", _boom)
    monkeypatch.setattr(
        "app.services.task_service._get_client",
        lambda: (_ for _ in ()).throw(AssertionError("aria2 client must not be used")),
    )

    items = await task_service.list_tasks(user_id=user_id, status_filter=None)

    assert len(items) == 1
    assert items[0]["download_speed"] == 12345
    assert items[0]["upload_speed"] == 67
    assert items[0]["completed_length"] == 400


@pytest.mark.asyncio
async def test_list_tasks_uses_projection_source(
    temp_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_id, tid = await _setup_task(
        "list_proj", "http://example.com/proj.bin", gid="gid-list"
    )
    raw = await _upsert_snapshot(tid)

    captured: dict[str, Any] = {}
    original = task_service.list_user_task_projections

    async def _spy(uid: int, statuses: Any = None) -> list[dict[str, Any]]:
        captured["user_id"] = uid
        captured["statuses"] = statuses
        return await original(uid, statuses)

    monkeypatch.setattr(
        "app.services.task_service.list_user_task_projections", _spy
    )

    items = await task_service.list_tasks(user_id=user_id, status_filter="active")

    assert captured["user_id"] == user_id
    assert len(items) == 1
    assert items[0]["total_length"] == int(raw["totalLength"])
    assert items[0]["download_speed"] == int(raw["downloadSpeed"])


@pytest.mark.asyncio
async def test_list_tasks_page_no_live_fetch_and_snapshot_speeds(
    temp_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_id, tid = await _setup_task(
        "list_page", "http://example.com/page.bin", gid="gid-list"
    )
    await _upsert_snapshot(tid)

    async def _boom(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("live aria2 RPC must not be called in page path")

    monkeypatch.setattr("app.aria2.client.Aria2Client.tell_active", _boom)
    monkeypatch.setattr("app.aria2.client.Aria2Client.tell_status", _boom)

    result = await task_service.list_tasks_page(
        user_id=user_id, status_filter=None, page=1, page_size=10
    )

    assert result["total"] == 1
    assert len(result["items"]) == 1
    item = result["items"][0]
    assert item["download_speed"] == 12345
    assert item["completed_length"] == 400


@pytest.mark.asyncio
async def test_list_tasks_without_snapshot_renders_from_db(
    temp_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_id, _ = await _setup_task(
        "list_nosnap", "http://example.com/nosnap.bin", gid="gid-list"
    )

    async def _boom(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("live aria2 RPC must not be called")

    monkeypatch.setattr("app.aria2.client.Aria2Client.tell_active", _boom)
    monkeypatch.setattr("app.aria2.client.Aria2Client.tell_status", _boom)

    items = await task_service.list_tasks(user_id=user_id, status_filter=None)

    assert len(items) == 1
    assert items[0]["download_speed"] == 0
    assert items[0]["total_length"] == 1000
