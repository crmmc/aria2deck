from __future__ import annotations

import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any


def get_disk_available_bytes(
    download_dir: str, *, min_free_disk: int
) -> int:
    download_path = Path(download_dir)
    download_path.mkdir(parents=True, exist_ok=True)
    return max(0, shutil.disk_usage(download_path).free - min_free_disk)


def _status_int(value: Any, default: int = -1) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def candidate_size_from_status(
    status: Mapping[str, Any], *, require_trusted_total: bool = False
) -> tuple[int, int] | None:
    completed = max(0, _status_int(status.get("completedLength"), 0))
    selected_total = 0
    selected_count = 0
    selected_complete = True
    files = status.get("files")
    if isinstance(files, list):
        for item in files:
            if not isinstance(item, Mapping):
                selected_complete = False
                continue
            selected = item.get("selected")
            if selected is False or (
                isinstance(selected, str) and selected.lower() == "false"
            ):
                continue
            selected_count += 1
            length = _status_int(item.get("length"))
            if length < 0:
                selected_complete = False
                continue
            selected_total += length
    total = _status_int(status.get("totalLength"))
    trusted_total = total > 0 or (
        selected_count > 0 and selected_complete and selected_total > 0
    )
    if require_trusted_total and not trusted_total:
        return None
    candidate = max(total, selected_total, completed)
    if candidate < 0 or (candidate == 0 and not trusted_total):
        return None
    return candidate, completed


def quota_available(*, quota_bytes: int, used_bytes: int, reserved_bytes: int) -> int:
    return max(0, int(quota_bytes) - int(used_bytes) - int(reserved_bytes))


def usage_with_available(row: dict, quota_bytes: int) -> dict[str, int]:
    used = int(row["used_bytes"])
    reserved = int(row["reserved_bytes"])
    return {
        "user_id": int(row["user_id"]),
        "quota_bytes": int(quota_bytes),
        "used_bytes": used,
        "reserved_bytes": reserved,
        "available_bytes": quota_available(
            quota_bytes=int(quota_bytes),
            used_bytes=used,
            reserved_bytes=reserved,
        ),
    }


def visible_space_from_quota(
    *,
    quota_bytes: int,
    used_bytes: int,
    reserved_bytes: int,
    machine_headroom: int,
) -> dict[str, int | bool | float]:
    """Compute user-visible space using the same budget as admission checks.

    ``machine_headroom`` must already be:
    ``max(0, disk.free - min_free_disk - global_physical_commitment)``.
    """
    quota_available_bytes = quota_available(
        quota_bytes=quota_bytes,
        used_bytes=used_bytes,
        reserved_bytes=reserved_bytes,
    )
    headroom = max(0, int(machine_headroom))
    limited = headroom < quota_available_bytes
    available = min(quota_available_bytes, headroom)
    total = int(used_bytes) + int(reserved_bytes) + available if limited else int(quota_bytes)
    return {
        "quota": int(quota_bytes),
        "used": int(used_bytes),
        "frozen": int(reserved_bytes),
        "available": available,
        "total": total,
        "limited": limited,
        "quota_headroom": quota_available_bytes,
        "machine_headroom": headroom,
    }


def usage_percent(*, used_bytes: int, quota_bytes: int) -> float:
    quota = int(quota_bytes)
    if quota <= 0:
        return 0.0
    return round(min(100.0, max(0.0, int(used_bytes) * 100.0 / quota)), 1)


def machine_share_percent(*, used_bytes: int, total_used_bytes: int) -> float:
    total_used = int(total_used_bytes)
    if total_used <= 0:
        return 0.0
    return round(min(100.0, max(0.0, int(used_bytes) * 100.0 / total_used)), 1)
