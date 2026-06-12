from __future__ import annotations

import time
from typing import Any

from sqlalchemy import case, delete, func, insert, select, update

from app.db.engine import transaction
from app.db.schema import pack_tasks, stored_files, user_files, user_storage_usage, users
from app.domain.pack import PACK_ACTIVE_STATUSES, PACK_TERMINAL_STATUSES


def now_ms() -> int:
    return int(time.time() * 1000)


async def convert_reserved_to_used(
    user_id: int,
    *,
    reserved_bytes: int,
    used_bytes: int,
) -> None:
    if reserved_bytes <= 0 and used_bytes <= 0:
        return
    reserved_expr = user_storage_usage.c.reserved_bytes - max(0, reserved_bytes)
    async with transaction() as conn:
        await conn.execute(
            update(user_storage_usage)
            .where(user_storage_usage.c.user_id == user_id)
            .values(
                used_bytes=user_storage_usage.c.used_bytes + max(0, used_bytes),
                reserved_bytes=case((reserved_expr < 0, 0), else_=reserved_expr),
                updated_at_ms=now_ms(),
            )
        )


async def get_pack_task_row(task_id: int) -> dict[str, Any] | None:
    async with transaction() as conn:
        row = (
            (
                await conn.execute(select(pack_tasks).where(pack_tasks.c.id == task_id))
            )
            .mappings()
            .first()
        )
    return dict(row) if row else None


async def get_user_pack_task_row(
    user_id: int,
    task_id: int,
) -> dict[str, Any] | None:
    async with transaction() as conn:
        row = (
            (
                await conn.execute(
                    select(pack_tasks).where(
                        pack_tasks.c.id == task_id,
                        pack_tasks.c.user_id == user_id,
                    )
                )
            )
            .mappings()
            .first()
        )
    return dict(row) if row else None


async def get_pack_task_status(task_id: int) -> str | None:
    async with transaction() as conn:
        row = (
            await conn.execute(
                select(pack_tasks.c.status).where(pack_tasks.c.id == task_id)
            )
        ).first()
    return str(row[0]) if row else None


async def get_pack_task_quota_snapshot(task_id: int) -> dict[str, Any] | None:
    async with transaction() as conn:
        row = (
            (
                await conn.execute(
                    select(pack_tasks.c.status, pack_tasks.c.reserved_bytes).where(
                        pack_tasks.c.id == task_id
                    )
                )
            )
            .mappings()
            .first()
        )
    return dict(row) if row else None


async def mark_pack_task_packing_if_pending(task_id: int) -> bool:
    async with transaction() as conn:
        row = (
            await conn.execute(
                update(pack_tasks)
                .where(pack_tasks.c.id == task_id, pack_tasks.c.status == "pending")
                .values(status="packing", updated_at_ms=now_ms())
                .returning(pack_tasks.c.id)
            )
        ).first()
    return row is not None


async def update_pack_task_progress(task_id: int, progress: int) -> None:
    async with transaction() as conn:
        await conn.execute(
            update(pack_tasks)
            .where(pack_tasks.c.id == task_id, pack_tasks.c.status == "packing")
            .values(progress=progress, updated_at_ms=now_ms())
        )


async def fail_active_pack_task(task_id: int, error: str) -> dict[str, Any] | None:
    async with transaction() as conn:
        task = (
            (
                await conn.execute(
                    select(pack_tasks).where(
                        pack_tasks.c.id == task_id,
                        pack_tasks.c.status.in_(PACK_ACTIVE_STATUSES),
                    )
                )
            )
            .mappings()
            .first()
        )
        if task:
            await conn.execute(
                update(pack_tasks)
                .where(pack_tasks.c.id == task_id)
                .values(
                    status="failed",
                    error_message=error,
                    reserved_bytes=0,
                    updated_at_ms=now_ms(),
                    finished_at_ms=now_ms(),
                )
            )
    return dict(task) if task else None


async def complete_packing_task(
    task_id: int,
    *,
    output_stored_file_id: int | None,
) -> dict[str, Any] | None:
    async with transaction() as conn:
        task = (
            (
                await conn.execute(select(pack_tasks).where(pack_tasks.c.id == task_id))
            )
            .mappings()
            .first()
        )
        if not task or task["status"] != "packing":
            return None
        completed = await conn.execute(
            update(pack_tasks)
            .where(pack_tasks.c.id == task_id, pack_tasks.c.status == "packing")
            .values(
                status="completed",
                progress=100,
                output_stored_file_id=output_stored_file_id,
                reserved_bytes=0,
                updated_at_ms=now_ms(),
                finished_at_ms=now_ms(),
            )
            .returning(pack_tasks.c.id)
        )
        if completed.first() is None:
            return None
    return dict(task)


async def active_pack_reserved_bytes() -> int:
    async with transaction() as conn:
        value = (
            await conn.execute(
                select(func.coalesce(func.sum(pack_tasks.c.reserved_bytes), 0)).where(
                    pack_tasks.c.status.in_(PACK_ACTIVE_STATUSES)
                )
            )
        ).scalar_one()
    return int(value or 0)


