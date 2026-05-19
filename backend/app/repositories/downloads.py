from __future__ import annotations

import time
from typing import Any

from collections.abc import Iterable

from sqlalchemy import case, delete, func, insert, or_, select, update
from sqlalchemy.exc import IntegrityError

from app.db.engine import transaction
from app.db.schema import global_downloads, user_files, user_storage_usage, user_tasks

ACTIVE_USER_TASK_STATUSES = ("queued", "active", "waiting", "paused")
ACTIVE_GLOBAL_DOWNLOAD_STATUSES = ACTIVE_USER_TASK_STATUSES
FAILABLE_GLOBAL_DOWNLOAD_STATUSES = (*ACTIVE_GLOBAL_DOWNLOAD_STATUSES, "completed")
TERMINAL_USER_TASK_STATUSES = ("completed", "failed", "cancelled")


def now_ms() -> int:
    return int(time.time() * 1000)


def _effective_terminal_user_task_condition():
    return or_(
        user_tasks.c.status.in_(TERMINAL_USER_TASK_STATUSES),
        user_tasks.c.global_download_id.in_(
            select(global_downloads.c.id).where(
                global_downloads.c.status.in_(TERMINAL_USER_TASK_STATUSES)
            )
        ),
    )


async def get_global_by_resource_key(resource_key: str) -> dict[str, Any] | None:
    async with transaction() as conn:
        row = (
            (
                await conn.execute(
                    select(global_downloads).where(
                        global_downloads.c.resource_key == resource_key
                    )
                )
            )
            .mappings()
            .first()
        )
    return dict(row) if row else None


async def get_global_download_by_gid(gid: str) -> dict[str, Any] | None:
    async with transaction() as conn:
        row = (
            (
                await conn.execute(
                    select(global_downloads).where(global_downloads.c.aria2_gid == gid)
                )
            )
            .mappings()
            .first()
        )
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
            (
                await conn.execute(
                    insert(global_downloads)
                    .values(**row_values)
                    .returning(global_downloads)
                )
            )
            .mappings()
            .one()
        )
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
            (
                await conn.execute(
                    select(user_tasks).where(
                        user_tasks.c.user_id == user_id,
                        user_tasks.c.global_download_id == global_download_id,
                    )
                )
            )
            .mappings()
            .first()
        )
    return dict(row) if row else None


def _user_task_download_select():
    return select(
        user_tasks.c.id,
        user_tasks.c.user_id,
        user_tasks.c.global_download_id,
        user_tasks.c.status,
        user_tasks.c.reserved_bytes,
        user_tasks.c.display_name,
        user_tasks.c.error_message,
        user_tasks.c.created_at_ms,
        user_tasks.c.updated_at_ms,
        user_tasks.c.finished_at_ms,
        global_downloads.c.resource_key,
        global_downloads.c.resource_kind,
        global_downloads.c.source_uri,
        global_downloads.c.display_name.label("global_display_name"),
        global_downloads.c.aria2_gid,
        global_downloads.c.status.label("global_status"),
        global_downloads.c.total_bytes,
        global_downloads.c.completed_bytes,
        global_downloads.c.error_code,
        global_downloads.c.error_message.label("global_error_message"),
        global_downloads.c.completed_at_ms,
    ).select_from(
        user_tasks.join(
            global_downloads,
            user_tasks.c.global_download_id == global_downloads.c.id,
        )
    )


async def get_user_task_by_id(user_id: int, user_task_id: int) -> dict[str, Any] | None:
    async with transaction() as conn:
        row = (
            (
                await conn.execute(
                    _user_task_download_select().where(
                        user_tasks.c.id == user_task_id,
                        user_tasks.c.user_id == user_id,
                    )
                )
            )
            .mappings()
            .first()
        )
    return dict(row) if row else None


async def get_user_task_by_gid(user_id: int, gid: str) -> dict[str, Any] | None:
    async with transaction() as conn:
        row = (
            (
                await conn.execute(
                    _user_task_download_select().where(
                        user_tasks.c.user_id == user_id,
                        global_downloads.c.aria2_gid == gid,
                    )
                )
            )
            .mappings()
            .first()
        )
    return dict(row) if row else None


