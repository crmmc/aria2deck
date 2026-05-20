from __future__ import annotations

import logging
import time
from typing import Any

from app.core.state import AppState, LiveStatusCacheEntry
from app.services.task_projection import is_current


LIVE_STATUS_CACHE_TTL_SECONDS = 0.5


async def _prune_expired_live_status_cache(state: AppState, now: float) -> None:
    async with state.lock:
        expired_gids = [
            gid
            for gid, entry in state.live_status_cache.items()
            if now - entry.fetched_at > LIVE_STATUS_CACHE_TTL_SECONDS
        ]
        for gid in expired_gids:
            state.live_status_cache.pop(gid, None)


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


async def fetch_cached_live_status_for_row(
    row: dict[str, Any],
    aria2_client: Any,
    state: AppState,
    logger: logging.Logger,
    local_cache: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    now = time.monotonic()
    await _prune_expired_live_status_cache(state, now)

    if not is_current(row):
        return None
    gid = str(row.get("aria2_gid") or "")
    if not gid:
        return None

    local_status = local_cache.get(gid)
    if local_status is not None:
        return local_status

    async with state.lock:
        entry = state.live_status_cache.get(gid)
        if entry is not None:
            local_cache[gid] = entry.status
            return entry.status

    try:
        status = await aria2_client.tell_status(gid)
    except Exception as exc:
        logger.warning(
            "aria2.tellStatus failed while enriching task update gid=%s",
            gid,
            exc_info=exc,
        )
        return None

    if not isinstance(status, dict):
        return None

    fetched_at = time.monotonic()
    local_cache[gid] = status
    async with state.lock:
        state.live_status_cache[gid] = LiveStatusCacheEntry(
            status=status,
            fetched_at=fetched_at,
        )
    return status
