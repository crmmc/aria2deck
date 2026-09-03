from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sqlalchemy import delete, exists, func, insert, or_, select, update
from sqlalchemy.exc import IntegrityError

from app.db.engine import transaction
from app.db.schema import (
    global_downloads,
    stored_files,
    user_files,
    user_storage_usage,
    user_tasks,
    users,
)
from app.domain.lifecycle import TerminalizationClaim, make_terminalization_claim
from app.domain.status import (
    ACTIVE_GLOBAL_DOWNLOAD_STATUSES,
    ACTIVE_USER_TASK_STATUSES,
    ERROR_DOWNLOAD_STATUSES,
    FAILABLE_GLOBAL_DOWNLOAD_STATUSES,
    REST_TASK_STATUS_FILTERS,
    TERMINAL_USER_TASK_STATUSES,
)
from app.repositories.errors import RepositoryConflictError
from app.repositories.task.downloads import (
    DiskAvailable,
    _complete_user_task_with_file,
    _fail_active_task_row,
    _lock_active_download,  # noqa: F401  # 保留既有模块属性兼容路径
    _reconcile_download_size_locked,  # noqa: F401  # 保留既有模块属性兼容路径
    _strict_adjust_usage_reserved,
    now_ms,
    refreshable_user_task_display_name_condition,
)

__all__ = [
    "ACTIVE_GLOBAL_DOWNLOAD_STATUSES",
    "ACTIVE_USER_TASK_STATUSES",
    "ERROR_DOWNLOAD_STATUSES",
    "FAILABLE_GLOBAL_DOWNLOAD_STATUSES",
    "REST_TASK_STATUS_FILTERS",
    "TERMINAL_USER_TASK_STATUSES",
    "Any",
    "DiskAvailable",
    "DownloadAdmissionError",
    "IntegrityError",
    "Iterable",
    "RepositoryConflictError",
    "TerminalizationClaim",
    "attach_completed_file_to_user",
    "cancel_active_user_task",
    "cancel_user_task_and_maybe_claim_attempt",
    "clear_terminal_user_tasks",
    "complete_active_user_tasks_for_stored_file",
    "count_active_user_tasks",
    "create_user_task",
    "delete",
    "delete_all_terminal_user_tasks",
    "delete_terminal_user_task",
    "delete_terminal_user_task_by_gid",
    "exists",
    "func",
    "get_representative_active_owner_id",
    "get_user_task",
    "get_user_task_by_gid",
    "get_user_task_by_id",
    "global_downloads",
    "insert",
    "list_user_tasks",
    "list_user_tasks_for_download",
    "list_user_tasks_page",
    "make_terminalization_claim",
    "mark_global_download_failed",
    "now_ms",
    "or_",
    "refreshable_user_task_display_name_condition",
    "repair_completed_download_with_stored_file",
    "select",
    "stored_files",
    "transaction",
    "update",
    "update_active_user_tasks",
    "user_files",
    "user_storage_usage",
    "user_tasks",
    "users",
]


class DownloadAdmissionError(ValueError):
    def __init__(self, reason: str):
        self.reason = reason
        message = "quota exceeded" if reason == "quota" else reason
        super().__init__(message)


def _effective_terminal_user_task_condition():
    return or_(
        user_tasks.c.status.in_(TERMINAL_USER_TASK_STATUSES),
        user_tasks.c.global_download_id.in_(
            select(global_downloads.c.id).where(
                global_downloads.c.status.in_(TERMINAL_USER_TASK_STATUSES)
            )
        ),
    )


async def get_representative_active_owner_id(download_id: int) -> int | None:
    async with transaction() as conn:
        row = (
            await conn.execute(
                select(user_tasks.c.user_id)
                .where(
                    user_tasks.c.global_download_id == download_id,
                    user_tasks.c.status.in_(ACTIVE_USER_TASK_STATUSES),
                )
                .limit(1)
            )
        ).first()
    return int(row[0]) if row else None


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
        user_tasks.c.history_expired_at_ms,
        global_downloads.c.resource_key,
        global_downloads.c.resource_kind,
        global_downloads.c.source_uri,
        global_downloads.c.bt_info_hash,
        global_downloads.c.display_name.label("global_display_name"),
        global_downloads.c.aria2_gid,
        global_downloads.c.status.label("global_status"),
        global_downloads.c.total_bytes,
        global_downloads.c.size_known,
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


def _active_task_user():
    return exists(
        select(users.c.id).where(
            users.c.id == user_tasks.c.user_id,
            users.c.pending_delete == 0,
        )
    )


