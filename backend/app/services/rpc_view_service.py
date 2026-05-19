from __future__ import annotations

from typing import Any

from app.repositories.downloads import list_user_tasks
from app.services.task_projection import (
    ACTIVE_LIKE_STATUSES,
    TERMINAL_STATUSES,
    build_aria2_status,
    is_current,
)


def status_from_task(
    row: dict[str, Any], live: dict[str, Any] | None = None
) -> dict[str, Any]:
    return build_aria2_status(row, live)


async def list_active_statuses(
    user_id: int,
    live_by_gid: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    rows = await list_user_tasks(user_id, ACTIVE_LIKE_STATUSES)
    live_by_gid = live_by_gid or {}
    return [
        status_from_task(row, live_by_gid.get(str(row.get("aria2_gid"))))
        for row in rows
        if is_current(row)
    ]


async def list_waiting_statuses(user_id: int) -> list[dict[str, Any]]:
    rows = await list_user_tasks(user_id, ACTIVE_LIKE_STATUSES)
    return [
        status_from_task(row)
        for row in rows
        if is_current(row) and str(row.get("status")) in {"queued", "waiting", "paused"}
    ]


async def list_stopped_statuses(user_id: int) -> list[dict[str, Any]]:
    rows = await list_user_tasks(user_id, None)
    return [
        status_from_task(row)
        for row in rows
        if str(row.get("status")) in TERMINAL_STATUSES
        or str(row.get("global_status")) in TERMINAL_STATUSES
    ]
