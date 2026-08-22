"""Shared download operations for aria2 listener and sync paths."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from app.core.security import sanitize_string
from app.services.task_projection import (
    METADATA_NAME_PREFIX,
    is_metadata_phase_status,
)

INFO_HASH_HEX_PATTERN = re.compile(r"^[a-fA-F0-9]{40}$")
ARIA2_TO_V0_STATUS = {
    "active": "active",
    "waiting": "waiting",
    "paused": "paused",
    "complete": "completed",
    "error": "failed",
    "removed": "failed",
}


def safe_int(value: str | int | None, default: int = 0) -> int:
    try:
        return int(value) if value is not None else default
    except (ValueError, TypeError):
        return default


def map_aria2_status(status: dict[str, Any] | None, default: str = "active") -> str:
    if not isinstance(status, dict):
        return default
    raw_status = str(status.get("status") or "")
    return ARIA2_TO_V0_STATUS.get(raw_status, default)


def bt_info_hash_from_status(status: dict[str, Any] | None) -> str | None:
    if not isinstance(status, dict):
        return None
    info_hash = str(status.get("infoHash") or "").strip()
    if INFO_HASH_HEX_PATTERN.fullmatch(info_hash):
        return info_hash.lower()
    return None


def extract_display_name(
    aria2_status: dict[str, Any],
    fallback: str | None,
) -> str | None:
    raw_name = aria2_status.get("bittorrent", {}).get("info", {}).get("name") or (
        aria2_status.get("files") or [{}]
    )[0].get("path")
    if isinstance(raw_name, str) and raw_name:
        name = Path(raw_name).name or raw_name
        if name.startswith(METADATA_NAME_PREFIX):
            return fallback
        return sanitize_string(name)
    return fallback


def map_progress_values(
    aria2_status: dict[str, Any],
    display_name_fallback: str | None,
    *,
    skip_total_on_metadata: bool = True,
) -> dict[str, Any]:
    values: dict[str, Any] = {}
    if not aria2_status:
        return values
    is_metadata = skip_total_on_metadata and is_metadata_phase_status(aria2_status)
    display_name = extract_display_name(aria2_status, display_name_fallback)
    if (
        not is_metadata
        and display_name
        and not display_name.startswith(METADATA_NAME_PREFIX)
    ):
        values["display_name"] = display_name
    # M10: total_bytes is admission-owned (downloads/handoff). Progress
    # projection only carries completed_bytes + display_name.
    values["completed_bytes"] = safe_int(aria2_status.get("completedLength"))
    return values


def first_followed_gid(aria2_status: dict[str, Any]) -> str | None:
    followed_by = aria2_status.get("followedBy")
    if not isinstance(followed_by, list):
        return None

    for gid in followed_by:
        if isinstance(gid, (str, int)) and str(gid):
            return str(gid)
    return None


def following_gid(aria2_status: dict[str, Any]) -> str | None:
    following = aria2_status.get("following") or aria2_status.get("followingGid")
    if isinstance(following, (str, int)) and str(following):
        return str(following)
    return None


def _is_magnet_download(download: dict[str, Any]) -> bool:
    kind = str(download.get("resource_kind") or "").lower()
    if kind == "torrent":
        return False
    if kind == "magnet":
        return True
    return str(download.get("source_uri") or "").lower().startswith("magnet:")


def is_metadata_handoff_pending(
    download: dict[str, Any],
    aria2_status: dict[str, Any],
) -> bool:
    if str(aria2_status.get("status") or "") != "complete":
        return False
    if not _is_magnet_download(download):
        return False
    if first_followed_gid(aria2_status) is not None:
        return False
    if following_gid(aria2_status) is not None:
        return False

    # A magnet's metadata GID can briefly look like a completed payload before
    # aria2 exposes followedBy. Do not let display names or file-path heuristics
    # turn that metadata completion into final artifact validation.
    return True

