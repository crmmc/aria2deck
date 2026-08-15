"""History retention repository: soft-expire terminal pids + GC orphaned sources.

Spec: M8 §3.6.4 / §3.7 — soft expire never DELETEs tid (G9).
"""

from __future__ import annotations

import logging
import time
from typing import Any

from sqlalchemy import delete, func, or_, select, update

from app.db.engine import transaction
from app.db.schema import global_downloads, user_tasks
from app.domain.status import (
    ACTIVE_USER_TASK_STATUSES,
    TERMINAL_USER_TASK_STATUSES,
)
from app.repositories.task.sources import (
    detached_source_uri_placeholder,
    strip_orphaned_download_source,
)

logger = logging.getLogger(__name__)

DAY_MS = 24 * 60 * 60 * 1000


def now_ms() -> int:
    return int(time.time() * 1000)


async def gc_source_if_orphaned(source_id: int | None) -> bool:
    """Strip S payload when no tid references source_id. Returns True if purged."""
    if source_id is None:
        return False
    sid = int(source_id)
    ts = now_ms()
    async with transaction() as conn:
        ref_count = (
            await conn.execute(
                select(func.count())
                .select_from(global_downloads)
                .where(global_downloads.c.source_id == sid)
            )
        ).scalar_one()
        if int(ref_count) > 0:
            return False
        purged = await strip_orphaned_download_source(
            conn, sid, timestamp_ms=ts
        )
    if purged:
        logger.info("GC download_source 完成 source_id=%s", sid)
    return purged


async def reclaim_zero_pid_tid(tid: int) -> dict[str, Any]:
    """Reclaim terminal tid after hard-delete left zero pid rows (§3.6.3).

    - completed + completed_file_id: keep shell; detach source_id; strip uri; GC S
    - failed/cancelled/other terminal without completed_file_id: DELETE tid; GC S
    - live: no-op
    """
    ts = now_ms()
    result: dict[str, Any] = {
        "action": "none",
        "tid": int(tid),
        "source_gc": False,
    }
    source_id: int | None = None

    async with transaction() as conn:
        gd = (
            (
                await conn.execute(
                    select(
                        global_downloads.c.id,
                        global_downloads.c.status,
                        global_downloads.c.source_id,
                        global_downloads.c.completed_file_id,
                        global_downloads.c.resource_kind,
                        global_downloads.c.resource_key,
                        global_downloads.c.bt_info_hash,
                        global_downloads.c.source_uri,
                    ).where(global_downloads.c.id == int(tid))
                )
            )
            .mappings()
            .first()
        )
        if gd is None:
            return result

        status = str(gd["status"] or "")
        if status in ACTIVE_USER_TASK_STATUSES:
            result["action"] = "skipped_live"
            return result
        if status not in TERMINAL_USER_TASK_STATUSES:
            result["action"] = "skipped_non_terminal"
            return result

        pid_count = (
            await conn.execute(
                select(func.count())
                .select_from(user_tasks)
                .where(user_tasks.c.global_download_id == int(tid))
            )
        ).scalar_one()
        if int(pid_count) > 0:
            result["action"] = "skipped_has_pids"
            return result

        source_id = (
            int(gd["source_id"]) if gd["source_id"] is not None else None
        )
        completed_file_id = gd["completed_file_id"]

        if status == "completed" and completed_file_id is not None:
            placeholder = detached_source_uri_placeholder(
                resource_kind=str(gd["resource_kind"] or ""),
                resource_key=str(gd["resource_key"] or ""),
                bt_info_hash=gd["bt_info_hash"],
                source_uri=str(gd["source_uri"] or ""),
            )
            await conn.execute(
                update(global_downloads)
                .where(global_downloads.c.id == int(tid))
                .values(
                    source_id=None,
                    source_uri=placeholder,
                    updated_at_ms=ts,
                )
            )
            result["action"] = "kept_completed_shell"
        else:
            await conn.execute(
                delete(global_downloads).where(global_downloads.c.id == int(tid))
            )
            result["action"] = "deleted_tid"

        if source_id is not None:
            ref_count = (
                await conn.execute(
                    select(func.count())
                    .select_from(global_downloads)
                    .where(global_downloads.c.source_id == source_id)
                )
            ).scalar_one()
            if int(ref_count) == 0:
                purged = await strip_orphaned_download_source(
                    conn, source_id, timestamp_ms=ts
                )
                result["source_gc"] = bool(purged)

    if result["action"] in {"kept_completed_shell", "deleted_tid"}:
        logger.info(
            "零 pid tid 回收完成 tid=%s action=%s source_gc=%s",
            tid,
            result["action"],
            result["source_gc"],
        )
    return result


