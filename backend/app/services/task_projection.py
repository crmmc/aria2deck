from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

ACTIVE_LIKE_STATUSES = ("queued", "active", "waiting", "paused")
TERMINAL_STATUSES = ("completed", "failed", "cancelled")
ERROR_STATUSES = ("failed", "cancelled")


def ms_to_iso(timestamp_ms: int | None) -> str | None:
    if timestamp_ms is None:
        return None
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).isoformat()


def effective_status(row: dict[str, Any]) -> str:
    user_status = str(row.get("status") or "")
    if user_status in TERMINAL_STATUSES:
        return user_status

    global_status = str(row.get("global_status") or user_status)
    if global_status in TERMINAL_STATUSES:
        return global_status
    return user_status


def legacy_rest_status(status: str) -> str:
    if status == "completed":
        return "complete"
    if status in ERROR_STATUSES:
        return "error"
    return status


def aria2_status(status: str) -> str:
    if status == "completed":
        return "complete"
    if status in ERROR_STATUSES:
        return "error"
    if status == "queued":
        return "waiting"
    return status


def is_user_terminal(row: dict[str, Any]) -> bool:
    return str(row.get("status") or "") in TERMINAL_STATUSES


def is_current(row: dict[str, Any]) -> bool:
    return effective_status(row) in ACTIVE_LIKE_STATUSES


def display_name(row: dict[str, Any]) -> str:
    for key in ("display_name", "global_display_name"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return Path(value).name

    source_uri = str(row.get("source_uri") or "")
    parsed = urlsplit(source_uri)
    if parsed.path:
        name = Path(unquote(parsed.path)).name
        if name:
            return name
    return str(row.get("aria2_gid") or f"task-{row['id']}")


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _has_file_name(files: Any) -> bool:
    if not isinstance(files, list):
        return False
    return any(
        isinstance(item, dict)
        and isinstance(item.get("path"), str)
        and item["path"].strip()
        for item in files
    )


def _files_from_task(
    row: dict[str, Any], total_bytes: int, completed_bytes: int
) -> list[dict[str, str]]:
    return [
        {
            "index": "1",
            "path": display_name(row),
            "length": str(total_bytes),
            "completedLength": str(completed_bytes),
            "selected": "true",
            "uris": [],
        }
    ]


def build_aria2_status(
    row: dict[str, Any], live: dict[str, Any] | None = None
) -> dict[str, Any]:
    live = live or {}
    effective = effective_status(row)
    status = aria2_status(effective)
    if live and effective not in TERMINAL_STATUSES:
        status = str(live.get("status") or status)

    gid = row.get("aria2_gid") or f"task-{row['id']}"
    total_bytes = _safe_int(row.get("total_bytes"))
    completed_bytes = _safe_int(row.get("completed_bytes"))
    if status == "complete" and completed_bytes <= 0:
        completed_bytes = total_bytes

    live_files = live.get("files")
    files = (
        live_files
        if _has_file_name(live_files)
        else _files_from_task(row, total_bytes, completed_bytes)
    )
    error_message = row.get("error_message") or row.get("global_error_message") or ""

    return {
        "gid": gid,
        "status": status,
        "totalLength": str(live.get("totalLength", total_bytes)),
        "completedLength": str(live.get("completedLength", completed_bytes)),
        "uploadLength": str(live.get("uploadLength", "0")),
        "downloadSpeed": str(live.get("downloadSpeed", "0")),
        "uploadSpeed": str(live.get("uploadSpeed", "0")),
        "pieceLength": str(live.get("pieceLength", "0")),
        "numPieces": str(live.get("numPieces", "0")),
        "connections": str(live.get("connections", "0")),
        "dir": "",
        "files": files,
        "errorCode": "1" if status == "error" else "0",
        "errorMessage": error_message if status == "error" else "",
        "infoHash": str(live.get("infoHash", "")),
        "numSeeders": str(live.get("numSeeders", "0")),
        "seeder": str(live.get("seeder", "false")),
        "bittorrent": live.get(
            "bittorrent",
            {
                "announceList": [],
                "comment": "",
                "creationDate": "0",
                "mode": "single",
                "info": {"name": display_name(row)},
            },
        ),
    }


def build_rest_task_response(row: dict[str, Any]) -> dict[str, Any]:
    error_message = row.get("error_message") or row.get("global_error_message")
    return {
        "id": row["id"],
        "task_id": row["global_download_id"],
        "status": legacy_rest_status(effective_status(row)),
        "name": row.get("display_name") or row.get("global_display_name") or display_name(row),
        "uri": row.get("source_uri") or "",
        "total_length": _safe_int(row.get("total_bytes")),
        "completed_length": _safe_int(row.get("completed_bytes")),
        "download_speed": 0,
        "upload_speed": 0,
        "error": error_message,
        "error_display": error_message,
        "created_at": ms_to_iso(row.get("created_at_ms")),
        "updated_at": ms_to_iso(row.get("updated_at_ms")),
        "frozen_space": _safe_int(row.get("reserved_bytes")),
    }


def filter_rows_for_status(
    rows: list[dict[str, Any]], status_filter: str | None
) -> list[dict[str, Any]]:
    if status_filter in {"active", "current"}:
        return [row for row in rows if is_current(row)]
    if status_filter == "complete":
        return [row for row in rows if effective_status(row) == "completed"]
    if status_filter == "error":
        return [row for row in rows if effective_status(row) in ERROR_STATUSES]
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
        elif status in TERMINAL_STATUSES:
            stopped += 1
    return {
        "active": active,
        "waiting": waiting,
        "stopped": stopped,
        "current": active + waiting,
    }
