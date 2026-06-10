"""aria2 polling sync for the v0 shared download model."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import aiohttp
from fastapi import WebSocket
from sqlalchemy import select

from app.aria2 import download_ops
from app.aria2.client import Aria2Client
from app.aria2.errors import prefer_aria2_error_message
from app.aria2.failed_task_cleanup import (
    cleanup_failed_task_artifacts,
    get_representative_owner_id,
)
from app.core.security import sanitize_string
from app.core.state import AppState
from app.db.engine import transaction
from app.db.schema import global_downloads
from app.repositories.downloads import (
    ACTIVE_USER_TASK_STATUSES,
    mark_global_download_failed,
    now_ms,
)
from app.services.task_projection import (
    has_live_bt_evidence,
    has_real_file_path,
    is_bt_resource_kind,
    is_metadata_phase_status,
)

logger = logging.getLogger(__name__)


OWNED_STOPPED_RESULT_CLEANUP_BATCH = 50
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
    return is_bt_resource_kind(download) or has_live_bt_evidence(status)


def _should_upgrade_to_torrent(
    status: dict[str, Any],
    download: dict[str, Any],
) -> bool:
    return not is_bt_resource_kind(download) and has_live_bt_evidence(status)


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

    total_bytes = download_ops.safe_int(status.get("totalLength"))
    completed_bytes = download_ops.safe_int(status.get("completedLength"))
    if total_bytes <= 0 or completed_bytes < total_bytes:
        return False

    return _has_bittorrent_evidence(status, download) and has_real_file_path(status)


def _map_v0_status(
    status: dict[str, Any],
    download_id: int,
    *,
    prefer_bittorrent_name: bool = False,
) -> dict[str, Any]:
    fallback_name = (status.get("files") or [{}])[0].get("path")
    if fallback_name:
        fallback_name = _sanitize_path(fallback_name, download_id)

    if prefer_bittorrent_name:
        extracted = download_ops.extract_display_name(status, fallback_name)
    else:
        extracted = sanitize_string(fallback_name) if fallback_name else None

    raw_status = str(status.get("status") or "unknown")
    raw_error = status.get("errorMessage")
    return {
        "status": download_ops.map_aria2_status(status),
        "raw_status": raw_status,
        "display_name": extracted,
        "total_bytes": download_ops.safe_int(status.get("totalLength")),
        "completed_bytes": download_ops.safe_int(status.get("completedLength")),
        "error_message": sanitize_string(
            prefer_aria2_error_message(status.get("errorCode"), raw_error, "后端错误")
            if raw_error or raw_status == "error"
            else None
        ),
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


async def _complete_v0_download_from_sync(
    *,
    state: AppState,
    client: Aria2Client,
    download: dict[str, Any],
    aria2_status: dict[str, Any],
    completion_gid: str,
    allow_metadata_handoff_defer: bool = True,
) -> None:
    from app.aria2.listener import handle_v0_download_complete

    completed = await handle_v0_download_complete(
        state=state,
        client=client,
        download=download,
        aria2_status=aria2_status,
        completion_gid=completion_gid,
        log_prefix="[Sync]",
        allow_metadata_handoff_defer=allow_metadata_handoff_defer,
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
    bt_evidence = _has_bittorrent_evidence(status, download)
    upgrade_to_torrent = _should_upgrade_to_torrent(status, download)
    mapped = _map_v0_status(
        status,
        download_id,
        prefer_bittorrent_name=bt_evidence,
    )
    gid = str(download.get("aria2_gid") or "")
    followed_by = status.get("followedBy") or []

    if mapped["raw_status"] == "complete" and followed_by:
        switched = await download_ops.switch_to_followed_download(
            client=client,
            download=download,
            metadata_gid=gid,
            followed_gid=str(followed_by[0]),
            display_name_fallback=str(download.get("display_name") or ""),
            log_prefix="[Sync]",
        )
        if switched:
            await _broadcast_download_update(state, int(download["id"]))
        return

    if download_ops.is_metadata_handoff_pending(download, status):
        followed_gid = None
        try:
            refreshed_status = await client.tell_status(gid)
            followed_by = refreshed_status.get("followedBy") or []
            if followed_by:
                followed_gid = str(followed_by[0])
        except Exception as exc:
            logger.debug(
                "[Sync] Failed to refresh metadata handoff gid=%s error=%s",
                gid,
                exc,
            )
        if followed_gid:
            switched = await download_ops.switch_to_followed_download(
                client=client,
                download=download,
                metadata_gid=gid,
                followed_gid=followed_gid,
                display_name_fallback=str(download.get("display_name") or ""),
                log_prefix="[Sync]",
            )
            if switched:
                await _broadcast_download_update(state, int(download["id"]))
            return

        logger.info(
            "[Sync] Metadata download complete without followedBy, waiting for handoff id=%s gid=%s",
            download_id,
            gid,
        )
        await download_ops.guarded_update_global_download(
            download_id,
            {"status": "active"},
        )
        await download_ops.update_active_user_tasks(download_id, status="active")
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

    # Skip progress and name updates during metadata download phase
    is_metadata = bt_evidence and is_metadata_phase_status(status)

    timestamp = now_ms()
    global_values: dict[str, Any] = {
        "status": mapped["status"],
        "completed_bytes": mapped["completed_bytes"],
        "updated_at_ms": timestamp,
    }
    if upgrade_to_torrent:
        global_values["resource_kind"] = "torrent"
    bt_info_hash = download_ops.bt_info_hash_from_status(status)
    if bt_evidence and bt_info_hash:
        global_values["bt_info_hash"] = bt_info_hash
    if not is_metadata:
        global_values["total_bytes"] = mapped["total_bytes"]
        if mapped["display_name"]:
            global_values["display_name"] = mapped["display_name"]

    changed = await download_ops.guarded_update_global_download(download_id, global_values)
    if not changed:
        return

    await download_ops.update_active_user_tasks(
        download_id,
        status=mapped["status"],
        display_name=mapped["display_name"] if not is_metadata else None,
    )
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

    while True:
        await _repair_inconsistent_completed_downloads_v0()
        client = get_aria2_client(state=state)

        downloads = await _list_v0_tracked_downloads()
        removable_stopped_gids: set[str] = set()

        async def fetch_and_update(download: dict[str, Any]) -> None:
            gid = str(download.get("aria2_gid") or "")
            if not gid:
                return
            download_id = int(download["id"])

            try:
                status = await client.tell_status(gid)
            except Exception as exc:
                if _is_missing_gid_error(exc):
                    # 检查任务是否已完成
                    async with transaction() as conn:
                        row = (
                            await conn.execute(
                                select(
                                    global_downloads.c.status,
                                    global_downloads.c.completed_file_id,
                                    global_downloads.c.completed_bytes,
                                    global_downloads.c.total_bytes,
                                ).where(global_downloads.c.id == download_id)
                            )
                        ).first()

                    if row is None:
                        logger.debug(
                            "[Sync] GID %s missing but download not found, skipping",
                            gid,
                        )
                        return

                    status_val, completed_file_id, completed_bytes, total_bytes = row

                    # 情况1: 任务已完成，GID 被外部清理是正常的
                    if status_val == "completed" and completed_file_id is not None:
                        logger.info(
                            "[Sync] GID %s missing but download already completed, skipping",
                            gid,
                        )
                        return

                    # 情况2: 任务不在活跃状态，不处理
                    if status_val not in V0_SYNC_TRACKED_STATUSES:
                        logger.debug(
                            "[Sync] GID %s missing but download not active (status=%s), skipping",
                            gid,
                            status_val,
                        )
                        return

                    # 情况3: 尝试从磁盘恢复
                    logger.warning(
                        "[Sync] GID %s missing, attempting recovery from disk download_id=%s",
                        gid,
                        download_id,
                    )

                    # 构造一个最小的 aria2_status 用于完成处理
                    # files 字段为空，完成处理会依赖 task_dir 扫描
                    fake_aria2_status: dict[str, Any] = {
                        "status": "complete",
                        "files": [],
                        "totalLength": total_bytes or 0,
                        "completedLength": completed_bytes or 0,
                    }

                    await _complete_v0_download_from_sync(
                        state=state,
                        client=client,
                        download=download,
                        aria2_status=fake_aria2_status,
                        completion_gid=gid,
                        allow_metadata_handoff_defer=False,
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
            if str(status.get("status") or "") in {"complete", "error", "removed"}:
                removable_stopped_gids.add(gid)

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

        await _cleanup_owned_stopped_results(
            client=client,
            removable_gids=removable_stopped_gids,
            max_actions=OWNED_STOPPED_RESULT_CLEANUP_BATCH,
        )
        await _cleanup_stale_queued_downloads_v0(state=state)

        await asyncio.sleep(interval)


async def _cleanup_owned_stopped_results(
    client: Aria2Client,
    removable_gids: set[str],
    max_actions: int,
) -> None:
    """Remove stopped aria2 results only after aria2deck has consumed them.

    Unknown GIDs may belong to another service sharing the same aria2 instance
    or may be useful debugging evidence, so they are intentionally ignored.
    Running tasks and unprocessed stopped results are never force-removed here.
    """
    try:
        stopped = await client.tell_stopped(0, 1000)
    except Exception as exc:
        logger.warning(
            "[Sync] Failed to list stopped aria2 tasks, skipping owned result cleanup: %s",
            exc,
        )
        return

    def _extract_gids(rows: list[dict[str, Any]]) -> list[str]:
        gids: list[str] = []
        for row in rows:
            gid = row.get("gid")
            if isinstance(gid, str) and gid:
                gids.append(gid)
        return gids

    stopped_gids = _extract_gids(stopped)

    actions = 0

    for gid in stopped_gids:
        if gid not in removable_gids:
            continue
        if actions >= max_actions:
            break
        try:
            await client.remove_download_result(gid)
            actions += 1
            logger.info("[Sync] Removed owned stopped aria2 result gid=%s", gid)
        except Exception as exc:
            logger.debug(
                "[Sync] Failed to remove owned stopped result gid=%s error=%s",
                gid,
                exc,
            )


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
