"""Task 5 — Pause / queue / handoff state machine (pure decisions).

Rules (see task spec):
- ``quota_queued`` / ``disk_queued`` are system-owned pauses: when the
  blocking resource recovers, the tid resumes (unpause + clear error_code).
- A pause without a system queue error_code is external (user or admin):
  the state machine never auto-unpauses it.
- ``size_known`` alone never triggers a resume.
- A known size over the total quota is terminal ``quota_exceeded``.

Decisions are pure; ``apply_decision`` performs backend + DB effects.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping

from app.modules.backend.port import BackendPort
from app.modules.task_core.states import (
    ERROR_DISK_QUEUED,
    ERROR_QUOTA_EXCEEDED,
    ERROR_QUOTA_QUEUED,
)
from app.repositories.downloads import update_global_download

SYSTEM_QUEUE_CODES = frozenset({ERROR_QUOTA_QUEUED, ERROR_DISK_QUEUED})

Action = Literal[
    "keep",
    "resume",
    "mark_resource_queued",
    "terminal_quota_exceeded",
    "noop",
]


@dataclass(frozen=True)
class QuotaContext:
    """Resource facts for one tid, supplied by the caller (owner scope)."""

    quota_bytes: int | None = None
    quota_used_bytes: int = 0
    disk_available: bool = True
    # True when the current pause was initiated by this state machine in
    # the current round (e.g. just mark_resource_queued). Prevents a
    # system pause from being re-read as external within the same pass.
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
    """Decide what (if anything) to do for one tid given its snapshot.

    Precedence: terminal quota check > queue-resume > external pause > keep.
    """
    db_error = tid_row.get("error_code")
    ctx = quota or QuotaContext()
    total = int(tid_row.get("total_bytes") or 0)

    # Terminal: known size exceeds the total quota. Illegal state, no resume.
    if (
        ctx.quota_bytes is not None
        and _is_size_known(tid_row)
        and total > ctx.quota_bytes
    ):
        return Decision(
            "terminal_quota_exceeded", error_code=ERROR_QUOTA_EXCEEDED, terminal=True
        )

    # System queue resume: only when the specific blocking resource recovered.
    if db_error == ERROR_QUOTA_QUEUED and ctx.quota_bytes is not None:
        headroom = ctx.quota_bytes - ctx.quota_used_bytes
        if _is_size_known(tid_row) and total <= headroom:
            return Decision("resume", clear_error_code=True)
    elif db_error == ERROR_DISK_QUEUED and ctx.disk_available:
        return Decision("resume", clear_error_code=True)

    # Unknown-size HTTP tasks are submitted with ``pause=true`` so the
    # coordinator can admit size before the first write.  The admission
    # pause is system-owned (``admission_paused``); policy must resume it.
    if status == "paused":
        if db_error == "admission_paused":
            return Decision("resume", clear_error_code=True)
        if db_error in SYSTEM_QUEUE_CODES or ctx.system_pause:
            # Still waiting on the blocking resource: hold the pause.
            return Decision("keep", error_code=db_error)
        if db_error is None:
            # External pause: record it, never auto-unpause.
            return Decision("mark_external_paused", error_code="external_paused")
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
    """Handoff eligibility once size is known (metadata resolved / follow).

    Returns the eligibility verdict; the caller maps it to a Decision.
    """
    if quota_bytes is not None:
        if size_bytes > quota_bytes:
            return "exceeded"
        if size_bytes > quota_bytes - quota_used_bytes:
            return "quota_queued"
    if not disk_ok:
        return "disk_queued"
    return "ok"
