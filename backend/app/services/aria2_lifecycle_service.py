"""Business lifecycle handling for aria2 listener and polling inputs."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp

from app.aria2.protocol import Aria2Gateway
from app.core.config import get_internal_base_url
from app.core.security import sanitize_string
from app.domain.lifecycle import ReconcileResult
from app.domain.status import (
    ACTIVE_USER_TASK_STATUSES,
    FAILABLE_GLOBAL_DOWNLOAD_STATUSES,
    TERMINAL_DOWNLOAD_STATUSES,
)
from app.repositories.downloads import (
    claim_attempt_terminal,
    claim_terminal_reclaim,
    clear_terminal_download_gid,
    get_global_download_by_gid,
    get_global_download_for_generation,
    get_global_download_status_snapshot,
    get_representative_active_owner_id,
    guarded_update_download_and_active_user_tasks,
    guarded_update_global_download,
    list_active_like_http_downloads,
    list_inconsistent_completed_download_ids,
    list_stale_queued_download_ids,
    list_tracked_global_downloads,
    now_ms,
    reconcile_download_size,
)
from app.services import download_ops
from app.services.download_service import (
    candidate_size_from_status,
    complete_global_download_locked,
    get_disk_available_bytes,
    get_download_lifecycle_lock,
)
from app.services.failed_task_cleanup import (
    CleanupResult,
    cleanup_with_claim,
)
from app.services.settings_service import get_max_task_size
from app.services.aria2_error_messages import prefer_aria2_error_message
from app.services.storage import cleanup_task_download_dir, get_downloading_dir
from app.services.task_broadcast import broadcast_task_update_to_subscribers
from app.services.task_projection import (
    METADATA_NAME_PREFIX,
    has_live_bt_evidence,
    is_bt_resource_kind,
    is_metadata_phase_status,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResolveResult:
    """Pure resolution of a GID observation to an attempt (spec §6.3).

    ``source_gid`` is ``None`` for direct matches (``observed_gid`` ==
    ``current_gid``), or set to the source GID for a handoff candidate
    where ``observed_status`` contains ``following=source_gid``.
    """

    download: dict[str, Any]
    observed_gid: str
    source_gid: str | None

    @property
    def is_handoff_candidate(self) -> bool:
        return self.source_gid is not None


COMPLETE_SOURCE_RETRY_COUNT = 4
COMPLETE_SOURCE_RETRY_INTERVAL = 0.5
COMPLETE_REPAIR_GRACE_SECONDS = 30.0
STALE_QUEUED_GRACE_SECONDS = 300.0
LEGACY_HTTP_STOP_ERROR = "无法安全停止遗留 HTTP 下载任务"
DOWNLOAD_DIR_NOT_FOUND_MESSAGE = "下载完成但下载目录不存在"
DOWNLOAD_FILE_NOT_FOUND_MESSAGE = "下载完成但下载文件未找到"
COMPLETED_SIZE_MISMATCH_MESSAGE = "下载完成但文件大小不匹配"
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


async def list_v0_tracked_downloads() -> list[dict[str, Any]]:
    return await list_tracked_global_downloads(V0_SYNC_TRACKED_STATUSES)


async def _broadcast_download_update(download_id: int) -> None:
    await broadcast_task_update_to_subscribers(download_id)


async def get_representative_owner_id(download_id: int) -> int | None:
    return await get_representative_active_owner_id(download_id)


async def _reclaim_terminal_with_claim(
    *,
    client: Aria2Gateway,
    download_id: int,
    gid: str,
    log_prefix: str,
) -> None:
    """Reclaim an already-terminal attempt using a repair claim (spec §10.4).

    Used by the coordinator after ``reconcile_download_size`` has already
    terminalized the row (disk_budget / max_task_size / no_subscribers).
    Obtains a ``RepairClaim`` via ``claim_terminal_reclaim``, then delegates
    physical cleanup to ``cleanup_with_claim``.  No ``skip_status_check``.
    """
    claim = await claim_terminal_reclaim(
        attempt_id=download_id,
        expected_gid=gid,
    )
    if claim is None:
        logger.debug(
            "%s Reclaim claim stale: attempt_id=%s gid=%s",
            log_prefix, download_id, gid,
        )
        return
    await cleanup_with_claim(client, claim, log_prefix=log_prefix)


_WRITER_GID_UNSET: object = object()


async def _fail_download_and_reclaim_operation(
    *,
    client: Aria2Gateway,
    download_id: int,
    message: str,
    error_code: str | None,
    expected_gid: str | None,
    writer_gid: str | None,
    expected_statuses: Iterable[str],
    clear_gid: bool,
    acquire_lifecycle_lock: bool,
    log_prefix: str,
) -> bool:
    """Claim terminal state, then reclaim physical resources.

    Implements the two-step model (spec §10.1):
    1. ``claim_attempt_terminal`` CAS — sole authorization to terminalize.
    2. ``cleanup_with_claim`` — physical reclamation under the claim.

    Claim failure means no authorization: no force_remove, no directory
    deletion, no error overwrite.  Cleanup failure does not roll back the
    terminal state (spec §10.6).
    """

    async def _claim_and_reclaim() -> bool:
        if (
            expected_gid is not None
            and writer_gid is not None
            and expected_gid != writer_gid
        ):
            writer_gids: list[str] = [expected_gid, writer_gid]
            result_gids: list[str] = [expected_gid, writer_gid]
        elif writer_gid is not None:
            writer_gids = [writer_gid]
            result_gids = [writer_gid]
        else:
            writer_gids = []
            result_gids = []

        claim = await claim_attempt_terminal(
            attempt_id=download_id,
            expected_gid=expected_gid,
            terminal_status="failed",
            error_code=error_code,
            error_message=message,
            expected_statuses=tuple(expected_statuses),
            writer_gids=writer_gids,
            result_gids=result_gids,
        )
        if claim is None:
            return False

        await cleanup_with_claim(client, claim, log_prefix=log_prefix)
        return True

    if not acquire_lifecycle_lock:
        return await _claim_and_reclaim()

    lifecycle_lock = await get_download_lifecycle_lock(download_id)
    async with lifecycle_lock:
        return await _claim_and_reclaim()


async def fail_download_and_reclaim(
    *,
    client: Aria2Gateway,
    download_id: int,
    message: str,
    error_code: str | None = None,
    expected_gid: str | None = None,
    writer_gid: str | None | object = _WRITER_GID_UNSET,
    expected_statuses: Iterable[str] = FAILABLE_GLOBAL_DOWNLOAD_STATUSES,
    clear_gid: bool = False,
    acquire_completion_lock: bool = False,
    acquire_lifecycle_lock: bool = True,
    log_prefix: str = "[Fail]",
) -> bool:
    """Mark failed and best-effort reclaim aria2 writer + downloading/<id>.

    Does not broadcast. Dual-gid handoff uses writer_gid != expected_gid.
    ``acquire_completion_lock`` is accepted for backward compatibility but
    is a no-op; lifecycle protection is solely via ``acquire_lifecycle_lock``.
    """
    if writer_gid is _WRITER_GID_UNSET:
        effective_writer: str | None = expected_gid
    else:
        effective_writer = writer_gid  # type: ignore[assignment]

    operation = asyncio.create_task(
        _fail_download_and_reclaim_operation(
            client=client,
            download_id=download_id,
            message=message,
            error_code=error_code,
            expected_gid=expected_gid,
            writer_gid=effective_writer,
            expected_statuses=expected_statuses,
            clear_gid=clear_gid,
            acquire_lifecycle_lock=acquire_lifecycle_lock,
            log_prefix=log_prefix,
        )
    )
    try:
        return await asyncio.shield(operation)
    except asyncio.CancelledError:
        await asyncio.shield(operation)
        raise



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
            client=client,
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


def _list_task_dir_entries(task_dir: Path) -> list[Path]:
    if not task_dir.exists() or not task_dir.is_dir():
        return []
    try:
        return [p for p in task_dir.iterdir() if not p.name.endswith(".aria2")]
    except OSError as exc:
        logger.error("Failed to list task directory %s: %s", task_dir, exc)
        return []


def source_not_found_error(task_dir: Path) -> tuple[str, str]:
    if not task_dir.exists() or not task_dir.is_dir():
        return "download_dir_not_found", DOWNLOAD_DIR_NOT_FOUND_MESSAGE
    return "download_file_not_found", DOWNLOAD_FILE_NOT_FOUND_MESSAGE


def expected_completed_size(
    aria2_status: dict[str, Any],
    source_path: Path,
) -> int | None:
    files = aria2_status.get("files", [])
    if isinstance(files, list) and files:
        expected = 0
        has_length = False
        has_file_item = False
        has_selected_file = False
        for item in files:
            if not isinstance(item, dict):
                continue
            has_file_item = True
            if "selected" in item and not _status_bool(item.get("selected")):
                continue
            has_selected_file = True
            raw_length = item.get("length")
            length = download_ops.safe_int(raw_length, default=-1)
            if length < 0:
                continue
            expected += length
            has_length = True
        if has_length:
            return expected
        if has_file_item and not has_selected_file:
            return 0

    total_length = download_ops.safe_int(aria2_status.get("totalLength"), default=-1)
    if total_length < 0:
        return None
    if source_path.is_dir() or total_length > 0:
        return total_length
    return None


def completed_size_mismatch_error(
    *,
    source_path: Path,
    expected_bytes: int,
    actual_bytes: int,
) -> tuple[str, str]:
    logger.error(
        "[WS] Completed download payload size mismatch path=%s expected=%s actual=%s",
        source_path,
        expected_bytes,
        actual_bytes,
    )
    return "completed_size_mismatch", COMPLETED_SIZE_MISMATCH_MESSAGE


def resolve_complete_source_path(
    task_dir: Path,
    files: list[dict[str, Any]],
    task_name: str | None,
) -> Path | None:
    task_candidates: list[Path] = []
    external_candidates: list[Path] = []

    for file_item in files:
        raw_path = file_item.get("path")
        if not raw_path or not isinstance(raw_path, str):
            continue

        file_path = Path(raw_path)
        try:
            rel_path = file_path.relative_to(task_dir)
            if rel_path.parts:
                task_candidates.append(task_dir / rel_path.parts[0])
            else:
                task_candidates.append(task_dir)
            continue
        except (OSError, ValueError) as exc:
            logger.debug(
                "Failed to resolve path %s relative to %s: %s",
                file_path,
                task_dir,
                exc,
            )

        external_candidates.append(file_path)

    existing_task_candidates: list[Path] = []
    seen: set[Path] = set()
    for candidate in task_candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if candidate.exists():
            existing_task_candidates.append(candidate)

    if len(existing_task_candidates) > 1 and task_dir.exists():
        return task_dir
    if len(existing_task_candidates) == 1:
        return existing_task_candidates[0]

    task_entries = _list_task_dir_entries(task_dir)
    if len(task_entries) > 1:
        return task_dir
    if len(task_entries) == 1:
        return task_entries[0]

    for candidate in external_candidates:
        if candidate.exists():
            return candidate

    if task_name and task_dir.exists():
        named_candidate = task_dir / task_name
        if named_candidate.exists():
            return named_candidate

    return None


async def resolve_complete_source_with_retry(
    *,
    completion_gid: str | None,
    task_dir: Path,
    files: list[dict[str, Any]],
    task_name: str | None,
    client: Aria2Gateway | None,
) -> Path | None:
    latest_files = files

    for attempt in range(COMPLETE_SOURCE_RETRY_COUNT):
        source = resolve_complete_source_path(task_dir, latest_files, task_name)
        if source:
            return source

        if not completion_gid or client is None:
            if attempt < COMPLETE_SOURCE_RETRY_COUNT - 1:
                await asyncio.sleep(COMPLETE_SOURCE_RETRY_INTERVAL)
            continue

        try:
            refreshed_status = await client.tell_status(completion_gid)
            refreshed_files = refreshed_status.get("files", [])
            if isinstance(refreshed_files, list):
                latest_files = refreshed_files
        except Exception as exc:
            logger.debug(
                "[WS] Failed to refresh complete status gid=%s error=%s",
                completion_gid,
                exc,
            )

        if attempt < COMPLETE_SOURCE_RETRY_COUNT - 1:
            await asyncio.sleep(COMPLETE_SOURCE_RETRY_INTERVAL)

    return None


async def _remove_download_result_best_effort(
    client: Aria2Gateway,
    gid: str,
    log_prefix: str,
) -> None:
    try:
        await client.remove_download_result(gid)
    except Exception as exc:
        logger.debug(
            "%s Failed to remove aria2 result gid=%s error=%s", log_prefix, gid, exc
        )


async def _stop_untracked_gid_best_effort(
    client: Aria2Gateway,
    gid: str,
    log_prefix: str,
) -> None:
    try:
        await client.force_remove(gid)
    except Exception as exc:
        logger.debug(
            "%s Failed to stop untracked aria2 gid=%s error=%s",
            log_prefix, gid, exc,
        )
    await _remove_download_result_best_effort(client, gid, log_prefix)


async def _guarded_update_global_download(
    download_id: int,
    values: Mapping[str, Any],
    *,
    expected_gid: str,
) -> dict[str, Any] | None:
    return await guarded_update_global_download(
        download_id,
        dict(values),
        expected_gid=expected_gid,
        return_row=True,
    )


async def update_download_and_active_user_tasks(
    *,
    download_id: int,
    expected_gid: str,
    global_values: dict[str, Any],
    user_status: str | None = None,
    force_display_name: bool = False,
) -> bool:
    updated = await guarded_update_download_and_active_user_tasks(
        download_id,
        global_values,
        expected_gid=expected_gid,
        user_status=user_status,
        display_name=global_values.get("display_name"),
        force_display_name=force_display_name,
    )
    return updated is not None


PAUSE_SUCCESS_STATUSES = {"paused"}
UNPAUSE_SUCCESS_STATUSES = {"active", "waiting"}



async def _requery_after_control_failure(
    *,
    client: Aria2Gateway,
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
    try:
        re_status = await client.tell_status(control_gid)
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
                client=client,
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
            client=client,
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
        client=client,
        download_id=download_id,
        message=failure_message,
        error_code=failure_error_code,
        expected_gid=expected_gid,
        writer_gid=control_gid,
        acquire_lifecycle_lock=acquire_lifecycle_lock,
        log_prefix="[Resize]",
    )
    return "failed"


async def coordinate_reported_size(
    *,
    client: Aria2Gateway,
    download: dict[str, Any],
    expected_gid: str,
    control_gid: str,
    status: Mapping[str, Any],
    require_trusted_total: bool = False,
    resume_after_admission: bool = True,
    acquire_lifecycle_lock: bool = True,
) -> dict[str, Any]:
    """Lock-internal size admission with idempotent pause/unpause (spec §8.2).

    When ``acquire_lifecycle_lock=False`` the caller must already hold the
    attempt lock.  Outcomes:

    - ``admitted`` – size accepted, proceed with projection.
    - ``stale`` – DB GID changed, no action.
    - ``unknown_size`` – no trusted size, caller terminalizes.
    - ``complete`` – re-query shows complete, route to completion.
    - ``rpc_unavailable`` – transient RPC, skip size admission.
    - ``terminalized`` – already terminalized (growth failure or missing GID).
    - ``disk_budget`` / ``max_task_size`` / ``no_subscribers`` – row already
      terminal in DB, caller does physical reclaim.
    """
    download_id = int(download["id"])

    current = await get_global_download_for_generation(
        download_id, expected_gid
    )
    if current is None:
        return {"outcome": "stale", "paused_by_us": False}

    require_trusted_total = require_trusted_total or not bool(
        current.get("size_known")
    )
    candidate = candidate_size_from_status(
        status, require_trusted_total=require_trusted_total
    )
    if candidate is None:
        return {"outcome": "unknown_size", "paused_by_us": False}
    old_size = int(current.get("total_bytes") or 0)
    raw_status = str(status.get("status") or "")

    paused_by_us = False

    # Pause for safe size accounting when size grew (spec §8.2).
    # Already paused or complete → skip pause (rule 1).
    if candidate[0] > old_size and raw_status not in {"paused", "complete"}:
        try:
            await client.pause(control_gid)
            paused_by_us = True
        except Exception:
            result_str = await _requery_after_control_failure(
                client=client,
                download_id=download_id,
                control_gid=control_gid,
                expected_gid=expected_gid,
                success_statuses=PAUSE_SUCCESS_STATUSES,
                failure_error_code="growth_pause_failed",
                failure_message="任务大小增长时无法安全暂停",
                acquire_lifecycle_lock=acquire_lifecycle_lock,
            )
            if result_str == "success":
                paused_by_us = True
            elif result_str == "complete":
                return {"outcome": "complete", "paused_by_us": False}
            elif result_str == "rpc_unavailable":
                return {"outcome": "rpc_unavailable", "paused_by_us": False}
            else:
                # failed, stale, missing → already terminalized or no action
                return {"outcome": "terminalized", "paused_by_us": False}

    # Budget / reservation atomic operation (spec §8.2).
    result = await reconcile_download_size(
        download_id=download_id,
        expected_gid=expected_gid,
        candidate_bytes=candidate[0],
        completed_bytes=candidate[1],
        size_limit_bytes=int(
            current.get("size_limit_bytes") or get_max_task_size()
        ),
        disk_available_bytes=get_disk_available_bytes,
    )
    result["paused_by_us"] = paused_by_us

    if not result.admitted:
        outcome = str(result.get("outcome") or "")
        if outcome == "stale":
            return result
        # disk_budget / max_task_size / no_subscribers: row already terminal
        # in DB.  Return outcome so caller does physical reclaim.
        return result

    # Idempotent unpause only when we confirmed the pause (spec §8.4).
    if paused_by_us and resume_after_admission:
        try:
            await client.unpause(control_gid)
        except Exception:
            result_str = await _requery_after_control_failure(
                client=client,
                download_id=download_id,
                control_gid=control_gid,
                expected_gid=expected_gid,
                success_statuses=UNPAUSE_SUCCESS_STATUSES,
                failure_error_code="growth_unpause_failed",
                failure_message="任务大小调整后恢复下载失败",
                acquire_lifecycle_lock=acquire_lifecycle_lock,
            )
            if result_str == "success":
                pass  # idempotent success
            elif result_str == "complete":
                result["outcome"] = "complete"
            elif result_str == "rpc_unavailable":
                result["outcome"] = "rpc_unavailable"
            else:
                # failed, stale, missing → terminalized
                result["outcome"] = "terminalized"

    return result


# -------------------------------------------------------------------------
# Handoff (spec §9): explicit followedBy/following GID handoff inside lock.
# -------------------------------------------------------------------------


async def _handoff_locked(
    *,
    client: Aria2Gateway,
    attempt_id: int,
    source_gid: str,
    payload_gid: str,
    snapshot: dict[str, Any],
    download: dict[str, Any],
    log_prefix: str,
    _payload_status: dict[str, Any] | None = None,
) -> tuple[ReconcileResult, tuple[str, dict[str, Any]] | None]:
    """Lock-internal handoff from source GID to payload GID (spec §9.2).

    Caller must already hold the attempt lock.

    Returns ``(result, complete_dispatch)`` where *complete_dispatch* is
    ``(completion_gid, completion_status)`` when the caller should run the
    completion path after releasing the lock, or ``None``.

    When ``_payload_status`` is provided, the payload's tell_status has
    already been fetched by the caller outside the lock.  When ``None``,
    the function falls back to fetching it inside the lock (legacy callers).

    Only accepts explicit Aria2 signals:
    - source status contains ``followedBy=[payload_gid]``
    - payload status contains ``following=source_gid``
    """
    current_gid = str(snapshot.get("aria2_gid") or "")
    status_before = str(snapshot.get("status") or "")

    # 1. Fencing: current_gid == source_gid and live.
    if current_gid != source_gid:
        if current_gid == payload_gid:
            logger.debug(
                "%s Handoff idempotent: already switched to payload %s",
                log_prefix,
                payload_gid,
            )
            return ReconcileResult.STALE, None
        logger.info(
            "%s Handoff stale: current=%s source=%s attempt_id=%s",
            log_prefix,
            current_gid,
            source_gid,
            attempt_id,
        )
        return ReconcileResult.STALE, None

    if status_before in TERMINAL_DOWNLOAD_STATUSES:
        logger.debug(
            "%s Handoff: already terminal attempt_id=%s status=%s",
            log_prefix,
            attempt_id,
            status_before,
        )
        return ReconcileResult.ALREADY_TERMINAL, None

    # 2. payload_gid confirmed by caller (explicit followedBy/following).

    # 3. Query payload latest status (or use pre-fetched status).
    payload_status = _payload_status
    if payload_status is None:
        try:
            payload_status = await client.tell_status(payload_gid)
        except Exception as exc:
            if is_transient_rpc_error(exc) or is_missing_gid_error(exc):
                logger.debug(
                    "%s Handoff payload tell_status unavailable, waiting "
                    "attempt_id=%s payload=%s error=%s",
                    log_prefix,
                    attempt_id,
                    payload_gid,
                    exc,
                )
                return ReconcileResult.WAITING, None
            raise

    raw_status = str(payload_status.get("status") or "")

    # 4. Unknown-size rules (spec §9.2: payload non-complete, no trusted size).
    #
    # When size_known=True the size was already admitted at creation time.
    # A transient totalLength=0 from the payload means aria2 has not yet
    # reported the payload size.  Wait for the next reconcile rather than
    # terminalizing or committing with stale data.
    require_trusted = not bool(download.get("size_known"))
    size_candidate = candidate_size_from_status(
        payload_status, require_trusted_total=require_trusted
    )

    if size_candidate is None and not require_trusted and raw_status != "complete":
        logger.debug(
            "%s Handoff payload not yet reporting size (size_known), "
            "waiting attempt_id=%s",
            log_prefix,
            attempt_id,
        )
        return ReconcileResult.WAITING, None

    if size_candidate is None and require_trusted and raw_status != "complete":
        if raw_status in ("waiting", "paused"):
            logger.debug(
                "%s Handoff payload unknown size, %s → waiting attempt_id=%s",
                log_prefix,
                raw_status,
                attempt_id,
            )
            return ReconcileResult.WAITING, None

        if raw_status == "active":
            # Pause and confirm (spec §9.2).
            try:
                await client.pause(payload_gid)
            except Exception:
                pass
            try:
                re_status = await client.tell_status(payload_gid)
            except Exception as re_exc:
                if is_transient_rpc_error(re_exc) or is_missing_gid_error(
                    re_exc
                ):
                    return ReconcileResult.WAITING, None
                raise
            re_raw = str(re_status.get("status") or "")
            if re_raw == "paused":
                return ReconcileResult.WAITING, None
            if re_raw == "complete":
                payload_status = re_status
                raw_status = "complete"
            else:
                # Pause not confirmed, fencing still matches (spec §9.2).
                logger.warning(
                    "%s Handoff unknown size: pause not confirmed, "
                    "terminalizing attempt_id=%s payload=%s",
                    log_prefix,
                    attempt_id,
                    payload_gid,
                )
                changed = await fail_download_and_reclaim(
                    client=client,
                    download_id=attempt_id,
                    message="磁力任务 payload 无法确认可信文件大小",
                    error_code="handoff_unknown_size",
                    expected_gid=source_gid,
                    writer_gid=payload_gid,
                    acquire_lifecycle_lock=False,
                    log_prefix=log_prefix,
                )
                if changed:
                    await _broadcast_download_update(attempt_id)
                    return ReconcileResult.TERMINALIZED, None
                return ReconcileResult.STALE, None

        # Other statuses (error/removed) → waiting for next reconcile.
        return ReconcileResult.WAITING, None

    # 5. Trusted-size admission (spec §9.2 step 5).
    admission = await coordinate_reported_size(
        client=client,
        download=download,
        expected_gid=source_gid,
        control_gid=payload_gid,
        status=payload_status,
        require_trusted_total=require_trusted,
        resume_after_admission=False,
        acquire_lifecycle_lock=False,
    )
    outcome = str(admission.get("outcome") or "")

    if outcome == "stale":
        return ReconcileResult.STALE, None
    if outcome == "rpc_unavailable":
        return ReconcileResult.WAITING, None
    if outcome == "terminalized":
        await _broadcast_download_update(attempt_id)
        return ReconcileResult.TERMINALIZED, None

    if outcome not in ("admitted", "complete"):
        # Admission rejected (unknown_size / disk_budget / max_task_size / …).
        changed = await fail_download_and_reclaim(
            client=client,
            download_id=attempt_id,
            message="磁力任务准入被拒绝",
            error_code=outcome if outcome else "admission_rejected",
            expected_gid=source_gid,
            writer_gid=payload_gid,
            acquire_lifecycle_lock=False,
            log_prefix=log_prefix,
        )
        if changed:
            await _broadcast_download_update(attempt_id)
            return ReconcileResult.TERMINALIZED, None
        return ReconcileResult.STALE, None

    # 6. Condition update: source → payload (spec §9.2 step 7).
    mapped_status = download_ops.map_aria2_status(payload_status)
    if raw_status == "complete":
        mapped_status = "active"

    display_name_fallback = str(
        download.get("display_name") or download.get("source_uri") or ""
    )
    progress = download_ops.map_progress_values(
        payload_status, display_name_fallback
    )

    global_values: dict[str, Any] = {
        "aria2_gid": payload_gid,
        "status": mapped_status,
        "completed_bytes": progress.get("completed_bytes", 0),
        "total_bytes": progress.get("total_bytes", 0),
    }
    if progress.get("display_name"):
        global_values["display_name"] = progress["display_name"]
    if str(download.get("resource_kind") or "") != "torrent":
        global_values["resource_kind"] = "torrent"
    bt_hash = download_ops.bt_info_hash_from_status(payload_status)
    if bt_hash:
        global_values["bt_info_hash"] = bt_hash

    updated = await guarded_update_download_and_active_user_tasks(
        attempt_id,
        global_values,
        expected_gid=source_gid,
        user_status=mapped_status,
        display_name=progress.get("display_name"),
        force_display_name=True,
    )

    if updated is None:
        # CAS failed → stale, do not cleanup payload (spec §9.2 step 8).
        logger.info(
            "%s Handoff CAS failed (stale) attempt_id=%s",
            log_prefix,
            attempt_id,
        )
        return ReconcileResult.STALE, None

    logger.info(
        "%s Handoff committed: %s → %s attempt_id=%s status=%s",
        log_prefix,
        source_gid,
        payload_gid,
        attempt_id,
        mapped_status,
    )

    # 7. Best-effort remove source stopped result (spec §9.2 step 9).
    if source_gid != payload_gid:
        await _remove_download_result_best_effort(
            client, source_gid, log_prefix
        )

    # 8. Resume payload if needed (spec §9.2 step 10).
    should_unpause = raw_status == "paused" or bool(
        admission.get("paused_by_us")
    )
    if should_unpause and raw_status != "complete":
        try:
            await client.unpause(payload_gid)
            # Only re-project when the payload itself was paused. An internal
            # size-admission pause of a waiting payload must not overwrite the
            # committed waiting status with active.
            if raw_status == "paused":
                await guarded_update_download_and_active_user_tasks(
                    attempt_id,
                    {"status": "active"},
                    expected_gid=payload_gid,
                    user_status="active",
                )
        except Exception:
            result_str = await _requery_after_control_failure(
                client=client,
                download_id=attempt_id,
                control_gid=payload_gid,
                expected_gid=payload_gid,
                success_statuses=UNPAUSE_SUCCESS_STATUSES,
                failure_error_code="unpause_failed",
                failure_message="磁力任务准入后恢复下载失败",
                acquire_lifecycle_lock=False,
            )
            if result_str == "success" and raw_status == "paused":
                await guarded_update_download_and_active_user_tasks(
                    attempt_id,
                    {"status": "active"},
                    expected_gid=payload_gid,
                    user_status="active",
                )
            elif result_str in ("failed", "missing"):
                await _broadcast_download_update(attempt_id)
                return ReconcileResult.TERMINALIZED, None
            elif result_str == "stale":
                return ReconcileResult.STALE, None

    # 9. Payload already complete → dispatch completion (spec §9.2 step 11).
    if raw_status == "complete" or outcome == "complete":
        await _broadcast_download_update(attempt_id)
        return ReconcileResult.CHANGED, (payload_gid, payload_status)

    await _broadcast_download_update(attempt_id)
    return ReconcileResult.CHANGED, None


async def switch_to_followed_download(
    *,
    client: Aria2Gateway,
    download: dict[str, Any],
    metadata_gid: str,
    followed_gid: str,
    display_name_fallback: str | None,
    log_prefix: str,
    complete_if_followed_complete: bool = False,
    _lock_held: bool = False,
    _real_status: dict[str, Any] | None = None,
) -> bool:
    """Thin wrapper: delegates to ``_handoff_locked`` (spec §9.2).

    Retained for backward-compatible callers (listener, sync, completion).
    Does NOT fabricate a ``complete`` event (spec §9.5).

    Fetches ``tell_status(followed_gid)`` OUTSIDE the lifecycle lock to
    avoid deadlock when another caller (e.g. cancel) holds or waits for
    the same lock.
    """
    download_id = int(download["id"])

    # Pre-fetch payload status outside the lock (spec §9.2 anti-deadlock).
    payload_status: dict[str, Any] | None = None
    if _real_status is not None:
        payload_status = _real_status
    else:
        try:
            payload_status = await client.tell_status(followed_gid)
        except Exception as exc:
            if is_transient_rpc_error(exc) or is_missing_gid_error(exc):
                logger.debug(
                    "%s Handoff pre-fetch tell_status failed, "
                    "will retry inside lock attempt_id=%s payload=%s error=%s",
                    log_prefix,
                    download_id,
                    followed_gid,
                    exc,
                )
            else:
                raise

    lifecycle_lock = await get_download_lifecycle_lock(download_id)
    async with lifecycle_lock:
        snapshot = await get_global_download_status_snapshot(download_id)
        if snapshot is None:
            await _stop_untracked_gid_best_effort(
                client, followed_gid, log_prefix
            )
            return False

        result, complete_dispatch = await _handoff_locked(
            client=client,
            attempt_id=download_id,
            source_gid=metadata_gid,
            payload_gid=followed_gid,
            snapshot=snapshot,
            download=download,
            log_prefix=log_prefix,
            _payload_status=payload_status,
        )

        if result == ReconcileResult.STALE:
            # Stale handoff: stop the untracked followed GID best-effort
            # but do NOT delete the shared download directory.
            await _stop_untracked_gid_best_effort(
                client, followed_gid, log_prefix
            )

    # Complete dispatch outside the lock (spec §9.2 step 11: no fake event).
    if complete_dispatch is not None and complete_if_followed_complete:
        completion_gid, completion_status = complete_dispatch
        updated = await get_global_download_by_gid(completion_gid)
        if updated is not None:
            await handle_v0_download_complete(
                client=client,
                download=updated,
                aria2_status=completion_status,
                completion_gid=completion_gid,
                log_prefix=log_prefix,
                allow_metadata_handoff_defer=False,
            )

    return result in (
        ReconcileResult.CHANGED,
        ReconcileResult.COMPLETED,
        ReconcileResult.TERMINALIZED,
    )


async def resolve_download_for_gid(
    gid: str,
    aria2_status: dict[str, Any] | None,
) -> ResolveResult | None:
    """Purely resolve a GID observation to an attempt (spec §6.3).

    Returns ``ResolveResult`` with the matching attempt dict.  Does not
    update ``aria2_gid``, ``resource_kind``, ``status`` or any user task.

    - Direct match: ``aria2_gid == observed_gid`` returns the attempt.
    - Handoff candidate: ``following=source_gid`` in *aria2_status* finds
      the attempt whose ``aria2_gid == source_gid`` and returns it without
      writing the new GID.
    """
    download = await get_global_download_by_gid(gid)
    if download is not None:
        return ResolveResult(
            download=download,
            observed_gid=gid,
            source_gid=None,
        )

    following_gid = download_ops.following_gid(aria2_status) if aria2_status else None
    if not following_gid:
        return None

    source_download = await get_global_download_by_gid(str(following_gid))
    if source_download is None:
        return None

    logger.debug(
        "[Resolve] Handoff candidate: source_gid=%s observed_gid=%s attempt_id=%s",
        following_gid,
        gid,
        source_download["id"],
    )
    return ResolveResult(
        download=source_download,
        observed_gid=gid,
        source_gid=str(following_gid),
    )


def _followed_gid_from_rows(
    rows: object,
    metadata_gid: str,
) -> str | None:
    if not isinstance(rows, list):
        return None
    for row in rows:
        if download_ops.following_gid(row) != metadata_gid:
            continue

        gid = row.get("gid")
        if isinstance(gid, (str, int)) and str(gid):
            return str(gid)
    return None


async def _find_followed_gid_by_following(
    client: Aria2Gateway,
    metadata_gid: str,
    log_prefix: str,
) -> str | None:
    list_calls = (
        ("active", client.tell_active, ()),
        ("waiting", client.tell_waiting, (0, 1000)),
        ("stopped", client.tell_stopped, (0, 1000)),
    )
    for label, call, args in list_calls:
        try:
            rows = await call(*args)
        except Exception as exc:
            logger.debug(
                "%s Failed to scan aria2 %s tasks for following=%s error=%s",
                log_prefix,
                label,
                metadata_gid,
                exc,
            )
            continue

        followed_gid = _followed_gid_from_rows(rows, metadata_gid)
        if followed_gid is not None:
            return followed_gid
    return None


async def _refresh_followed_gid(
    client: Aria2Gateway,
    gid: str | None,
    log_prefix: str,
) -> str | None:
    if not gid:
        return None

    for attempt in range(COMPLETE_SOURCE_RETRY_COUNT):
        try:
            status = await client.tell_status(gid)
        except Exception as exc:
            logger.debug(
                "%s Failed to refresh metadata status gid=%s error=%s",
                log_prefix,
                gid,
                exc,
            )
        else:
            followed_gid = download_ops.first_followed_gid(status)
            if followed_gid is not None:
                return followed_gid

        followed_gid = await _find_followed_gid_by_following(client, gid, log_prefix)
        if followed_gid is not None:
            return followed_gid

        if attempt < COMPLETE_SOURCE_RETRY_COUNT - 1:
            await asyncio.sleep(COMPLETE_SOURCE_RETRY_INTERVAL)

    return None


async def switch_to_late_followed_download_if_supported(
    *,
    client: Aria2Gateway,
    download: dict[str, Any],
    metadata_gid: str,
    display_name_fallback: str | None,
    log_prefix: str,
    complete_if_followed_complete: bool = False,
) -> bool:
    followed_gid = await _refresh_followed_gid(client, metadata_gid, log_prefix)
    if followed_gid is None:
        return False

    return await switch_to_followed_download(
        client=client,
        download=download,
        metadata_gid=metadata_gid,
        followed_gid=followed_gid,
        display_name_fallback=display_name_fallback,
        log_prefix=log_prefix,
        complete_if_followed_complete=complete_if_followed_complete,
    )


async def defer_metadata_completion_if_handoff_pending(
    *,
    client: Aria2Gateway,
    download: dict[str, Any],
    aria2_status: dict[str, Any],
    metadata_gid: str,
    display_name_fallback: str | None,
    log_prefix: str,
) -> tuple[bool, bool]:
    if not download_ops.is_metadata_handoff_pending(download, aria2_status):
        return False, False

    switched = await switch_to_late_followed_download_if_supported(
        client=client,
        download=download,
        metadata_gid=metadata_gid,
        display_name_fallback=display_name_fallback,
        log_prefix=log_prefix,
    )
    if switched:
        return True, True

    logger.info(
        "%s Metadata download complete without followedBy, waiting for handoff id=%s gid=%s",
        log_prefix,
        download["id"],
        metadata_gid,
    )
    changed = await update_download_and_active_user_tasks(
        download_id=int(download["id"]),
        expected_gid=metadata_gid,
        global_values={"status": "active"},
        user_status="active",
    )
    return True, changed


def should_defer_stopped_result_cleanup(
    download: dict[str, Any],
    aria2_status: dict[str, Any],
) -> bool:
    return download_ops.is_metadata_handoff_pending(download, aria2_status)


async def handle_v0_download_complete(
    *,
    client: Aria2Gateway,
    download: dict[str, Any],
    aria2_status: dict[str, Any],
    completion_gid: str,
    log_prefix: str = "[WS]",
    allow_metadata_handoff_defer: bool = True,
) -> bool:
    download_id = int(download["id"])

    # Pre-lock phase: metadata handoff deferral and late-followed switch
    # acquire the lifecycle lock internally, so they must run outside it.
    current = await get_global_download_for_generation(
        download_id, completion_gid
    )
    if current is None:
        logger.debug(
            "%s Completion generation is stale id=%s gid=%s",
            log_prefix,
            download_id,
            completion_gid,
        )
        return False

    display_name_fallback = str(
        current.get("display_name") or current.get("source_uri") or ""
    )
    files = aria2_status.get("files", [])
    if not isinstance(files, list):
        files = []

    task_name = download_ops.extract_display_name(
        aria2_status,
        display_name_fallback,
    )
    if allow_metadata_handoff_defer:
        deferred, changed = await defer_metadata_completion_if_handoff_pending(
            client=client,
            download=current,
            aria2_status=aria2_status,
            metadata_gid=completion_gid,
            display_name_fallback=display_name_fallback,
            log_prefix=log_prefix,
        )
        if deferred:
            return changed

    task_dir = get_downloading_dir() / str(download_id)
    source_path = await resolve_complete_source_with_retry(
        completion_gid=completion_gid,
        task_dir=task_dir,
        files=files,
        task_name=task_name,
        client=client,
    )
    if source_path is None:
        if await switch_to_late_followed_download_if_supported(
            client=client,
            download=current,
            metadata_gid=completion_gid,
            display_name_fallback=display_name_fallback,
            log_prefix=log_prefix,
        ):
            return True

    original_name = task_name or (source_path.name if source_path else "")
    if str(current.get("resource_kind") or "") == "http" and current.get(
        "display_name"
    ):
        original_name = str(current["display_name"])
    elif source_path and original_name.startswith(METADATA_NAME_PREFIX):
        original_name = source_path.name
    expected_size = (
        expected_completed_size(aria2_status, source_path)
        if source_path is not None
        else None
    )

    # Lock phase: complete or fail under the lifecycle lock.
    lifecycle_lock = await get_download_lifecycle_lock(download_id)
    async with lifecycle_lock:
        fenced = await get_global_download_for_generation(
            download_id, completion_gid
        )
        if fenced is None:
            logger.debug(
                "%s Completion generation is stale id=%s gid=%s",
                log_prefix,
                download_id,
                completion_gid,
            )
            return False

        if source_path is None:
            error_code, error_message = source_not_found_error(task_dir)
            logger.error(
                "%s Download completed but source path was not found id=%s gid=%s dir=%s files=%s error_code=%s",
                log_prefix,
                download_id,
                completion_gid,
                task_dir,
                len(files),
                error_code,
            )
            return await fail_download_and_reclaim(
                client=client,
                download_id=download_id,
                message=error_message,
                error_code=error_code,
                expected_gid=completion_gid,
                writer_gid=completion_gid,
                acquire_lifecycle_lock=False,
                log_prefix=log_prefix,
            )

        result = await complete_global_download_locked(
            global_download_id=download_id,
            expected_gid=completion_gid,
            source_path=source_path,
            original_name=original_name,
            expected_size=expected_size,
        )
        if result is None:
            return False
        if result["status"] == "rejected":
            await _reclaim_terminal_with_claim(
                client=client,
                download_id=download_id,
                gid=completion_gid,
                log_prefix=log_prefix,
            )
            return True
        if result["status"] == "invalid_source":
            return await fail_download_and_reclaim(
                client=client,
                download_id=download_id,
                message="下载完成但文件布局无效",
                error_code="invalid_completed_layout",
                expected_gid=completion_gid,
                writer_gid=completion_gid,
                acquire_lifecycle_lock=False,
                log_prefix=log_prefix,
            )
        logger.info(
            "%s Completed v0 download id=%s user_files_created=%s",
            log_prefix,
            download_id,
            result["user_files_created"],
        )

    if result["status"] != "incomplete":
        await _remove_download_result_best_effort(client, completion_gid, log_prefix)
        try:
            await cleanup_task_download_dir(download_id)
        except Exception as exc:
            logger.warning(
                "%s Failed to reclaim completed download dir id=%s error=%s",
                log_prefix,
                download_id,
                exc,
            )
        return True

    # Incomplete: try late-followed switch outside the lock, then fail if needed.
    if await switch_to_late_followed_download_if_supported(
        client=client,
        download=current,
        metadata_gid=completion_gid,
        display_name_fallback=display_name_fallback,
        log_prefix=log_prefix,
    ):
        return True
    error_code, error_message = completed_size_mismatch_error(
        source_path=source_path,
        expected_bytes=int(expected_size or 0),
        actual_bytes=int(result["size_bytes"]),
    )
    lifecycle_lock = await get_download_lifecycle_lock(download_id)
    async with lifecycle_lock:
        return await fail_download_and_reclaim(
            client=client,
            download_id=download_id,
            message=error_message,
            error_code=error_code,
            expected_gid=completion_gid,
            writer_gid=completion_gid,
            acquire_lifecycle_lock=False,
            log_prefix=log_prefix,
        )


async def _terminalize_missing_gid_locked(
    *,
    client: Aria2Gateway,
    attempt_id: int,
    current_gid: str,
    total_bytes: int,
    log_prefix: str,
) -> ReconcileResult:
    """Terminalize a live attempt whose GID is confirmed missing (spec §14.1)."""
    if total_bytes > 0:
        error_code = "gid_missing"
        message = "aria2 任务 GID 已丢失"
    else:
        error_code = "unknown_size"
        message = "任务 GID 丢失且文件大小未知，无法安全恢复"
    logger.warning(
        "%s Missing GID for live attempt_id=%s current_gid=%s total_bytes=%s error_code=%s",
        log_prefix,
        attempt_id,
        current_gid,
        total_bytes,
        error_code,
    )
    changed = await fail_download_and_reclaim(
        client=client,
        download_id=attempt_id,
        message=message,
        error_code=error_code,
        expected_gid=current_gid,
        writer_gid=current_gid,
        acquire_lifecycle_lock=False,
        log_prefix=log_prefix,
    )
    if changed:
        await _broadcast_download_update(attempt_id)
        return ReconcileResult.TERMINALIZED
    return ReconcileResult.STALE


async def reconcile_attempt_signal(
    *,
    client: Aria2Gateway,
    observed_gid: str,
    event: str | None,
    observed_status: Mapping[str, Any] | None,
    observed_error: Exception | None = None,
    log_prefix: str,
) -> ReconcileResult:
    """Unified coordinator entry for a single lifecycle observation (spec §6).

    T10: normal status projection (active/waiting/paused), error/removed
    terminalization, and missing-GID handling are resolved inside the
    attempt lock.  Complete still delegates to the existing complete path.
    Size coordination and handoff remain for T11/T12.
    """
    # 1. Pure resolve (no DB writes).
    status_for_resolve: dict[str, Any] | None = (
        dict(observed_status) if observed_status else None
    )
    resolved = await resolve_download_for_gid(observed_gid, status_for_resolve)
    if resolved is None:
        logger.debug(
            "%s No attempt for observed_gid=%s event=%s",
            log_prefix,
            observed_gid,
            event,
        )
        return ReconcileResult.IGNORED

    attempt_id = int(resolved.download["id"])
    is_handoff = resolved.is_handoff_candidate

    # 2. Attempt lock + reread.
    lifecycle_lock = await get_download_lifecycle_lock(attempt_id)

    # Set by the complete/handoff branch for post-lock delegation.
    complete_dispatch: tuple[str, dict[str, Any]] | None = None

    async with lifecycle_lock:
        snapshot = await get_global_download_status_snapshot(attempt_id)
        if snapshot is None:
            logger.debug(
                "%s Attempt vanished during reconcile attempt_id=%s",
                log_prefix,
                attempt_id,
            )
            return ReconcileResult.IGNORED

        current_gid = str(snapshot.get("aria2_gid") or "")
        status_before = str(snapshot.get("status") or "")
        completed_file_id = snapshot.get("completed_file_id")
        total_bytes = download_ops.safe_int(snapshot.get("total_bytes"))

        logger.info(
            "%s reconcile attempt_id=%s observed_gid=%s current_gid=%s "
            "status_before=%s event=%s fence=acquired",
            log_prefix,
            attempt_id,
            observed_gid,
            current_gid,
            status_before,
            event,
        )

        # ---- Missing GID (spec §14.1) ----
        if observed_error is not None and is_missing_gid_error(observed_error):
            if current_gid != observed_gid:
                logger.info(
                    "%s Missing GID stale: observed=%s current=%s attempt_id=%s",
                    log_prefix,
                    observed_gid,
                    current_gid,
                    attempt_id,
                )
                return ReconcileResult.STALE
            if status_before == "completed":
                if completed_file_id is not None:
                    return ReconcileResult.ALREADY_COMPLETE
                logger.info(
                    "%s Missing GID + completed without file, recovery_pending attempt_id=%s",
                    log_prefix,
                    attempt_id,
                )
                return ReconcileResult.RECOVERY_PENDING
            if status_before in ("failed", "cancelled"):
                return ReconcileResult.ALREADY_TERMINAL
            # Live attempt: terminalize based on trusted-size availability.
            return await _terminalize_missing_gid_locked(
                client=client,
                attempt_id=attempt_id,
                current_gid=current_gid,
                total_bytes=total_bytes,
                log_prefix=log_prefix,
            )

        # ---- Transient RPC error → waiting (spec §6.2.4) ----
        if observed_error is not None and is_transient_rpc_error(observed_error):
            logger.debug(
                "%s Transient RPC error, waiting attempt_id=%s",
                log_prefix,
                attempt_id,
            )
            return ReconcileResult.WAITING

        # ---- Generic terminal check ----
        if status_before in TERMINAL_DOWNLOAD_STATUSES:
            if status_before == "completed":
                return ReconcileResult.ALREADY_COMPLETE
            logger.debug(
                "%s Already terminal attempt_id=%s status=%s",
                log_prefix,
                attempt_id,
                status_before,
            )
            return ReconcileResult.ALREADY_TERMINAL

        # ---- Fencing ----
        fence_gid = resolved.source_gid if is_handoff else observed_gid
        if current_gid != fence_gid:
            logger.info(
                "%s Stale: fence_gid=%s current_gid=%s attempt_id=%s handoff=%s",
                log_prefix,
                fence_gid,
                current_gid,
                attempt_id,
                is_handoff,
            )
            return ReconcileResult.STALE

        # ---- Handoff candidate: call _handoff_locked (spec §9) ----
        if is_handoff:
            result, complete_info = await _handoff_locked(
                client=client,
                attempt_id=attempt_id,
                source_gid=str(resolved.source_gid),
                payload_gid=observed_gid,
                snapshot=snapshot,
                download=resolved.download,
                log_prefix=log_prefix,
                _payload_status=(
                    dict(observed_status) if observed_status else None
                ),
            )
            if complete_info is not None:
                complete_dispatch = complete_info
            if result == ReconcileResult.CHANGED:
                if complete_dispatch is None:
                    return ReconcileResult.CHANGED
                # Handoff committed + payload complete: fall through to
                # end of lock, then post-lock complete delegation.
                # Must NOT enter the projection section below — it would
                # use the stale pre-handoff current_gid for tell_status.
            else:
                return result

        else:
            # ---- Determine aria2 status ----
            working_status: dict[str, Any] | None = (
                dict(observed_status) if observed_status else None
            )
            if working_status is None:
                if not current_gid:
                    return ReconcileResult.WAITING
                try:
                    working_status = await client.tell_status(current_gid)
                except Exception as exc:
                    logger.debug(
                        "%s tell_status failed attempt_id=%s error=%s",
                        log_prefix,
                        attempt_id,
                        exc,
                    )
                    if is_transient_rpc_error(exc):
                        return ReconcileResult.WAITING
                    if is_missing_gid_error(exc):
                        return await _terminalize_missing_gid_locked(
                            client=client,
                            attempt_id=attempt_id,
                            current_gid=current_gid,
                            total_bytes=total_bytes,
                            log_prefix=log_prefix,
                        )
                    raise

            raw_status = str(working_status.get("status") or "")

            # ---- error/removed → terminalize inside lock ----
            if raw_status in ("error", "removed"):
                if raw_status == "removed":
                    message = "外部取消（管理员/外部客户端）"
                    error_code = "removed"
                else:
                    raw_error = working_status.get("errorMessage")
                    message = prefer_aria2_error_message(
                        working_status.get("errorCode"),
                        str(raw_error) if raw_error is not None else None,
                        "后端错误",
                    )
                    error_code = str(working_status.get("errorCode") or "error")
                logger.warning(
                    "%s Terminalizing attempt_id=%s raw_status=%s error_code=%s "
                    "aria2_error=%s",
                    log_prefix,
                    attempt_id,
                    raw_status,
                    error_code,
                    working_status.get("errorMessage"),
                )
                changed = await fail_download_and_reclaim(
                    client=client,
                    download_id=attempt_id,
                    message=sanitize_string(message),
                    error_code=error_code,
                    expected_gid=current_gid,
                    writer_gid=current_gid,
                    acquire_lifecycle_lock=False,
                    log_prefix=log_prefix,
                )
                if changed:
                    await _broadcast_download_update(attempt_id)
                    return ReconcileResult.TERMINALIZED
                return ReconcileResult.STALE

            # ---- complete → handoff or delegate completion ----
            if raw_status == "complete":
                followed_gid = download_ops.first_followed_gid(working_status)
                if followed_gid:
                    # Pre-fetch followed status before _handoff_locked
                    # to avoid redundant RPC inside the lock.
                    pre_fetched: dict[str, Any] | None = None
                    try:
                        pre_fetched = await client.tell_status(followed_gid)
                    except Exception as exc:
                        if is_transient_rpc_error(exc) or is_missing_gid_error(
                            exc
                        ):
                            logger.debug(
                                "%s Complete+followedBy pre-fetch failed, "
                                "attempt_id=%s payload=%s error=%s",
                                log_prefix,
                                attempt_id,
                                followed_gid,
                                exc,
                            )
                        else:
                            raise
                    result, complete_info = await _handoff_locked(
                        client=client,
                        attempt_id=attempt_id,
                        source_gid=current_gid,
                        payload_gid=followed_gid,
                        snapshot=snapshot,
                        download=resolved.download,
                        log_prefix=log_prefix,
                        _payload_status=pre_fetched,
                    )
                    if complete_info is not None:
                        complete_dispatch = complete_info
                    if result == ReconcileResult.CHANGED:
                        if complete_dispatch is not None:
                            pass  # Fall through to post-lock complete delegation.
                        else:
                            return ReconcileResult.CHANGED
                    else:
                        return result
                else:
                    complete_dispatch = (current_gid, working_status)

            # ---- active/waiting/paused → guarded projection (spec §8.1) ----
            elif raw_status in ("active", "waiting", "paused"):
                bt_evidence = _has_bittorrent_evidence(
                    working_status, resolved.download
                )
                is_metadata = bt_evidence and is_metadata_phase_status(
                    working_status
                )
                admission: dict[str, Any] | None = None

                # Size admission for non-metadata live states (spec §8.2).
                # coordinate_reported_size is a lock-internal action here;
                # acquire_lifecycle_lock=False because we already hold the lock.
                if not is_metadata:
                    require_trusted = not bool(
                        resolved.download.get("size_known")
                    )
                    size_candidate = candidate_size_from_status(
                        working_status, require_trusted_total=require_trusted
                    )
                    if require_trusted or size_candidate is not None:
                        admission = await coordinate_reported_size(
                            client=client,
                            download=resolved.download,
                            expected_gid=current_gid,
                            control_gid=current_gid,
                            status=working_status,
                            require_trusted_total=require_trusted,
                            acquire_lifecycle_lock=False,
                        )
                        size_outcome = str(admission.get("outcome") or "")
                        if size_outcome == "stale":
                            return ReconcileResult.STALE
                        if size_outcome == "terminalized":
                            await _broadcast_download_update(attempt_id)
                            return ReconcileResult.TERMINALIZED
                        if size_outcome == "rpc_unavailable":
                            pass  # Skip size admission, continue with projection
                        elif size_outcome == "complete":
                            complete_dispatch = (
                                current_gid, working_status,
                            )
                        elif size_outcome == "unknown_size":
                            changed = await fail_download_and_reclaim(
                                client=client,
                                download_id=attempt_id,
                                message="任务运行时无法确认可信文件大小",
                                error_code="unknown_size",
                                expected_gid=current_gid,
                                writer_gid=current_gid,
                                acquire_lifecycle_lock=False,
                                log_prefix=log_prefix,
                            )
                            if changed:
                                await _broadcast_download_update(attempt_id)
                                return ReconcileResult.TERMINALIZED
                            return ReconcileResult.STALE
                        elif size_outcome != "admitted":
                            # disk_budget / max_task_size / no_subscribers:
                            # row already terminal in DB, do physical reclaim
                            # via repair claim (no skip_status_check).
                            await _reclaim_terminal_with_claim(
                                client=client,
                                download_id=attempt_id,
                                gid=current_gid,
                                log_prefix=log_prefix,
                            )
                            await _broadcast_download_update(attempt_id)
                            return ReconcileResult.TERMINALIZED

                prev_status = str(snapshot.get("status") or "")
                prev_error_code = str(snapshot.get("error_code") or "")
                size_paused_by_us = bool(
                    admission is not None and admission.get("paused_by_us")
                )

                mapped = _map_v0_status(
                    working_status,
                    attempt_id,
                    prefer_bittorrent_name=bt_evidence,
                )

                global_values: dict[str, Any] = {
                    "status": mapped["status"],
                    "completed_bytes": mapped["completed_bytes"],
                    "updated_at_ms": now_ms(),
                }
                if not is_metadata:
                    global_values["total_bytes"] = mapped["total_bytes"]
                    if mapped["display_name"]:
                        global_values["display_name"] = mapped["display_name"]

                # Pause projection (arch M2):
                # - never auto-unpause merely because size_known (removed a554c30 path)
                # - never mark metadata-phase pause as external
                # - never mark pause that size-admission just owned (paused_by_us)
                # - never overwrite growth/handoff/admission or queue error codes
                # - only when transitioning into paused from a non-paused live status
                protected_error_codes = {
                    "growth_pause_failed",
                    "growth_unpause_failed",
                    "unpause_failed",
                    "handoff_unknown_size",
                    "unknown_size",
                    "disk_budget",
                    "disk_budget_exceeded",
                    "max_task_size",
                    "admission_rejected",
                    "quota_queued",
                    "disk_queued",
                }
                if (
                    mapped["status"] == "paused"
                    and not is_metadata
                    and not size_paused_by_us
                    and prev_status in {"active", "queued", "waiting"}
                    and prev_error_code not in protected_error_codes
                ):
                    global_values["error_message"] = "任务已被外部暂停"
                    global_values["error_code"] = "external_paused"
                    global_values["disk_reserved_bytes"] = max(
                        0,
                        download_ops.safe_int(snapshot.get("completed_bytes")),
                    )
                elif mapped["status"] == "active" and prev_error_code in {
                    "external_paused",
                    "admin_paused",
                }:
                    # Clear sticky external-pause hint when download resumes.
                    global_values["error_code"] = None
                    global_values["error_message"] = None

                updated = await guarded_update_download_and_active_user_tasks(
                    attempt_id,
                    global_values,
                    expected_gid=current_gid,
                    user_status=mapped["status"],
                    display_name=(
                        mapped["display_name"] if not is_metadata else None
                    ),
                )
                if updated is not None:
                    await _broadcast_download_update(attempt_id)
                    logger.debug(
                        "%s Projected attempt_id=%s status_after=%s",
                        log_prefix,
                        attempt_id,
                        mapped["status"],
                    )
                    return ReconcileResult.CHANGED
                return ReconcileResult.STALE

            else:
                logger.debug(
                    "%s Unknown raw_status=%s, waiting attempt_id=%s",
                    log_prefix,
                    raw_status,
                    attempt_id,
                )
                return ReconcileResult.WAITING

    # ---- Post-lock: delegate completion (spec §9.2 step 11: no fake event) ----
    if complete_dispatch is not None:
        completion_gid, completion_status = complete_dispatch
        try:
            updated = await get_global_download_by_gid(completion_gid)
            if updated is not None:
                changed = await handle_v0_download_complete(
                    client=client,
                    download=updated,
                    aria2_status=completion_status,
                    completion_gid=completion_gid,
                    log_prefix=log_prefix,
                )
            else:
                changed = False
        except Exception as exc:
            if is_transient_rpc_error(exc):
                logger.debug(
                    "%s Complete delegation hit transient RPC error attempt_id=%s",
                    log_prefix,
                    attempt_id,
                )
                return ReconcileResult.WAITING
            raise
        if changed:
            await _broadcast_download_update(attempt_id)
            return ReconcileResult.COMPLETED
        return ReconcileResult.WAITING

    return ReconcileResult.WAITING


async def repair_inconsistent_completed_downloads_v0(
    client: Aria2Gateway | None = None,
) -> None:
    if client is None:
        from app.aria2.gateway import get_aria2_client

        client = get_aria2_client()
    threshold_ms = now_ms() - int(COMPLETE_REPAIR_GRACE_SECONDS * 1000)
    for snapshot in await list_inconsistent_completed_download_ids(threshold_ms):
        download_id = int(snapshot["id"])
        logger.warning(
            "[Sync] Completed v0 download was not indexed, failing id=%s", download_id
        )
        gid = snapshot.get("aria2_gid")
        changed = await fail_download_and_reclaim(
            client=client,
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
    client: Aria2Gateway | None = None,
) -> None:
    if client is None:
        from app.aria2.gateway import get_aria2_client

        client = get_aria2_client()
    threshold_ms = now_ms() - int(grace_seconds * 1000)
    for download_id in await list_stale_queued_download_ids(threshold_ms):
        logger.warning("[Sync] Cleaning stale v0 queued download_id=%s", download_id)
        changed = await fail_download_and_reclaim(
            client=client,
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

