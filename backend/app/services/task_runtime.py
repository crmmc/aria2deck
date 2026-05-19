from __future__ import annotations

import logging
from typing import Any

from app.services.task_projection import is_current


async def fetch_active_live_statuses_by_gid(
    rows: list[dict[str, Any]],
    aria2_client: Any,
    logger: logging.Logger,
) -> dict[str, dict[str, Any]]:
    owned_gids = {
        str(row.get("aria2_gid") or "")
        for row in rows
        if is_current(row) and row.get("aria2_gid")
    }
    owned_gids.discard("")
    if not owned_gids:
        return {}

    try:
        statuses = await aria2_client.tell_active()
    except Exception as exc:
        logger.warning("aria2.tellActive failed while enriching user speeds", exc_info=exc)
        return {}

    result: dict[str, dict[str, Any]] = {}
    if not isinstance(statuses, list):
        return result
    for status in statuses:
        if not isinstance(status, dict):
            continue
        gid = str(status.get("gid") or "")
        if gid in owned_gids:
            result[gid] = status
    return result


async def fetch_live_status_for_row(
    row: dict[str, Any],
    aria2_client: Any,
    logger: logging.Logger,
) -> dict[str, Any] | None:
    if not is_current(row):
        return None
    gid = str(row.get("aria2_gid") or "")
    if not gid:
        return None
    try:
        status = await aria2_client.tell_status(gid)
    except Exception as exc:
        logger.warning(
            "aria2.tellStatus failed while enriching task update gid=%s",
            gid,
            exc_info=exc,
        )
        return None
    return status if isinstance(status, dict) else None
