"""aria2 polling sync — trigger-only observer for the lifecycle coordinator.

Sync enumerates live attempts, fetches an observed ``tell_status`` snapshot
for each ``current_gid``, and delegates every state transition to
``reconcile_attempt_signal``.  It never writes DB rows or deletes files
directly.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.aria2.gateway import get_aria2_client
from app.aria2.protocol import Aria2Gateway
from app.domain.lifecycle import ReconcileResult
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
    # Legacy module-level repair/stale-queued helpers still live in lifecycle;
    # migrating them into coordinator calls requires changing lifecycle.py
    # (deferred to T18/T20).  Sync itself performs no direct DB writes here.
    await lifecycle.repair_inconsistent_completed_downloads_v0()
    client = get_aria2_client()
    downloads = await lifecycle.list_v0_tracked_downloads()

    removable_gids: set[str] = set()
    saw_backend_ok = False
    saw_backend_fail = False

    async def fetch_and_reconcile(download: dict[str, Any]) -> None:
        nonlocal saw_backend_ok, saw_backend_fail
        gid = str(download.get("aria2_gid") or "")
        if not gid:
            return

        observed_status: dict[str, Any] | None = None
        observed_error: Exception | None = None
        try:
            observed_status = await client.tell_status(gid)
            saw_backend_ok = True
        except Exception as exc:
            if lifecycle.is_missing_gid_error(exc):
                saw_backend_ok = True
            elif lifecycle.is_transient_rpc_error(exc):
                saw_backend_fail = True
                logger.warning(
                    "[Sync] Transient RPC for gid=%s, will retry next round: %s",
                    gid,
                    exc,
                )
                return
            else:
                logger.error(
                    "[Sync] Failed to fetch gid=%s status: %s", gid, exc
                )
            observed_error = exc

        result = await lifecycle.reconcile_attempt_signal(
            client=client,
            observed_gid=gid,
            event=None,
            observed_status=observed_status,
            observed_error=observed_error,
            log_prefix="[Sync]",
        )
        if result in (ReconcileResult.TERMINALIZED, ReconcileResult.COMPLETED):
            removable_gids.add(gid)

    if downloads:
        results = await asyncio.gather(
            *[fetch_and_reconcile(d) for d in downloads],
            return_exceptions=True,
        )
        for index, result in enumerate(results):
            if isinstance(result, Exception):
                logger.warning(
                    "[Sync] reconcile failed download_id=%s error=%s",
                    downloads[index]["id"],
                    result,
                )
    else:
        try:
            await client.get_version()
            saw_backend_ok = True
        except Exception as exc:
            if lifecycle.is_transient_rpc_error(exc):
                saw_backend_fail = True
                logger.warning(
                    "[Sync] Backend reachability probe failed: %s", exc
                )
            else:
                logger.error(
                    "[Sync] Backend reachability probe failed: %s", exc
                )

    if saw_backend_ok:
        await backend_connectivity.mark_ok()
    elif saw_backend_fail:
        await backend_connectivity.mark_fail()

    await _cleanup_owned_stopped_results(
        client=client,
        removable_gids=removable_gids,
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
