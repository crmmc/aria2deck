"""Task 5 — Pause / queue / handoff state machine (pure decisions).

Rules (see task spec + M7/M9 ownership model):
- System-owned pause codes (SYSTEM_OWNED_PAUSE_CODES) may auto-resume only
  with an explicit predicate — never from size_known alone.
- A pause without a system ownership error_code is external: never auto-unpause.
- active/waiting + system ownership code → clear the code (target reached),
  except metadata_admission_paused which never clears via this generic branch.
- apply_decision(resume) clears only after re-query ∈ {active, waiting}.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from app.domain.error_text import over_limit

logger = logging.getLogger(__name__)
from app.modules.backend.port import BackendPort
from app.modules.task_core.states import (
    ERROR_ADMISSION_PAUSED,
    ERROR_DISK_QUEUED,
    ERROR_EXTERNAL_PAUSED,
    ERROR_METADATA_ADMISSION_PAUSED,
    ERROR_QUOTA_EXCEEDED,
    ERROR_QUOTA_QUEUED,
    ERROR_UNPAUSE_FAILED,
    PENDING_RELEASE_CODES,
    SYSTEM_OWNED_PAUSE_CODES,
)
from app.repositories.task.downloads import (
    get_global_download_by_id,
    guarded_update_global_download,
    update_global_download,
)

_RUNNING_STATUSES = frozenset({"active", "waiting"})

SYSTEM_QUEUE_CODES = frozenset({ERROR_QUOTA_QUEUED, ERROR_DISK_QUEUED})

# PENDING codes that need trusted size before auto-resume (subset of PENDING).
# admission_paused resumes immediately when paused (create-time HTTP/torrent).
# growth_pause_failed is owned but not PENDING — never auto-resume.
_PENDING_RESUME_WHEN_SIZE_KNOWN = PENDING_RELEASE_CODES - {ERROR_ADMISSION_PAUSED}

Action = Literal[
    "keep",
    "resume",
    "mark_resource_queued",
    "terminal_quota_exceeded",
    "mark_external_paused",
    "clear_error_code",
    "noop",
]


@dataclass(frozen=True)
class QuotaContext:
    """Resource facts for one tid, supplied by the caller (owner scope)."""

    quota_bytes: int | None = None
    quota_used_bytes: int = 0
    disk_available: bool = True
    system_pause: bool = False


@dataclass(frozen=True)
class Decision:
    action: Action
    error_code: str | None = None
    clear_error_code: bool = False
    terminal: bool = False
    # Filled only for terminal_quota_exceeded so apply_decision can render
    # the E-4 message with actual values.
    total_bytes: int | None = None
    quota_bytes: int | None = None


def _is_size_known(row: Mapping[str, Any]) -> bool:
    if "size_known" in row:
        return bool(row.get("size_known"))
    return int(row.get("total_bytes") or 0) > 0


def decide_on_snapshot(
    tid_row: Mapping[str, Any],
    status: str,
    *,
    quota: QuotaContext | None = None,
) -> Decision:
    """Decide what (if anything) to do for one tid given its snapshot."""
    db_error = tid_row.get("error_code")
    ctx = quota or QuotaContext()
    total = int(tid_row.get("total_bytes") or 0)

    if (
        ctx.quota_bytes is not None
        and _is_size_known(tid_row)
        and total > ctx.quota_bytes
    ):
        return Decision(
            "terminal_quota_exceeded",
            error_code=ERROR_QUOTA_EXCEEDED,
            terminal=True,
            total_bytes=total,
            quota_bytes=ctx.quota_bytes,
        )

    # Target reached with leftover system ownership: clear code only.
    # metadata_admission_paused is excluded: metadata-phase active/waiting must
    # not drop the credential (Spec §3.1.1 / T9c).
    if (
        status in _RUNNING_STATUSES
        and db_error in SYSTEM_OWNED_PAUSE_CODES
        and db_error != ERROR_METADATA_ADMISSION_PAUSED
    ):
        return Decision("clear_error_code", clear_error_code=True)

    if db_error == ERROR_QUOTA_QUEUED and ctx.quota_bytes is not None:
        headroom = ctx.quota_bytes - ctx.quota_used_bytes
        if _is_size_known(tid_row) and total <= headroom:
            return Decision("resume", clear_error_code=True)
    elif db_error == ERROR_DISK_QUEUED and ctx.disk_available:
        return Decision("resume", clear_error_code=True)

    if status == "paused":
        # Create-time / growth admission credential: resume without size gate.
        if db_error == ERROR_ADMISSION_PAUSED:
            return Decision("resume", clear_error_code=True)
        # Other PENDING_RELEASE codes: resume only when size is trusted.
        if db_error in _PENDING_RESUME_WHEN_SIZE_KNOWN:
            if _is_size_known(tid_row) and total > 0:
                return Decision("resume", clear_error_code=True)
            return Decision("keep", error_code=str(db_error) if db_error else None)
        if db_error in SYSTEM_QUEUE_CODES or ctx.system_pause:
            return Decision("keep", error_code=db_error)
        if db_error is None:
            return Decision(
                "mark_external_paused", error_code=ERROR_EXTERNAL_PAUSED
            )
        # Unknown or external/admin / growth_pause_failed: never auto-resume.
        return Decision("keep", error_code=db_error)

    return Decision("noop")


async def _observe_backend_status(backend: BackendPort, tid: int) -> str | None:
    """Best-effort re-query of backend status for ``tid`` after unpause."""
    row = await get_global_download_by_id(tid)
    gid = row.get("aria2_gid") if row else None
    if not gid:
        return None
    try:
        raw: object = await backend.tell_status(str(gid))
    except Exception as exc:  # noqa: BLE001  # backend observation is best effort
        logger.debug("后端状态观测失败 error_type=%s", type(exc).__name__)
        return None
    if not isinstance(raw, Mapping):
        return None
    status = raw.get("status")
    return str(status) if status is not None else None


async def apply_decision(
    backend: BackendPort,
    tid: int,
    decision: Decision,
) -> None:
    """Apply a Decision: backend effect plus minimal DB error_code update."""
    if decision.action == "resume":
        row = await get_global_download_by_id(tid)
        gid = str(row.get("aria2_gid") or "") if row else ""
        try:
            await backend.unpause(tid)
        except Exception as exc:  # noqa: BLE001  # unpause failure is followed by status re-query
            # Still re-query: unpause may have taken effect despite RPC error.
            logger.debug("恢复任务失败，将继续复查 error_type=%s", type(exc).__name__)
        observed = await _observe_backend_status(backend, tid)
        if observed in _RUNNING_STATUSES:
            # Fenced clear: only the generation we observed running may drop
            # the credential (09-05 fix-pause-ownership-loss).
            if gid:
                await guarded_update_global_download(
                    tid,
                    {"error_code": None, "error_message": None},
                    expected_gid=gid,
                )
            else:
                await update_global_download(
                    tid, {"error_code": None, "error_message": None}
                )
            return
        # Still paused / unknown: keep pending credential or stamp soft fail.
        # Prefer preserving an existing system code; only write unpause_failed
        # when the row would otherwise become bare paused.
        row = await get_global_download_by_id(tid)
        current_code = row.get("error_code") if row else None
        if current_code in SYSTEM_OWNED_PAUSE_CODES:
            return
        await update_global_download(
            tid,
            {
                "error_code": ERROR_UNPAUSE_FAILED,
                "error_message": "恢复下载未进入运行态",
            },
        )
    elif decision.action == "clear_error_code":
        await update_global_download(
            tid, {"error_code": None, "error_message": None}
        )
    elif decision.action == "terminal_quota_exceeded":
        if decision.total_bytes is not None and decision.quota_bytes is not None:
            error_message = over_limit(
                "文件大小", decision.total_bytes, "超过用户配额", decision.quota_bytes
            )
        else:
            error_message = "文件大小超过用户配额（数值未知）"
        await backend.pause(tid)
        await update_global_download(
            tid,
            {
                "status": "failed",
                "error_code": ERROR_QUOTA_EXCEEDED,
                "error_message": error_message,
            },
        )
    elif decision.action == "mark_resource_queued":
        await backend.pause(tid)
        await update_global_download(tid, {"error_code": decision.error_code})
    elif decision.action == "mark_external_paused":
        await update_global_download(tid, {"error_code": decision.error_code})


def on_followed_size(
    *,
    size_bytes: int,
    quota_bytes: int | None,
    quota_used_bytes: int = 0,
    disk_ok: bool = True,
) -> Literal["ok", "exceeded", "quota_queued", "disk_queued"]:
    """Handoff eligibility once size is known (metadata resolved / follow)."""
    if quota_bytes is not None:
        if size_bytes > quota_bytes:
            return "exceeded"
        if size_bytes > quota_bytes - quota_used_bytes:
            return "quota_queued"
    if not disk_ok:
        return "disk_queued"
    return "ok"
