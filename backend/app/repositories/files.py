from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any

from sqlalchemy import delete, insert, select

from app.db.engine import transaction
from app.db.schema import stored_file_entries, stored_files, user_files


def now_ms() -> int:
    return int(time.time() * 1000)


async def create_stored_file_with_entries(
    values: dict[str, Any],
    entry_templates: Sequence[dict[str, Any]],
) -> tuple[dict[str, Any], int]:
    row_values = {
        "created_at_ms": now_ms(),
        **values,
    }
    async with transaction() as conn:
        row = (
            await conn.execute(
                insert(stored_files).values(**row_values).returning(stored_files)
            )
        ).mappings().one()
        entries = [
            {"stored_file_id": row["id"], **entry}
            for entry in entry_templates
        ]
        if entries:
            await conn.execute(insert(stored_file_entries), entries)
    return dict(row), len(entry_templates)


async def delete_stored_file(stored_file_id: int) -> bool:
    async with transaction() as conn:
        row = (
            await conn.execute(
                delete(stored_files)
                .where(stored_files.c.id == stored_file_id)
                .returning(stored_files.c.id)
            )
        ).first()
    return row is not None


async def get_stored_file_by_content_hash(content_hash: str) -> dict[str, Any] | None:
    async with transaction() as conn:
        row = (
            await conn.execute(
                select(stored_files).where(stored_files.c.content_hash == content_hash)
            )
        ).mappings().first()
    return dict(row) if row else None


async def delete_user_file(user_id: int, user_file_id: int) -> dict[str, Any] | None:
    async with transaction() as conn:
        row = (
            await conn.execute(
                delete(user_files)
                .where(
                    user_files.c.id == user_file_id,
                    user_files.c.user_id == user_id,
                )
                .returning(user_files)
            )
        ).mappings().first()
    return dict(row) if row else None
