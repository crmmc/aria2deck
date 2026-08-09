"""Unref: user-facing cancel of one pid, with zero-ref reclaim of the tid.

Behavior:
- Cancel the pid owned by ``user_id``; other active subscribers on the same
  tid keep the attempt live (AC-4 unbind).
- When the last active subscriber unrefs, the attempt is terminalized via
  ``cancel_user_task_and_maybe_claim_attempt`` (same CAS + budget semantics
  as the existing claim path) and, when a ``BackendPort`` is supplied, the
  backend handle is removed via ``backend.remove(tid)`` (AC-3 reclaim).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.modules.backend.port import BackendPort
from app.repositories.task.user_tasks import cancel_user_task_and_maybe_claim_attempt

ERROR_NOT_FOUND = "not_found"
ERROR_FORBIDDEN = "forbidden"
ERROR_ALREADY_TERMINAL = "already_terminal"

NOT_FOUND_MESSAGE = "任务不存在"
FORBIDDEN_MESSAGE = "任务不属于当前用户"
ALREADY_TERMINAL_MESSAGE = "任务已结束"


class UnrefError(Exception):
    """Structured unref failure with a stable error code."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class UnrefResult:
    pid: int
    tid: int
    status: str  # terminal status of the pid (cancelled)
    reclaimed: bool  # True when this unref terminalized the tid (last ref)
    tid_status: str | None  # terminal tid status when reclaimed, else None


async def unref(
    *,
    user_id: int,
    pid: int,
    backend: BackendPort | None = None,
    error_message: str = "用户取消",
    expected_gid: str | None = None,
) -> UnrefResult:
    """Cancel one user task (pid) and reclaim the tid if this was the last ref.

    ``expected_gid`` fences the terminal CAS on the gid the caller observed;
    pass the gid bound at submission time for legacy (v0) attempts.

    Raises ``UnrefError`` with a stable code when the pid does not exist,
    belongs to another user, or is already terminal.
    """
    updated_task, claim = await cancel_user_task_and_maybe_claim_attempt(
        user_id=user_id,
        user_task_id=pid,
        expected_gid=expected_gid,
        error_message=error_message,
    )
    if updated_task is None:
        # The repository folds not-found / not-owned / already-terminal into
        # a single ``None``; distinguish with a follow-up read for the API.
        raise await _diagnose_unref_failure(user_id=user_id, pid=pid)

    tid = int(updated_task["global_download_id"])
    reclaimed = claim is not None
    tid_status: str | None = None

    if reclaimed:
        tid_status = str(claim.terminal_status) if claim is not None else None
        if backend is not None:
            await backend.remove(tid)

    return UnrefResult(
        pid=pid,
        tid=tid,
        status=str(updated_task["status"]),
        reclaimed=reclaimed,
        tid_status=tid_status,
    )


async def _diagnose_unref_failure(*, user_id: int, pid: int) -> UnrefError:
    """Map a failed unref to a stable error code for the caller."""
    from sqlalchemy import select

    from app.db.engine import transaction
    from app.db.schema import user_tasks
    from app.domain.status import ACTIVE_USER_TASK_STATUSES

    async with transaction() as conn:
        row = (
            (
                await conn.execute(
                    select(user_tasks.c.user_id, user_tasks.c.status).where(
                        user_tasks.c.id == pid
                    )
                )
            )
            .mappings()
            .first()
        )
    if row is None:
        return UnrefError(ERROR_NOT_FOUND, NOT_FOUND_MESSAGE)
    if int(row["user_id"]) != int(user_id):
        return UnrefError(ERROR_FORBIDDEN, FORBIDDEN_MESSAGE)
    if str(row["status"]) not in ACTIVE_USER_TASK_STATUSES:
        return UnrefError(ERROR_ALREADY_TERMINAL, ALREADY_TERMINAL_MESSAGE)
    # Defensive: repository declined but the row looks active and owned.
    return UnrefError(ERROR_NOT_FOUND, NOT_FOUND_MESSAGE)
