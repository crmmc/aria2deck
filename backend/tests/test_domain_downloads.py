from __future__ import annotations

import pytest

from app.domain.downloads import (
    ACTIVE_LIKE_DOWNLOAD_STATUSES,
    CANCELABLE_TASK_STATUSES,
    ERROR_DOWNLOAD_STATUSES,
    REST_TASK_STATUS_FILTERS,
    RETRYABLE_DOWNLOAD_STATUSES,
    TERMINAL_DOWNLOAD_STATUSES,
    InvalidTaskStatusFilter,
    aria2_status,
    effective_status,
    filter_rows_for_status,
    is_current,
    is_user_terminal,
    legacy_rest_status,
    stat_counts,
)


def test_status_sets_capture_current_download_language() -> None:
    assert ACTIVE_LIKE_DOWNLOAD_STATUSES == ("queued", "active", "waiting", "paused")
    assert TERMINAL_DOWNLOAD_STATUSES == ("completed", "failed", "cancelled")
    assert ERROR_DOWNLOAD_STATUSES == ("failed", "cancelled")
    assert RETRYABLE_DOWNLOAD_STATUSES == frozenset({"failed", "cancelled"})
    assert CANCELABLE_TASK_STATUSES == frozenset(
        {"queued", "active", "waiting", "paused"}
    )
    assert REST_TASK_STATUS_FILTERS == frozenset(
        {"active", "current", "complete", "error"}
    )


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        ({"status": "active", "global_status": "active"}, "active"),
        ({"status": "completed", "global_status": "active"}, "completed"),
        ({"status": "active", "global_status": "failed"}, "failed"),
        ({"status": "paused", "global_status": None}, "paused"),
    ],
)
def test_effective_status_prefers_terminal_user_or_global_status(
    row: dict[str, object], expected: str
) -> None:
    assert effective_status(row) == expected


@pytest.mark.parametrize(
    ("status", "legacy", "aria2"),
    [
        ("completed", "complete", "complete"),
        ("failed", "error", "error"),
        ("cancelled", "error", "error"),
        ("queued", "queued", "waiting"),
        ("active", "active", "active"),
    ],
)
def test_status_mappings(status: str, legacy: str, aria2: str) -> None:
    assert legacy_rest_status(status) == legacy
    assert aria2_status(status) == aria2


def test_row_predicates_use_effective_status_language() -> None:
    assert is_user_terminal({"status": "failed"}) is True
    assert is_user_terminal({"status": "active"}) is False
    assert is_current({"status": "active", "global_status": "active"}) is True
    assert is_current({"status": "active", "global_status": "completed"}) is False


def test_filter_rows_for_status_maps_rest_filters() -> None:
    rows = [
        {"id": 1, "status": "active", "global_status": "active"},
        {"id": 2, "status": "waiting", "global_status": "waiting"},
        {"id": 3, "status": "active", "global_status": "completed"},
        {"id": 4, "status": "failed", "global_status": "failed"},
    ]

    assert [row["id"] for row in filter_rows_for_status(rows, None)] == [1, 2, 3, 4]
    assert [row["id"] for row in filter_rows_for_status(rows, "active")] == [1, 2]
    assert [row["id"] for row in filter_rows_for_status(rows, "current")] == [1, 2]
    assert [row["id"] for row in filter_rows_for_status(rows, "complete")] == [3]
    assert [row["id"] for row in filter_rows_for_status(rows, "error")] == [4]


def test_filter_rows_for_status_rejects_unknown_filter() -> None:
    with pytest.raises(InvalidTaskStatusFilter) as exc_info:
        filter_rows_for_status([], "done")

    assert exc_info.value.args == ("done",)


def test_stat_counts_uses_domain_status_groups() -> None:
    rows = [
        {"status": "active", "global_status": "active"},
        {"status": "queued", "global_status": "queued"},
        {"status": "waiting", "global_status": "waiting"},
        {"status": "completed", "global_status": "completed"},
        {"status": "active", "global_status": "failed"},
    ]

    assert stat_counts(rows) == {
        "active": 1,
        "waiting": 2,
        "stopped": 2,
        "current": 3,
    }
