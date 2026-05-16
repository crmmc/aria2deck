from __future__ import annotations

import time
from typing import Any

from sqlalchemy import select, update

from app.db.engine import transaction
from app.db.schema import app_settings


def now_ms() -> int:
    return int(time.time() * 1000)


async def get_settings_row() -> dict[str, Any] | None:
    async with transaction() as conn:
        row = (
            (await conn.execute(select(app_settings).where(app_settings.c.id == 1)))
            .mappings()
            .first()
        )
    return dict(row) if row else None


async def update_settings_row(values: dict[str, Any]) -> dict[str, Any] | None:
    if not values:
        return await get_settings_row()

    update_values = dict(values)
    update_values["updated_at_ms"] = now_ms()
    async with transaction() as conn:
        row = (
            (
                await conn.execute(
                    update(app_settings)
                    .where(app_settings.c.id == 1)
                    .values(**update_values)
                    .returning(app_settings)
                )
            )
            .mappings()
            .first()
        )
    return dict(row) if row else None
