from __future__ import annotations

import time
from typing import Any

from sqlalchemy import case, func, select, update

from app.db.engine import transaction
from app.db.schema import (
    pack_tasks,
    stored_files,
    user_files,
    user_storage_usage,
    user_tasks,
)
from app.domain.status import ACTIVE_USER_TASK_STATUSES


def now_ms() -> int:
    return int(time.time() * 1000)


async def rebuild_usage_from_authoritative_state() -> None:
    used_subquery = (
        select(func.coalesce(func.sum(stored_files.c.size_bytes), 0))
        .select_from(
            user_files.join(
                stored_files, user_files.c.stored_file_id == stored_files.c.id
            )
        )
        .where(user_files.c.user_id == user_storage_usage.c.user_id)
        .scalar_subquery()
    )
    download_subquery = (
        select(func.coalesce(func.sum(user_tasks.c.reserved_bytes), 0))
        .where(
            user_tasks.c.user_id == user_storage_usage.c.user_id,
            user_tasks.c.status.in_(ACTIVE_USER_TASK_STATUSES),
        )
        .scalar_subquery()
    )
    pack_subquery = (
        select(func.coalesce(func.sum(pack_tasks.c.reserved_bytes), 0))
        .where(
            pack_tasks.c.user_id == user_storage_usage.c.user_id,
            pack_tasks.c.status.in_(("pending", "packing")),
        )
        .scalar_subquery()
    )
    async with transaction() as conn:
        await conn.execute(
            update(user_storage_usage).values(
                used_bytes=used_subquery,
                reserved_bytes=download_subquery + pack_subquery,
                updated_at_ms=now_ms(),
            )
        )


async def get_usage_row(user_id: int) -> dict[str, Any]:
    async with transaction() as conn:
        row = (
            (
                await conn.execute(
                    select(user_storage_usage).where(
                        user_storage_usage.c.user_id == user_id
                    )
                )
            )
            .mappings()
            .one()
        )
    return dict(row)


async def list_usage_rows() -> list[dict[str, Any]]:
    async with transaction() as conn:
        rows = (
            await conn.execute(select(user_storage_usage))
        ).mappings().all()
    return [dict(row) for row in rows]


async def apply_usage_delta(
    user_id: int, *, used_delta: int = 0, reserved_delta: int = 0
) -> dict[str, Any]:
    used_expr = user_storage_usage.c.used_bytes + used_delta
    reserved_expr = user_storage_usage.c.reserved_bytes + reserved_delta
    async with transaction() as conn:
        row = (
            (
                await conn.execute(
                    update(user_storage_usage)
                    .where(user_storage_usage.c.user_id == user_id)
                    .values(
                        used_bytes=case((used_expr < 0, 0), else_=used_expr),
                        reserved_bytes=case(
                            (reserved_expr < 0, 0), else_=reserved_expr
                        ),
                        updated_at_ms=now_ms(),
                    )
                    .returning(user_storage_usage)
                )
            )
            .mappings()
            .one()
        )
    return dict(row)


async def reserve_usage_bytes_if_within_quota(
    user_id: int, *, amount: int, quota_bytes: int
) -> dict[str, Any] | None:
    reserved_expr = user_storage_usage.c.reserved_bytes + amount
    within_quota = (
        user_storage_usage.c.used_bytes + user_storage_usage.c.reserved_bytes + amount
        <= quota_bytes
    )
    async with transaction() as conn:
        row = (
            (
                await conn.execute(
                    update(user_storage_usage)
                    .where(user_storage_usage.c.user_id == user_id)
                    .where(within_quota)
                    .values(
                        reserved_bytes=reserved_expr,
                        updated_at_ms=now_ms(),
                    )
                    .returning(user_storage_usage)
                )
            )
            .mappings()
            .one_or_none()
        )
    return dict(row) if row else None
