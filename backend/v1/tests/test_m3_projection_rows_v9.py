"""M3 T06: 投影 join / 行组装测试。

验证 list_user_task_projections：
- 有快照时 backend_snapshot / backend_files 字段正确（sanitized）
- 无快照时 backend_snapshot 为 None、backend_files 为 []，不报错
- 保留 list_user_tasks 原有 row 字段
"""

from __future__ import annotations

import pytest

from app.modules.task_core.sync import record_observed_snapshot
from app.services.aria2_snapshot_sanitize import sanitize_status
from app.services.task_projection_rows import list_user_task_projections
from tests.helpers_v0 import (
    create_global_download_v0,
    create_user_task_v0,
    create_user_v0,
)


async def _setup_task(username: str, resource_key: str) -> tuple[int, int]:
    user = await create_user_v0(username=username)
    gd = await create_global_download_v0(
        resource_key=resource_key,
        source_uri="http://example.com/file.bin",
        resource_kind="http",
        status="active",
        aria2_gid="gid-proj",
        total_bytes=1000,
        size_known=True,
    )
    await create_user_task_v0(
        user_id=user["id"], global_download_id=gd["id"], status="active"
    )
    return int(user["id"]), int(gd["id"])


@pytest.mark.asyncio
async def test_projection_row_with_snapshot(temp_db: str) -> None:
    user_id, tid = await _setup_task("proj_with", "http://example.com/with.bin")
    raw = {
        "gid": "gid-proj",
        "status": "active",
        "totalLength": "1000",
        "completedLength": "400",
        "downloadSpeed": "123",
        "uploadSpeed": "7",
        "files": [{"index": "1", "path": "movie.mkv", "length": "1000"}],
    }
    await record_observed_snapshot(tid=tid, observed_status=raw)

    rows = await list_user_task_projections(user_id)
    assert len(rows) == 1
    row = rows[0]

    # 原有 row 字段保留
    assert row["global_download_id"] == tid
    assert row["user_id"] == user_id
    assert row["aria2_gid"] == "gid-proj"
    assert row["global_status"] == "active"

    # 快照字段为 sanitized 观测条目
    expected = sanitize_status(raw)
    assert row["backend_snapshot"] == expected
    assert row["backend_snapshot"]["downloadSpeed"] == "123"
    assert row["backend_files"] == expected["files"]
    assert row["backend_files"][0]["path"] == "movie.mkv"


@pytest.mark.asyncio
async def test_projection_row_without_snapshot(temp_db: str) -> None:
    user_id, tid = await _setup_task("proj_without", "http://example.com/without.bin")

    rows = await list_user_task_projections(user_id)
    assert len(rows) == 1
    row = rows[0]
    assert row["global_download_id"] == tid
    assert row["backend_snapshot"] is None
    assert row["backend_files"] == []


@pytest.mark.asyncio
async def test_projection_rows_status_filter(temp_db: str) -> None:
    user_id, tid = await _setup_task("proj_filter", "http://example.com/filter.bin")
    await record_observed_snapshot(
        tid=tid,
        observed_status={"gid": "gid-proj", "status": "active", "downloadSpeed": "1"},
    )

    rows = await list_user_task_projections(user_id, statuses=["active"])
    assert len(rows) == 1
    assert rows[0]["backend_snapshot"]["status"] == "active"

    rows = await list_user_task_projections(user_id, statuses=["completed"])
    assert rows == []