def _rest_task_status_condition(status_filter: str | None):
    if status_filter is None:
        return None
    if status_filter not in REST_TASK_STATUS_FILTERS:
        raise ValueError(status_filter)

    user_terminal = user_tasks.c.status.in_(TERMINAL_USER_TASK_STATUSES)
    global_terminal = global_downloads.c.status.in_(TERMINAL_USER_TASK_STATUSES)
    active = user_tasks.c.status.in_(ACTIVE_USER_TASK_STATUSES)
    if status_filter in {"active", "current"}:
        return (~user_terminal) & (~global_terminal) & active
    if status_filter == "complete":
        return (user_tasks.c.status == "completed") | (
            (~user_terminal) & (global_downloads.c.status == "completed")
        )
    return user_tasks.c.status.in_(ERROR_DOWNLOAD_STATUSES) | (
        (~user_terminal) & global_downloads.c.status.in_(ERROR_DOWNLOAD_STATUSES)
    )


async def get_user_task_by_id(
    user_id: int, user_task_id: int, *, include_pending_user: bool = False
) -> dict[str, Any] | None:
    conditions = [
        user_tasks.c.id == user_task_id,
        user_tasks.c.user_id == user_id,
    ]
    if not include_pending_user:
        conditions.append(_active_task_user())
    async with transaction() as conn:
        row = (
            await conn.execute(_user_task_download_select().where(*conditions))
        ).mappings().first()
    return dict(row) if row else None


async def get_user_task_by_gid(user_id: int, gid: str) -> dict[str, Any] | None:
    async with transaction() as conn:
        row = (
            await conn.execute(
                _user_task_download_select().where(
                    user_tasks.c.user_id == user_id,
                    global_downloads.c.aria2_gid == gid,
                    _active_task_user(),
                )
            )
        ).mappings().first()
    return dict(row) if row else None


async def list_user_tasks(
    user_id: int,
    statuses: Iterable[str] | None = None,
    *,
    include_pending_user: bool = False,
) -> list[dict[str, Any]]:
    query = _user_task_download_select().where(user_tasks.c.user_id == user_id)
    if not include_pending_user:
        query = query.where(_active_task_user())
    if statuses is not None:
        query = query.where(user_tasks.c.status.in_(tuple(statuses)))
    query = query.order_by(user_tasks.c.updated_at_ms.desc(), user_tasks.c.id.desc())

    async with transaction() as conn:
        rows = (await conn.execute(query)).mappings().all()
    return [dict(row) for row in rows]


async def list_user_tasks_page(
    user_id: int,
    *,
    page: int,
    page_size: int,
    status_filter: str | None = None,
    statuses: Iterable[str] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    if page < 1 or page_size < 1:
        raise ValueError("invalid page")

    conditions = [user_tasks.c.user_id == user_id, _active_task_user()]
    if statuses is not None:
        conditions.append(user_tasks.c.status.in_(tuple(statuses)))
    status_condition = _rest_task_status_condition(status_filter)
    if status_condition is not None:
        conditions.append(status_condition)

    join = user_tasks.join(
        global_downloads, user_tasks.c.global_download_id == global_downloads.c.id
    )
    query = (
        _user_task_download_select()
        .where(*conditions)
        .order_by(user_tasks.c.created_at_ms.desc(), user_tasks.c.id.desc())
        .limit(page_size)
        .offset((page - 1) * page_size)
    )
    count_query = select(func.count()).select_from(join).where(*conditions)
    async with transaction() as conn:
        total = int((await conn.execute(count_query)).scalar_one())
        rows = (await conn.execute(query)).mappings().all()
    return [dict(row) for row in rows], total


async def delete_all_terminal_user_tasks(user_id: int) -> list[int]:
    """Hard-delete all terminal pids for user. Returns global_download_ids (tids)."""
    async with transaction() as conn:
        rows = (
            await conn.execute(
                delete(user_tasks)
                .where(
                    user_tasks.c.user_id == user_id,
                    _effective_terminal_user_task_condition(),
                )
                .returning(user_tasks.c.global_download_id)
            )
        ).all()
    return [int(row[0]) for row in rows]


async def delete_terminal_user_task_by_gid(user_id: int, gid: str) -> int | None:
    """Hard-delete a terminal pid by aria2 gid. Returns global_download_id if deleted."""
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
                .returning(user_tasks.c.id, user_tasks.c.global_download_id)
            )
        ).first()
    if row is None:
        return None
    return int(row[1])


