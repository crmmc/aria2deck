"""M3 T04: sync 写全量快照到 task_backend_snapshots。

验证 sync_once 成功拿到 Snapshot 后写入投影行（速度/进度/文件/raw），
以及 aria2 tell_many 失败或返回空时不更新旧快照。
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from app.modules.backend.port import BackendPort, Snapshot
from app.modules.task_core.sync import apply_queue_policy, sync_once
from app.repositories.backend_snapshots import get_snapshot
from tests.helpers_v0 import (
    create_global_download_v0,
    create_user_task_v0,
    create_user_v0,
)


def _raw_status(**overrides: object) -> dict:
    raw = {
        "gid": "gid-s1",
        "status": "active",
        "totalLength": "1000",
        "completedLength": "400",
        "downloadSpeed": "123",
        "uploadSpeed": "7",
        "files": [
            {
                "index": "1",
                "path": "/data/downloads/secret/movie.mkv",
                "length": "1000",
                "completedLength": "400",
                "selected": "true",
                "uris": [{"uri": "http://example.com/movie.mkv", "status": "used"}],
            }
        ],
    }
    raw.update(overrides)
    return raw


async def _setup_active_download(username: str, resource_key: str) -> int:
    user = await create_user_v0(username=username)
    gd = await create_global_download_v0(
        resource_key=resource_key,
        source_uri="http://example.com/file.bin",
        resource_kind="http",
        status="active",
        aria2_gid="gid-s1",
        total_bytes=1000,
        size_known=True,
    )
    await create_user_task_v0(
        user_id=user["id"], global_download_id=gd["id"], status="active"
    )
    return int(gd["id"])


@pytest.mark.asyncio
async def test_sync_once_writes_snapshot_row(temp_db: str) -> None:
    tid = await _setup_active_download("snap1", "http://example.com/s1.bin")
    backend = AsyncMock(spec=BackendPort)
    backend.tell_many = AsyncMock(
        return_value=[Snapshot(tid=tid, status="active", raw=_raw_status())]
    )

    await sync_once(backend)

    row = await get_snapshot(tid)
    assert row is not None
    assert row["global_download_id"] == tid
    assert row["download_speed"] == 123
    assert row["upload_speed"] == 7
    assert row["total_length"] == 1000
    assert row["completed_length"] == 400
    assert row["status"] == "active"
    assert row["updated_at_ms"] > 0

    files = json.loads(row["files_json"])
    assert len(files) == 1
    # 路径已脱敏为文件名
    assert files[0]["path"] == "movie.mkv"
    assert files[0]["length"] == "1000"
    assert files[0]["completedLength"] == "400"

    raw = json.loads(row["raw_json"])
    assert raw["gid"] == "gid-s1"
    assert raw["downloadSpeed"] == "123"
    assert raw["totalLength"] == "1000"
    assert raw["dir"] == ""


@pytest.mark.asyncio
async def test_apply_queue_policy_also_writes_snapshot(temp_db: str) -> None:
    user = await create_user_v0(username="snap2")
    gd = await create_global_download_v0(
        resource_key="http://example.com/s2.bin",
        source_uri="http://example.com/s2.bin",
        resource_kind="http",
        status="paused",
        aria2_gid="gid-s2",
        total_bytes=500,
        size_known=True,
    )
    await create_user_task_v0(
        user_id=user["id"], global_download_id=gd["id"], status="paused"
    )
    backend = AsyncMock(spec=BackendPort)
    backend.tell_many = AsyncMock(
        return_value=[
            Snapshot(
                tid=gd["id"],
                status="paused",
                raw=_raw_status(
                    gid="gid-s2",
                    status="paused",
                    totalLength="500",
                    completedLength="100",
                    downloadSpeed="0",
                ),
            )
        ]
    )

    await apply_queue_policy(backend)

    row = await get_snapshot(int(gd["id"]))
    assert row is not None
    assert row["status"] == "paused"
    assert row["total_length"] == 500
    assert row["completed_length"] == 100
    assert row["download_speed"] == 0


@pytest.mark.asyncio
async def test_tell_many_failure_keeps_old_snapshot(temp_db: str) -> None:
    tid = await _setup_active_download("snap3", "http://example.com/s3.bin")

    # 先写入一次旧快照
    backend_ok = AsyncMock(spec=BackendPort)
    backend_ok.tell_many = AsyncMock(
        return_value=[
            Snapshot(
                tid=tid,
                status="active",
                raw=_raw_status(downloadSpeed="111", completedLength="100"),
            )
        ]
    )
    await sync_once(backend_ok)
    before = await get_snapshot(tid)
    assert before is not None
    assert before["download_speed"] == 111

    # tell_many 抛异常：sync_once 失败且快照保持旧行
    backend_fail = AsyncMock(spec=BackendPort)
    backend_fail.tell_many = AsyncMock(side_effect=ConnectionError("aria2 down"))
    with pytest.raises(ConnectionError):
        await sync_once(backend_fail)
    after = await get_snapshot(tid)
    assert after is not None
    assert after["download_speed"] == 111
    assert after["completed_length"] == 100
    assert after["updated_at_ms"] == before["updated_at_ms"]


@pytest.mark.asyncio
async def test_tell_many_empty_result_no_snapshot(temp_db: str) -> None:
    tid = await _setup_active_download("snap4", "http://example.com/s4.bin")
    backend = AsyncMock(spec=BackendPort)
    backend.tell_many = AsyncMock(return_value=[])

    report = await sync_once(backend)

    assert report.fetched == 0
    assert report.updated == 0
    assert await get_snapshot(tid) is None


@pytest.mark.asyncio
async def test_tid_missing_from_tell_many_keeps_old_snapshot(temp_db: str) -> None:
    """aria2 未返回该 tid 的快照时，旧行保留不被覆盖。"""
    tid = await _setup_active_download("snap5", "http://example.com/s5.bin")

    backend_ok = AsyncMock(spec=BackendPort)
    backend_ok.tell_many = AsyncMock(
        return_value=[
            Snapshot(
                tid=tid,
                status="active",
                raw=_raw_status(downloadSpeed="222", completedLength="200"),
            )
        ]
    )
    await sync_once(backend_ok)
    before = await get_snapshot(tid)
    assert before is not None

    # 下一轮 tell_many 不包含该 tid
    backend_partial = AsyncMock(spec=BackendPort)
    backend_partial.tell_many = AsyncMock(return_value=[])
    await sync_once(backend_partial)

    after = await get_snapshot(tid)
    assert after is not None
    assert after["download_speed"] == 222
    assert after["completed_length"] == 200
    assert after["updated_at_ms"] == before["updated_at_ms"]
