"""aria2 polling sync for the v0 shared download model."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.aria2.gateway import get_aria2_client
from app.aria2.protocol import Aria2Gateway
from app.services import aria2_lifecycle_service as lifecycle
from app.services import backend_connectivity

logger = logging.getLogger(__name__)

OWNED_STOPPED_RESULT_CLEANUP_BATCH = 50
COMPLETE_REPAIR_GRACE_SECONDS = lifecycle.COMPLETE_REPAIR_GRACE_SECONDS
STALE_QUEUED_GRACE_SECONDS = lifecycle.STALE_QUEUED_GRACE_SECONDS
MISSING_GID_KEYWORDS = lifecycle.MISSING_GID_KEYWORDS
MISSING_GID_PATTERNS = lifecycle.MISSING_GID_PATTERNS
TRANSIENT_RPC_ERROR_KEYWORDS = lifecycle.TRANSIENT_RPC_ERROR_KEYWORDS
V0_SYNC_TRACKED_STATUSES = lifecycle.V0_SYNC_TRACKED_STATUSES


async def sync_tasks(interval: float) -> None:
    """Synchronize aria2 task state into v0 tables."""
    while True:
        try:
            await _sync_tasks_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[Sync] Synchronization round failed")
        await asyncio.sleep(interval)


async def _sync_tasks_once() -> None:
    await lifecycle.repair_inconsistent_completed_downloads_v0()
    client = get_aria2_client()
    downloads = await lifecycle.list_v0_tracked_downloads()
    removable_stopped_gids: set[str] = set()
    saw_backend_ok = False
    saw_backend_fail = False

    async def fetch_and_update(download: dict[str, Any]) -> None:
        nonlocal saw_backend_ok, saw_backend_fail
        gid = str(download.get("aria2_gid") or "")
        if not gid:
            return

        try:
            status = await client.tell_status(gid)
            saw_backend_ok = True
        except Exception as exc:
            if lifecycle.is_missing_gid_error(exc):
                # Backend responded: the GID is gone, not the transport.
                saw_backend_ok = True
                await lifecycle.handle_missing_gid(
                    client=client,
                    download=download,
                    gid=gid,
                )
                return

            if lifecycle.is_transient_rpc_error(exc):
                saw_backend_fail = True
                logger.warning(
                    "[Sync] Failed to fetch GID %s status, retry later: %s", gid, exc
                )
                return

            logger.error(
                "[Sync] Failed to fetch GID %s status, retry later: %s", gid, exc
            )
            return

        handled = await lifecycle.update_v0_download_from_aria2(
            client=client,
            download=download,
            status=status,
        )
        if not handled:
            return
        if str(status.get("status") or "") in {"complete", "error", "removed"}:
            if lifecycle.should_defer_stopped_result_cleanup(download, status):
                return
            removable_stopped_gids.add(gid)

    if downloads:
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
    else:
        # No tracked downloads: still probe backend reachability.
        try:
            await client.get_version()
            saw_backend_ok = True
        except Exception as exc:
            if lifecycle.is_transient_rpc_error(exc):
                saw_backend_fail = True
                logger.warning(
                    "[Sync] Backend reachability probe failed, retry later: %s", exc
                )
            else:
                logger.error(
                    "[Sync] Backend reachability probe failed, retry later: %s", exc
                )

    if saw_backend_ok:
        await backend_connectivity.mark_ok()
    elif saw_backend_fail:
        await backend_connectivity.mark_fail()

    await _cleanup_owned_stopped_results(
        client=client,
        removable_gids=removable_stopped_gids,
        max_actions=OWNED_STOPPED_RESULT_CLEANUP_BATCH,
    )
    await lifecycle.cleanup_stale_queued_downloads_v0()


async def _cleanup_owned_stopped_results(
    client: Aria2Gateway,
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
