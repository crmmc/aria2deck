from __future__ import annotations

import time
from typing import Any

from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import IntegrityError

from app.db.engine import transaction
from app.db.schema import global_downloads, user_tasks


def now_ms() -> int:
    return int(time.time() * 1000)


async def get_global_by_resource_key(resource_key: str) -> dict[str, Any] | None:
    async with transaction() as conn:
        row = (
            await conn.execute(
                select(global_downloads).where(
                    global_downloads.c.resource_key == resource_key
                )
            )
        ).mappings().first()
    return dict(row) if row else None


async def create_global_download(values: dict[str, Any]) -> dict[str, Any]:
    timestamp = now_ms()
    row_values = {
        "status": "queued",
        "total_bytes": 0,
        "completed_bytes": 0,
        "created_at_ms": timestamp,
        "updated_at_ms": timestamp,
        **values,
    }
    async with transaction() as conn:
        row = (
            await conn.execute(
                insert(global_downloads).values(**row_values).returning(global_downloads)
            )
        ).mappings().one()
    return dict(row)


async def get_or_create_global_download(values: dict[str, Any]) -> dict[str, Any]:
    existing = await get_global_by_resource_key(str(values["resource_key"]))
    if existing:
        return existing

    try:
        return await create_global_download(values)
    except IntegrityError:
        fallback = await get_global_by_resource_key(str(values["resource_key"]))
        if fallback:
            return fallback
        raise


async def get_user_task(user_id: int, global_download_id: int) -> dict[str, Any] | None:
    async with transaction() as conn:
        row = (
            await conn.execute(
                select(user_tasks).where(
                    user_tasks.c.user_id == user_id,
                    user_tasks.c.global_download_id == global_download_id,
                )
            )
        ).mappings().first()
    return dict(row) if row else None


async def create_user_task(values: dict[str, Any]) -> dict[str, Any]:
    timestamp = now_ms()
    row_values = {
        "status": "queued",
        "reserved_bytes": 0,
        "created_at_ms": timestamp,
        "updated_at_ms": timestamp,
        **values,
    }
    async with transaction() as conn:
        row = (
            await conn.execute(insert(user_tasks).values(**row_values).returning(user_tasks))
        ).mappings().one()
    return dict(row)


async def update_global_download(
    download_id: int, values: dict[str, Any]
) -> dict[str, Any] | None:
    if not values:
        async with transaction() as conn:
            row = (
                await conn.execute(
                    select(global_downloads).where(global_downloads.c.id == download_id)
                )
            ).mappings().first()
        return dict(row) if row else None

    row_values = {**values, "updated_at_ms": now_ms()}
    async with transaction() as conn:
        row = (
            await conn.execute(
                update(global_downloads)
                .where(global_downloads.c.id == download_id)
                .values(**row_values)
                .returning(global_downloads)
            )
        ).mappings().first()
    return dict(row) if row else None


async def update_user_task(task_id: int, values: dict[str, Any]) -> dict[str, Any] | None:
    if not values:
        async with transaction() as conn:
            row = (
                await conn.execute(select(user_tasks).where(user_tasks.c.id == task_id))
            ).mappings().first()
        return dict(row) if row else None

    row_values = {**values, "updated_at_ms": now_ms()}
    async with transaction() as conn:
        row = (
            await conn.execute(
                update(user_tasks)
                .where(user_tasks.c.id == task_id)
                .values(**row_values)
                .returning(user_tasks)
            )
        ).mappings().first()
    return dict(row) if row else None


async def count_active_user_tasks(global_download_id: int) -> int:
    async with transaction() as conn:
        count = (
            await conn.execute(
                select(func.count())
                .select_from(user_tasks)
                .where(
                    user_tasks.c.global_download_id == global_download_id,
                    user_tasks.c.status.in_(("queued", "active", "waiting", "paused")),
                )
            )
        ).scalar_one()
    return int(count)
