from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from app.domain.downloads import (
    ACTIVE_LIKE_DOWNLOAD_STATUSES,
    REST_TASK_STATUS_FILTERS,
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

BT_TRACKER_PLACEHOLDER = "http://aria2deck.invalid/announce"
INFO_HASH_HEX_PATTERN = re.compile(r"^[a-fA-F0-9]{40}$")


def ms_to_iso(timestamp_ms: int | None) -> str | None:
    if timestamp_ms is None:
        return None
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).isoformat()


def display_name(row: dict[str, Any]) -> str:
    for key in ("display_name", "global_display_name"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value if is_uri_like_path(value) else Path(value).name

    source_uri = str(row.get("source_uri") or "")
    if is_uri_like_path(source_uri):
        return source_uri
    parsed = urlsplit(source_uri)
    if parsed.path:
        name = Path(unquote(parsed.path)).name
        if name:
            return name
    return f"task-{row['id']}"


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def is_uri_like_path(path: str) -> bool:
    lowered = path.strip().lower()
    return lowered.startswith(("magnet:", "torrent:"))


def has_real_file_path(status: dict[str, Any]) -> bool:
    files = status.get("files")
    if not isinstance(files, list):
        return False
    return any(
        isinstance(item, dict)
        and isinstance(item.get("path"), str)
        and item["path"].strip()
        and not is_uri_like_path(item["path"])
        for item in files
    )


def _has_file_name(files: Any) -> bool:
    return has_real_file_path({"files": files})


def _files_from_task(
    row: dict[str, Any], total_bytes: int, completed_bytes: int
) -> list[dict[str, Any]]:
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


def is_bt_resource_kind(row: dict[str, Any]) -> bool:
    return str(row.get("resource_kind") or "") in ("magnet", "torrent")


def is_torrent_row(row: dict[str, Any]) -> bool:
    return is_bt_resource_kind(row)


def has_bittorrent_payload(live: dict[str, Any] | None) -> bool:
    if not isinstance(live, dict):
        return False
    bt = live.get("bittorrent")
    return isinstance(bt, dict) and bool(bt)


def _hex_info_hash_parts(value: Any) -> list[str]:
    if not isinstance(value, str):
        return []
    return [
        part.lower()
        for part in re.split(r"[^a-fA-F0-9]+", value)
        if INFO_HASH_HEX_PATTERN.fullmatch(part)
    ]


def info_hash_from_row(row: dict[str, Any]) -> str:
    bt_info_hash = row.get("bt_info_hash")
    if isinstance(bt_info_hash, str):
        normalized = bt_info_hash.strip().lower()
        if INFO_HASH_HEX_PATTERN.fullmatch(normalized):
            return normalized

    for key in ("resource_key", "source_uri"):
        parts = _hex_info_hash_parts(row.get(key))
        if parts:
            return parts[0]
    return ""


def has_live_bt_evidence(live: dict[str, Any] | None) -> bool:
    if not isinstance(live, dict):
        return False
    info_hash = str(live.get("infoHash") or "").strip()
    if INFO_HASH_HEX_PATTERN.fullmatch(info_hash):
        return True
    followed_by = live.get("followedBy")
    if isinstance(followed_by, list) and followed_by:
        return True
    return bool(live.get("following") or live.get("followingGid"))


def should_project_bittorrent(
    row: dict[str, Any], live: dict[str, Any] | None = None
) -> bool:
    return is_bt_resource_kind(row) or has_live_bt_evidence(live)


def build_bittorrent_payload(name: str = "", mode: str = "single") -> dict[str, Any]:
    return {
        "announceList": [[BT_TRACKER_PLACEHOLDER]],
        "comment": "",
        "creationDate": 0,
        "mode": mode or "single",
        "info": {"name": name},
    }


def _live_bittorrent_mode(live: dict[str, Any]) -> str:
    bt = live.get("bittorrent")
    if isinstance(bt, dict):
        mode = bt.get("mode")
        if isinstance(mode, str) and mode.strip():
            return mode
    return "single"


def build_aria2_status(
    row: dict[str, Any], live: dict[str, Any] | None = None
) -> dict[str, Any]:
    live = live or {}
    effective = effective_status(row)
    status = aria2_status(effective)
    if live and effective not in TERMINAL_DOWNLOAD_STATUSES:
        status = str(live.get("status") or status)

    gid = f"task-{row['id']}"
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

    result = {
        "gid": gid,
        "status": status,
        "totalLength": str(live.get("totalLength", total_bytes)),
        "completedLength": str(live.get("completedLength", completed_bytes)),
        "uploadLength": str(live.get("uploadLength", "0")),
        "downloadSpeed": str(live.get("downloadSpeed", "0")),
        "uploadSpeed": str(live.get("uploadSpeed", "0")),
        "pieceLength": "1048576",
        "numPieces": "0",
        "connections": str(live.get("connections", "0")),
        "dir": "",
        "files": files,
        "errorCode": "1" if status == "error" else "0",
        "errorMessage": error_message if status == "error" else "",
    }
    if should_project_bittorrent(row, live):
        # 元数据阶段还没有真实种子名时，用磁力链接占位以便区分不同任务
        name = _extract_live_display_name(live) or display_name(row)
        result["infoHash"] = str(live.get("infoHash") or info_hash_from_row(row))
        result["numSeeders"] = "0"
        result["seeder"] = "false"
        result["bittorrent"] = build_bittorrent_payload(
            name, _live_bittorrent_mode(live)
        )
    return result


def projected_speeds(
    row: dict[str, Any],
    live: dict[str, Any] | None = None,
) -> tuple[int, int]:
    if effective_status(row) not in ACTIVE_LIKE_DOWNLOAD_STATUSES:
        return 0, 0
    live = live or {}
    return _safe_int(live.get("downloadSpeed")), _safe_int(live.get("uploadSpeed"))


def speed_totals(
    rows: list[dict[str, Any]],
    live_by_gid: dict[str, dict[str, Any]] | None = None,
) -> dict[str, int]:
    live_by_gid = live_by_gid or {}
    download_speed = 0
    upload_speed = 0
    for row in rows:
        gid = str(row.get("aria2_gid") or "")
        row_download, row_upload = projected_speeds(row, live_by_gid.get(gid))
        download_speed += row_download
        upload_speed += row_upload
    return {"download_speed": download_speed, "upload_speed": upload_speed}


METADATA_NAME_PREFIX = "[METADATA]"


def is_metadata_phase_status(aria2_status: dict[str, Any]) -> bool:
    """Check if aria2 status represents a metadata download phase.

    Returns True when ``bittorrent.info.name`` starts with the ``[METADATA]``
    prefix that aria2 assigns during magnet-link metadata resolution.
    """
    bt = aria2_status.get("bittorrent")
    if not isinstance(bt, dict):
        return False
    info = bt.get("info")
    if not isinstance(info, dict):
        return False
    return str(info.get("name") or "").startswith(METADATA_NAME_PREFIX)


def _extract_live_display_name(live: dict[str, Any]) -> str | None:
    """Extract display name from live aria2 status, filtering placeholders."""
    bt = live.get("bittorrent")
    if isinstance(bt, dict):
        info = bt.get("info")
        if isinstance(info, dict):
            name = str(info.get("name") or "").strip()
            if name and not name.startswith(METADATA_NAME_PREFIX):
                return name
    return None


def build_rest_task_response(
    row: dict[str, Any],
    live: dict[str, Any] | None = None,
) -> dict[str, Any]:
    error_message = row.get("error_message") or row.get("global_error_message")
    download_speed, upload_speed = projected_speeds(row, live)
    name = row.get("display_name") or row.get("global_display_name") or display_name(row)
    total_length = _safe_int(row.get("total_bytes"))
    completed_length = _safe_int(row.get("completed_bytes"))

    # Prefer live aria2 data for active tasks — fresher and avoids
    # stale/polluted DB values (e.g. during metadata download phase).
    if live and effective_status(row) in ACTIVE_LIKE_DOWNLOAD_STATUSES:
        project_bt = should_project_bittorrent(row, live)
        if project_bt:
            live_name = _extract_live_display_name(live)
            if live_name:
                name = live_name
        if not (project_bt and is_metadata_phase_status(live)):
            live_total = _safe_int(live.get("totalLength"))
            if live_total > 0:
                total_length = live_total
                completed_length = _safe_int(live.get("completedLength"))
        else:
            # Metadata phase: show downloaded bytes so user sees activity,
            # but keep total_length at 0 to avoid a misleading percentage.
            completed_length = _safe_int(live.get("completedLength"))

    return {
        "id": row["id"],
        "task_id": row["global_download_id"],
        "status": legacy_rest_status(effective_status(row)),
        "name": name,
        "uri": row.get("source_uri") or "",
        "total_length": total_length,
        "completed_length": completed_length,
        "download_speed": download_speed,
        "upload_speed": upload_speed,
        "error": error_message,
        "error_display": error_message,
        "created_at": ms_to_iso(row.get("created_at_ms")),
        "updated_at": ms_to_iso(row.get("updated_at_ms")),
        "frozen_space": _safe_int(row.get("reserved_bytes")),
    }
