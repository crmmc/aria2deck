"""aria2 polling sync for the v0 shared download model."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

import aiohttp
from fastapi import WebSocket
from sqlalchemy import select, update

from app.aria2.client import Aria2Client
from app.aria2.errors import parse_error_message
from app.aria2.failed_task_cleanup import (
    cleanup_failed_task_artifacts,
    get_representative_owner_id,
)
from app.core.security import sanitize_string
from app.core.state import AppState
from app.db.engine import transaction
from app.db.schema import global_downloads, user_tasks
from app.repositories.downloads import (
    ACTIVE_USER_TASK_STATUSES,
    mark_global_download_failed,
    now_ms,
)
from app.services.task_projection import has_real_file_path

logger = logging.getLogger(__name__)


ORPHAN_GRACE_SECONDS = 60.0
ORPHAN_CLEANUP_BATCH = 50
COMPLETE_REPAIR_GRACE_SECONDS = 30.0
STALE_QUEUED_GRACE_SECONDS = 300.0
MISSING_GID_KEYWORDS = ("gid", "not found")
MISSING_GID_PATTERNS = (
    "gid#",
    "no such download",
    "unknown gid",
    "invalid gid",
)
TRANSIENT_RPC_ERROR_KEYWORDS = (
    "cannot connect to host",
    "connection refused",
    "temporarily unavailable",
    "timed out",
)

ARIA2_TO_V0_STATUS = {
    "active": "active",
    "waiting": "waiting",
    "paused": "paused",
    "complete": "completed",
    "error": "failed",
    "removed": "failed",
}
V0_SYNC_TRACKED_STATUSES = ACTIVE_USER_TASK_STATUSES


def _sanitize_path(file_path: str | None, task_id: int) -> str | None:
    """Convert an absolute aria2 path to a safe display filename."""
    if not file_path:
        return None

    try:
        abs_path = Path(file_path)
        return abs_path.name if abs_path.name else file_path
    except (ValueError, OSError) as exc:
        logger.debug("Failed to sanitize path for download %s: %s", task_id, exc)
        return file_path


def _safe_int(value: str | int | None, default: int = 0) -> int:
    try:
        return int(value) if value is not None else default
    except (ValueError, TypeError):
        return default


def _status_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return False


def _has_bittorrent_evidence(
    status: dict[str, Any],
    download: dict[str, Any],
) -> bool:
    if str(status.get("infoHash") or "").strip():
        return True
    return str(download.get("resource_kind") or "") in {"magnet", "torrent"}


def _is_effectively_complete_active_bt_status(
    status: dict[str, Any],
    download: dict[str, Any],
) -> bool:
    if str(status.get("status") or "") != "active":
        return False
    if status.get("followedBy"):
        return False
    if _status_bool(status.get("verifyIntegrityPending")):
        return False

    total_bytes = _safe_int(status.get("totalLength"))
    completed_bytes = _safe_int(status.get("completedLength"))
    if total_bytes <= 0 or completed_bytes < total_bytes:
        return False

    return _has_bittorrent_evidence(status, download) and has_real_file_path(status)


def _map_v0_status(status: dict[str, Any], download_id: int) -> dict[str, Any]:
    raw_name = status.get("bittorrent", {}).get("info", {}).get("name") or (
        status.get("files") or [{}]
    )[0].get("path")
    raw_status = str(status.get("status") or "unknown")
    raw_error = status.get("errorMessage")
    error_display = parse_error_message(raw_error) if raw_error else None
    return {
        "status": ARIA2_TO_V0_STATUS.get(raw_status, "active"),
        "raw_status": raw_status,
        "display_name": sanitize_string(_sanitize_path(raw_name, download_id)),
        "total_bytes": _safe_int(status.get("totalLength")),
        "completed_bytes": _safe_int(status.get("completedLength")),
        "error_message": sanitize_string(error_display or raw_error),
    }


def _exception_message(exc: Exception) -> str:
    return str(exc).lower()


def _is_missing_gid_error(exc: Exception) -> bool:
    message = _exception_message(exc)
    if all(keyword in message for keyword in MISSING_GID_KEYWORDS):
        return True
    return any(pattern in message for pattern in MISSING_GID_PATTERNS)


def _is_transient_rpc_error(exc: Exception) -> bool:
    if isinstance(exc, (aiohttp.ClientError, TimeoutError, OSError, ConnectionError)):
        return True
    message = _exception_message(exc)
    return any(keyword in message for keyword in TRANSIENT_RPC_ERROR_KEYWORDS)


async def _list_v0_downloads() -> list[dict[str, Any]]:
    async with transaction() as conn:
        rows = (await conn.execute(select(global_downloads))).mappings().all()
    return [dict(row) for row in rows]


async def _list_v0_tracked_downloads() -> list[dict[str, Any]]:
    async with transaction() as conn:
        rows = (
            (
                await conn.execute(
                    select(global_downloads).where(
                        global_downloads.c.aria2_gid.is_not(None),
                        global_downloads.c.status.in_(V0_SYNC_TRACKED_STATUSES),
                    )
                )
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


async def _broadcast_download_update(state: AppState, download_id: int) -> None:
    from app.routers.tasks import broadcast_task_update_to_subscribers

    await broadcast_task_update_to_subscribers(state, download_id)


async def _fail_v0_download_and_cleanup(
    *,
    state: AppState,
    client: Aria2Client,
    download_id: int,
    gid: str | None,
    message: str,
    error_code: str | None,
    log_prefix: str,
) -> None:
    from app.core.state import get_task_complete_lock

    completion_lock = await get_task_complete_lock(state, download_id)
    async with completion_lock:
        owner_id = await get_representative_owner_id(download_id)
        failed_download = await mark_global_download_failed(
            download_id,
            message=message,
            error_code=error_code,
            clear_gid=True,
        )
        if failed_download is None or failed_download["status"] != "failed":
            return

        await cleanup_failed_task_artifacts(
            client=client,
            task_id=download_id,
            gid=gid,
            owner_id=owner_id,
            log_prefix=log_prefix,
        )
        await _broadcast_download_update(state, download_id)


async def _guarded_update_global_download(
    download_id: int,
    values: dict[str, Any],
) -> bool:
    if not values:
        return False

    row_values = {**values}
    row_values.setdefault("updated_at_ms", now_ms())
    async with transaction() as conn:
        row = (
            await conn.execute(
                update(global_downloads)
                .where(
                    global_downloads.c.id == download_id,
                    global_downloads.c.status.in_(V0_SYNC_TRACKED_STATUSES),
                    global_downloads.c.completed_file_id.is_(None),
                )
                .values(**row_values)
                .returning(global_downloads.c.id)
            )
        ).first()
    return row is not None


async def _update_active_user_task_status(
    download_id: int,
    status: str,
) -> None:
    async with transaction() as conn:
        await conn.execute(
            update(user_tasks)
            .where(
                user_tasks.c.global_download_id == download_id,
                user_tasks.c.status.in_(V0_SYNC_TRACKED_STATUSES),
            )
            .values(status=status, updated_at_ms=now_ms())
        )


async def _complete_v0_download_from_sync(
    *,
    state: AppState,
    client: Aria2Client,
    download: dict[str, Any],
    aria2_status: dict[str, Any],
    completion_gid: str,
) -> None:
    from app.aria2.listener import handle_v0_download_complete

    completed = await handle_v0_download_complete(
        state=state,
        client=client,
        download=download,
        aria2_status=aria2_status,
        completion_gid=completion_gid,
        log_prefix="[Sync]",
    )
    if completed:
        await _broadcast_download_update(state, int(download["id"]))


async def _update_v0_download_from_aria2(
    *,
    state: AppState,
    client: Aria2Client,
    download: dict[str, Any],
    status: dict[str, Any],
) -> None:
    download_id = int(download["id"])
    mapped = _map_v0_status(status, download_id)
    gid = str(download.get("aria2_gid") or "")
    followed_by = status.get("followedBy") or []

    if mapped["raw_status"] == "complete" and followed_by:
        new_gid = str(followed_by[0])
        logger.info(
            "[Sync] Metadata download complete, updating GID: %s -> %s", gid, new_gid
        )
        changed = await _guarded_update_global_download(
            download_id,
            {
                "aria2_gid": new_gid,
                "status": "active",
            },
        )
        if not changed:
            return

        await _update_active_user_task_status(download_id, "active")
        if gid != new_gid:
            try:
                await client.remove_download_result(gid)
            except Exception as exc:
                logger.debug(
                    "[Sync] Failed to remove metadata result gid=%s error=%s", gid, exc
                )
        await _broadcast_download_update(state, download_id)
        return

    if mapped["raw_status"] == "complete":
        await _complete_v0_download_from_sync(
            state=state,
            client=client,
            download=download,
            aria2_status=status,
            completion_gid=gid,
        )
        return

    if _is_effectively_complete_active_bt_status(status, download):
        await _complete_v0_download_from_sync(
            state=state,
            client=client,
            download=download,
            aria2_status=status,
            completion_gid=gid,
        )
        return

    if mapped["status"] == "failed":
        message = mapped["error_message"] or (
            "外部取消（管理员/外部客户端）"
            if mapped["raw_status"] == "removed"
            else "后端错误"
        )
        logger.warning(
            "[Sync] v0 download failed download_id=%s gid=%s error=%s",
            download_id,
            gid,
            message,
        )
        await _fail_v0_download_and_cleanup(
            state=state,
            client=client,
            download_id=download_id,
            gid=gid,
            message=message,
            error_code=str(status.get("errorCode") or mapped["raw_status"]),
            log_prefix="[Sync]",
        )
        return

    timestamp = now_ms()
    global_values: dict[str, Any] = {
        "status": mapped["status"],
        "total_bytes": mapped["total_bytes"],
        "completed_bytes": mapped["completed_bytes"],
        "updated_at_ms": timestamp,
    }
    if mapped["display_name"]:
        global_values["display_name"] = mapped["display_name"]

    changed = await _guarded_update_global_download(download_id, global_values)
    if not changed:
        return

    await _update_active_user_task_status(download_id, mapped["status"])
    await _broadcast_download_update(state, download_id)


async def _repair_inconsistent_completed_downloads_v0() -> None:
    threshold_ms = now_ms() - int(COMPLETE_REPAIR_GRACE_SECONDS * 1000)
    async with transaction() as conn:
        rows = (
            await conn.execute(
                select(global_downloads.c.id).where(
                    global_downloads.c.status == "completed",
                    global_downloads.c.completed_file_id.is_(None),
                    global_downloads.c.updated_at_ms < threshold_ms,
                )
            )
        ).all()

    for row in rows:
        download_id = int(row[0])
        logger.warning(
            "[Sync] Completed v0 download was not indexed, failing id=%s", download_id
        )
        await mark_global_download_failed(
            download_id,
            message="下载完成但文件未入库",
            error_code="completion_not_indexed",
        )


async def _cleanup_stale_queued_downloads_v0(
    state: AppState,
    grace_seconds: float = STALE_QUEUED_GRACE_SECONDS,
) -> None:
    threshold_ms = now_ms() - int(grace_seconds * 1000)
    async with transaction() as conn:
        rows = (
            await conn.execute(
                select(global_downloads.c.id).where(
                    global_downloads.c.status == "queued",
                    global_downloads.c.aria2_gid.is_(None),
                    global_downloads.c.updated_at_ms < threshold_ms,
                )
            )
        ).all()

    for row in rows:
        download_id = int(row[0])
        async with state.lock:
            submit_lock = state.task_submit_locks.get(download_id)
            locked = submit_lock is not None and submit_lock.locked()
        if locked:
            logger.debug(
                "[Sync] skipped stale v0 queued download_id=%s reason=submit_in_progress",
                download_id,
            )
            continue

        logger.warning("[Sync] Cleaning stale v0 queued download_id=%s", download_id)
        failed_download = await mark_global_download_failed(
            download_id,
            message="任务提交超时，已自动清理",
            error_code="submit_timeout",
        )
        if failed_download is not None and failed_download["status"] == "failed":
            await _broadcast_download_update(state, download_id)


async def sync_tasks(
    state: AppState,
    interval: float,
) -> None:
    """Synchronize aria2 task state into v0 tables."""
    from app.core.state import get_aria2_client

    orphan_seen_at: dict[str, float] = {}

    while True:
        await _repair_inconsistent_completed_downloads_v0()
        client = get_aria2_client(state=state)

        tracked_downloads = await _list_v0_downloads()
        tracked_gids = {
            str(row["aria2_gid"]) for row in tracked_downloads if row.get("aria2_gid")
        }
        downloads = await _list_v0_tracked_downloads()

        async def fetch_and_update(download: dict[str, Any]) -> None:
            gid = str(download.get("aria2_gid") or "")
            if not gid:
                return
            download_id = int(download["id"])

            try:
                status = await client.tell_status(gid)
            except Exception as exc:
                if _is_missing_gid_error(exc):
                    logger.error(
                        "[Sync] GID %s missing, failing v0 download: %s", gid, exc
                    )
                    await _fail_v0_download_and_cleanup(
                        state=state,
                        client=client,
                        download_id=download_id,
                        gid=gid,
                        message="后端错误",
                        error_code="missing_gid",
                        log_prefix="[Sync]",
                    )
                    return

                level = logger.warning if _is_transient_rpc_error(exc) else logger.error
                level("[Sync] Failed to fetch GID %s status, retry later: %s", gid, exc)
                return

            await _update_v0_download_from_aria2(
                state=state,
                client=client,
                download=download,
                status=status,
            )

        results = await asyncio.gather(
            *[fetch_and_update(download) for download in downloads],
            return_exceptions=True,
        )
        for index, result in enumerate(results):
            if isinstance(result, Exception):
                logger.warning(
                    "sync_tasks: v0 download update failed download_id=%s error=%s",
                    downloads[index]["id"],
                    result,
                )

        await _cleanup_orphan_aria2_tasks(
            client=client,
            tracked_gids=tracked_gids,
            orphan_seen_at=orphan_seen_at,
            grace_seconds=ORPHAN_GRACE_SECONDS,
            max_actions=ORPHAN_CLEANUP_BATCH,
        )
        await _cleanup_stale_queued_downloads_v0(state=state)

        await asyncio.sleep(interval)


async def _cleanup_orphan_aria2_tasks(
    client: Aria2Client,
    tracked_gids: set[str],
    orphan_seen_at: dict[str, float],
    grace_seconds: float,
    max_actions: int,
) -> None:
    """Clean aria2 tasks whose GIDs no longer exist in the database."""
    try:
        active = await client.tell_active()
        waiting = await client.tell_waiting(0, 1000)
        stopped = await client.tell_stopped(0, 1000)
    except Exception as exc:
        logger.warning(
            "[Sync] Failed to list aria2 tasks, skipping orphan cleanup: %s", exc
        )
        return

    now = time.monotonic()

    def _extract_gids(rows: list[dict[str, Any]]) -> list[str]:
        gids: list[str] = []
        for row in rows:
            gid = row.get("gid")
            if isinstance(gid, str) and gid:
                gids.append(gid)
        return gids

    active_gids = _extract_gids(active)
    waiting_gids = _extract_gids(waiting)
    stopped_gids = _extract_gids(stopped)

    current_orphan_gids = {
        gid
        for gid in [*active_gids, *waiting_gids, *stopped_gids]
        if gid not in tracked_gids
    }

    for gid in list(orphan_seen_at.keys()):
        if gid not in current_orphan_gids:
            orphan_seen_at.pop(gid, None)

    for gid in current_orphan_gids:
        if gid not in orphan_seen_at:
            orphan_seen_at[gid] = now
            logger.warning("[Sync] Found orphan aria2 task gid=%s", gid)

    actions = 0

    for gid in stopped_gids:
        if gid in tracked_gids:
            continue
        if actions >= max_actions:
            break
        try:
            await client.remove_download_result(gid)
            orphan_seen_at.pop(gid, None)
            actions += 1
            logger.info("[Sync] Removed orphan stopped task gid=%s", gid)
        except Exception as exc:
            logger.debug(
                "[Sync] Failed to remove orphan stopped result gid=%s error=%s",
                gid,
                exc,
            )

    for gid in [*active_gids, *waiting_gids]:
        if gid in tracked_gids:
            continue
        if actions >= max_actions:
            break

        first_seen = orphan_seen_at.get(gid, now)
        if (now - first_seen) < grace_seconds:
            continue

        try:
            await client.force_remove(gid)
        except Exception as exc:
            logger.debug(
                "[Sync] Failed to force remove orphan gid=%s error=%s", gid, exc
            )

        try:
            await client.remove_download_result(gid)
        except Exception as exc:
            logger.debug(
                "[Sync] Failed to remove orphan result gid=%s error=%s", gid, exc
            )

        orphan_seen_at.pop(gid, None)
        actions += 1
        logger.warning("[Sync] Removed orphan running task gid=%s", gid)


async def register_ws(state: AppState, user_id: int, ws: WebSocket) -> None:
    async with state.lock:
        state.ws_connections.setdefault(user_id, set()).add(ws)


async def unregister_ws(state: AppState, user_id: int, ws: WebSocket) -> None:
    async with state.lock:
        sockets = state.ws_connections.get(user_id)
        if sockets:
            sockets.discard(ws)


async def broadcast_notification(
    state: AppState,
    user_id: int,
    message: str,
    level: str = "info",
) -> None:
    async with state.lock:
        sockets = list(state.ws_connections.get(user_id, set()))

    notification = {"type": "notification", "message": message, "level": level}
    failed_sockets = []

    for ws in sockets:
        try:
            await ws.send_json(notification)
        except Exception as exc:
            logger.debug(
                "[Sync] Notification send failed user_id=%s error=%s", user_id, exc
            )
            failed_sockets.append(ws)

    if failed_sockets:
        async with state.lock:
            user_sockets = state.ws_connections.get(user_id)
            if user_sockets:
                for ws in failed_sockets:
                    user_sockets.discard(ws)
