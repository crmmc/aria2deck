"""aria2 polling sync for the v0 shared download model."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.aria2.client import Aria2Client
from app.core.state import AppState
from app.services import aria2_lifecycle_service as lifecycle

logger = logging.getLogger(__name__)

OWNED_STOPPED_RESULT_CLEANUP_BATCH = 50
COMPLETE_REPAIR_GRACE_SECONDS = lifecycle.COMPLETE_REPAIR_GRACE_SECONDS
STALE_QUEUED_GRACE_SECONDS = lifecycle.STALE_QUEUED_GRACE_SECONDS
MISSING_GID_KEYWORDS = lifecycle.MISSING_GID_KEYWORDS
MISSING_GID_PATTERNS = lifecycle.MISSING_GID_PATTERNS
TRANSIENT_RPC_ERROR_KEYWORDS = lifecycle.TRANSIENT_RPC_ERROR_KEYWORDS
V0_SYNC_TRACKED_STATUSES = lifecycle.V0_SYNC_TRACKED_STATUSES


def _sanitize_path(file_path: str | None, task_id: int) -> str | None:
    return lifecycle._sanitize_path(file_path, task_id)


def _status_bool(value: Any) -> bool:
    return lifecycle._status_bool(value)


def _exception_message(exc: Exception) -> str:
    return lifecycle._exception_message(exc)


def _is_missing_gid_error(exc: Exception) -> bool:
    return lifecycle.is_missing_gid_error(exc)


def _is_transient_rpc_error(exc: Exception) -> bool:
    return lifecycle.is_transient_rpc_error(exc)


async def _list_v0_tracked_downloads() -> list[dict[str, Any]]:
    return await lifecycle.list_v0_tracked_downloads()


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
    await lifecycle.fail_v0_download_and_cleanup(
        state=state,
        client=client,
        download_id=download_id,
        gid=gid,
        message=message,
        error_code=error_code,
        log_prefix=log_prefix,
    )


async def _complete_v0_download_from_sync(
    *,
    state: AppState,
    client: Aria2Client,
    download: dict[str, Any],
    aria2_status: dict[str, Any],
    completion_gid: str,
    allow_metadata_handoff_defer: bool = True,
) -> None:
    await lifecycle.complete_v0_download_from_sync(
        state=state,
        client=client,
        download=download,
        aria2_status=aria2_status,
        completion_gid=completion_gid,
        allow_metadata_handoff_defer=allow_metadata_handoff_defer,
    )


async def _update_v0_download_from_aria2(
    *,
    state: AppState,
    client: Aria2Client,
    download: dict[str, Any],
    status: dict[str, Any],
) -> None:
    await lifecycle.update_v0_download_from_aria2(
        state=state,
        client=client,
        download=download,
        status=status,
    )


async def _repair_inconsistent_completed_downloads_v0() -> None:
    await lifecycle.repair_inconsistent_completed_downloads_v0()


async def _cleanup_stale_queued_downloads_v0(
    state: AppState,
    grace_seconds: float = STALE_QUEUED_GRACE_SECONDS,
) -> None:
    await lifecycle.cleanup_stale_queued_downloads_v0(
        state=state,
        grace_seconds=grace_seconds,
    )


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

            try:
                status = await client.tell_status(gid)
            except Exception as exc:
                if _is_missing_gid_error(exc):
                    await lifecycle.handle_missing_gid(
                        state=state,
                        client=client,
                        download=download,
                        gid=gid,
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
    """Remove stopped aria2 results only after aria2deck has consumed them."""
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

    actions = 0
    for gid in _extract_gids(stopped):
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
