from __future__ import annotations

from app.services.task_projection import (
    build_aria2_status,
    build_rest_task_response,
    effective_status,
    filter_rows_for_status,
    has_real_file_path,
    stat_counts,
)


def _row(
    *,
    user_status: str,
    global_status: str,
    name: str = "file.bin",
    user_name: str | None = None,
    completed_bytes: int = 3,
    total_bytes: int = 10,
) -> dict:
    return {
        "id": 10,
        "user_id": 1,
        "global_download_id": 20,
        "status": user_status,
        "reserved_bytes": 7,
        "display_name": user_name,
        "error_message": None,
        "created_at_ms": 1_700_000_000_000,
        "updated_at_ms": 1_700_000_001_000,
        "finished_at_ms": None,
        "resource_key": "http:file",
        "resource_kind": "http",
        "source_uri": "https://example.com/file.bin",
        "global_display_name": name,
        "aria2_gid": "gid-1",
        "global_status": global_status,
        "total_bytes": total_bytes,
        "completed_bytes": completed_bytes,
        "error_code": None,
        "global_error_message": None,
        "completed_at_ms": None,
    }


def test_effective_status_user_terminal_wins_over_live_global() -> None:
    row = _row(user_status="cancelled", global_status="active")

    assert effective_status(row) == "cancelled"
    assert build_aria2_status(row, {"status": "active"})["status"] == "error"


def test_effective_status_global_terminal_wins_for_active_user_task() -> None:
    assert effective_status(_row(user_status="active", global_status="completed")) == "completed"
    assert effective_status(_row(user_status="active", global_status="failed")) == "failed"
    assert effective_status(_row(user_status="queued", global_status="cancelled")) == "cancelled"


def test_aria2_status_keeps_global_terminal_over_live_active_status() -> None:
    live = {"status": "active"}

    assert build_aria2_status(_row(user_status="active", global_status="completed"), live)["status"] == "complete"
    assert build_aria2_status(_row(user_status="active", global_status="failed"), live)["status"] == "error"
    assert build_aria2_status(_row(user_status="active", global_status="cancelled"), live)["status"] == "error"


def test_rest_response_maps_effective_status_and_uses_progress() -> None:
    response = build_rest_task_response(
        _row(
            user_status="active",
            global_status="completed",
            name="global.bin",
            user_name="mine.bin",
            completed_bytes=10,
            total_bytes=10,
        )
    )

    assert response["status"] == "complete"
    assert response["name"] == "mine.bin"
    assert response["total_length"] == 10
    assert response["completed_length"] == 10


def test_filter_rows_for_status_uses_effective_state() -> None:
    rows = [
        _row(user_status="active", global_status="active", name="active.bin"),
        _row(user_status="active", global_status="completed", name="done.bin"),
        _row(user_status="active", global_status="failed", name="failed.bin"),
        _row(user_status="completed", global_status="active", name="user-done.bin"),
    ]

    assert [r["global_display_name"] for r in filter_rows_for_status(rows, "current")] == ["active.bin"]
    assert [r["global_display_name"] for r in filter_rows_for_status(rows, "complete")] == [
        "done.bin",
        "user-done.bin",
    ]
    assert [r["global_display_name"] for r in filter_rows_for_status(rows, "error")] == ["failed.bin"]


def test_stat_counts_match_effective_projection() -> None:
    rows = [
        _row(user_status="active", global_status="active"),
        _row(user_status="queued", global_status="active"),
        _row(user_status="paused", global_status="active"),
        _row(user_status="active", global_status="completed"),
        _row(user_status="failed", global_status="active"),
    ]

    assert stat_counts(rows) == {"active": 1, "waiting": 2, "stopped": 2, "current": 3}


def test_live_uri_like_file_path_does_not_override_projected_file_name() -> None:
    live = {
        "files": [
            {
                "path": "magnet:?xt=urn:btih:abc123",
                "length": "10",
                "completedLength": "10",
            }
        ]
    }

    assert not has_real_file_path(live)
    assert build_aria2_status(
        _row(user_status="active", global_status="active", name="payload.bin"),
        live,
    )["files"][0]["path"] == "payload.bin"
