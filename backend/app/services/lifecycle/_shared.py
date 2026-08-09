"""Shared private helpers for the lifecycle package.

Extracted from ``aria2_lifecycle_service.py`` (M4 T05) so that the
upcoming lifecycle submodules (coordinator / handoff / completion /
cleanup / repair) share a single definition.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import aiohttp

from app.core.security import sanitize_string
from app.modules.backend.port import BackendPort
from app.repositories.task.downloads import get_global_download_for_generation
from app.repositories.task.user_tasks import get_representative_active_owner_id
from app.services import download_ops
from app.services.aria2_error_messages import prefer_aria2_error_message
from app.services.task_broadcast import broadcast_task_update_to_subscribers

logger = logging.getLogger(__name__)

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
PAUSE_SUCCESS_STATUSES = {"paused"}
UNPAUSE_SUCCESS_STATUSES = {"active", "waiting"}


def _sanitize_path(file_path: str | None, task_id: int) -> str | None:
    if not file_path:
        return None
    try:
        abs_path = Path(file_path)
        return abs_path.name if abs_path.name else file_path
    except (ValueError, OSError) as exc:
        logger.debug("Failed to sanitize path for download %s: %s", task_id, exc)
        return file_path


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


def is_missing_gid_error(exc: Exception) -> bool:
    message = _exception_message(exc)
    if all(keyword in message for keyword in MISSING_GID_KEYWORDS):
        return True
    return any(pattern in message for pattern in MISSING_GID_PATTERNS)


def is_transient_rpc_error(exc: Exception) -> bool:
    if isinstance(exc, (aiohttp.ClientError, TimeoutError, OSError, ConnectionError)):
        return True
    message = _exception_message(exc)
    return any(keyword in message for keyword in TRANSIENT_RPC_ERROR_KEYWORDS)


async def _broadcast_download_update(download_id: int) -> None:
    await broadcast_task_update_to_subscribers(download_id)


async def get_representative_owner_id(download_id: int) -> int | None:
    return await get_representative_active_owner_id(download_id)


async def _requery_after_control_failure(
    *,
    backend: BackendPort,
    download_id: int,
    control_gid: str,
    expected_gid: str,
    success_statuses: set[str],
    failure_error_code: str,
    failure_message: str,
    acquire_lifecycle_lock: bool,
) -> str:
    """Re-query aria2 after a pause/unpause RPC exception (spec §8.3/§8.4).

    Returns one of:
    - ``"success"`` – re-query shows the target state was reached.
    - ``"complete"`` – download has completed.
    - ``"failed"`` – confirmed real control failure, task terminalized.
    - ``"stale"`` – DB GID changed or task already terminal.
    - ``"missing"`` – GID gone while DB still points to it, terminalized.
    - ``"rpc_unavailable"`` – transient RPC error, state undetermined.
    """
    # Deferred to avoid an import cycle: cleanup.py imports helpers from
    # this module.
    from app.services.lifecycle.cleanup import fail_download_and_reclaim

    try:
        re_status = await backend.tell_status(control_gid)
    except Exception as re_exc:
        if is_missing_gid_error(re_exc):
            current = await get_global_download_for_generation(
                download_id, expected_gid
            )
            if current is None:
                return "stale"
            total = int(current.get("total_bytes") or 0)
            code = "gid_missing" if total > 0 else "unknown_size"
            msg = (
                "暂停下载时 GID 已丢失"
                if failure_error_code == "growth_pause_failed"
                else "恢复下载时 GID 已丢失"
            )
            await fail_download_and_reclaim(
                backend=backend,
                download_id=download_id,
                message=msg,
                error_code=code,
                expected_gid=expected_gid,
                writer_gid=control_gid,
                acquire_lifecycle_lock=acquire_lifecycle_lock,
                log_prefix="[Resize]",
            )
            return "missing"
        if is_transient_rpc_error(re_exc):
            logger.debug(
                "[Resize] Transient RPC on re-query download_id=%s", download_id
            )
            return "rpc_unavailable"
        current = await get_global_download_for_generation(
            download_id, expected_gid
        )
        if current is None:
            return "stale"
        await fail_download_and_reclaim(
            backend=backend,
            download_id=download_id,
            message=failure_message,
            error_code=failure_error_code,
            expected_gid=expected_gid,
            writer_gid=control_gid,
            acquire_lifecycle_lock=acquire_lifecycle_lock,
            log_prefix="[Resize]",
        )
        return "failed"

    re_raw = str(re_status.get("status") or "")
    if re_raw in success_statuses:
        return "success"
    if re_raw == "complete":
        return "complete"

    # active/waiting (for pause) or paused (for unpause) → real failure
    current = await get_global_download_for_generation(
        download_id, expected_gid
    )
    if current is None:
        return "stale"
    await fail_download_and_reclaim(
        backend=backend,
        download_id=download_id,
        message=failure_message,
        error_code=failure_error_code,
        expected_gid=expected_gid,
        writer_gid=control_gid,
        acquire_lifecycle_lock=acquire_lifecycle_lock,
        log_prefix="[Resize]",
    )
    return "failed"
