"""Startup/sync repair helpers extracted from ``aria2_lifecycle_service.py`` (M4 T09).

Hosts the legacy HTTP download reconciliation, the completed-without-index
repair, and the stale queued cleanup run at startup or by the sync loop.
"""

from __future__ import annotations

import logging
from typing import Any

from app.aria2.protocol import Aria2Gateway
from app.modules.backend.port import BackendPort
from app.core.config import get_internal_base_url
from app.core.time_utils import now_ms
from app.repositories.task.downloads import (
    list_active_like_http_downloads,
    list_inconsistent_completed_download_ids,
    list_stale_queued_download_ids,
    list_tracked_global_downloads,
)
from app.services.lifecycle._shared import (
    _broadcast_download_update,
    is_missing_gid_error,
)
from app.services.lifecycle.cleanup import fail_download_and_reclaim
from app.services.lifecycle.coordinator import V0_SYNC_TRACKED_STATUSES

logger = logging.getLogger(__name__)

COMPLETE_REPAIR_GRACE_SECONDS = 30.0
STALE_QUEUED_GRACE_SECONDS = 300.0
LEGACY_HTTP_STOP_ERROR = "无法安全停止遗留 HTTP 下载任务"


async def list_v0_tracked_downloads() -> list[dict[str, Any]]:
    return await list_tracked_global_downloads(V0_SYNC_TRACKED_STATUSES)


def _has_only_internal_gateway_uris(
    uris: object,
    *,
    internal_base: str,
    download_id: int,
) -> bool:
    if not isinstance(uris, list) or not uris:
        return False
    prefix = f"{internal_base}/_internal/fetch/{download_id}/"
    for item in uris:
        uri = item.get("uri") if isinstance(item, dict) else None
        index = (
            uri[len(prefix) :]
            if isinstance(uri, str) and uri.startswith(prefix)
            else ""
        )
        if not index or not index.isascii() or not index.isdigit():
            return False
    return True


async def _stop_legacy_http_job(
    client: Aria2Gateway,
    *,
    download_id: int,
    gid: str,
) -> None:
    try:
        await client.force_remove(gid)
    except Exception as exc:
        if not is_missing_gid_error(exc):
            logger.error(
                "[Startup] Failed to stop legacy HTTP job download_id=%s "
                "error_type=%s",
                download_id,
                type(exc).__name__,
            )
            raise RuntimeError(LEGACY_HTTP_STOP_ERROR) from None

    try:
        await client.remove_download_result(gid)
    except Exception as exc:
        logger.warning(
            "[Startup] Failed to remove legacy HTTP result download_id=%s "
            "error_type=%s",
            download_id,
            type(exc).__name__,
        )


async def reconcile_legacy_http_downloads_v0(client: Aria2Gateway) -> int:
    internal_base = get_internal_base_url()
    failed_count = 0
    for download in await list_active_like_http_downloads():
        download_id = int(download["id"])
        gid = str(download.get("aria2_gid") or "")
        uris: object = None
        if gid:
            try:
                uris = await client.get_uris(gid)
            except Exception as exc:
                logger.warning(
                    "[Startup] HTTP URI verification failed download_id=%s error_type=%s",
                    download_id,
                    type(exc).__name__,
                )

        valid = _has_only_internal_gateway_uris(
            uris,
            internal_base=internal_base,
            download_id=download_id,
        )
        if valid:
            continue

        if gid:
            await _stop_legacy_http_job(
                client,
                download_id=download_id,
                gid=gid,
            )
        changed = await fail_download_and_reclaim(
            backend=client,
            download_id=download_id,
            message="HTTP 下载未通过内部网关校验，已停止",
            error_code="unsafe_http_download_uri",
            expected_gid=gid or None,
            writer_gid=None,
            clear_gid=bool(gid),
            log_prefix="[Startup]",
        )
        if not changed:
            continue
        failed_count += 1
    return failed_count


async def repair_inconsistent_completed_downloads_v0(
    backend: BackendPort | None = None,
) -> None:
    if backend is None:
        from app.aria2.gateway import get_aria2_client

        backend = get_aria2_client()
    threshold_ms = now_ms() - int(COMPLETE_REPAIR_GRACE_SECONDS * 1000)
    for snapshot in await list_inconsistent_completed_download_ids(threshold_ms):
        download_id = int(snapshot["id"])
        logger.warning(
            "[Sync] Completed v0 download was not indexed, failing id=%s", download_id
        )
        gid = snapshot.get("aria2_gid")
        changed = await fail_download_and_reclaim(
            backend=backend,
            download_id=download_id,
            message="下载完成但文件未入库",
            error_code="completion_not_indexed",
            expected_gid=gid if isinstance(gid, str) and gid else None,
            writer_gid=gid if isinstance(gid, str) and gid else None,
            expected_statuses=("completed",),
            log_prefix="[Sync]",
        )
        if changed:
            await _broadcast_download_update(download_id)


async def cleanup_stale_queued_downloads_v0(
    grace_seconds: float = STALE_QUEUED_GRACE_SECONDS,
    backend: BackendPort | None = None,
) -> None:
    if backend is None:
        from app.aria2.gateway import get_aria2_client

        backend = get_aria2_client()
    threshold_ms = now_ms() - int(grace_seconds * 1000)
    for download_id in await list_stale_queued_download_ids(threshold_ms):
        logger.warning("[Sync] Cleaning stale v0 queued download_id=%s", download_id)
        changed = await fail_download_and_reclaim(
            backend=backend,
            download_id=download_id,
            message="任务提交超时，已自动清理",
            error_code="submit_timeout",
            expected_gid=None,
            writer_gid=None,
            expected_statuses=("queued",),
            log_prefix="[Sync]",
        )
        if changed:
            await _broadcast_download_update(download_id)
