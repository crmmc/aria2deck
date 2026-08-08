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
