"""M3 T09: Web 用户统计切投影。

验证 ``stats_service.get_user_stats``：
- 数据速度来自 ``task_backend_snapshots``（经 ``list_user_task_projections``），
  不调用 aria2 实时 RPC；aria2 不可用时统计仍返回
- 速度只对 current/active 任务聚合，终态任务快照速度不计入
- 磁盘/配额字段（used/frozen/total/limited）保持现状
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.repositories.backend_snapshots import upsert_snapshot
from app.services import stats_service
from tests.helpers_v0 import (
    create_global_download_v0,
    create_user_task_v0,
    create_user_v0,
)


async def _setup_task(
    username: str,
    resource_key: str,
    *,
    gid: str,
    user_status: str = "active",
    global_status: str = "active",
) -> tuple[int, int]:
    user = await create_user_v0(username=username)
    gd = await create_global_download_v0(
        resource_key=resource_key,
        source_uri=f"http://example.com/{resource_key}.bin",
        resource_kind="http",
        status=global_status,
        aria2_gid=gid,
        total_bytes=1000,
        size_known=True,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=gd["id"],
        status=user_status,
    )
    return int(user["id"]), int(gd["id"])


async def _upsert_speed_snapshot(
    tid: int, *, download_speed: int, upload_speed: int
) -> None:
    raw = {
        "gid": "gid-stats",
        "status": "active",
        "totalLength": "1000",
        "completedLength": "500",
        "downloadSpeed": str(download_speed),
        "uploadSpeed": str(upload_speed),
    }
    await upsert_snapshot(
        global_download_id=tid,
        download_speed=download_speed,
        upload_speed=upload_speed,
        total_length=1000,
        completed_length=500,
        status="active",
        files_json=json.dumps([]),
        raw_json=json.dumps(raw),
        updated_at_ms=1,
    )


@pytest.mark.asyncio
async def test_get_user_stats_never_calls_live_fetch(
    temp_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_id, tid = await _setup_task(
        "stats_nofetch", "nofetch", gid="gid-stats"
    )
    await _upsert_speed_snapshot(tid, download_speed=12345, upload_speed=67)

    async def _boom(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("live aria2 RPC must not be called in stats path")

    monkeypatch.setattr("app.aria2.client.Aria2Client.tell_active", _boom)
    monkeypatch.setattr("app.aria2.client.Aria2Client.tell_status", _boom)
    monkeypatch.setattr(
        "app.aria2.gateway.get_aria2_client",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("aria2 client must not be used")
        ),
    )

    result = await stats_service.get_user_stats(user_id=user_id, quota_bytes=0)

    assert result["download_speed"] == 12345
    assert result["upload_speed"] == 67
    assert result["active_task_count"] == 1


@pytest.mark.asyncio
async def test_get_user_stats_works_without_aria2(temp_db: str) -> None:
    user_id, _ = await _setup_task("stats_noaria2", "noaria2", gid="gid-stats")

    result = await stats_service.get_user_stats(user_id=user_id, quota_bytes=0)

    assert result["download_speed"] == 0
    assert result["upload_speed"] == 0
    assert result["active_task_count"] == 1


@pytest.mark.asyncio
async def test_get_user_stats_speeds_come_from_snapshot(temp_db: str) -> None:
    user_id, active_tid = await _setup_task(
        "stats_snap", "snap-active", gid="gid-stats-a"
    )
    complete_gd = await create_global_download_v0(
        resource_key="snap-complete",
        source_uri="http://example.com/snap-complete.bin",
        resource_kind="http",
        status="completed",
        aria2_gid="gid-stats-c",
        total_bytes=1000,
        size_known=True,
    )
    await create_user_task_v0(
        user_id=user_id,
        global_download_id=complete_gd["id"],
        status="completed",
    )
    complete_tid = int(complete_gd["id"])
    await _upsert_speed_snapshot(active_tid, download_speed=4096, upload_speed=64)
    await _upsert_speed_snapshot(complete_tid, download_speed=9999, upload_speed=9999)

    result = await stats_service.get_user_stats(user_id=user_id, quota_bytes=0)

    assert result["active_task_count"] == 1
    assert result["download_speed"] == 4096
    assert result["upload_speed"] == 64


@pytest.mark.asyncio
async def test_get_user_stats_disk_fields_unchanged(temp_db: str) -> None:
    user_id, _ = await _setup_task("stats_disk", "disk", gid="gid-stats")

    result = await stats_service.get_user_stats(user_id=user_id, quota_bytes=2048)

    assert result["disk_total_space"] == 2048
    assert result["disk_used_space"] == 0
    assert result["disk_frozen_space"] == 0
    assert result["disk_space_limited"] is False
