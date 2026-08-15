from app.domain.status import REST_TASK_STATUS_FILTERS
from app.services.task_projection import (
    BT_TRACKER_PLACEHOLDER,
    METADATA_NAME_PREFIX,
    InvalidTaskStatusFilter,
    build_aria2_status,
    build_rest_task_response,
    effective_status,
    filter_rows_for_status,
    has_real_file_path,
    is_metadata_phase_status,
    speed_totals,
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
        "bt_info_hash": None,
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


def test_filter_rows_for_status_none_returns_all_rows() -> None:
    rows = [
        _row(user_status="active", global_status="active", name="active.bin"),
        _row(user_status="completed", global_status="completed", name="done.bin"),
    ]

    assert filter_rows_for_status(rows, None) == rows


def test_filter_rows_for_status_rejects_unknown_filter() -> None:
    rows = [_row(user_status="active", global_status="active", name="active.bin")]

    try:
        filter_rows_for_status(rows, "bogus")
    except InvalidTaskStatusFilter as exc:
        assert exc.args == ("bogus",)
    else:
        raise AssertionError("InvalidTaskStatusFilter was not raised")


def test_rest_task_status_filter_whitelist_is_explicit() -> None:
    assert REST_TASK_STATUS_FILTERS == frozenset(
        {"active", "current", "complete", "error"}
    )


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


def test_rest_response_uses_live_speeds_for_current_task() -> None:
    response = build_rest_task_response(
        _row(user_status="active", global_status="active"),
        {"downloadSpeed": "4096", "uploadSpeed": "128"},
    )

    assert response["download_speed"] == 4096
    assert response["upload_speed"] == 128


def test_rest_response_zeroes_live_speeds_for_terminal_task() -> None:
    response = build_rest_task_response(
        _row(user_status="completed", global_status="completed"),
        {"downloadSpeed": "4096", "uploadSpeed": "128"},
    )

    assert response["download_speed"] == 0
    assert response["upload_speed"] == 0


def test_speed_totals_sum_current_rows_only() -> None:
    rows = [
        {**_row(user_status="active", global_status="active", name="a.bin"), "aria2_gid": "gid-a"},
        {**_row(user_status="paused", global_status="active", name="b.bin"), "aria2_gid": "gid-b"},
        {**_row(user_status="completed", global_status="completed", name="c.bin"), "aria2_gid": "gid-c"},
    ]
    live_by_gid = {
        "gid-a": {"downloadSpeed": "100", "uploadSpeed": "10"},
        "gid-b": {"downloadSpeed": "200", "uploadSpeed": "20"},
        "gid-c": {"downloadSpeed": "999", "uploadSpeed": "999"},
    }

    assert speed_totals(rows, live_by_gid) == {
        "download_speed": 300,
        "upload_speed": 30,
    }


# ---------------------------------------------------------------------------
# Metadata phase detection
# ---------------------------------------------------------------------------

def test_is_metadata_phase_status_detects_metadata_prefix() -> None:
    status = {
        "files": [{"path": "[METADATA]abc123"}],
    }
    assert is_metadata_phase_status(status) is True


def test_is_metadata_phase_status_rejects_when_bittorrent_info_exists() -> None:
    status = {
        "bittorrent": {"info": {"name": "[METADATA]abc123"}},
        "files": [{"path": "[METADATA]abc123"}],
    }
    assert is_metadata_phase_status(status) is False


def test_is_metadata_phase_status_passes_real_name() -> None:
    status = {
        "bittorrent": {"info": {"name": "Ubuntu.24.04.iso"}},
        "files": [{"path": "/downloads/Ubuntu.24.04.iso"}],
    }
    assert is_metadata_phase_status(status) is False


def test_is_metadata_phase_status_returns_false_without_files() -> None:
    assert is_metadata_phase_status({}) is False
    assert is_metadata_phase_status({"files": []}) is False
    assert is_metadata_phase_status({"files": [{}]}) is False


def test_is_metadata_phase_status_returns_false_without_metadata_prefix() -> None:
    status = {"files": [{"path": "/downloads/file.iso"}]}
    assert is_metadata_phase_status(status) is False


def test_metadata_name_prefix_is_exact_string() -> None:
    assert METADATA_NAME_PREFIX == "[METADATA]"


# ---------------------------------------------------------------------------
# Live-first projection: normal (non-metadata) active task
# ---------------------------------------------------------------------------

def test_rest_response_prefers_live_progress_for_active_task() -> None:
    row = _row(user_status="active", global_status="active",
               total_bytes=100, completed_bytes=50)
    live = {"totalLength": "2000", "completedLength": "1000",
            "downloadSpeed": "500", "uploadSpeed": "0"}

    response = build_rest_task_response(row, live)

    assert response["total_length"] == 2000
    assert response["completed_length"] == 1000
    assert response["download_speed"] == 500


def test_rest_response_prefers_live_bt_name_for_active_task() -> None:
    row = {
        **_row(user_status="active", global_status="active", name="old.bin"),
        "resource_kind": "torrent",
    }
    live = {"bittorrent": {"info": {"name": "real_file.iso"}},
            "downloadSpeed": "0", "uploadSpeed": "0"}

    response = build_rest_task_response(row, live)

    assert response["name"] == "real_file.iso"


def test_http_rest_response_ignores_bt_name_without_live_evidence() -> None:
    row = _row(user_status="active", global_status="active", name="old.bin")
    live = {
        "bittorrent": {"info": {"name": "real_file.iso"}},
        "totalLength": "2000",
        "completedLength": "1000",
        "downloadSpeed": "0",
        "uploadSpeed": "0",
    }

    response = build_rest_task_response(row, live)

    assert response["name"] == "old.bin"
    assert response["total_length"] == 2000
    assert response["completed_length"] == 1000


def test_http_torrent_conversion_projects_bittorrent_with_live_infohash() -> None:
    info_hash = "0123456789abcdef0123456789abcdef01234567"
    row = _row(
        user_status="active",
        global_status="active",
        name="payload.torrent",
    )
    live = {
        "infoHash": info_hash,
        "bittorrent": {"mode": "multi", "info": {"name": "real torrent"}},
    }

    result = build_aria2_status(row, live)

    assert result["infoHash"] == info_hash
    assert result["bittorrent"] == {
        "announceList": [[BT_TRACKER_PLACEHOLDER]],
        "comment": "",
        "creationDate": 0,
        "mode": "multi",
        "info": {"name": "real torrent"},
    }


def test_stopped_torrent_status_uses_row_info_hash_when_live_missing() -> None:
    info_hash = "0123456789abcdef0123456789abcdef01234567"
    row = {
        **_row(
            user_status="completed",
            global_status="completed",
            name="done.torrent",
            total_bytes=10,
            completed_bytes=10,
        ),
        "resource_kind": "torrent",
        "resource_key": f"torrent:{info_hash}",
        "source_uri": f"magnet:?xt=urn:btih:{info_hash}",
    }

    result = build_aria2_status(row)

    assert result["status"] == "complete"
    assert result["infoHash"] == info_hash
    assert result["bittorrent"]["info"]["name"] == "done.torrent"


def test_stopped_torrent_status_prefers_bt_info_hash_field() -> None:
    info_hash = "abcdefabcdefabcdefabcdefabcdefabcdefabcd"
    row = {
        **_row(
            user_status="completed",
            global_status="completed",
            name="payload.torrent",
            total_bytes=10,
            completed_bytes=10,
        ),
        "resource_kind": "torrent",
        "resource_key": "sync:http-torrent-upgrade",
        "source_uri": "https://example.com/payload.torrent",
        "bt_info_hash": info_hash.upper(),
    }

    result = build_aria2_status(row)

    assert result["status"] == "complete"
    assert result["infoHash"] == info_hash


def test_rest_response_uses_db_for_terminal_task_even_with_live() -> None:
    row = _row(user_status="completed", global_status="completed",
               total_bytes=999, completed_bytes=999, name="done.bin")
    live = {"totalLength": "1", "completedLength": "0"}

    response = build_rest_task_response(row, live)

    assert response["total_length"] == 999
    assert response["completed_length"] == 999
    assert response["name"] == "done.bin"


# ---------------------------------------------------------------------------
# Live-first projection: metadata phase
# ---------------------------------------------------------------------------

def test_rest_response_skips_metadata_total_but_keeps_completed() -> None:
    """During metadata phase, totalLength is the tiny metadata size — skip it.
    completedLength should still pass through so the user sees activity."""
    row = {
        **_row(user_status="active", global_status="active",
               total_bytes=0, completed_bytes=0),
        "resource_kind": "magnet",
    }
    live = {"totalLength": "32768", "completedLength": "16384",
            "files": [{"path": "[METADATA]abc"}],
            "downloadSpeed": "5000", "uploadSpeed": "0"}

    response = build_rest_task_response(row, live)

    assert response["total_length"] == 0           # DB fallback (not metadata size)
    assert response["completed_length"] == 16384   # live passthrough
    assert response["download_speed"] == 5000      # speed always from live


def test_rest_response_filters_metadata_name_uses_db_fallback() -> None:
    row = {
        **_row(user_status="active", global_status="active", name="global.bin",
               user_name="magnet:?xt=urn:btih:abc"),
        "resource_kind": "magnet",
    }
    live = {"bittorrent": {"info": {"name": "[METADATA]ida93"}},
            "downloadSpeed": "0", "uploadSpeed": "0"}

    response = build_rest_task_response(row, live)

    # [METADATA] name filtered, falls back to user_name from row
    assert response["name"] == "magnet:?xt=urn:btih:abc"
    assert METADATA_NAME_PREFIX not in response["name"]


def test_task_list_projection_retryable_for_terminal_failed() -> None:
    response = build_rest_task_response(
        _row(user_status="failed", global_status="failed", name="fail.bin")
    )
    assert response["retryable"] is True
    assert response["retry_blocked_reason"] is None


def test_task_list_projection_retryable_false_when_expired() -> None:
    row = _row(user_status="failed", global_status="failed", name="old.bin")
    row["history_expired_at_ms"] = 1_700_000_000_000
    response = build_rest_task_response(row)
    assert response["retryable"] is False
    assert response["retry_blocked_reason"] is not None
    assert "已过期" in response["retry_blocked_reason"]


def test_task_list_projection_retryable_false_for_completed() -> None:
    response = build_rest_task_response(
        _row(user_status="completed", global_status="completed", name="done.bin")
    )
    assert response["retryable"] is False
    assert response["retry_blocked_reason"] is not None


def test_projected_speeds_zero_for_paused_snapshot() -> None:
    """幽灵速度回归：paused 观测不得把上一轮速度带给 REST/WS/stats。"""
    row = _row(user_status="active", global_status="active")
    row["backend_snapshot"] = {
        "status": "paused",
        "downloadSpeed": "999",
        "uploadSpeed": "111",
    }
    response = build_rest_task_response(row)
    assert response["download_speed"] == 0
    assert response["upload_speed"] == 0


def test_projected_speeds_zero_for_terminal_row_with_active_snapshot() -> None:
    row = _row(user_status="failed", global_status="failed")
    row["backend_snapshot"] = {
        "status": "active",
        "downloadSpeed": "777",
        "uploadSpeed": "0",
    }
    response = build_rest_task_response(row)
    assert response["download_speed"] == 0


def test_aria2_status_zero_speed_for_paused_observation() -> None:
    """RPC 投影：paused/终态观测速度清零，对齐真实 aria2 语义。"""
    row = _row(user_status="active", global_status="active")
    row["backend_snapshot"] = {
        "status": "paused",
        "totalLength": "2048",
        "completedLength": "1024",
        "downloadSpeed": "555",
        "uploadSpeed": "5",
    }
    status = build_aria2_status(row)
    assert status["downloadSpeed"] == "0"
    assert status["uploadSpeed"] == "0"
