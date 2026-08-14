"""Task 5 — Pause / queue / handoff state machine (pure decisions).

Rules (see task spec + M7 ownership model):
- System-owned pause codes (SYSTEM_OWNED_PAUSE_CODES) may auto-resume only
  with an explicit predicate — never from size_known alone.
- A pause without a system ownership error_code is external: never auto-unpause.
- active/waiting + system ownership code → clear the code (target reached).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping

from app.modules.backend.port import BackendPort
from app.modules.task_core.states import (
    ERROR_ADMISSION_PAUSED,
    ERROR_DISK_QUEUED,
    ERROR_EXTERNAL_PAUSED,
    ERROR_GROWTH_UNPAUSE_FAILED,
    ERROR_METADATA_ADMISSION_PAUSED,
    ERROR_QUOTA_EXCEEDED,
    ERROR_QUOTA_QUEUED,
    ERROR_UNPAUSE_FAILED,
    SYSTEM_OWNED_PAUSE_CODES,
)
from app.repositories.task.downloads import update_global_download

SYSTEM_QUEUE_CODES = frozenset({ERROR_QUOTA_QUEUED, ERROR_DISK_QUEUED})

# Codes policy may auto-resume when status is paused (subset of owned).
# growth_pause_failed is ownership-only: keep while paused, clear when active.
_AUTO_RESUME_WHEN_SIZE_KNOWN = frozenset(
    {
        ERROR_METADATA_ADMISSION_PAUSED,
        ERROR_GROWTH_UNPAUSE_FAILED,
        ERROR_UNPAUSE_FAILED,
    }
)

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
            "terminal_quota_exceeded", error_code=ERROR_QUOTA_EXCEEDED, terminal=True
        )

    # Target reached with leftover system ownership: clear code only.
    if status in {"active", "waiting"} and db_error in SYSTEM_OWNED_PAUSE_CODES:
        return Decision("clear_error_code", clear_error_code=True)

    if db_error == ERROR_QUOTA_QUEUED and ctx.quota_bytes is not None:
        headroom = ctx.quota_bytes - ctx.quota_used_bytes
        if _is_size_known(tid_row) and total <= headroom:
            return Decision("resume", clear_error_code=True)
    elif db_error == ERROR_DISK_QUEUED and ctx.disk_available:
        return Decision("resume", clear_error_code=True)

    if status == "paused":
        if db_error == ERROR_ADMISSION_PAUSED:
            return Decision("resume", clear_error_code=True)
        if db_error in _AUTO_RESUME_WHEN_SIZE_KNOWN:
            if _is_size_known(tid_row) and total > 0:
                return Decision("resume", clear_error_code=True)
            return Decision("keep", error_code=str(db_error) if db_error else None)
        if db_error in SYSTEM_QUEUE_CODES or ctx.system_pause:
            return Decision("keep", error_code=db_error)
        if db_error is None:
            return Decision(
                "mark_external_paused", error_code=ERROR_EXTERNAL_PAUSED
            )
        # Unknown or external/admin: never auto-resume.
        return Decision("keep", error_code=db_error)

    return Decision("noop")


async def apply_decision(
    backend: BackendPort,
    tid: int,
    decision: Decision,
) -> None:
    """Apply a Decision: backend effect plus minimal DB error_code update."""
    if decision.action == "resume":
        await backend.unpause(tid)
        await update_global_download(
            tid, {"error_code": None, "error_message": None}
        )
    elif decision.action == "clear_error_code":
        await update_global_download(
            tid, {"error_code": None, "error_message": None}
        )
    elif decision.action == "terminal_quota_exceeded":
        await backend.pause(tid)
        await update_global_download(
            tid,
            {
                "status": "failed",
                "error_code": ERROR_QUOTA_EXCEEDED,
                "error_message": "文件大小超过用户总配额",
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
