from __future__ import annotations

from typing import Any

from app.domain.status import (
    ACTIVE_LIKE_DOWNLOAD_STATUSES,
    TERMINAL_DOWNLOAD_STATUSES,
)
from app.domain.task_policy import (
    effective_status,
    is_current,
)
from app.services.task_projection import build_aria2_status
from app.services.task_projection_rows import list_user_task_projections


def status_from_task(
    row: dict[str, Any], live: dict[str, Any] | None = None
) -> dict[str, Any]:
    return build_aria2_status(row, live)


async def list_active_statuses(
    user_id: int,
    live_by_gid: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    rows = await list_user_task_projections(user_id, ACTIVE_LIKE_DOWNLOAD_STATUSES)
    live_by_gid = live_by_gid or {}
    # Real-aria2 contract: tellActive lists ACTIVE downloads only —
    # queued/waiting/paused belong to tellWaiting. Also keeps
    # len(tellActive) == getGlobalStat.numActive.
    return [
        status_from_task(row, live_by_gid.get(str(row.get("aria2_gid"))))
        for row in rows
        if is_current(row) and effective_status(row) == "active"
    ]


async def list_waiting_statuses(user_id: int) -> list[dict[str, Any]]:
    rows = await list_user_task_projections(user_id, ACTIVE_LIKE_DOWNLOAD_STATUSES)
    return [
        status_from_task(row)
        for row in rows
        if is_current(row) and str(row.get("status")) in {"queued", "waiting", "paused"}
    ]


async def list_stopped_statuses(user_id: int) -> list[dict[str, Any]]:
    rows = await list_user_task_projections(user_id, None)
    return [
        status_from_task(row)
        for row in rows
        if str(row.get("status")) in TERMINAL_DOWNLOAD_STATUSES
        or str(row.get("global_status")) in TERMINAL_DOWNLOAD_STATUSES
    ]