async def list_user_tasks(
    user_id: int,
    statuses: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    query = _user_task_download_select().where(user_tasks.c.user_id == user_id)
    if statuses is not None:
        query = query.where(user_tasks.c.status.in_(tuple(statuses)))
    query = query.order_by(user_tasks.c.updated_at_ms.desc(), user_tasks.c.id.desc())

    async with transaction() as conn:
        rows = (await conn.execute(query)).mappings().all()
    return [dict(row) for row in rows]


async def delete_all_terminal_user_tasks(user_id: int) -> int:
    async with transaction() as conn:
        result = await conn.execute(
            delete(user_tasks).where(
                user_tasks.c.user_id == user_id,
                _effective_terminal_user_task_condition(),
            )
        )
    return int(result.rowcount or 0)


async def delete_terminal_user_task_by_gid(user_id: int, gid: str) -> bool:
    async with transaction() as conn:
        row = (
            await conn.execute(
                delete(user_tasks)
                .where(user_tasks.c.user_id == user_id)
                .where(_effective_terminal_user_task_condition())
                .where(
                    user_tasks.c.global_download_id.in_(
                        select(global_downloads.c.id).where(
                            global_downloads.c.aria2_gid == gid
                        )
                    )
                )
                .returning(user_tasks.c.id)
            )
        ).first()
    return row is not None


async def list_user_tasks_for_download(
    global_download_id: int,
    statuses: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    query = _user_task_download_select().where(
        user_tasks.c.global_download_id == global_download_id
    )
    if statuses is not None:
        query = query.where(user_tasks.c.status.in_(tuple(statuses)))
    query = query.order_by(user_tasks.c.updated_at_ms.desc(), user_tasks.c.id.desc())

    async with transaction() as conn:
        rows = (await conn.execute(query)).mappings().all()
    return [dict(row) for row in rows]


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
            (
                await conn.execute(
                    insert(user_tasks).values(**row_values).returning(user_tasks)
                )
            )
            .mappings()
            .one()
        )
    return dict(row)


async def update_global_download(
    download_id: int, values: dict[str, Any]
) -> dict[str, Any] | None:
    if not values:
        async with transaction() as conn:
            row = (
                (
                    await conn.execute(
                        select(global_downloads).where(
                            global_downloads.c.id == download_id
                        )
                    )
                )
                .mappings()
                .first()
            )
        return dict(row) if row else None

    row_values = {**values, "updated_at_ms": now_ms()}
    async with transaction() as conn:
        row = (
            (
                await conn.execute(
                    update(global_downloads)
                    .where(global_downloads.c.id == download_id)
                    .values(**row_values)
                    .returning(global_downloads)
                )
            )
            .mappings()
            .first()
        )
    return dict(row) if row else None


async def update_user_task(
    task_id: int, values: dict[str, Any]
) -> dict[str, Any] | None:
    if not values:
        async with transaction() as conn:
            row = (
                (
                    await conn.execute(
                        select(user_tasks).where(user_tasks.c.id == task_id)
                    )
                )
                .mappings()
                .first()
            )
        return dict(row) if row else None

    row_values = {**values, "updated_at_ms": now_ms()}
    async with transaction() as conn:
        row = (
            (
                await conn.execute(
                    update(user_tasks)
                    .where(user_tasks.c.id == task_id)
                    .values(**row_values)
                    .returning(user_tasks)
                )
            )
            .mappings()
            .first()
        )
    return dict(row) if row else None


async def attach_completed_file_to_user(
    *,
    user_id: int,
    quota_bytes: int,
    global_download_id: int,
    stored_file_id: int,
    size_bytes: int,
    display_name: str,
    finished_at_ms: int,
) -> dict[str, Any]:
    timestamp = now_ms()
    async with transaction() as conn:
        task = (
            (
                await conn.execute(
                    select(user_tasks).where(
                        user_tasks.c.user_id == user_id,
                        user_tasks.c.global_download_id == global_download_id,
                    )
                )
            )
            .mappings()
            .first()
        )

        user_file = (
            await conn.execute(
                select(user_files.c.id).where(
                    user_files.c.user_id == user_id,
                    user_files.c.stored_file_id == stored_file_id,
                )
            )
        ).first()
        if user_file is None:
            used_expr = user_storage_usage.c.used_bytes + size_bytes
            within_quota = (
                user_storage_usage.c.used_bytes
                + user_storage_usage.c.reserved_bytes
                + size_bytes
                <= quota_bytes
            )
            usage = (
                await conn.execute(
                    update(user_storage_usage)
                    .where(user_storage_usage.c.user_id == user_id)
                    .where(within_quota)
                    .values(used_bytes=used_expr, updated_at_ms=timestamp)
                    .returning(user_storage_usage.c.user_id)
                )
            ).first()
            if usage is None:
                raise ValueError("quota exceeded")
            await conn.execute(
                insert(user_files).values(
                    user_id=user_id,
                    stored_file_id=stored_file_id,
                    display_name=display_name,
                    created_at_ms=timestamp,
                    updated_at_ms=timestamp,
                )
            )

        reserved_bytes = int(task["reserved_bytes"] or 0) if task else 0
        if reserved_bytes > 0:
            reserved_expr = user_storage_usage.c.reserved_bytes - reserved_bytes
            await conn.execute(
                update(user_storage_usage)
                .where(user_storage_usage.c.user_id == user_id)
                .values(
                    reserved_bytes=case((reserved_expr < 0, 0), else_=reserved_expr),
                    updated_at_ms=timestamp,
                )
            )

        task_values = {
            "status": "completed",
            "reserved_bytes": 0,
            "display_name": display_name,
            "error_message": None,
            "updated_at_ms": timestamp,
            "finished_at_ms": finished_at_ms,
        }
        if task:
            row = (
                (
                    await conn.execute(
                        update(user_tasks)
                        .where(user_tasks.c.id == task["id"])
                        .values(**task_values)
                        .returning(user_tasks)
                    )
                )
                .mappings()
                .one()
            )
        else:
            row = (
                (
                    await conn.execute(
                        insert(user_tasks)
                        .values(
                            user_id=user_id,
                            global_download_id=global_download_id,
                            created_at_ms=timestamp,
                            **task_values,
                        )
                        .returning(user_tasks)
                    )
                )
                .mappings()
                .one()
            )
    return dict(row)


async def complete_active_user_tasks_for_stored_file(
    *,
    global_download_id: int,
    stored_file_id: int,
    size_bytes: int,
    original_name: str,
    completed_at_ms: int,
) -> int:
    timestamp = now_ms()
    async with transaction() as conn:
        global_row = (
            await conn.execute(
                update(global_downloads)
                .where(
                    global_downloads.c.id == global_download_id,
                    global_downloads.c.status.in_(ACTIVE_GLOBAL_DOWNLOAD_STATUSES),
                    global_downloads.c.completed_file_id.is_(None),
                )
                .values(
                    status="completed",
                    completed_file_id=stored_file_id,
                    completed_bytes=size_bytes,
                    completed_at_ms=completed_at_ms,
                    aria2_gid=None,
                    error_code=None,
                    error_message=None,
                    updated_at_ms=timestamp,
                )
                .returning(global_downloads.c.id)
            )
        ).first()
        if global_row is None:
            raise LookupError("global download is not active")

        tasks = (
            (
                await conn.execute(
                    select(user_tasks).where(
                        user_tasks.c.global_download_id == global_download_id,
                        user_tasks.c.status.in_(ACTIVE_USER_TASK_STATUSES),
                    )
                )
            )
            .mappings()
            .all()
        )

        user_files_created = 0
        for task in tasks:
            user_id = int(task["user_id"])
            display_name = str(task["display_name"] or original_name)
            user_file = (
                await conn.execute(
                    select(user_files.c.id).where(
                        user_files.c.user_id == user_id,
                        user_files.c.stored_file_id == stored_file_id,
                    )
                )
            ).first()
            if user_file is None:
                await conn.execute(
                    insert(user_files).values(
                        user_id=user_id,
                        stored_file_id=stored_file_id,
                        display_name=display_name,
                        created_at_ms=timestamp,
                        updated_at_ms=timestamp,
                    )
                )
                await conn.execute(
                    update(user_storage_usage)
                    .where(user_storage_usage.c.user_id == user_id)
                    .values(
                        used_bytes=user_storage_usage.c.used_bytes + size_bytes,
                        updated_at_ms=timestamp,
                    )
                )
                user_files_created += 1

            reserved_bytes = int(task["reserved_bytes"] or 0)
            if reserved_bytes > 0:
                reserved_expr = user_storage_usage.c.reserved_bytes - reserved_bytes
                await conn.execute(
                    update(user_storage_usage)
                    .where(user_storage_usage.c.user_id == user_id)
                    .values(
                        reserved_bytes=case(
                            (reserved_expr < 0, 0),
                            else_=reserved_expr,
                        ),
                        updated_at_ms=timestamp,
                    )
                )

            await conn.execute(
                update(user_tasks)
                .where(user_tasks.c.id == task["id"])
                .values(
                    status="completed",
                    reserved_bytes=0,
                    error_message=None,
                    updated_at_ms=timestamp,
                    finished_at_ms=completed_at_ms,
                )
            )
    return user_files_created


async def repair_completed_download_with_stored_file(
    *,
    global_download_id: int,
    stored_file_id: int,
    size_bytes: int,
    original_name: str,
    completed_at_ms: int,
) -> bool:
    timestamp = now_ms()
    async with transaction() as conn:
        row = (
            await conn.execute(
                update(global_downloads)
                .where(
                    global_downloads.c.id == global_download_id,
                    global_downloads.c.status == "completed",
                    global_downloads.c.completed_file_id.is_(None),
                )
                .values(
                    completed_file_id=stored_file_id,
                    completed_bytes=size_bytes,
                    completed_at_ms=completed_at_ms,
                    error_code=None,
                    error_message=None,
                    updated_at_ms=timestamp,
                )
                .returning(global_downloads.c.id)
            )
        ).first()
        if row is None:
            return False

        tasks = (
            (
                await conn.execute(
                    select(user_tasks).where(
                        user_tasks.c.global_download_id == global_download_id,
                        user_tasks.c.status.in_(ACTIVE_USER_TASK_STATUSES),
                    )
                )
            )
            .mappings()
            .all()
        )

        for task in tasks:
            user_id = int(task["user_id"])
            display_name = str(task["display_name"] or original_name)
            existing_file = (
                await conn.execute(
                    select(user_files.c.id).where(
                        user_files.c.user_id == user_id,
                        user_files.c.stored_file_id == stored_file_id,
                    )
                )
            ).first()
            if existing_file is None:
                await conn.execute(
                    insert(user_files).values(
                        user_id=user_id,
                        stored_file_id=stored_file_id,
                        display_name=display_name,
                        created_at_ms=timestamp,
                        updated_at_ms=timestamp,
                    )
                )
                await conn.execute(
                    update(user_storage_usage)
                    .where(user_storage_usage.c.user_id == user_id)
                    .values(
                        used_bytes=user_storage_usage.c.used_bytes + size_bytes,
                        updated_at_ms=timestamp,
                    )
                )

            reserved_bytes = int(task["reserved_bytes"] or 0)
            if reserved_bytes > 0:
                reserved_expr = user_storage_usage.c.reserved_bytes - reserved_bytes
                await conn.execute(
                    update(user_storage_usage)
                    .where(user_storage_usage.c.user_id == user_id)
                    .values(
                        reserved_bytes=case(
                            (reserved_expr < 0, 0),
                            else_=reserved_expr,
                        ),
                        updated_at_ms=timestamp,
                    )
                )

            await conn.execute(
                update(user_tasks)
                .where(user_tasks.c.id == task["id"])
                .values(
                    status="completed",
                    reserved_bytes=0,
                    error_message=None,
                    updated_at_ms=timestamp,
                    finished_at_ms=completed_at_ms,
                )
            )
    return True


async def mark_global_download_failed(
    download_id: int,
    *,
    message: str,
    error_code: str | None = None,
    clear_gid: bool = True,
) -> dict[str, Any] | None:
    timestamp = now_ms()
    global_values: dict[str, Any] = {
        "status": "failed",
        "error_code": error_code,
        "error_message": message,
        "updated_at_ms": timestamp,
    }
    if clear_gid:
        global_values["aria2_gid"] = None

    async with transaction() as conn:
        row = (
            (
                await conn.execute(
                    update(global_downloads)
                    .where(
                        global_downloads.c.id == download_id,
                        global_downloads.c.status.in_(
                            FAILABLE_GLOBAL_DOWNLOAD_STATUSES
                        ),
                        global_downloads.c.completed_file_id.is_(None),
                    )
                    .values(**global_values)
                    .returning(global_downloads)
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            current = (
                (
                    await conn.execute(
                        select(global_downloads).where(
                            global_downloads.c.id == download_id
                        )
                    )
                )
                .mappings()
                .first()
            )
            return dict(current) if current else None

        active_tasks = (
            (
                await conn.execute(
                    select(user_tasks).where(
                        user_tasks.c.global_download_id == download_id,
                        user_tasks.c.status.in_(ACTIVE_USER_TASK_STATUSES),
                    )
                )
            )
            .mappings()
            .all()
        )

        for task in active_tasks:
            reserved_bytes = int(task["reserved_bytes"] or 0)
            if reserved_bytes <= 0:
                continue
            reserved_expr = user_storage_usage.c.reserved_bytes - reserved_bytes
            await conn.execute(
                update(user_storage_usage)
                .where(user_storage_usage.c.user_id == task["user_id"])
                .values(
                    reserved_bytes=case((reserved_expr < 0, 0), else_=reserved_expr),
                    updated_at_ms=timestamp,
                )
            )

        await conn.execute(
            update(user_tasks)
            .where(
                user_tasks.c.global_download_id == download_id,
                user_tasks.c.status.in_(ACTIVE_USER_TASK_STATUSES),
            )
            .values(
                status="failed",
                reserved_bytes=0,
                error_message=message,
                updated_at_ms=timestamp,
                finished_at_ms=timestamp,
            )
        )
    return dict(row)


async def cancel_active_user_task(
    user_id: int,
    user_task_id: int,
    *,
    error_message: str,
    finished_at_ms: int,
) -> dict[str, Any] | None:
    timestamp = now_ms()
    async with transaction() as conn:
        task = (
            (
                await conn.execute(
                    select(user_tasks).where(
                        user_tasks.c.id == user_task_id,
                        user_tasks.c.user_id == user_id,
                        user_tasks.c.status.in_(ACTIVE_USER_TASK_STATUSES),
                    )
                )
            )
            .mappings()
            .first()
        )
        if task is None:
            return None

        reserved_bytes = int(task["reserved_bytes"] or 0)
        if reserved_bytes > 0:
            reserved_expr = user_storage_usage.c.reserved_bytes - reserved_bytes
            await conn.execute(
                update(user_storage_usage)
                .where(user_storage_usage.c.user_id == user_id)
                .values(
                    reserved_bytes=case(
                        (reserved_expr < 0, 0),
                        else_=reserved_expr,
                    ),
                    updated_at_ms=timestamp,
                )
            )

        row = (
            (
                await conn.execute(
                    update(user_tasks)
                    .where(
                        user_tasks.c.id == user_task_id,
                        user_tasks.c.user_id == user_id,
                        user_tasks.c.status.in_(ACTIVE_USER_TASK_STATUSES),
                    )
                    .values(
                        status="cancelled",
                        reserved_bytes=0,
                        error_message=error_message,
                        finished_at_ms=finished_at_ms,
                        updated_at_ms=timestamp,
                    )
                    .returning(user_tasks)
                )
            )
            .mappings()
            .first()
        )
    return dict(row) if row else None


async def count_active_user_tasks(global_download_id: int) -> int:
    async with transaction() as conn:
        count = (
            await conn.execute(
                select(func.count())
                .select_from(user_tasks)
                .where(
                    user_tasks.c.global_download_id == global_download_id,
                    user_tasks.c.status.in_(ACTIVE_USER_TASK_STATUSES),
                )
            )
        ).scalar_one()
    return int(count)


async def delete_terminal_user_task(user_id: int, user_task_id: int) -> bool:
    async with transaction() as conn:
        row = (
            await conn.execute(
                delete(user_tasks)
                .where(
                    user_tasks.c.id == user_task_id,
                    user_tasks.c.user_id == user_id,
                    _effective_terminal_user_task_condition(),
                )
                .returning(user_tasks.c.id)
            )
        ).first()
    return row is not None


async def clear_terminal_user_tasks(user_id: int) -> int:
    async with transaction() as conn:
        rows = (
            await conn.execute(
                delete(user_tasks)
                .where(
                    user_tasks.c.user_id == user_id,
                    _effective_terminal_user_task_condition(),
                )
                .returning(user_tasks.c.id)
            )
        ).all()
    return len(rows)
