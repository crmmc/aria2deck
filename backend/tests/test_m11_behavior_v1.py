"""M11 Task 3: attach 读侧判定集成测试（时效 / gid 防串 / 终态 TTL 逐出）。

种子统一用相对时间戳构造（无需冻结时钟）：

- age 16_000ms → 超 STALE_MS(15s)，条目视为缺失但不 evict；
- age 700_000ms → 超 TERMINAL_TTL_MS(600s)，终态行读侧应逐出条目。

机制回归基线 T11（空缓存首读）来自 Task 2 的 miss 回退，green-on-arrival。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import update

from app.core.time_utils import now_ms
from app.db.engine import transaction
from app.db.schema import global_downloads
from app.modules.backend.aria2_adapter import Aria2BackendAdapter
from app.modules.task_core import observation_store
from app.repositories.task.user_tasks import list_user_tasks
from app.services import task_service
from app.services.aria2_snapshot_sanitize import sanitize_status
from app.services.rpc import Aria2RpcHandler
from app.services.task_projection import build_rest_task_response
from app.services.task_projection_rows import attach_snapshots_to_rows
from tests.fakes import make_aria2_client
from tests.helpers_v0 import (
    create_global_download_v0,
    create_user_task_v0,
    create_user_v0,
)

FRESH_MS = 1_000  # STALE_MS 窗口内
STALE_MS_SEED = 16_000  # > observation_store.STALE_MS
TERMINAL_SERVE_MS = 60_000  # > STALE_MS 且 < TERMINAL_TTL_MS：终态服务窗口内
EXPIRED_MS_SEED = 700_000  # > observation_store.TERMINAL_TTL_MS


async def _setup_task(
    username: str,
    resource_key: str,
    *,
    gid: str,
    user_status: str = "active",
    global_status: str = "active",
    total_bytes: int = 1000,
    completed_bytes: int = 400,
) -> tuple[int, int]:
    user = await create_user_v0(username=username)
    name = f"{resource_key.split(':')[-1]}.bin"
    gd = await create_global_download_v0(
        resource_key=resource_key,
        source_uri=f"http://example.com/{resource_key}.bin",
        resource_kind="http",
        status=global_status,
        aria2_gid=gid,
        display_name=name,
        total_bytes=total_bytes,
        completed_bytes=completed_bytes,
        size_known=True,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=gd["id"],
        status=user_status,
        display_name=name,
    )
    return int(user["id"]), int(gd["id"])


def _seed_entry(
    tid: int,
    *,
    gid: str,
    status: str,
    age_ms: int,
    files: list[dict[str, Any]] | None = None,
    **extra: Any,
) -> None:
    raw: dict[str, Any] = {"gid": gid, "status": status, **extra}
    if files is not None:
        raw["files"] = files
    observation_store.record_observed_detail(
        tid, sanitize_status(raw), now_ms() - age_ms
    )


def _snapshot_files(name: str, length: str = "1000") -> list[dict[str, Any]]:
    return [
        {
            "index": "1",
            "path": name,
            "length": length,
            "completedLength": length,
            "selected": "true",
            "uris": [],
        }
    ]


@pytest.fixture
def rpc_handler_factory():
    """RPC handler 工厂：读路径不得触达 aria2 client（调用即抛错）。"""
    client = make_aria2_client()
    client.tell_status = AsyncMock(side_effect=RuntimeError("aria2 must not be called"))
    client.get_files = AsyncMock(side_effect=RuntimeError("aria2 must not be called"))
    task_service.set_task_backend_override(Aria2BackendAdapter(client))
    yield lambda user_id: Aria2RpcHandler(user_id)
    task_service.set_task_backend_override(None)


# ---------------------------------------------------------------------------
# T8 终态 TTL 逐出
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t8_terminal_row_with_expired_entry_misses_and_evicts(
    temp_db: str, rpc_handler_factory
) -> None:
    user_id, tid = await _setup_task(
        "m11_t8", "http:t8", gid="gid-t8",
        user_status="completed", global_status="completed",
        total_bytes=1000, completed_bytes=1000,
    )
    _seed_entry(
        tid, gid="gid-t8", status="complete", age_ms=EXPIRED_MS_SEED,
        downloadSpeed="777",
        files=_snapshot_files("snapshot-t8.bin"),
    )

    rows = await attach_snapshots_to_rows(await list_user_tasks(user_id))
    row = rows[0]

    assert row["backend_snapshot"] is None
    assert row["backend_files"] == []
    # 条目被读侧逐出
    assert observation_store.get_observed_detail(tid) is None

    # RPC 读路径：速度 0，getFiles 回退 DB 派生，无异常
    handler = rpc_handler_factory(user_id)
    status = await handler.handle(
        "aria2.tellStatus", ["gid-t8", ["status", "downloadSpeed"]]
    )
    assert status["downloadSpeed"] == "0"
    files = await handler.handle("aria2.getFiles", ["gid-t8"])
    assert files[0]["path"] == "t8.bin"
    assert files[0]["path"] != "snapshot-t8.bin"


# ---------------------------------------------------------------------------
# T8c 终态服务窗口：终态行 + 终态条目 age 在 (STALE, TTL) 之间 → 仍命中快照
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t8c_terminal_entry_served_until_ttl(
    temp_db: str, rpc_handler_factory
) -> None:
    """PRD FR-3：终态条目按 TTL(600s) 逐出前持续服务，不受 STALE(15s) 断供窗口影响。"""
    user_id, tid = await _setup_task(
        "m11_t8c", "http:t8c", gid="gid-t8c",
        user_status="completed", global_status="completed",
        total_bytes=1000, completed_bytes=1000,
    )
    _seed_entry(
        tid, gid="gid-t8c", status="complete", age_ms=TERMINAL_SERVE_MS,
        downloadSpeed="777",
        files=_snapshot_files("snapshot-t8c.bin"),
    )

    rows = await attach_snapshots_to_rows(await list_user_tasks(user_id))
    row = rows[0]

    assert row["backend_snapshot"] is not None
    assert row["backend_snapshot"]["downloadSpeed"] == "777"
    assert row["backend_files"][0]["path"] == "snapshot-t8c.bin"
    # 条目保留，未因超 STALE 被逐出
    assert observation_store.get_observed_detail(tid) is not None

    # RPC 读路径命中快照：getFiles 用快照文件名而非 DB 派生名
    handler = rpc_handler_factory(user_id)
    files = await handler.handle("aria2.getFiles", ["gid-t8c"])
    assert files[0]["path"] == "snapshot-t8c.bin"


# ---------------------------------------------------------------------------
# T8b 断供降级：active 行 + 16s 未刷新条目 → 不冻结最后速度
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t8b_stale_entry_degrades_to_zero_speed_without_freezing(
    temp_db: str, rpc_handler_factory
) -> None:
    user_id, tid = await _setup_task(
        "m11_t8b", "http:t8b", gid="gid-t8b",
        total_bytes=1000, completed_bytes=400,
    )
    _seed_entry(
        tid, gid="gid-t8b", status="active", age_ms=STALE_MS_SEED,
        downloadSpeed="555", uploadSpeed="55",
        totalLength="1000", completedLength="400",
        files=_snapshot_files("frozen-t8b.bin"),
    )

    rows = await attach_snapshots_to_rows(await list_user_tasks(user_id))
    row = rows[0]

    assert row["backend_snapshot"] is None
    assert row["backend_files"] == []
    # stale 只视为缺失，条目保留等写侧刷新
    assert observation_store.get_observed_detail(tid) is not None

    rest = build_rest_task_response(row)
    assert rest["download_speed"] == 0
    assert rest["upload_speed"] == 0

    handler = rpc_handler_factory(user_id)
    status = await handler.handle(
        "aria2.tellStatus", ["gid-t8b", ["downloadSpeed", "uploadSpeed"]]
    )
    assert status["downloadSpeed"] == "0"
    assert status["uploadSpeed"] == "0"
    files = await handler.handle("aria2.getFiles", ["gid-t8b"])
    assert files[0]["path"] == "t8b.bin"


# ---------------------------------------------------------------------------
# T9 无终态观测逐出：DB 已 cancelled + 条目仍 active 且超 TTL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t9_cancelled_row_with_active_entry_past_ttl_evicts(
    temp_db: str,
) -> None:
    user_id, tid = await _setup_task(
        "m11_t9", "http:t9", gid="gid-t9",
        user_status="cancelled", global_status="cancelled",
        total_bytes=1000, completed_bytes=100,
    )
    _seed_entry(
        tid, gid="gid-t9", status="active", age_ms=EXPIRED_MS_SEED,
        downloadSpeed="123",
    )

    rows = await attach_snapshots_to_rows(await list_user_tasks(user_id))
    row = rows[0]

    assert row["backend_snapshot"] is None
    assert row["backend_files"] == []
    assert observation_store.get_observed_detail(tid) is None


# ---------------------------------------------------------------------------
# T10 内存有界：多个终态超时条目经 attach 后 store 条目数下降
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t10_attach_shrinks_store_for_expired_terminal_entries(
    temp_db: str,
) -> None:
    user = await create_user_v0(username="m11_t10")
    tids: list[int] = []
    for key, gid, status in (
        ("http:t10-a", "gid-t10-a", "completed"),
        ("http:t10-b", "gid-t10-b", "failed"),
        ("http:t10-c", "gid-t10-c", "cancelled"),
    ):
        gd = await create_global_download_v0(
            resource_key=key,
            source_uri=f"http://example.com/{key}.bin",
            resource_kind="http",
            status=status,
            aria2_gid=gid,
            display_name=f"{key}.bin",
            total_bytes=100,
            completed_bytes=100,
            size_known=True,
        )
        await create_user_task_v0(
            user_id=user["id"], global_download_id=gd["id"], status=status
        )
        tids.append(int(gd["id"]))
    for tid, gid in zip(tids, ("gid-t10-a", "gid-t10-b", "gid-t10-c")):
        _seed_entry(tid, gid=gid, status="active", age_ms=EXPIRED_MS_SEED)

    assert all(observation_store.get_observed_detail(t) is not None for t in tids)

    rows = await attach_snapshots_to_rows(await list_user_tasks(user["id"]))
    assert len(rows) == 3
    assert all(row["backend_snapshot"] is None for row in rows)
    assert all(observation_store.get_observed_detail(t) is None for t in tids)


# ---------------------------------------------------------------------------
# T12 tid 复用防串 / handoff 换 gid：gid 不一致的新鲜条目 miss + evict
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t12_gid_mismatch_entry_is_ignored_and_evicted(temp_db: str) -> None:
    # 场景一：tid 复用——新行 gid 与旧条目 gid 不一致（条目新鲜）
    user_id, tid = await _setup_task(
        "m11_t12", "http:t12", gid="g-new", total_bytes=500, completed_bytes=50,
    )
    _seed_entry(
        tid, gid="g-old", status="active", age_ms=FRESH_MS, downloadSpeed="999",
        files=_snapshot_files("old-task.bin", "500"),
    )

    rows = await attach_snapshots_to_rows(await list_user_tasks(user_id))
    row = rows[0]

    assert row["backend_snapshot"] is None
    assert row["backend_files"] == []
    assert observation_store.get_observed_detail(tid) is None

    # 场景二：handoff——同 tid 换 gid（BT 元数据阶段 followedBy 换新 gid）
    user2_id, tid2 = await _setup_task(
        "m11_t12b", "http:t12b", gid="g-handoff-old",
        total_bytes=800, completed_bytes=80,
    )
    _seed_entry(
        tid2, gid="g-handoff-old", status="active", age_ms=FRESH_MS,
        downloadSpeed="88",
    )
    async with transaction() as conn:
        await conn.execute(
            update(global_downloads)
            .where(global_downloads.c.id == tid2)
            .values(aria2_gid="g-handoff-new")
        )

    rows2 = await attach_snapshots_to_rows(await list_user_tasks(user2_id))
    row2 = rows2[0]

    assert row2["backend_snapshot"] is None
    assert row2["backend_files"] == []
    assert observation_store.get_observed_detail(tid2) is None


# ---------------------------------------------------------------------------
# T11 空缓存首读（回归基线，机制来自 Task 2 的 miss 回退，green-on-arrival）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t11_empty_cache_first_read_uses_db_without_error(
    temp_db: str, rpc_handler_factory
) -> None:
    user_id, tid = await _setup_task(
        "m11_t11", "http:t11", gid="gid-t11",
        total_bytes=1000, completed_bytes=400,
    )
    assert observation_store.get_observed_detail(tid) is None

    rows = await attach_snapshots_to_rows(await list_user_tasks(user_id))
    row = rows[0]

    assert row["backend_snapshot"] is None
    assert row["backend_files"] == []

    rest = build_rest_task_response(row)
    assert rest["status"] == "active"
    assert rest["total_length"] == 1000
    assert rest["completed_length"] == 400
    assert rest["download_speed"] == 0

    handler = rpc_handler_factory(user_id)
    status = await handler.handle(
        "aria2.tellStatus", ["gid-t11", ["status", "completedLength", "downloadSpeed"]]
    )
    assert status["status"] == "active"
    assert status["completedLength"] == "400"
    assert status["downloadSpeed"] == "0"
