from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.db.engine import transaction
from app.db.schema import task_backend_snapshots

_SNAPSHOT_COLUMNS = (
    "download_speed",
    "upload_speed",
    "total_length",
    "completed_length",
    "status",
    "files_json",
    "raw_json",
    "updated_at_ms",
)


async def upsert_snapshot(
    *,
    global_download_id: int,
    download_speed: int,
    upload_speed: int,
    total_length: int,
    completed_length: int,
    status: str,
    files_json: str,
    raw_json: str,
    updated_at_ms: int,
) -> None:
    values: dict[str, Any] = {
        "global_download_id": global_download_id,
        "download_speed": download_speed,
        "upload_speed": upload_speed,
        "total_length": total_length,
        "completed_length": completed_length,
        "status": status,
        "files_json": files_json,
        "raw_json": raw_json,
        "updated_at_ms": updated_at_ms,
    }
    statement = sqlite_insert(task_backend_snapshots).values(**values)
    statement = statement.on_conflict_do_update(
        index_elements=[task_backend_snapshots.c.global_download_id],
        set_={name: values[name] for name in _SNAPSHOT_COLUMNS},
    )
    async with transaction() as conn:
        await conn.execute(statement)


async def get_snapshot(global_download_id: int) -> dict[str, Any] | None:
    async with transaction() as conn:
        row = (
            (
                await conn.execute(
                    select(task_backend_snapshots).where(
                        task_backend_snapshots.c.global_download_id
                        == global_download_id
                    )
                )
            )
            .mappings()
            .first()
        )
    return dict(row) if row else None


async def get_snapshots_for_tids(tids: list[int]) -> dict[int, dict[str, Any]]:
    if not tids:
        return {}
    async with transaction() as conn:
        rows = (
            (
                await conn.execute(
                    select(task_backend_snapshots).where(
                        task_backend_snapshots.c.global_download_id.in_(tids)
                    )
                )
            )
            .mappings()
            .all()
        )
    return {row["global_download_id"]: dict(row) for row in rows}


