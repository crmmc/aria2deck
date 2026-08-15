from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from app.core.time_utils import now_ms
from app.domain.status import (
    ACTIVE_LIKE_DOWNLOAD_STATUSES,
    REST_TASK_STATUS_FILTERS,
    TERMINAL_DOWNLOAD_STATUSES,
)
from app.domain.task_policy import (
    InvalidTaskStatusFilter,
    aria2_status,
    effective_status,
    filter_rows_for_status,
    is_current,
    is_user_terminal,
    legacy_rest_status,
    stat_counts,
)
from app.modules.user_ref.projection import user_visible_label
from app.services.history_service import history_retry_projection

_REEXPORTED_POLICY_SYMBOLS = (
    InvalidTaskStatusFilter,
    REST_TASK_STATUS_FILTERS,
    filter_rows_for_status,
    is_current,
    is_user_terminal,
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


def _display_total(
    *,
    db_total: int,
    size_known: bool,
    live_total: int | None,
    active_like: bool = True,
    is_metadata: bool = False,
) -> int:
    """Single display rule for total size (M10 size truth).

    Prefer admitted DB total when size is known. Otherwise fall back to a
    trustworthy live total (live > 0) while active-like; when not active-like
    or live is unavailable/zero, keep the DB value. Metadata phase keeps the
    RPC-compatible behavior of not promoting live metadata size.
    """
    if is_metadata:
        return db_total
    if size_known and db_total > 0:
        return db_total
    if not active_like:
        return db_total
    if live_total is None:
        return db_total
    if live_total > 0:
        return live_total
    return db_total


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


SNAPSHOT_FRESH_WINDOW_MS = 30_000


def _snapshot_is_fresh(live: dict[str, Any], *, explicit: bool) -> bool:
    """Explicit live observations are always fresh; stored snapshots only
    within SNAPSHOT_FRESH_WINDOW_MS (a stale one must never override DB
    status — e.g. a paused-era snapshot surviving an unpause heal)."""
    if explicit:
        return True
    updated_at = _safe_int(live.get("_snapshot_updated_at_ms"))
    if updated_at <= 0:
        return False
    return (now_ms() - updated_at) <= SNAPSHOT_FRESH_WINDOW_MS


def build_aria2_status(
    row: dict[str, Any], live: dict[str, Any] | None = None
) -> dict[str, Any]:
    explicit_live = live is not None
    if live is None:
        live = row.get("backend_snapshot")
    live = live or {}
    effective = effective_status(row)
    status = aria2_status(effective)
    if (
        live
        and effective not in TERMINAL_DOWNLOAD_STATUSES
        and _snapshot_is_fresh(live, explicit=explicit_live)
    ):
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

    # M10: when size is admitted, prefer DB truth over live totalLength noise
    # (e.g. never-started BT reports totalLength=0). Align live>0 with REST.
    size_known = bool(row.get("size_known"))
    live_total_raw = live.get("totalLength") if live else None
    live_total = (
        _safe_int(live_total_raw) if live_total_raw is not None else None
    )
    display_total = _display_total(
        db_total=total_bytes,
        size_known=size_known,
        live_total=live_total,
        active_like=True,
        is_metadata=False,
    )

    # Ghost-speed guard: match real aria2 semantics — non-active
    # observations report zero speed (stale last-poll values must not leak
    # into RPC tellStatus/tellStopped for paused/terminal downloads).
    live_status = str(live.get("status") or "")
    live_active = (
        (live_status == "active") if live_status else (status == "active")
    )

    result = {
        "gid": gid,
        "status": status,
        "totalLength": str(display_total),
        "completedLength": str(live.get("completedLength", completed_bytes)),
        "uploadLength": str(live.get("uploadLength", "0")),
        "downloadSpeed": (
            str(live.get("downloadSpeed", "0")) if live_active else "0"
        ),
        "uploadSpeed": (
            str(live.get("uploadSpeed", "0")) if live_active else "0"
        ),
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
    if live is None:
        live = row.get("backend_snapshot")
    live = live or {}
    # Ghost-speed guard: only an "active" observation carries speed; paused/
    # terminal snapshots keep stale last-poll values that must not surface in
    # REST/WS/stats. Minimal live dicts without a status key trust the row's
    # active-like status (explicit-live callers always carry status).
    live_status = str(live.get("status") or "")
    if live_status:
        speed_active = live_status == "active"
    else:
        speed_active = effective_status(row) in ACTIVE_LIKE_DOWNLOAD_STATUSES
    if not speed_active:
        return 0, 0
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
    """Check if aria2 status represents a magnet metadata download phase.

    aria2 1.36.0 在 magnet 元数据阶段：
    - ``bittorrent.info`` 字段不存在（metadata 内容为空）
    - ``files[0].path`` 以 ``[METADATA]`` 前缀命名

    因此用 ``files[0].path`` 判断，而不是 ``bittorrent.info.name``。
    """
    bt = aria2_status.get("bittorrent")
    if isinstance(bt, dict) and isinstance(bt.get("info"), dict):
        return False
    files = aria2_status.get("files")
    if not isinstance(files, list) or not files:
        return False
    first = files[0]
    if not isinstance(first, dict):
        return False
    path = str(first.get("path") or "")
    return path.startswith(METADATA_NAME_PREFIX)


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
    if live is None:
        live = row.get("backend_snapshot")
    error_message = row.get("error_message") or row.get("global_error_message")
    download_speed, upload_speed = projected_speeds(row, live)
    name = row.get("display_name") or row.get("global_display_name") or display_name(row)
    total_length = _safe_int(row.get("total_bytes"))
    completed_length = _safe_int(row.get("completed_bytes"))
    effective = effective_status(row)
    size_known = bool(row.get("size_known"))

    # Progress display: admitted DB truth wins when size_known; otherwise live
    # total is only a preview (never overwrites DB — M10).
    active_like = effective in ACTIVE_LIKE_DOWNLOAD_STATUSES
    if live and active_like:
        project_bt = should_project_bittorrent(row, live)
        if project_bt:
            live_name = _extract_live_display_name(live)
            if live_name:
                name = live_name
        is_metadata = bool(project_bt and is_metadata_phase_status(live))
        live_total_raw = live.get("totalLength")
        live_total = (
            _safe_int(live_total_raw) if live_total_raw is not None else None
        )
        total_length = _display_total(
            db_total=total_length,
            size_known=size_known,
            live_total=live_total,
            active_like=True,
            is_metadata=is_metadata,
        )
        if is_metadata:
            # Metadata phase: show downloaded bytes so user sees activity,
            # but keep total_length at DB (usually 0) to avoid a misleading %.
            completed_length = _safe_int(live.get("completedLength"))
        elif size_known and _safe_int(row.get("total_bytes")) > 0:
            # Keep DB total; still refresh completed from live when present.
            live_completed = live.get("completedLength")
            if live_completed is not None:
                completed_length = _safe_int(live_completed)
        elif live_total is not None and live_total > 0:
            completed_length = _safe_int(live.get("completedLength"))

    # frozen_space is reservation accounting (DB), not live totalLength.
    # Only when reserved is zero do we fall back to a known DB total so a
    # freshly admitted size is not shown as frozen_space=0 before reserve.
    reserved = _safe_int(row.get("reserved_bytes"))
    db_total = _safe_int(row.get("total_bytes"))
    frozen_space = max(reserved, db_total if reserved == 0 and db_total > 0 else reserved)

    # Retry projection uses user-task status (matches POST /retry gate).
    retryable, retry_blocked_reason = history_retry_projection(row)

    return {
        "id": row["id"],
        "task_id": row["global_download_id"],
        "status": legacy_rest_status(effective),
        "status_label": user_visible_label(
            effective, row.get("global_error_code") or row.get("error_code")
        ),
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
        "frozen_space": frozen_space,
        "retryable": retryable,
        "retry_blocked_reason": retry_blocked_reason,
    }
