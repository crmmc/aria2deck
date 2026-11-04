"""tracker_list_cache 单行缓存表的数据访问。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import insert, select, update

from app.db.engine import transaction
from app.db.schema import tracker_list_cache


async def get_tracker_cache_row() -> dict[str, Any] | None:
    async with transaction() as conn:
        row = (
            (
                await conn.execute(
                    select(tracker_list_cache).where(tracker_list_cache.c.id == 1)
                )
            )
            .mappings()
            .first()
        )
    return dict(row) if row else None


async def save_tracker_cache(values: dict[str, Any]) -> None:
    async with transaction() as conn:
        existing = (
            await conn.execute(
                select(tracker_list_cache.c.id).where(tracker_list_cache.c.id == 1)
            )
        ).first()
        if existing is not None:
            await conn.execute(
                update(tracker_list_cache)
                .where(tracker_list_cache.c.id == 1)
                .values(**values)
            )
        else:
            await conn.execute(insert(tracker_list_cache).values(id=1, **values))
