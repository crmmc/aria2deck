"""M3 T07: 投影渲染从 row.backend_snapshot 取数（无外部 live）。

调用方不再传 live 时，渲染层从 row 内的 ``backend_snapshot`` / ``backend_files``
（T06 由 ``list_user_task_projections`` join）读取速度、进度与文件。
"""

from __future__ import annotations

from typing import Any

from app.services.task_projection import (
    build_aria2_status,
    build_rest_task_response,
    projected_speeds,
)


def _row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": 10,
        "user_id": 1,
        "global_download_id": 20,
        "status": "active",
        "reserved_bytes": 0,
        "display_name": None,
        "error_message": None,
        "created_at_ms": 1_700_000_000_000,
        "updated_at_ms": 1_700_000_001_000,
        "finished_at_ms": None,
        "resource_key": "magnet:abc",
        "resource_kind": "magnet",
        "source_uri": "magnet:?xt=urn:btih:abc",
        "bt_info_hash": None,
        "global_display_name": "snapshot.bin",
        "aria2_gid": "gid-1",
        "global_status": "active",
        "total_bytes": 100,
        "completed_bytes": 40,
        "error_code": None,
        "global_error_message": None,
        "completed_at_ms": None,
    }
    row.update(overrides)
    return row


def test_projected_speeds_read_backend_snapshot_without_live() -> None:
    row = _row(
        backend_snapshot={"downloadSpeed": "4096", "uploadSpeed": "128"},
    )

    assert projected_speeds(row) == (4096, 128)


def test_rest_response_uses_backend_snapshot_without_live() -> None:
    row = _row(
        backend_snapshot={
            "totalLength": "2000",
            "completedLength": "1000",
            "downloadSpeed": "500",
            "uploadSpeed": "0",
        },
    )

    response = build_rest_task_response(row)

    assert response["total_length"] == 2000
    assert response["completed_length"] == 1000
    assert response["download_speed"] == 500
    assert response["upload_speed"] == 0


def test_rest_response_ignores_snapshot_speeds_for_terminal_task() -> None:
    row = _row(
        status="completed",
        global_status="completed",
        total_bytes=999,
        completed_bytes=999,
        backend_snapshot={"downloadSpeed": "4096", "uploadSpeed": "128"},
    )

    response = build_rest_task_response(row)

    assert response["download_speed"] == 0
    assert response["upload_speed"] == 0


def test_aria2_status_files_come_from_backend_snapshot() -> None:
    files = [
        {
            "index": "1",
            "path": "/downloads/real.iso",
            "length": "2000",
            "completedLength": "1000",
            "selected": "true",
            "uris": [],
        }
    ]
    row = _row(
        backend_snapshot={
            "status": "active",
            "totalLength": "2000",
            "completedLength": "1000",
            "downloadSpeed": "0",
            "uploadSpeed": "0",
            "files": files,
        },
        backend_files=files,
    )

    result = build_aria2_status(row)

    assert result["status"] == "active"
    assert result["totalLength"] == "2000"
    assert result["completedLength"] == "1000"
    assert result["files"] == files


def test_aria2_status_snapshot_status_does_not_override_terminal_row() -> None:
    row = _row(
        status="active",
        global_status="completed",
        total_bytes=10,
        completed_bytes=10,
        backend_snapshot={"status": "active", "files": []},
        backend_files=[],
    )

    result = build_aria2_status(row)

    assert result["status"] == "complete"


def test_row_without_snapshot_still_renders_from_db_fields() -> None:
    row = _row(
        total_bytes=100,
        completed_bytes=40,
        backend_snapshot=None,
        backend_files=[],
    )

    response = build_rest_task_response(row)
    status = build_aria2_status(row)

    assert response["total_length"] == 100
    assert response["completed_length"] == 40
    assert response["download_speed"] == 0
    assert status["files"][0]["path"] == "snapshot.bin"
    assert status["completedLength"] == "40"
