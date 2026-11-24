from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ReconcileResult(StrEnum):
    """Internal outcome of ``reconcile_attempt_signal`` (spec §6.1, §20)."""

    CHANGED = "changed"
    STALE = "stale"
    WAITING = "waiting"
    COMPLETED = "completed"
    TERMINALIZED = "terminalized"
    IGNORED = "ignored"
    ALREADY_TERMINAL = "already_terminal"
    RECOVERY_PENDING = "recovery_pending"
    ALREADY_ACTIVE = "already_active"
    ALREADY_COMPLETE = "already_complete"
    CLEANUP_PENDING = "cleanup_pending"


@dataclass(frozen=True)
class TerminalizationClaim:
    """Authorization produced by a successful terminal CAS (spec §10.2–10.3).

    ``writer_gids`` is every GID that may still be writing to disk for this
    attempt; ``result_gids`` is the narrower set whose stopped results may be
    removed.  This claim is the sole credential ``reclaim`` accepts.
    """

    attempt_id: int
    expected_current_gid: str | None
    writer_gids: tuple[str, ...]
    result_gids: tuple[str, ...]
    terminal_status: str
    claim_timestamp: int
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class RepairClaim:
    """Physical-reclaim authorization for an already-terminal attempt (§10.4).

    Does not change the business terminal state — it is already set.  Only
    grants the current round of physical reclamation after CAS-confirming the
    attempt is still failed/cancelled with an unchanged GID.
    """

    attempt_id: int
    expected_current_gid: str | None
    writer_gids: tuple[str, ...]
    result_gids: tuple[str, ...]
    terminal_status: str
    claim_timestamp: int


def make_terminalization_claim(
    *,
    attempt_id: int,
    expected_current_gid: str | None,
    writer_gids: tuple[str, ...] | list[str],
    result_gids: tuple[str, ...] | list[str],
    terminal_status: str,
    claim_timestamp: int,
    error_code: str | None = None,
    error_message: str | None = None,
) -> TerminalizationClaim:
    return TerminalizationClaim(
        attempt_id=attempt_id,
        expected_current_gid=expected_current_gid,
        writer_gids=tuple(writer_gids),
        result_gids=tuple(result_gids),
        terminal_status=terminal_status,
        claim_timestamp=claim_timestamp,
        error_code=error_code,
        error_message=error_message,
    )


def make_repair_claim(
    *,
    attempt_id: int,
    expected_current_gid: str | None,
    writer_gids: tuple[str, ...] | list[str],
    result_gids: tuple[str, ...] | list[str],
    terminal_status: str,
    claim_timestamp: int,
) -> RepairClaim:
    return RepairClaim(
        attempt_id=attempt_id,
        expected_current_gid=expected_current_gid,
        writer_gids=tuple(writer_gids),
        result_gids=tuple(result_gids),
        terminal_status=terminal_status,
        claim_timestamp=claim_timestamp,
    )