async def list_user_tasks_for_download(
    global_download_id: int,
    statuses: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    query = _user_task_download_select().where(
        user_tasks.c.global_download_id == global_download_id,
        _active_task_user(),
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
    try:
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
    except IntegrityError as exc:
        raise RepositoryConflictError(str(exc)) from exc
    return dict(row)




async def update_active_user_tasks(
    download_id: int,
    *,
    expected_gid: str,
    status: str | None = None,
    display_name: str | None = None,
    force_display_name: bool = False,
) -> None:
    timestamp = now_ms()
    base_condition = [
        user_tasks.c.global_download_id == download_id,
        user_tasks.c.status.in_(ACTIVE_USER_TASK_STATUSES),
        user_tasks.c.global_download_id.in_(
            select(global_downloads.c.id).where(
                global_downloads.c.id == download_id,
                global_downloads.c.aria2_gid == expected_gid,
            )
        ),
    ]
    async with transaction() as conn:
        if status is not None:
            await conn.execute(
                update(user_tasks)
                .where(*base_condition)
                .values(status=status, updated_at_ms=timestamp)
            )
        if display_name:
            if force_display_name:
                await conn.execute(
                    update(user_tasks)
                    .where(*base_condition)
                    .values(display_name=display_name, updated_at_ms=timestamp)
                )
            else:
                await conn.execute(
                    update(user_tasks)
                    .where(
                        *base_condition,
                        refreshable_user_task_display_name_condition(),
                    )
                    .values(display_name=display_name, updated_at_ms=timestamp)
                )



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
        active_target = (
            await conn.execute(
                select(users.c.id).where(
                    users.c.id == user_id,
                    users.c.pending_delete == 0,
                    exists(
                        select(stored_files.c.id).where(
                            stored_files.c.id == stored_file_id,
                            stored_files.c.pending_delete == 0,
                        )
                    ),
                )
            )
        ).first()
        if active_target is None:
            raise RepositoryConflictError("用户或存储文件正在删除")
        task = (
            (
                await conn.execute(
                    select(user_tasks)
                    .select_from(
                        user_tasks.join(users, users.c.id == user_tasks.c.user_id)
                    )
                    .where(
                        user_tasks.c.user_id == user_id,
                        user_tasks.c.global_download_id == global_download_id,
                        users.c.pending_delete == 0,
                        exists(
                            select(stored_files.c.id).where(
                                stored_files.c.id == stored_file_id,
                                stored_files.c.pending_delete == 0,
                            )
                        ),
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
                <= select(users.c.quota_bytes)
                .where(
                    users.c.id == user_id,
                    users.c.pending_delete == 0,
                )
                .scalar_subquery()
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
            released = (
                await conn.execute(
                    update(user_storage_usage)
                    .where(
                        user_storage_usage.c.user_id == user_id,
                        user_storage_usage.c.reserved_bytes >= reserved_bytes,
                    )
                    .values(
                        reserved_bytes=(
                            user_storage_usage.c.reserved_bytes - reserved_bytes
                        ),
                        updated_at_ms=timestamp,
                    )
                    .returning(user_storage_usage.c.user_id)
                )
            ).first()
            if released is None:
                raise RepositoryConflictError("reserved usage drift")

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
    expected_gid: str,
    stored_file_id: int,
    size_bytes: int,
    original_name: str,
    completed_at_ms: int,
) -> int | None:
    timestamp = now_ms()
    active_subscriber = exists(
        select(user_tasks.c.id)
        .select_from(user_tasks.join(users, users.c.id == user_tasks.c.user_id))
        .where(
            user_tasks.c.global_download_id == global_download_id,
            user_tasks.c.status.in_(ACTIVE_USER_TASK_STATUSES),
            users.c.pending_delete == 0,
        )
    )
    active_stored = exists(
        select(stored_files.c.id).where(
            stored_files.c.id == stored_file_id,
            stored_files.c.pending_delete == 0,
        )
    )
    async with transaction() as conn:
        global_row = (
            await conn.execute(
                update(global_downloads)
                .where(
                    global_downloads.c.id == global_download_id,
                    global_downloads.c.aria2_gid == expected_gid,
                    global_downloads.c.status.in_(ACTIVE_GLOBAL_DOWNLOAD_STATUSES),
                    global_downloads.c.completed_file_id.is_(None),
                    active_subscriber,
                    active_stored,
                )
                .values(
                    status="completed",
                    completed_file_id=stored_file_id,
                    total_bytes=size_bytes,
                    completed_bytes=size_bytes,
                    size_known=1,
                    disk_reserved_bytes=0,
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
            return None

        tasks = (
            (
                await conn.execute(
                    select(user_tasks)
                    .select_from(
                        user_tasks.join(users, users.c.id == user_tasks.c.user_id)
                    )
                    .where(
                        user_tasks.c.global_download_id == global_download_id,
                        user_tasks.c.status.in_(ACTIVE_USER_TASK_STATUSES),
                        users.c.pending_delete == 0,
                    )
                )
            )
            .mappings()
            .all()
        )

        user_files_created = 0
        for task in tasks:
            created = await _complete_user_task_with_file(
                conn,
                task,
                stored_file_id=stored_file_id,
                size_bytes=size_bytes,
                original_name=original_name,
                completed_at_ms=completed_at_ms,
                timestamp=timestamp,
            )
            user_files_created += int(created)
    return user_files_created


async def repair_completed_download_with_stored_file(
    *,
    global_download_id: int,
    expected_gid: str | None,
    stored_file_id: int,
    size_bytes: int,
    original_name: str,
    completed_at_ms: int,
) -> bool:
    timestamp = now_ms()
    active_subscriber = exists(
        select(user_tasks.c.id)
        .select_from(user_tasks.join(users, users.c.id == user_tasks.c.user_id))
        .where(
            user_tasks.c.global_download_id == global_download_id,
            user_tasks.c.status.in_(ACTIVE_USER_TASK_STATUSES),
            users.c.pending_delete == 0,
        )
    )
    active_stored = exists(
        select(stored_files.c.id).where(
            stored_files.c.id == stored_file_id,
            stored_files.c.pending_delete == 0,
        )
    )
    gid_condition = (
        global_downloads.c.aria2_gid.is_(None)
        if expected_gid is None
        else global_downloads.c.aria2_gid == expected_gid
    )
    async with transaction() as conn:
        row = (
            await conn.execute(
                update(global_downloads)
                .where(
                    global_downloads.c.id == global_download_id,
                    global_downloads.c.status == "completed",
                    global_downloads.c.completed_file_id.is_(None),
                    gid_condition,
                    active_subscriber,
                    active_stored,
                )
                .values(
                    completed_file_id=stored_file_id,
                    total_bytes=size_bytes,
                    completed_bytes=size_bytes,
                    size_known=1,
                    disk_reserved_bytes=0,
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
                    select(user_tasks)
                    .select_from(
                        user_tasks.join(users, users.c.id == user_tasks.c.user_id)
                    )
                    .where(
                        user_tasks.c.global_download_id == global_download_id,
                        user_tasks.c.status.in_(ACTIVE_USER_TASK_STATUSES),
                        users.c.pending_delete == 0,
                    )
                )
            )
            .mappings()
            .all()
        )

        for task in tasks:
            await _complete_user_task_with_file(
                conn,
                task,
                stored_file_id=stored_file_id,
                size_bytes=size_bytes,
                original_name=original_name,
                completed_at_ms=completed_at_ms,
                timestamp=timestamp,
            )
    return True


async def mark_global_download_failed(
    download_id: int,
    *,
    expected_gid: str | None,
    message: str,
    error_code: str | None = None,
    clear_gid: bool = False,
    expected_statuses: Iterable[str] = FAILABLE_GLOBAL_DOWNLOAD_STATUSES,
) -> dict[str, Any] | None:
    timestamp = now_ms()
    gid_condition = (
        global_downloads.c.aria2_gid.is_(None)
        if expected_gid is None
        else global_downloads.c.aria2_gid == expected_gid
    )
    global_values: dict[str, Any] = {
        "status": "failed",
        "disk_reserved_bytes": 0,
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
                        gid_condition,
                        global_downloads.c.status.in_(tuple(expected_statuses)),
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
            return None

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
            await _fail_active_task_row(
                conn,
                task,
                message=message,
                timestamp=timestamp,
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
            await _strict_adjust_usage_reserved(
                conn,
                user_id=user_id,
                delta=-reserved_bytes,
                timestamp=timestamp,
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
        remaining = (
            await conn.execute(
                select(func.count()).select_from(user_tasks).where(
                    user_tasks.c.global_download_id == task["global_download_id"],
                    user_tasks.c.status.in_(ACTIVE_USER_TASK_STATUSES),
                )
            )
        ).scalar_one()
        if int(remaining) == 0:
            await conn.execute(
                update(global_downloads)
                .where(
                    global_downloads.c.id == task["global_download_id"],
                    global_downloads.c.status.in_(ACTIVE_GLOBAL_DOWNLOAD_STATUSES),
                )
                .values(
                    status="cancelled",
                    aria2_gid=None,
                    disk_reserved_bytes=0,
                    error_message=error_message,
                    updated_at_ms=timestamp,
                )
            )
    return dict(row) if row else None


async def cancel_user_task_and_maybe_claim_attempt(
    *,
    user_id: int,
    user_task_id: int,
    expected_gid: str | None,
    error_message: str = "用户取消",
) -> tuple[dict[str, Any] | None, TerminalizationClaim | None]:
    """Atomically cancel a user task and conditionally claim the attempt (§13).

    If other active subscribers remain, only the user task is cancelled and
    the global attempt stays live.  If this is the last active subscriber,
    the attempt is CAS-terminalized to ``cancelled`` in the same transaction,
    preserving ``aria2_gid`` for residual cleanup fencing.

    Returns ``(updated_task, claim)`` when the last subscriber cancels and the
    CAS succeeds; ``(updated_task, None)`` when other subscribers remain or the
    CAS does not match (global already terminal, GID changed); ``(None, None)``
    when the user task is already terminal, does not belong to the user, or
    does not exist.
    """
    timestamp = now_ms()
    claim: TerminalizationClaim | None = None
    updated_task: dict[str, Any] | None = None
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
            return None, None

        download_id = int(task["global_download_id"])

        reserved_bytes = int(task["reserved_bytes"] or 0)
        if reserved_bytes > 0:
            await _strict_adjust_usage_reserved(
                conn,
                user_id=user_id,
                delta=-reserved_bytes,
                timestamp=timestamp,
            )

        updated_row = (
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
                        finished_at_ms=timestamp,
                        updated_at_ms=timestamp,
                    )
                    .returning(user_tasks)
                )
            )
            .mappings()
            .first()
        )
        if updated_row is None:
            return None, None

        remaining = (
            await conn.execute(
                select(func.count())
                .select_from(user_tasks)
                .where(
                    user_tasks.c.global_download_id == download_id,
                    user_tasks.c.status.in_(ACTIVE_USER_TASK_STATUSES),
                )
            )
        ).scalar_one()

        if int(remaining) > 0:
            updated_task = dict(updated_row)
        else:
            gid_condition = (
                global_downloads.c.aria2_gid.is_(None)
                if expected_gid is None
                else global_downloads.c.aria2_gid == expected_gid
            )
            global_row = (
                (
                    await conn.execute(
                        update(global_downloads)
                        .where(
                            global_downloads.c.id == download_id,
                            gid_condition,
                            global_downloads.c.status.in_(
                                ACTIVE_GLOBAL_DOWNLOAD_STATUSES
                            ),
                            global_downloads.c.completed_file_id.is_(None),
                        )
                        .values(
                            status="cancelled",
                            disk_reserved_bytes=0,
                            error_code="user_cancelled",
                            error_message=error_message,
                            updated_at_ms=timestamp,
                        )
                        .returning(global_downloads)
                    )
                )
                .mappings()
                .first()
            )
            updated_task = dict(updated_row)
            if global_row is not None:
                current_gid = global_row["aria2_gid"]
                w_gids = (current_gid,) if current_gid is not None else ()
                claim = make_terminalization_claim(
                    attempt_id=download_id,
                    expected_current_gid=current_gid,
                    writer_gids=w_gids,
                    result_gids=w_gids,
                    terminal_status="cancelled",
                    claim_timestamp=timestamp,
                    error_code="user_cancelled",
                    error_message=error_message,
                )
    return updated_task, claim


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


async def delete_terminal_user_task(user_id: int, user_task_id: int) -> int | None:
    """Hard-delete a terminal pid. Returns global_download_id if deleted."""
    async with transaction() as conn:
        row = (
            await conn.execute(
                delete(user_tasks)
                .where(
                    user_tasks.c.id == user_task_id,
                    user_tasks.c.user_id == user_id,
                    _effective_terminal_user_task_condition(),
                )
                .returning(user_tasks.c.id, user_tasks.c.global_download_id)
            )
        ).first()
    if row is None:
        return None
    return int(row[1])


async def clear_terminal_user_tasks(user_id: int) -> list[int]:
    """Hard-delete all terminal pids for user. Returns global_download_ids."""
    async with transaction() as conn:
        rows = (
            await conn.execute(
                delete(user_tasks)
                .where(
                    user_tasks.c.user_id == user_id,
                    _effective_terminal_user_task_condition(),
                )
                .returning(user_tasks.c.global_download_id)
            )
        ).all()
    return [int(row[0]) for row in rows]
