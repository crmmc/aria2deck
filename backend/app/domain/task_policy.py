from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.domain.status import (
    ACTIVE_LIKE_DOWNLOAD_STATUSES,
    ACTIVE_USER_TASK_STATUSES,
    ERROR_DOWNLOAD_STATUSES,
    REST_TASK_STATUS_FILTERS,
    TERMINAL_DOWNLOAD_STATUSES,
)

RETRYABLE_DOWNLOAD_STATUSES = frozenset(ERROR_DOWNLOAD_STATUSES)
RETRYABLE_TASK_STATUSES = frozenset(ERROR_DOWNLOAD_STATUSES)
CANCELABLE_TASK_STATUSES = frozenset(ACTIVE_USER_TASK_STATUSES)


class InvalidTaskStatusFilter(ValueError):
    pass


def effective_status(row: Mapping[str, Any]) -> str:
    user_status = str(row.get("status") or "")
    if user_status in TERMINAL_DOWNLOAD_STATUSES:
        return user_status

    global_status = str(row.get("global_status") or user_status)
    if global_status in TERMINAL_DOWNLOAD_STATUSES:
        return global_status
    return user_status


def legacy_rest_status(status: str) -> str:
    if status == "completed":
        return "complete"
    if status in ERROR_DOWNLOAD_STATUSES:
        return "error"
    return status


def aria2_status(status: str) -> str:
    if status == "completed":
        return "complete"
    if status in ERROR_DOWNLOAD_STATUSES:
        return "error"
    if status == "queued":
        return "waiting"
    return status


def is_user_terminal(row: Mapping[str, Any]) -> bool:
    return str(row.get("status") or "") in TERMINAL_DOWNLOAD_STATUSES


def is_current(row: Mapping[str, Any]) -> bool:
    return effective_status(row) in ACTIVE_LIKE_DOWNLOAD_STATUSES


def can_cancel(status: str) -> bool:
    return status in CANCELABLE_TASK_STATUSES


def can_retry(status: str) -> bool:
    return status in RETRYABLE_TASK_STATUSES


def filter_rows_for_status(
    rows: list[dict[str, Any]], status_filter: str | None
) -> list[dict[str, Any]]:
    if status_filter is None:
        return rows
    if status_filter not in REST_TASK_STATUS_FILTERS:
        raise InvalidTaskStatusFilter(status_filter)
    if status_filter in {"active", "current"}:
        return [row for row in rows if is_current(row)]
    if status_filter == "complete":
        return [row for row in rows if effective_status(row) == "completed"]
    if status_filter == "error":
        return [
            row for row in rows if effective_status(row) in ERROR_DOWNLOAD_STATUSES
        ]
    return rows


def stat_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    active = 0
    waiting = 0
    stopped = 0
    for row in rows:
        status = effective_status(row)
        if status == "active":
            active += 1
        elif status in {"queued", "waiting", "paused"}:
            waiting += 1
        elif status in TERMINAL_DOWNLOAD_STATUSES:
            stopped += 1
    return {
        "active": active,
        "waiting": waiting,
        "stopped": stopped,
        "current": active + waiting,
    }
