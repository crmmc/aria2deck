"""M11 Task 2: 快照读写源从 task_backend_snapshots 表切换到 observation_store。

- 写路径：``record_observed_snapshot`` 之后不得再写
  ``task_backend_snapshots`` 表。
- 读路径数据源：同一调用后 ``observation_store`` 中必须存在该 tid 的
  sanitized 条目。

注意：断言 A 在 v13 删表之后结构性恒真（表不存在则必然无行），
只作过渡期回归签名；真正拦截“表被重建/回写”的守卫是
test_m11_schema_v1 的 bootstrap（fresh/upgrade 不重建表）与
contract（遗留标识符只允许出现在 migrations.py）断言。
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.db.engine import transaction
from app.modules.task_core import observation_store
from app.modules.task_core.sync import record_observed_snapshot
from tests.helpers_v0 import (
    create_global_download_v0,
    create_user_task_v0,
    create_user_v0,
)


def _raw_status() -> dict:
    return {
        "gid": "gid-switchover",
        "status": "active",
        "totalLength": "1000",
        "completedLength": "400",
        "downloadSpeed": "123",
        "uploadSpeed": "7",
        "files": [],
    }


async def _setup_active_download(username: str, resource_key: str) -> int:
    user = await create_user_v0(username=username)
    gd = await create_global_download_v0(
        resource_key=resource_key,
        source_uri="http://example.com/switch.bin",
        resource_kind="http",
        status="active",
        aria2_gid="gid-switchover",
        total_bytes=1000,
        size_known=True,
    )
    await create_user_task_v0(
        user_id=user["id"], global_download_id=gd["id"], status="active"
    )
    return int(gd["id"])


@pytest.mark.asyncio
async def test_record_observed_snapshot_writes_no_table_row(temp_db: str) -> None:
    """断言 A：观测写入后 task_backend_snapshots 表中无该 tid 行。"""
    tid = await _setup_active_download("switch1", "http:switch1")

    await record_observed_snapshot(tid=tid, observed_status=_raw_status())

    async with transaction() as conn:
        # v13 已删除该表；表不存在时断言同样成立（无行可写）。
        table_exists = (
            await conn.execute(
                text(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='task_backend_snapshots'"
                )
            )
        ).first()
        row = None
        if table_exists is not None:
            row = (
                await conn.execute(
                    text(
                        "SELECT global_download_id FROM task_backend_snapshots "
                        "WHERE global_download_id = :tid"
                    ),
                    {"tid": tid},
                )
            ).first()
    assert row is None, "record_observed_snapshot 不得再写 task_backend_snapshots 表"


@pytest.mark.asyncio
async def test_record_observed_snapshot_populates_store(temp_db: str) -> None:
    """断言 B：观测写入后 observation_store 中存在 sanitized 条目。"""
    tid = await _setup_active_download("switch2", "http:switch2")

    await record_observed_snapshot(tid=tid, observed_status=_raw_status())

    entry = observation_store.get_observed_detail(tid)
    assert entry is not None
    assert entry.sanitized["status"] == "active"
    assert entry.sanitized["downloadSpeed"] == "123"
    assert entry.updated_at_ms > 0
