"""In-process read model for observed task details (speed, file list, ...).

State truth lives in the DB (global_downloads rows); this store only caches
the latest sanitized aria2 observation per tid in memory, replacing the
legacy task_backend_snapshots raw_json for read paths.

Single-process boundary: production runs one uvicorn process with one event
loop, so the store is synchronous and lock-free (all access from the loop).
A future multi-worker deployment must swap this module for a shared store —
it is the single replacement point.
"""

from __future__ import annotations

from dataclasses import dataclass

STALE_MS = 15_000  # freshness window: older entries count as missing
TERMINAL_TTL_MS = 600_000  # how long terminal entries are retained
_OBSERVED_TERMINAL_STATUSES = {"complete", "error", "removed"}  # sanitized


@dataclass
class ObservedDetail:
    sanitized: dict  # full sanitize_status output (equiv. legacy raw_json)
    updated_at_ms: int
    terminal_at_ms: int | None  # stamped when observed status turns terminal


_details: dict[int, ObservedDetail] = {}


def record_observed_detail(tid: int, sanitized: dict, updated_at_ms: int) -> None:
    """Insert/overwrite the entry for ``tid``; sweep expired rows.

    The sweep runs on every write with ``updated_at_ms`` as "now": entries
    older than TERMINAL_TTL_MS are dropped regardless of terminal stamping
    (terminal-stamped entries are a subset), so the map stays bounded even
    when a terminal observation never arrives (e.g. a lost cancel event).
    """
    terminal_at_ms = (
        updated_at_ms if sanitized.get("status") in _OBSERVED_TERMINAL_STATUSES else None
    )
    _details[tid] = ObservedDetail(
        sanitized=sanitized,
        updated_at_ms=updated_at_ms,
        terminal_at_ms=terminal_at_ms,
    )
    expired = [
        tid_
        for tid_, detail in _details.items()
        if updated_at_ms - detail.updated_at_ms > TERMINAL_TTL_MS
    ]
    for tid_ in expired:
        del _details[tid_]


def get_observed_detail(tid: int) -> ObservedDetail | None:
    """Raw entry, no freshness filtering."""
    return _details.get(tid)


def evict(tid: int) -> None:
    _details.pop(tid, None)


def clear() -> None:
    _details.clear()


def matches_row_gid(detail: ObservedDetail | None, row_gid: str | None) -> bool:
    """Strict gid equality; conservative False on missing/empty/mismatch."""
    if detail is None or not row_gid:
        return False
    return detail.sanitized.get("gid") == row_gid