async def soft_expire_due_history(
    *,
    now: int | None = None,
    cutoff_ms: int,
) -> dict[str, int]:
    """Soft-expire due terminal pids. Never DELETE tid/pid."""
    ts = int(now if now is not None else now_ms())
    cutoff_ms = int(cutoff_ms)

    expired_count = 0
    detached_source_tids = 0
    gcs_sources = 0
    skipped_live = 0

    async with transaction() as conn:
        due_rows = (
            (
                await conn.execute(
                    select(
                        user_tasks.c.id,
                        user_tasks.c.global_download_id,
                        user_tasks.c.display_name,
                        user_tasks.c.error_message,
                        global_downloads.c.display_name.label("global_display_name"),
                        global_downloads.c.error_message.label("global_error_message"),
                        global_downloads.c.status.label("global_status"),
                        global_downloads.c.source_id,
                        global_downloads.c.resource_kind,
                        global_downloads.c.resource_key,
                        global_downloads.c.bt_info_hash,
                        global_downloads.c.source_uri,
                    )
                    .select_from(
                        user_tasks.join(
                            global_downloads,
                            user_tasks.c.global_download_id == global_downloads.c.id,
                        )
                    )
                    .where(
                        user_tasks.c.status.in_(TERMINAL_USER_TASK_STATUSES),
                        user_tasks.c.history_expired_at_ms.is_(None),
                        # finished_at_ms if set, else updated_at_ms
                        or_(
                            (
                                user_tasks.c.finished_at_ms.is_not(None)
                                & (user_tasks.c.finished_at_ms < cutoff_ms)
                            ),
                            (
                                user_tasks.c.finished_at_ms.is_(None)
                                & (user_tasks.c.updated_at_ms < cutoff_ms)
                            ),
                        ),
                    )
                )
            )
            .mappings()
            .all()
        )

        affected_tids: set[int] = set()
        for row in due_rows:
            pid = int(row["id"])
            tid = int(row["global_download_id"])
            display_name = row["display_name"] or row["global_display_name"]
            error_message = row["error_message"] or row["global_error_message"]
            values: dict[str, Any] = {"history_expired_at_ms": ts}
            if not row["display_name"] and display_name:
                values["display_name"] = display_name
            if not row["error_message"] and error_message:
                values["error_message"] = error_message
            await conn.execute(
                update(user_tasks).where(user_tasks.c.id == pid).values(**values)
            )
            expired_count += 1
            affected_tids.add(tid)

        for tid in affected_tids:
            # Only detach when tid is terminal and every pid on it is soft-expired.
            gd = (
                (
                    await conn.execute(
                        select(
                            global_downloads.c.id,
                            global_downloads.c.status,
                            global_downloads.c.source_id,
                            global_downloads.c.resource_kind,
                            global_downloads.c.resource_key,
                            global_downloads.c.bt_info_hash,
                            global_downloads.c.source_uri,
                        ).where(global_downloads.c.id == tid)
                    )
                )
                .mappings()
                .first()
            )
            if gd is None:
                continue
            if str(gd["status"]) in ACTIVE_USER_TASK_STATUSES:
                skipped_live += 1
                continue
            if str(gd["status"]) not in TERMINAL_USER_TASK_STATUSES:
                continue

            unexpired = (
                await conn.execute(
                    select(func.count())
                    .select_from(user_tasks)
                    .where(
                        user_tasks.c.global_download_id == tid,
                        user_tasks.c.history_expired_at_ms.is_(None),
                    )
                )
            ).scalar_one()
            if int(unexpired) > 0:
                continue

            source_id = gd["source_id"]
            placeholder = detached_source_uri_placeholder(
                resource_kind=str(gd["resource_kind"] or ""),
                resource_key=str(gd["resource_key"] or ""),
                bt_info_hash=gd["bt_info_hash"],
                source_uri=str(gd["source_uri"] or ""),
            )
            await conn.execute(
                update(global_downloads)
                .where(global_downloads.c.id == tid)
                .values(
                    source_id=None,
                    source_uri=placeholder,
                    updated_at_ms=ts,
                )
            )
            detached_source_tids += 1
            if source_id is not None:
                ref_count = (
                    await conn.execute(
                        select(func.count())
                        .select_from(global_downloads)
                        .where(global_downloads.c.source_id == int(source_id))
                    )
                ).scalar_one()
                if int(ref_count) == 0:
                    purged = await strip_orphaned_download_source(
                        conn, int(source_id), timestamp_ms=ts
                    )
                    if purged:
                        gcs_sources += 1

    if expired_count:
        logger.info(
            "历史软过期完成 expired=%s detached_source_tids=%s gcs_sources=%s cutoff_ms=%s",
            expired_count,
            detached_source_tids,
            gcs_sources,
            cutoff_ms,
        )
    return {
        "expired_count": expired_count,
        "detached_source_tids": detached_source_tids,
        "gcs_sources": gcs_sources,
        "skipped_live": skipped_live,
    }
