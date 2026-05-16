from __future__ import annotations

from typing import Any

from app.repositories.usage import (
    apply_usage_delta,
    get_usage_row,
    reserve_usage_bytes_if_within_quota,
)


def _with_available(row: dict[str, Any], quota_bytes: int) -> dict[str, int]:
    used = int(row["used_bytes"])
    reserved = int(row["reserved_bytes"])
    available = max(0, quota_bytes - used - reserved)
    return {
        "user_id": int(row["user_id"]),
        "quota_bytes": quota_bytes,
        "used_bytes": used,
        "reserved_bytes": reserved,
        "available_bytes": available,
    }


async def _resolve_quota(user_id: int, quota_bytes: int | None) -> int:
    if quota_bytes is not None:
        return int(quota_bytes)

    from app.repositories.auth import get_user_by_id

    user = await get_user_by_id(user_id)
    return int(user["quota_bytes"]) if user else 0


async def get_usage(user_id: int, quota_bytes: int) -> dict[str, int]:
    return _with_available(await get_usage_row(user_id), int(quota_bytes))


async def reserve_bytes(
    user_id: int, amount: int, *, quota_bytes: int | None = None
) -> dict[str, int]:
    if amount < 0:
        raise ValueError("amount must be non-negative")

    quota = await _resolve_quota(user_id, quota_bytes)
    row = await reserve_usage_bytes_if_within_quota(
        user_id, amount=amount, quota_bytes=quota
    )
    if row is None:
        raise ValueError("quota exceeded")

    return _with_available(row, quota)


async def release_reserved(
    user_id: int, amount: int, *, quota_bytes: int | None = None
) -> dict[str, int]:
    if amount < 0:
        raise ValueError("amount must be non-negative")

    row = await apply_usage_delta(user_id, reserved_delta=-amount)
    quota = await _resolve_quota(user_id, quota_bytes)
    return _with_available(row, quota)


async def update_used_bytes(
    user_id: int, amount_delta: int, *, quota_bytes: int | None = None
) -> dict[str, int]:
    row = await apply_usage_delta(user_id, used_delta=amount_delta)
    quota = await _resolve_quota(user_id, quota_bytes)
    return _with_available(row, quota)
