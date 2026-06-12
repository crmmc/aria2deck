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
    machine_free: int,
) -> dict[str, int | bool]:
    quota_available_bytes = quota_available(
        quota_bytes=quota_bytes,
        used_bytes=used_bytes,
        reserved_bytes=reserved_bytes,
    )
    limited = int(machine_free) < quota_available_bytes
    available = max(0, int(machine_free) if limited else quota_available_bytes)
    total = int(used_bytes) + int(reserved_bytes) + available if limited else int(quota_bytes)
    return {
        "quota": int(quota_bytes),
        "used": int(used_bytes),
        "frozen": int(reserved_bytes),
        "available": available,
        "total": total,
        "limited": limited,
    }
