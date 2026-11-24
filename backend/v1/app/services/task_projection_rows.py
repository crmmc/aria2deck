"""M3 T06: 投影行组装 —— user_tasks + global_downloads + observation_store。

为 RPC/Web 读路径提供统一的「row + snapshot」查询。

读侧判定（M11 T3 / review-fix）：非终态条目只在 STALE 窗口内附加
（断供时视为缺失但不 evict，等写侧刷新）；终态条目（观测已打终态
点或行为终态）在 TERMINAL_TTL_MS 内持续服务，超 TTL 才 miss 并逐出；
gid 不一致（tid 复用 / handoff 换 gid）一律 miss 并逐出，保证内存
有界且不串数据。

返回的每行在 ``list_user_tasks`` 原有字段基础上新增两个键：

- ``backend_snapshot``: dict 或 None
  进程内存观测仓 ``observation_store`` 中的 sanitized tellStatus dict；
  无快照条目时为 None。
- ``backend_files``: list
  sanitized 快照中的文件列表；无快照时为空 list。

快照中的标量字段（downloadSpeed 等）可直接从 ``backend_snapshot`` 读取
（例如 ``backend_snapshot.get("downloadSpeed")``），或由投影层（T07）
负责展开。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from app.core.time_utils import now_ms
from app.domain.status import TERMINAL_DOWNLOAD_STATUSES
from app.modules.task_core import observation_store
from app.repositories.task.user_tasks import list_user_tasks


def _row_is_terminal(row: dict[str, Any]) -> bool:
    return (
        str(row.get("status")) in TERMINAL_DOWNLOAD_STATUSES
        or str(row.get("global_status")) in TERMINAL_DOWNLOAD_STATUSES
    )


async def attach_snapshots_to_rows(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """为已有任务行批量附加 backend_snapshot / backend_files。

    逐行读 raw 条目自判（时效/防串策略在本层，store 只做存储 + 终态
    TTL 清扫）：

    - 条目缺失 → miss；
    - gid 与行 aria2_gid 不一致（tid 复用 / handoff 换 gid）→ miss 并 evict；
    - 终态服务分支（条目已打终态点，或行为终态）：年龄超
      TERMINAL_TTL_MS 才 miss 并 evict，否则照常附值——终态任务不再被
      sync 轮询，条目必然超 STALE，须按 TTL 服务到逐出为止（PRD FR-3；
      行终态的判定同时覆盖无终态观测的取消路径）；
    - 非终态（活跃行 + 活跃条目）：年龄超 STALE_MS → miss（不 evict，
      条目保留等写侧刷新）。

    只读约定：row["backend_snapshot"] 与 store 条目共享引用，消费方不得
    就地修改。
    """
    for row in rows:
        tid = int(row["global_download_id"])
        current_ms = now_ms()
        entry = observation_store.get_observed_detail(tid)
        if entry is not None and not observation_store.matches_row_gid(
            entry, row.get("aria2_gid")
        ):
            observation_store.evict(tid)
            entry = None
        if entry is not None:
            age_ms = current_ms - entry.updated_at_ms
            if entry.terminal_at_ms is not None or _row_is_terminal(row):
                if age_ms > observation_store.TERMINAL_TTL_MS:
                    observation_store.evict(tid)
                    entry = None
            elif age_ms > observation_store.STALE_MS:
                entry = None
        if entry is None:
            row["backend_snapshot"] = None
            row["backend_files"] = []
        else:
            row["backend_snapshot"] = entry.sanitized
            row["backend_files"] = entry.sanitized.get("files") or []
    return rows


async def list_user_task_projections(
    user_id: int,
    statuses: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """列出用户任务行，并批量附加后端观测快照。

    不改 ``list_user_tasks`` 默认行为；本函数独立组合。
    """
    rows = await list_user_tasks(user_id, statuses)
    return await attach_snapshots_to_rows(rows)