async def get_user_quota_bytes(user_id: int) -> int:
    async with transaction() as conn:
        row = (
            await conn.execute(select(users.c.quota_bytes).where(users.c.id == user_id))
        ).first()
    return int(row[0]) if row else 0


def _pack_task_select():
    return select(
        pack_tasks,
        stored_files.c.size_bytes.label("output_size"),
    ).select_from(
        pack_tasks.outerjoin(
            stored_files,
            pack_tasks.c.output_stored_file_id == stored_files.c.id,
        )
    )


async def clear_terminal_pack_tasks(user_id: int) -> int:
    async with transaction() as conn:
        result = await conn.execute(
            delete(pack_tasks)
            .where(
                pack_tasks.c.user_id == user_id,
                pack_tasks.c.status.in_(PACK_TERMINAL_STATUSES),
            )
            .returning(pack_tasks.c.id)
        )
        rows = result.all()
    return len(rows)


async def cancel_active_pack_task(user_id: int, task_id: int) -> dict[str, Any] | None:
    async with transaction() as conn:
        row = (
            await conn.execute(
                update(pack_tasks)
                .where(
                    pack_tasks.c.id == task_id,
                    pack_tasks.c.user_id == user_id,
                    pack_tasks.c.status.in_(PACK_ACTIVE_STATUSES),
                )
                .values(
                    status="cancelled",
                    progress=0,
                    reserved_bytes=0,
                    updated_at_ms=now_ms(),
                    finished_at_ms=now_ms(),
                )
                .returning(pack_tasks)
            )
        ).mappings().first()
    return dict(row) if row else None


async def delete_user_pack_task(user_id: int, task_id: int) -> None:
    async with transaction() as conn:
        await conn.execute(
            delete(pack_tasks).where(
                pack_tasks.c.id == task_id,
                pack_tasks.c.user_id == user_id,
            )
        )


async def completed_pack_output_name(
    *,
    user_id: int,
    source_user_file_ids_json: str,
) -> str | None:
    async with transaction() as conn:
        done_tasks = (
            (
                await conn.execute(
                    select(pack_tasks).where(
                        pack_tasks.c.user_id == user_id,
                        pack_tasks.c.source_user_file_ids_json
                        == source_user_file_ids_json,
                        pack_tasks.c.status == "completed",
                        pack_tasks.c.output_stored_file_id.is_not(None),
                    )
                )
            )
            .mappings()
            .all()
        )
        for done_task in done_tasks:
            user_file = (
                await conn.execute(
                    select(user_files.c.display_name).where(
                        user_files.c.user_id == user_id,
                        user_files.c.stored_file_id
                        == done_task["output_stored_file_id"],
                    )
                )
            ).first()
            if user_file:
                return str(user_file[0] or "未知文件")
    return None


async def create_pending_pack_task(
    *,
    user_id: int,
    source_user_file_ids_json: str,
    source_size_bytes: int,
    reserved_bytes: int,
    output_name: str | None,
    delete_source: bool,
) -> dict[str, Any]:
    async with transaction() as conn:
        existing_result = await conn.execute(
            select(pack_tasks.c.id).where(
                pack_tasks.c.user_id == user_id,
                pack_tasks.c.source_user_file_ids_json == source_user_file_ids_json,
                pack_tasks.c.status.in_(PACK_ACTIVE_STATUSES),
            )
        )
        if existing_result.first() is not None:
            raise ValueError("active_duplicate")

        timestamp = now_ms()
        row = (
            (
                await conn.execute(
                    insert(pack_tasks)
                    .values(
                        user_id=user_id,
                        source_user_file_ids_json=source_user_file_ids_json,
                        source_size_bytes=source_size_bytes,
                        reserved_bytes=reserved_bytes,
                        output_name=output_name,
                        delete_source=1 if delete_source else 0,
                        status="pending",
                        progress=0,
                        created_at_ms=timestamp,
                        updated_at_ms=timestamp,
                    )
                    .returning(pack_tasks)
                )
            )
            .mappings()
            .one()
        )
    return dict(row)


async def list_pack_task_rows(user_id: int) -> list[dict[str, Any]]:
    async with transaction() as conn:
        rows = (
            (
                await conn.execute(
                    _pack_task_select()
                    .where(pack_tasks.c.user_id == user_id)
                    .order_by(pack_tasks.c.created_at_ms.desc())
                )
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


async def get_pack_task_detail_row(
    user_id: int,
    task_id: int,
) -> dict[str, Any] | None:
    async with transaction() as conn:
        row = (
            (
                await conn.execute(
                    _pack_task_select().where(
                        pack_tasks.c.id == task_id,
                        pack_tasks.c.user_id == user_id,
                    )
                )
            )
            .mappings()
            .first()
        )
    return dict(row) if row else None
