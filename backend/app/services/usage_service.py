from __future__ import annotations

from pathlib import Path
import shutil

from app.core.config import settings
from app.domain.quota import usage_with_available, visible_space_from_quota

from app.repositories.usage import (
    apply_usage_delta,
    get_usage_row,
    reserve_usage_bytes_if_within_quota,
)


async def _resolve_quota(user_id: int, quota_bytes: int | None) -> int:
    if quota_bytes is not None:
        return int(quota_bytes)

    from app.repositories.auth import get_user_by_id

    user = await get_user_by_id(user_id)
    return int(user["quota_bytes"]) if user else 0


async def get_usage(user_id: int, quota_bytes: int) -> dict[str, int]:
    return usage_with_available(await get_usage_row(user_id), int(quota_bytes))


def visible_space_from_usage(
    usage: dict[str, int],
    *,
    machine_free: int,
) -> dict[str, int | bool]:
    return visible_space_from_quota(
        quota_bytes=int(usage["quota_bytes"]),
        used_bytes=int(usage["used_bytes"]),
        reserved_bytes=int(usage["reserved_bytes"]),
        machine_free=int(machine_free),
    )


async def get_visible_space(user_id: int, quota_bytes: int) -> dict[str, int | bool]:
    usage = await get_usage(user_id, quota_bytes)
    download_path = Path(settings.download_dir)
    download_path.mkdir(parents=True, exist_ok=True)
    machine_free = shutil.disk_usage(download_path).free
    return visible_space_from_usage(usage, machine_free=machine_free)


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

    return usage_with_available(row, quota)


async def release_reserved(
    user_id: int, amount: int, *, quota_bytes: int | None = None
) -> dict[str, int]:
    if amount < 0:
        raise ValueError("amount must be non-negative")

    row = await apply_usage_delta(user_id, reserved_delta=-amount)
    quota = await _resolve_quota(user_id, quota_bytes)
    return usage_with_available(row, quota)


async def update_used_bytes(
    user_id: int, amount_delta: int, *, quota_bytes: int | None = None
) -> dict[str, int]:
    row = await apply_usage_delta(user_id, used_delta=amount_delta)
    quota = await _resolve_quota(user_id, quota_bytes)
    return usage_with_available(row, quota)
