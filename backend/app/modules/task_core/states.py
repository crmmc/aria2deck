"""Task Core internal state model.

Tid (global_downloads.id) is the internal task identity. Pid (user_tasks.id)
is the user-facing identity. v1 keeps using the existing DB status strings
verbatim — no migration — and layers an explicit projection on top.
"""

from __future__ import annotations

from enum import StrEnum

# Error codes are a side-channel to the user projection: they explain *why*
# a tid is in a given state (queued by quota, paused externally, etc.) so the
# UI can render the right label without guessing from progress bytes.
ERROR_QUOTA_QUEUED = "quota_queued"
ERROR_DISK_QUEUED = "disk_queued"
ERROR_QUOTA_EXCEEDED = "quota_exceeded"
ERROR_EXTERNAL_PAUSED = "external_paused"
ERROR_MAX_TASK_SIZE_EXCEEDED = "max_task_size_exceeded"
# System-owned pause after magnet pause-metadata; only handoff/policy may resume.
ERROR_METADATA_ADMISSION_PAUSED = "metadata_admission_paused"
ERROR_ADMISSION_PAUSED = "admission_paused"
ERROR_UNPAUSE_FAILED = "unpause_failed"
ERROR_GROWTH_UNPAUSE_FAILED = "growth_unpause_failed"
ERROR_GROWTH_PAUSE_FAILED = "growth_pause_failed"

# Codes that may auto-resume under policy with an explicit predicate (M9).
# growth_pause_failed is system-owned but never auto-resumed while paused.
PENDING_RELEASE_CODES = frozenset(
    {
        ERROR_ADMISSION_PAUSED,
        ERROR_METADATA_ADMISSION_PAUSED,
        ERROR_UNPAUSE_FAILED,
        ERROR_GROWTH_UNPAUSE_FAILED,
    }
)

# Single source for system-owned pause/resume codes (M7).
# Projection must not overwrite these with external_paused; policy may
# resume only with an explicit predicate (never from size_known alone).
# SYSTEM_OWNED = PENDING ∪ quota_queued ∪ disk_queued ∪ growth_pause_failed
SYSTEM_OWNED_PAUSE_CODES = PENDING_RELEASE_CODES | frozenset(
    {
        ERROR_GROWTH_PAUSE_FAILED,
        ERROR_QUOTA_QUEUED,
        ERROR_DISK_QUEUED,
    }
)

# Codes that block branding a pause as external (owned + hard rejects).
PROJECTION_PROTECTED_ERROR_CODES = SYSTEM_OWNED_PAUSE_CODES | frozenset(
    {
        "handoff_unknown_size",
        "unknown_size",
        "disk_budget",
        "disk_budget_exceeded",
        "max_task_size",
        "admission_rejected",
    }
)

# Sticky codes cleared when aria2 maps to active (owned + external/admin).
ACTIVE_CLEAR_ERROR_CODES = SYSTEM_OWNED_PAUSE_CODES | frozenset(
    {
        ERROR_EXTERNAL_PAUSED,
        "admin_paused",
    }
)


class TidState(StrEnum):
    """Internal task state; values match the existing DB strings."""

    QUEUED = "queued"
    ACTIVE = "active"
    WAITING = "waiting"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PidState(StrEnum):
    """User-visible state; collapses internal nuances."""

    DOWNLOADING = "downloading"
    QUEUED = "queued"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


def to_db_status(state: TidState | str) -> str:
    """Return the DB string for a TidState. Accepts raw strings for interop."""
    if isinstance(state, TidState):
        return state.value
    return str(state)


_TID_TO_PID: dict[TidState, PidState] = {
    TidState.QUEUED: PidState.QUEUED,
    TidState.ACTIVE: PidState.DOWNLOADING,
    TidState.WAITING: PidState.DOWNLOADING,
    TidState.PAUSED: PidState.PAUSED,
    TidState.COMPLETED: PidState.COMPLETED,
    TidState.FAILED: PidState.FAILED,
    TidState.CANCELLED: PidState.CANCELLED,
}


def tid_to_pid(state: TidState) -> PidState:
    """Project an internal state to the user-visible state."""
    return _TID_TO_PID[state]
