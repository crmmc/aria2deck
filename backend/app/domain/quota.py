from __future__ import annotations


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
