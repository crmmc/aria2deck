"""M3 T06: 投影行组装 —— user_tasks + global_downloads + task_backend_snapshots。

为 RPC/Web 读路径提供统一的「row + snapshot」查询。

返回的每行在 ``list_user_tasks`` 原有字段基础上新增两个键：

- ``backend_snapshot``: dict 或 None
  ``task_backend_snapshots.raw_json`` 解析（json.loads）后的 sanitized
  tellStatus dict；无快照行时为 None。
- ``backend_files``: list
  ``task_backend_snapshots.files_json`` 解析后的 sanitized 文件列表；
  无快照时为空 list。

快照中的标量列（download_speed 等）仍保留在 ``backend_snapshot`` 之外，
调用方需要扁平字段时可直接从 ``backend_snapshot`` 读取（例如
``backend_snapshot.get("downloadSpeed")``），或由投影层（T07）负责展开。
"""

from __future__ import annotations

import json
from typing import Any, Iterable

from app.repositories.backend_snapshots import get_snapshots_for_tids
from app.repositories.task.user_tasks import list_user_tasks


def _parse_json_object(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None
    parsed = json.loads(value)
    return parsed if isinstance(parsed, dict) else None


def _parse_json_list(value: str | None) -> list[Any]:
    if not value:
        return []
    parsed = json.loads(value)
    return parsed if isinstance(parsed, list) else []


async def attach_snapshots_to_rows(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """为已有任务行批量附加 backend_snapshot / backend_files。

    快照 dict 额外带 `_snapshot_updated_at_ms` 供消费方判断新鲜度。
    """
    tids = [int(row["global_download_id"]) for row in rows]
    snapshots = await get_snapshots_for_tids(tids)
    for row in rows:
        snapshot = snapshots.get(int(row["global_download_id"]))
        parsed = _parse_json_object(snapshot.get("raw_json")) if snapshot else None
        if parsed is not None:
            parsed["_snapshot_updated_at_ms"] = int(
                snapshot.get("updated_at_ms") or 0
            )
        row["backend_snapshot"] = parsed
        row["backend_files"] = (
            _parse_json_list(snapshot.get("files_json")) if snapshot else []
        )
    return rows


async def list_user_task_projections(
    user_id: int,
    statuses: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """列出用户任务行，并批量 join 后端快照。

    不改 ``list_user_tasks`` 默认行为；本函数独立组合。
    """
    rows = await list_user_tasks(user_id, statuses)
    return await attach_snapshots_to_rows(rows)
