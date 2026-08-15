"""History retention: soft-expire terminal pids + GC orphaned download_sources.

Spec: M8 §3.6.4 / §3.7 — soft expire never DELETEs tid (G9).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from app.repositories.settings import get_settings_row
from app.repositories.task import retention as retention_repo

logger = logging.getLogger(__name__)

DEFAULT_HISTORY_RETENTION_DAYS = 30
HISTORY_RETENTION_INTERVAL_SECONDS = 3600
DAY_MS = 24 * 60 * 60 * 1000


def now_ms() -> int:
    return int(time.time() * 1000)


async def gc_source_if_orphaned(source_id: int | None) -> bool:
    """Strip S payload when no tid references source_id. Returns True if purged."""
    return await retention_repo.gc_source_if_orphaned(source_id)


async def reclaim_zero_pid_tid(tid: int) -> dict[str, Any]:
    """Reclaim terminal tid after hard-delete left zero pid rows (§3.6.3)."""
    return await retention_repo.reclaim_zero_pid_tid(tid)


async def soft_expire_due_history(
    *,
    now: int | None = None,
    retention_days: int | None = None,
    cutoff_ms: int | None = None,
) -> dict[str, int]:
    """Soft-expire due terminal pids per §3.6.4. Never DELETE tid/pid."""
    ts = int(now if now is not None else now_ms())
    if cutoff_ms is None:
        if retention_days is None:
            settings = await get_settings_row()
            retention_days = int(
                (settings or {}).get("history_retention_days")
                or DEFAULT_HISTORY_RETENTION_DAYS
            )
        retention_days = max(1, int(retention_days))
        cutoff_ms = ts - retention_days * DAY_MS
    else:
        cutoff_ms = int(cutoff_ms)

    return await retention_repo.soft_expire_due_history(now=ts, cutoff_ms=cutoff_ms)


async def purge_by_cutoff(
    *,
    before_ms: int | None = None,
    older_than_days: int | None = None,
    now: int | None = None,
) -> dict[str, int]:
    """Admin purge: soft-expire terminal pids by cutoff (same as soft_expire).

    Does not DELETE still-visible pids; never touches live tasks.
    """
    from app.domain.errors import BadRequestError

    ts = int(now if now is not None else now_ms())
    cutoffs: list[int] = []
    if before_ms is not None:
        cutoffs.append(int(before_ms))
    if older_than_days is not None:
        days = int(older_than_days)
        if days < 1:
            raise BadRequestError("older_than_days 必须 >= 1")
        cutoffs.append(ts - days * DAY_MS)
    if not cutoffs:
        raise BadRequestError("before_ms 与 older_than_days 至少提供一个")

    # Use the more aggressive (larger) cutoff so both constraints are covered
    # when both are provided: expire anything older than the looser bound.
    # Spec: either is enough; if both, take the more recent cutoff (smaller
    # window of survivors) i.e. max(cutoff) = more aggressive expire.
    cutoff_ms = max(cutoffs)

    result = await soft_expire_due_history(now=ts, cutoff_ms=cutoff_ms)
    return {
        "expired_user_tasks": int(result["expired_count"]),
        "detached_source_tids": int(result["detached_source_tids"]),
        "gcs_sources": int(result["gcs_sources"]),
        "skipped_live": int(result["skipped_live"]),
    }


async def history_retention_worker(
    interval_seconds: float = HISTORY_RETENTION_INTERVAL_SECONDS,
) -> None:
    """Periodic soft-expire loop (tests call soft_expire_due_history directly)."""
    while True:
        try:
            await soft_expire_due_history()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("历史保留期软过期任务失败")
        await asyncio.sleep(interval_seconds)
