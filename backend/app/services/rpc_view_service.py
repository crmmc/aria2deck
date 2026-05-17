from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from app.repositories.downloads import list_user_tasks


def _aria2_status(status: str) -> str:
    if status == "completed":
        return "complete"
    if status in {"failed", "cancelled"}:
        return "error"
    if status == "queued":
        return "waiting"
    return status


def _display_name(row: dict[str, Any]) -> str:
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
            "path": _display_name(row),
            "length": str(total_bytes),
            "completedLength": str(completed_bytes),
            "selected": "true",
            "uris": [],
        }
    ]


def status_from_task(
    row: dict[str, Any], live: dict[str, Any] | None = None
) -> dict[str, Any]:
    live = live or {}
    gid = row.get("aria2_gid") or f"task-{row['id']}"
    status = _aria2_status(str(row["status"]))
    total_bytes = int(row.get("total_bytes") or 0)
    completed_bytes = int(row.get("completed_bytes") or 0)
    if status == "complete" and completed_bytes <= 0:
        completed_bytes = total_bytes
    error_message = row.get("error_message") or row.get("global_error_message") or ""
    live_files = live.get("files")
    files = (
        live_files
        if _has_file_name(live_files)
        else _files_from_task(row, total_bytes, completed_bytes)
    )
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
                "info": {"name": _display_name(row)},
            },
        ),
    }


async def list_active_statuses(
    user_id: int,
    live_by_gid: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    rows = await list_user_tasks(user_id, ["active"])
    live_by_gid = live_by_gid or {}
    return [
        status_from_task(row, live_by_gid.get(str(row.get("aria2_gid"))))
        for row in rows
    ]


async def list_waiting_statuses(user_id: int) -> list[dict[str, Any]]:
    rows = await list_user_tasks(user_id, ["queued", "waiting", "paused"])
    return [status_from_task(row) for row in rows]


async def list_stopped_statuses(user_id: int) -> list[dict[str, Any]]:
    rows = await list_user_tasks(user_id, ["completed", "failed", "cancelled"])
    return [status_from_task(row) for row in rows]
