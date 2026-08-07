from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
import time
from typing import Any, Literal, overload

from sqlalchemy import case, delete, exists, func, insert, literal, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.sql.elements import ColumnElement

from app.db.engine import transaction
from app.db.schema import (
    global_downloads,
    pack_tasks,
    stored_files,
    user_files,
    user_storage_usage,
    user_tasks,
    users,
)
from app.domain.lifecycle import (
    RepairClaim,
    TerminalizationClaim,
    make_repair_claim,
    make_terminalization_claim,
)
from app.domain.status import (
    ACTIVE_GLOBAL_DOWNLOAD_STATUSES,
    ACTIVE_USER_TASK_STATUSES,
    ERROR_DOWNLOAD_STATUSES,
    FAILABLE_GLOBAL_DOWNLOAD_STATUSES,
    REST_TASK_STATUS_FILTERS,
    TERMINAL_USER_TASK_STATUSES,
)
from app.repositories.errors import RepositoryConflictError


class DownloadAdmissionError(ValueError):
    def __init__(self, reason: str):
        self.reason = reason
        message = "quota exceeded" if reason == "quota" else reason
        super().__init__(message)


class SizeReconcileResult(dict[str, Any]):
    @property
    def admitted(self) -> bool:
        return self.get("outcome") == "admitted"


DiskAvailable = int | Callable[[], int]


def now_ms() -> int:
    return int(time.time() * 1000)


async def _strict_adjust_usage_reserved(
    conn: Any,
    *,
    user_id: int,
    delta: int,
    quota_bytes: int | None = None,
    timestamp: int,
) -> bool:
    if delta == 0:
        return True
    reserved = user_storage_usage.c.reserved_bytes + delta
    conditions = [user_storage_usage.c.user_id == user_id, reserved >= 0]
    if delta > 0:
        if quota_bytes is None:
            raise ValueError("quota_bytes is required for reservation growth")
        conditions.extend(
            [
                user_storage_usage.c.used_bytes + reserved <= quota_bytes,
                exists(
                    select(users.c.id).where(
                        users.c.id == user_id,
                        users.c.pending_delete == 0,
                    )
                ),
            ]
        )
    row = (
        await conn.execute(
            update(user_storage_usage)
            .where(*conditions)
            .values(reserved_bytes=reserved, updated_at_ms=timestamp)
            .returning(user_storage_usage.c.user_id)
        )
    ).first()
    if row is None and delta < 0:
        raise RepositoryConflictError("reserved usage drift")
    return row is not None


async def active_physical_commitment_bytes(conn: Any) -> int:
    """Future disk write commitment for admission / visible headroom.

    Only tasks that are actively claiming future write capacity count.
    Waiting/paused do not lock full claimed size; terminal states never count.
    Bytes already on disk are reflected by ``df.free``, not this commitment.
    """
    remaining = case(
        (
            global_downloads.c.status.in_(("active", "queued")),
            case(
                (
                    global_downloads.c.disk_reserved_bytes
                    > global_downloads.c.completed_bytes,
                    global_downloads.c.disk_reserved_bytes
                    - global_downloads.c.completed_bytes,
                ),
                else_=0,
            ),
        ),
        else_=0,
    )
    downloads_reserved = (
        await conn.execute(
            select(func.coalesce(func.sum(remaining), 0)).where(
                global_downloads.c.status.in_(ACTIVE_GLOBAL_DOWNLOAD_STATUSES)
            )
        )
    ).scalar_one()
    pack_remaining = case(
        (
            pack_tasks.c.reserved_bytes > pack_tasks.c.materialized_bytes,
            pack_tasks.c.reserved_bytes - pack_tasks.c.materialized_bytes,
        ),
        else_=0,
    )
    packs_reserved = (
        await conn.execute(
            select(
                func.coalesce(
                    func.sum(pack_remaining + pack_tasks.c.install_reserved_bytes),
                    0,
                )
            ).where(pack_tasks.c.status.in_(("pending", "packing")))
        )
    ).scalar_one()
    return int(downloads_reserved or 0) + int(packs_reserved or 0)


async def get_active_physical_commitment_bytes() -> int:
    async with transaction() as conn:
        return await active_physical_commitment_bytes(conn)


def _remaining_disk_bytes(reserved: int, completed: int) -> int:
    return max(0, reserved - completed)


async def _lock_active_download(
    conn: Any,
    download_id: int,
    *,
    expected_gid: str | None,
) -> dict[str, Any] | None:
    gid_condition = (
        global_downloads.c.aria2_gid.is_(None)
        if expected_gid is None
        else global_downloads.c.aria2_gid == expected_gid
    )
    row = (
        await conn.execute(
            update(global_downloads)
            .where(
                global_downloads.c.id == download_id,
                gid_condition,
                global_downloads.c.status.in_(ACTIVE_GLOBAL_DOWNLOAD_STATUSES),
                global_downloads.c.completed_file_id.is_(None),
            )
            .values(updated_at_ms=global_downloads.c.updated_at_ms)
            .returning(global_downloads)
        )
    ).mappings().first()
    return dict(row) if row else None


async def _terminate_active_task_row(
    conn: Any,
    task: Any,
    *,
    terminal_status: str,
    message: str,
    timestamp: int,
) -> None:
    reserved = int(task["reserved_bytes"] or 0)
    if reserved:
        await _strict_adjust_usage_reserved(
            conn,
            user_id=int(task["user_id"]),
            delta=-reserved,
            timestamp=timestamp,
        )
    await conn.execute(
        update(user_tasks)
        .where(
            user_tasks.c.id == task["id"],
            user_tasks.c.status.in_(ACTIVE_USER_TASK_STATUSES),
        )
        .values(
            status=terminal_status,
            reserved_bytes=0,
            error_message=message,
            updated_at_ms=timestamp,
            finished_at_ms=timestamp,
        )
    )


async def _fail_active_task_row(
    conn: Any,
    task: Any,
    *,
    message: str,
    timestamp: int,
) -> None:
    await _terminate_active_task_row(
        conn, task, terminal_status="failed", message=message, timestamp=timestamp
    )


async def _fail_download_rows(
    conn: Any,
    download: Mapping[str, Any],
    *,
    message: str,
    error_code: str,
    timestamp: int,
) -> None:
    tasks = (
        await conn.execute(
            select(user_tasks).where(
                user_tasks.c.global_download_id == download["id"],
                user_tasks.c.status.in_(ACTIVE_USER_TASK_STATUSES),
            )
        )
    ).mappings().all()
    for task in tasks:
        await _fail_active_task_row(
            conn, task, message=message, timestamp=timestamp
        )
    await conn.execute(
        update(global_downloads)
        .where(global_downloads.c.id == download["id"])
        .values(
            status="failed",
            disk_reserved_bytes=0,
            error_code=error_code,
            error_message=message,
            updated_at_ms=timestamp,
        )
    )


def _effective_terminal_user_task_condition():
    return or_(
        user_tasks.c.status.in_(TERMINAL_USER_TASK_STATUSES),
        user_tasks.c.global_download_id.in_(
            select(global_downloads.c.id).where(
                global_downloads.c.status.in_(TERMINAL_USER_TASK_STATUSES)
            )
        ),
    )


def refreshable_user_task_display_name_condition() -> ColumnElement[bool]:
    """Return rows whose user task name is still a system placeholder."""
    synthetic_torrent_name = literal("torrent-") + func.substr(
        global_downloads.c.resource_key,
        1,
        12,
    )
    return or_(
        user_tasks.c.display_name.is_(None),
        user_tasks.c.display_name == "",
        user_tasks.c.display_name.startswith("magnet:"),
        user_tasks.c.display_name.startswith("torrent:"),
        user_tasks.c.display_name.startswith("[METADATA]"),
        exists().where(
            global_downloads.c.id == user_tasks.c.global_download_id,
            global_downloads.c.resource_kind == "torrent",
            user_tasks.c.display_name == synthetic_torrent_name,
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


async def find_live_global_download_by_resource_key(
    resource_key: str,
) -> dict[str, Any] | None:
    async with transaction() as conn:
        row = (
            (
                await conn.execute(
                    select(global_downloads).where(
                        global_downloads.c.resource_key == resource_key,
                        global_downloads.c.status.in_(ACTIVE_GLOBAL_DOWNLOAD_STATUSES),
                    )
                )
            )
            .mappings()
            .first()
        )
    return dict(row) if row else None


async def find_latest_completed_global_download_by_resource_key(
    resource_key: str,
) -> dict[str, Any] | None:
    async with transaction() as conn:
        row = (
            (
                await conn.execute(
                    select(global_downloads)
                    .where(
                        global_downloads.c.resource_key == resource_key,
                        global_downloads.c.status == "completed",
                        global_downloads.c.completed_file_id.is_not(None),
                    )
                    .order_by(global_downloads.c.id.desc())
                    .limit(1)
                )
            )
            .mappings()
            .first()
        )
    return dict(row) if row else None


async def get_global_download_by_id(download_id: int) -> dict[str, Any] | None:
    async with transaction() as conn:
        row = (
            (
                await conn.execute(
                    select(global_downloads).where(global_downloads.c.id == download_id)
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


async def list_active_global_downloads() -> list[dict[str, Any]]:
    async with transaction() as conn:
        rows = (
            await conn.execute(
                select(global_downloads).where(
                    global_downloads.c.status.in_(ACTIVE_GLOBAL_DOWNLOAD_STATUSES)
                )
            )
        ).mappings().all()
    return [dict(row) for row in rows]


async def list_active_like_http_downloads() -> list[dict[str, Any]]:
    async with transaction() as conn:
        rows = (
            await conn.execute(
                select(global_downloads).where(
                    global_downloads.c.resource_kind == "http",
                    global_downloads.c.status.in_(ACTIVE_GLOBAL_DOWNLOAD_STATUSES),
                )
            )
        ).mappings().all()
    return [dict(row) for row in rows]


async def list_tracked_global_downloads(
    statuses: Iterable[str],
) -> list[dict[str, Any]]:
    async with transaction() as conn:
        rows = (
            (
                await conn.execute(
                    select(global_downloads).where(
                        global_downloads.c.aria2_gid.is_not(None),
                        global_downloads.c.status.in_(tuple(statuses)),
                    )
                )
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


async def get_global_download_status_snapshot(
    download_id: int,
) -> dict[str, Any] | None:
    async with transaction() as conn:
        row = (
            (
                await conn.execute(
                    select(
                        global_downloads.c.status,
                        global_downloads.c.aria2_gid,
                        global_downloads.c.completed_file_id,
                        global_downloads.c.completed_bytes,
                        global_downloads.c.total_bytes,
                        global_downloads.c.error_code,
                        global_downloads.c.error_message,
                    ).where(global_downloads.c.id == download_id)
                )
            )
            .mappings()
            .first()
        )
    return dict(row) if row else None


async def get_global_download_for_generation(
    download_id: int, expected_gid: str
) -> dict[str, Any] | None:
    async with transaction() as conn:
        row = (
            (
                await conn.execute(
                    select(global_downloads).where(
                        global_downloads.c.id == download_id,
                        global_downloads.c.aria2_gid == expected_gid,
                        global_downloads.c.status.in_(ACTIVE_GLOBAL_DOWNLOAD_STATUSES),
                        global_downloads.c.completed_file_id.is_(None),
                    )
                )
            )
            .mappings()
            .first()
        )
    return dict(row) if row else None


async def list_inconsistent_completed_download_ids(
    threshold_ms: int,
) -> list[dict[str, Any]]:
    async with transaction() as conn:
        rows = (
            (
                await conn.execute(
                    select(
                        global_downloads.c.id,
                        global_downloads.c.status,
                        global_downloads.c.aria2_gid,
                    ).where(
                        global_downloads.c.status == "completed",
                        global_downloads.c.completed_file_id.is_(None),
                        global_downloads.c.updated_at_ms < threshold_ms,
                    )
                )
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


async def list_completed_downloads_without_file() -> list[dict[str, Any]]:
    async with transaction() as conn:
        rows = (
            (
                await conn.execute(
                    select(global_downloads).where(
                        global_downloads.c.status == "completed",
                        global_downloads.c.completed_file_id.is_(None),
                    )
                )
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


async def list_stale_queued_download_ids(threshold_ms: int) -> list[int]:
    async with transaction() as conn:
        rows = (
            await conn.execute(
                select(global_downloads.c.id).where(
                    global_downloads.c.status == "queued",
                    global_downloads.c.aria2_gid.is_(None),
                    global_downloads.c.updated_at_ms < threshold_ms,
                )
            )
        ).all()
    return [int(row[0]) for row in rows]


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
    try:
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
    except IntegrityError as exc:
        raise RepositoryConflictError(str(exc)) from exc
    return dict(row)


async def create_global_download_attempt(values: dict[str, Any]) -> dict[str, Any]:
    """Create a fresh download attempt; live resource uniqueness is DB-enforced."""
    return await create_global_download(values)


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
        global_downloads.c.bt_info_hash,
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
        user_tasks.c.global_download_id == global_download_id,
        _active_task_user(),
    )
    if statuses is not None:
        query = query.where(user_tasks.c.status.in_(tuple(statuses)))
    query = query.order_by(user_tasks.c.updated_at_ms.desc(), user_tasks.c.id.desc())

    async with transaction() as conn:
        rows = (await conn.execute(query)).mappings().all()
    return [dict(row) for row in rows]


async def _resize_active_task(
    conn: Any,
    task: Mapping[str, Any],
    *,
    target_bytes: int,
    timestamp: int,
) -> bool:
    current = int(task["reserved_bytes"] or 0)
    delta = target_bytes - current
    quota_row = (
        await conn.execute(
            select(users.c.quota_bytes).where(
                users.c.id == task["user_id"],
                users.c.pending_delete == 0,
            )
        )
    ).first()
    if quota_row is None:
        return False
    if not await _strict_adjust_usage_reserved(
        conn,
        user_id=int(task["user_id"]),
        delta=delta,
        quota_bytes=int(quota_row[0]),
        timestamp=timestamp,
    ):
        return False
    await conn.execute(
        update(user_tasks)
        .where(
            user_tasks.c.id == task["id"],
            user_tasks.c.status.in_(ACTIVE_USER_TASK_STATUSES),
        )
        .values(reserved_bytes=target_bytes, updated_at_ms=timestamp)
    )
    return True


async def _disk_resize_fits(
    conn: Any,
    download: Mapping[str, Any],
    *,
    target_bytes: int,
    disk_available_bytes: DiskAvailable,
    target_completed_bytes: int | None = None,
) -> bool:
    completed = int(download["completed_bytes"] or 0)
    target_completed = (
        completed
        if target_completed_bytes is None
        else max(completed, target_completed_bytes)
    )
    current = _remaining_disk_bytes(
        int(download["disk_reserved_bytes"] or 0), completed
    )
    target = _remaining_disk_bytes(target_bytes, target_completed)
    growth = target - current
    if growth <= 0:
        return True
    available = (
        disk_available_bytes()
        if callable(disk_available_bytes)
        else disk_available_bytes
    )
    return await active_physical_commitment_bytes(conn) + growth <= max(0, available)


async def reset_active_accounting_for_startup() -> None:
    timestamp = now_ms()
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
            update(global_downloads)
            .where(global_downloads.c.status.in_(ACTIVE_GLOBAL_DOWNLOAD_STATUSES))
            .values(disk_reserved_bytes=0, updated_at_ms=timestamp)
        )
        await conn.execute(
            update(user_tasks)
            .where(user_tasks.c.status.in_(ACTIVE_USER_TASK_STATUSES))
            .values(reserved_bytes=0, updated_at_ms=timestamp)
        )
        await conn.execute(
            update(user_storage_usage).values(
                used_bytes=used_subquery,
                reserved_bytes=pack_subquery,
                updated_at_ms=timestamp,
            )
        )


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


async def admit_user_task(
    *,
    user_id: int,
    global_download_id: int,
    expected_gid: str | None,
    display_name: str | None,
    total_bytes: int,
    size_known: bool,
    size_limit_bytes: int,
    disk_available_bytes: DiskAvailable,
) -> dict[str, Any]:
    timestamp = now_ms()
    admission_error: str | None = None
    async with transaction() as conn:
        download = await _lock_active_download(
            conn, global_download_id, expected_gid=expected_gid
        )
        if download is None:
            raise DownloadAdmissionError("stale")

        limit = int(download["size_limit_bytes"] or size_limit_bytes)
        known = bool(download["size_known"]) or size_known
        target = (
            int(download["total_bytes"] or 0)
            if bool(download["size_known"])
            else max(0, total_bytes)
        )

        task = (
            await conn.execute(
                select(user_tasks).where(
                    user_tasks.c.user_id == user_id,
                    user_tasks.c.global_download_id == global_download_id,
                )
            )
        ).mappings().first()
        if task is not None and task["status"] not in {"failed", "cancelled"}:
            raise RepositoryConflictError("duplicate task")

        quota_row = (
            await conn.execute(
                select(users.c.quota_bytes).where(
                    users.c.id == user_id,
                    users.c.pending_delete == 0,
                )
            )
        ).first()
        if quota_row is None:
            raise DownloadAdmissionError("user_missing")
        current_reserved = int(task["reserved_bytes"] or 0) if task else 0
        if not known and not await _strict_adjust_usage_reserved(
            conn,
            user_id=user_id,
            delta=-current_reserved,
            quota_bytes=int(quota_row[0]),
            timestamp=timestamp,
        ):
            raise DownloadAdmissionError("quota")

        task_status = (
            str(download["status"])
            if download["status"] in {"active", "waiting", "paused"}
            else "queued"
        )
        values = {
            "status": task_status,
            "reserved_bytes": current_reserved if known else 0,
            "display_name": display_name,
            "error_message": None,
            "updated_at_ms": timestamp,
            "finished_at_ms": None,
        }
        if task is None:
            row = (
                await conn.execute(
                    insert(user_tasks)
                    .values(
                        user_id=user_id,
                        global_download_id=global_download_id,
                        created_at_ms=timestamp,
                        **values,
                    )
                    .returning(user_tasks)
                )
            ).mappings().one()
        else:
            row = (
                await conn.execute(
                    update(user_tasks)
                    .where(user_tasks.c.id == task["id"])
                    .values(**values)
                    .returning(user_tasks)
                )
            ).mappings().one()

        if known:
            result = await _reconcile_download_size_locked(
                conn,
                download,
                candidate=target,
                completed_bytes=int(download["completed_bytes"] or 0),
                size_limit_bytes=limit,
                disk_available_bytes=disk_available_bytes,
                timestamp=timestamp,
            )
            refreshed = (
                await conn.execute(
                    select(user_tasks).where(user_tasks.c.id == row["id"])
                )
            ).mappings().one()
            row = refreshed
            if str(row["status"]) == "failed" and user_id in result.get(
                "rejected_user_ids", []
            ):
                admission_error = "quota"
            elif not result.admitted:
                admission_error = str(result["outcome"])
        else:
            await conn.execute(
                update(global_downloads)
                .where(global_downloads.c.id == global_download_id)
                .values(size_limit_bytes=limit, updated_at_ms=timestamp)
            )
    if admission_error is not None:
        raise DownloadAdmissionError(admission_error)
    return dict(row)


async def _resize_subscribers(
    conn: Any,
    *,
    download_id: int,
    target_bytes: int,
    timestamp: int,
) -> tuple[int, list[int]]:
    tasks = (
        await conn.execute(
            select(user_tasks)
            .select_from(user_tasks.join(users, users.c.id == user_tasks.c.user_id))
            .where(
                user_tasks.c.global_download_id == download_id,
                user_tasks.c.status.in_(ACTIVE_USER_TASK_STATUSES),
                users.c.pending_delete == 0,
            )
        )
    ).mappings().all()
    admitted = 0
    rejected_users: list[int] = []
    for task in tasks:
        if await _resize_active_task(
            conn, task, target_bytes=target_bytes, timestamp=timestamp
        ):
            admitted += 1
            continue
        rejected_users.append(int(task["user_id"]))
        await _fail_active_task_row(
            conn,
            task,
            message="空间不足，已取消该订阅任务",
            timestamp=timestamp,
        )
    return admitted, rejected_users


async def _cancel_download_without_subscribers(
    conn: Any,
    download_id: int,
    *,
    timestamp: int,
) -> None:
    await conn.execute(
        update(global_downloads)
        .where(global_downloads.c.id == download_id)
        .values(
            status="cancelled",
            disk_reserved_bytes=0,
            error_code="no_eligible_subscribers",
            error_message="没有满足配额要求的订阅用户",
            updated_at_ms=timestamp,
        )
    )


async def _reconcile_download_size_locked(
    conn: Any,
    download: Mapping[str, Any],
    *,
    candidate: int,
    completed_bytes: int,
    size_limit_bytes: int,
    disk_available_bytes: DiskAvailable,
    timestamp: int,
) -> SizeReconcileResult:
    download_id = int(download["id"])
    limit = int(download["size_limit_bytes"] or size_limit_bytes)
    if candidate > limit:
        await _fail_download_rows(
            conn, download, message="任务大小超过系统限制",
            error_code="max_task_size_exceeded", timestamp=timestamp,
        )
        return SizeReconcileResult(outcome="max_task_size", rejected_user_ids=[])

    admitted, rejected = await _resize_subscribers(
        conn, download_id=download_id, target_bytes=candidate, timestamp=timestamp
    )
    if admitted == 0:
        await _cancel_download_without_subscribers(
            conn, download_id, timestamp=timestamp
        )
        return SizeReconcileResult(
            outcome="no_subscribers", rejected_user_ids=rejected
        )
    if not await _disk_resize_fits(
        conn, download, target_bytes=candidate,
        disk_available_bytes=disk_available_bytes,
        target_completed_bytes=completed_bytes,
    ):
        await _fail_download_rows(
            conn, download, message="磁盘可用空间不足",
            error_code="disk_budget_exceeded", timestamp=timestamp,
        )
        return SizeReconcileResult(
            outcome="disk_budget", rejected_user_ids=rejected
        )

    await conn.execute(
        update(global_downloads)
        .where(global_downloads.c.id == download_id)
        .values(
            total_bytes=candidate,
            completed_bytes=max(0, completed_bytes),
            size_known=1,
            size_limit_bytes=limit,
            disk_reserved_bytes=candidate,
            updated_at_ms=timestamp,
        )
    )
    return SizeReconcileResult(
        outcome="admitted", rejected_user_ids=rejected, size_bytes=candidate
    )


async def reconcile_download_size(
    *,
    download_id: int,
    expected_gid: str | None,
    candidate_bytes: int,
    completed_bytes: int,
    size_limit_bytes: int,
    disk_available_bytes: DiskAvailable,
) -> SizeReconcileResult:
    candidate = max(0, candidate_bytes, completed_bytes)
    timestamp = now_ms()
    async with transaction() as conn:
        download = await _lock_active_download(
            conn, download_id, expected_gid=expected_gid
        )
        if download is None:
            return SizeReconcileResult(outcome="stale", rejected_user_ids=[])
        return await _reconcile_download_size_locked(
            conn,
            download,
            candidate=candidate,
            completed_bytes=completed_bytes,
            size_limit_bytes=size_limit_bytes,
            disk_available_bytes=disk_available_bytes,
            timestamp=timestamp,
        )


async def assign_submitted_gid(
    *,
    download_id: int,
    gid: str,
    status: str,
) -> dict[str, Any] | None:
    timestamp = now_ms()
    async with transaction() as conn:
        download = await _lock_active_download(
            conn, download_id, expected_gid=None
        )
        if download is None or download["status"] != "queued":
            return None
        row = (
            await conn.execute(
                update(global_downloads)
                .where(
                    global_downloads.c.id == download_id,
                    global_downloads.c.aria2_gid.is_(None),
                    global_downloads.c.status == "queued",
                    global_downloads.c.completed_file_id.is_(None),
                )
                .values(
                    aria2_gid=gid,
                    status=status,
                    updated_at_ms=timestamp,
                )
                .returning(global_downloads)
            )
        ).mappings().first()
        if row is None:
            return None
        await conn.execute(
            update(user_tasks)
            .where(
                user_tasks.c.global_download_id == download_id,
                user_tasks.c.status.in_(ACTIVE_USER_TASK_STATUSES),
            )
            .values(status=status, updated_at_ms=timestamp)
        )
    return dict(row)


async def claim_submitted_gid_for_failure(
    *,
    download_id: int,
    gid: str,
    message: str,
) -> dict[str, Any] | None:
    timestamp = now_ms()
    async with transaction() as conn:
        row = (
            await conn.execute(
                update(global_downloads)
                .where(
                    global_downloads.c.id == download_id,
                    global_downloads.c.aria2_gid.is_(None),
                    global_downloads.c.status == "queued",
                    global_downloads.c.completed_file_id.is_(None),
                )
                .values(
                    aria2_gid=gid,
                    status="failed",
                    disk_reserved_bytes=0,
                    error_code="submit_failed",
                    error_message=message,
                    updated_at_ms=timestamp,
                )
                .returning(global_downloads)
            )
        ).mappings().first()
        if row is None:
            return None
        tasks = (
            await conn.execute(
                select(user_tasks).where(
                    user_tasks.c.global_download_id == download_id,
                    user_tasks.c.status.in_(ACTIVE_USER_TASK_STATUSES),
                )
            )
        ).mappings().all()
        for task in tasks:
            await _fail_active_task_row(
                conn, task, message=message, timestamp=timestamp
            )
    return dict(row)


async def fail_user_task_submission(
    *,
    task_id: int,
    global_download_id: int,
    message: str,
) -> None:
    timestamp = now_ms()
    async with transaction() as conn:
        await conn.execute(
            update(global_downloads)
            .where(global_downloads.c.id == global_download_id)
            .values(updated_at_ms=global_downloads.c.updated_at_ms)
        )
        task = (
            await conn.execute(
                select(user_tasks).where(
                    user_tasks.c.id == task_id,
                    user_tasks.c.status.in_(ACTIVE_USER_TASK_STATUSES),
                )
            )
        ).mappings().first()
        if task is None:
            return
        await _fail_active_task_row(
            conn, task, message=message, timestamp=timestamp
        )
        remaining = (
            await conn.execute(
                select(func.count()).select_from(user_tasks).where(
                    user_tasks.c.global_download_id == global_download_id,
                    user_tasks.c.status.in_(ACTIVE_USER_TASK_STATUSES),
                )
            )
        ).scalar_one()
        if int(remaining) == 0:
            await conn.execute(
                update(global_downloads)
                .where(
                    global_downloads.c.id == global_download_id,
                    global_downloads.c.status.in_(ACTIVE_GLOBAL_DOWNLOAD_STATUSES),
                )
                .values(
                    status="failed",
                    disk_reserved_bytes=0,
                    error_code="submit_failed",
                    error_message=message,
                    updated_at_ms=timestamp,
                )
            )


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


@overload
async def guarded_update_global_download(
    download_id: int,
    values: dict[str, Any],
    *,
    expected_gid: str,
    return_row: Literal[False] = False,
) -> bool: ...


@overload
async def guarded_update_global_download(
    download_id: int,
    values: dict[str, Any],
    *,
    expected_gid: str,
    return_row: Literal[True],
) -> dict[str, Any] | None: ...


async def guarded_update_global_download(
    download_id: int,
    values: dict[str, Any],
    *,
    expected_gid: str,
    return_row: bool = False,
) -> dict[str, Any] | bool | None:
    if not values:
        return None if return_row else False

    row_values = {**values}
    row_values.setdefault("updated_at_ms", now_ms())
    stmt = (
        update(global_downloads)
        .where(
            global_downloads.c.id == download_id,
            global_downloads.c.aria2_gid == expected_gid,
            global_downloads.c.status.in_(ACTIVE_GLOBAL_DOWNLOAD_STATUSES),
            global_downloads.c.completed_file_id.is_(None),
        )
        .values(**row_values)
    )
    if return_row:
        stmt = stmt.returning(global_downloads)
    else:
        stmt = stmt.returning(global_downloads.c.id)

    async with transaction() as conn:
        row = (await conn.execute(stmt)).mappings().first()

    if return_row:
        return dict(row) if row else None
    return row is not None


async def guarded_update_download_and_active_user_tasks(
    download_id: int,
    global_values: dict[str, Any],
    *,
    expected_gid: str,
    user_status: str | None = None,
    display_name: str | None = None,
    force_display_name: bool = False,
) -> dict[str, Any] | None:
    if not global_values:
        return None

    timestamp = now_ms()
    row_values = {**global_values}
    row_values.setdefault("updated_at_ms", timestamp)
    async with transaction() as conn:
        row = (
            (
                await conn.execute(
                    update(global_downloads)
                    .where(
                        global_downloads.c.id == download_id,
                        global_downloads.c.aria2_gid == expected_gid,
                        global_downloads.c.status.in_(
                            ACTIVE_GLOBAL_DOWNLOAD_STATUSES
                        ),
                        global_downloads.c.completed_file_id.is_(None),
                    )
                    .values(**row_values)
                    .returning(global_downloads)
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            return None

        base_condition = [
            user_tasks.c.global_download_id == download_id,
            user_tasks.c.status.in_(ACTIVE_USER_TASK_STATUSES),
        ]
        user_values: dict[str, Any] = {"updated_at_ms": timestamp}
        if user_status is not None:
            user_values["status"] = user_status
        if "error_message" in global_values:
            user_values["error_message"] = global_values.get("error_message")
        if len(user_values) > 1:
            await conn.execute(
                update(user_tasks)
                .where(*base_condition)
                .values(**user_values)
            )
        if display_name:
            name_update = update(user_tasks).where(*base_condition)
            if not force_display_name:
                name_update = name_update.where(
                    refreshable_user_task_display_name_condition()
                )
            await conn.execute(
                name_update.values(
                    display_name=display_name,
                    updated_at_ms=timestamp,
                )
            )
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


async def _complete_user_task_with_file(
    conn: Any,
    task: Any,
    *,
    stored_file_id: int,
    size_bytes: int,
    original_name: str,
    completed_at_ms: int,
    timestamp: int,
) -> bool:
    reserved = int(task["reserved_bytes"] or 0)
    if reserved != size_bytes:
        raise RepositoryConflictError("task reservation does not match actual size")
    user_id = int(task["user_id"])
    existing = (
        await conn.execute(
            select(user_files.c.id).where(
                user_files.c.user_id == user_id,
                user_files.c.stored_file_id == stored_file_id,
            )
        )
    ).first()
    values: dict[str, Any] = {
        "reserved_bytes": user_storage_usage.c.reserved_bytes - reserved,
        "updated_at_ms": timestamp,
    }
    if existing is None:
        values["used_bytes"] = user_storage_usage.c.used_bytes + size_bytes
    usage = (
        await conn.execute(
            update(user_storage_usage)
            .where(
                user_storage_usage.c.user_id == user_id,
                user_storage_usage.c.reserved_bytes >= reserved,
                user_storage_usage.c.used_bytes
                + user_storage_usage.c.reserved_bytes
                <= select(users.c.quota_bytes)
                .where(
                    users.c.id == user_id,
                    users.c.pending_delete == 0,
                )
                .scalar_subquery(),
            )
            .values(**values)
            .returning(user_storage_usage.c.user_id)
        )
    ).first()
    if usage is None:
        raise RepositoryConflictError("usage drift during completion")
    display_name = str(task["display_name"] or original_name)
    if existing is None:
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
        update(user_tasks)
        .where(
            user_tasks.c.id == task["id"],
            user_tasks.c.status.in_(ACTIVE_USER_TASK_STATUSES),
        )
        .values(
            status="completed",
            reserved_bytes=0,
            error_message=None,
            updated_at_ms=timestamp,
            finished_at_ms=completed_at_ms,
        )
    )
    return existing is None


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


async def complete_attempt(
    *,
    attempt_id: int,
    expected_gid: str,
    stored_file_id: int,
    size_bytes: int,
    original_name: str,
    completed_at_ms: int,
) -> dict[str, Any] | None:
    """Conditionally transition a live attempt to completed (spec §11.2, §16.1).

    CAS requires: ``id = attempt_id``, ``aria2_gid = expected_gid``,
    ``status ∈ active``, ``completed_file_id IS NULL``, an active subscriber
    exists, and the stored file is not pending delete.

    On success: sets ``status = completed``, writes ``completed_file_id``,
    clears ``aria2_gid``, completes all active user_tasks with the stored
    file, and returns the updated ``global_downloads`` row.
    Returns ``None`` when the CAS does not match.
    """
    timestamp = now_ms()
    active_subscriber = exists(
        select(user_tasks.c.id)
        .select_from(
            user_tasks.join(users, users.c.id == user_tasks.c.user_id)
        )
        .where(
            user_tasks.c.global_download_id == attempt_id,
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
        row = (
            (
                await conn.execute(
                    update(global_downloads)
                    .where(
                        global_downloads.c.id == attempt_id,
                        global_downloads.c.aria2_gid == expected_gid,
                        global_downloads.c.status.in_(
                            ACTIVE_GLOBAL_DOWNLOAD_STATUSES
                        ),
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
                    .returning(global_downloads)
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            return None

        tasks = (
            (
                await conn.execute(
                    select(user_tasks)
                    .select_from(
                        user_tasks.join(
                            users, users.c.id == user_tasks.c.user_id
                        )
                    )
                    .where(
                        user_tasks.c.global_download_id == attempt_id,
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
    return dict(row)


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


async def replace_terminal_download_gid(
    download_id: int,
    *,
    expected_gid: str,
    residual_gid: str,
) -> bool:
    async with transaction() as conn:
        row = (
            await conn.execute(
                update(global_downloads)
                .where(
                    global_downloads.c.id == download_id,
                    global_downloads.c.aria2_gid == expected_gid,
                    global_downloads.c.status.in_(("failed", "cancelled")),
                    global_downloads.c.completed_file_id.is_(None),
                )
                .values(aria2_gid=residual_gid, updated_at_ms=now_ms())
                .returning(global_downloads.c.id)
            )
        ).first()
    return row is not None


async def clear_terminal_download_gid(
    download_id: int,
    *,
    expected_gid: str | None = None,
) -> bool:
    async with transaction() as conn:
        conditions = [
            global_downloads.c.id == download_id,
            global_downloads.c.status.in_(("failed", "cancelled")),
            global_downloads.c.completed_file_id.is_(None),
            global_downloads.c.aria2_gid.is_not(None),
        ]
        if expected_gid is not None:
            conditions.append(global_downloads.c.aria2_gid == expected_gid)
        row = (
            await conn.execute(
                update(global_downloads)
                .where(*conditions)
                .values(
                    aria2_gid=None,
                    disk_reserved_bytes=0,
                    updated_at_ms=now_ms(),
                )
                .returning(global_downloads.c.id)
            )
        ).first()
    return row is not None


async def list_terminal_downloads_with_residual_gid() -> list[dict[str, Any]]:
    async with transaction() as conn:
        rows = (
            await conn.execute(
                select(global_downloads).where(
                    global_downloads.c.status.in_(("failed", "cancelled")),
                    global_downloads.c.aria2_gid.is_not(None),
                    global_downloads.c.completed_file_id.is_(None),
                )
            )
        ).mappings().all()
    return [dict(row) for row in rows]


async def list_terminal_download_ids() -> list[int]:
    """Return terminal download ids whose downloading dir is safe to reclaim.

    - failed / cancelled: always reclaimable
    - completed: only when already indexed into store (completed_file_id set)
      Incomplete completed rows may still hold the only copy under downloading/.
    """
    async with transaction() as conn:
        rows = (
            await conn.execute(
                select(global_downloads.c.id).where(
                    global_downloads.c.status.in_(("failed", "cancelled"))
                    | (
                        (global_downloads.c.status == "completed")
                        & global_downloads.c.completed_file_id.is_not(None)
                    )
                )
            )
        ).all()
    return [int(row[0]) for row in rows]


async def list_completed_downloads_pending_index() -> list[dict[str, Any]]:
    """Completed rows that never received a stored_file index."""
    async with transaction() as conn:
        rows = (
            await conn.execute(
                select(global_downloads).where(
                    global_downloads.c.status == "completed",
                    global_downloads.c.completed_file_id.is_(None),
                )
            )
        ).mappings().all()
    return [dict(row) for row in rows]


async def reopen_completed_download_for_index_repair(
    download_id: int,
    *,
    recovery_gid: str | None = None,
) -> dict[str, Any] | None:
    """Move incomplete completed row back to active so completion can re-run."""
    values: dict[str, Any] = {
        "status": "active",
        "error_code": None,
        "error_message": None,
        "completed_at_ms": None,
        "updated_at_ms": now_ms(),
    }
    if recovery_gid is not None:
        values["aria2_gid"] = recovery_gid
    async with transaction() as conn:
        row = (
            await conn.execute(
                update(global_downloads)
                .where(
                    global_downloads.c.id == download_id,
                    global_downloads.c.status == "completed",
                    global_downloads.c.completed_file_id.is_(None),
                )
                .values(**values)
                .returning(global_downloads)
            )
        ).mappings().first()
        if row is None:
            return None
        await conn.execute(
            update(user_tasks)
            .where(
                user_tasks.c.global_download_id == download_id,
                user_tasks.c.status.in_(TERMINAL_USER_TASK_STATUSES),
            )
            .values(
                status="active",
                error_message=None,
                finished_at_ms=None,
                updated_at_ms=now_ms(),
            )
        )
    return dict(row)


async def restore_incomplete_completed_download(
    download_id: int,
    *,
    aria2_gid: str | None,
) -> dict[str, Any] | None:
    """Undo reopen when pending-index recovery cannot finish safely.

    Leaves the row completed without completed_file_id so purge keeps the
    downloading dir (it may still hold the only payload copy).
    """
    timestamp = now_ms()
    async with transaction() as conn:
        row = (
            await conn.execute(
                update(global_downloads)
                .where(
                    global_downloads.c.id == download_id,
                    global_downloads.c.status.in_(ACTIVE_GLOBAL_DOWNLOAD_STATUSES),
                    global_downloads.c.completed_file_id.is_(None),
                )
                .values(
                    status="completed",
                    aria2_gid=aria2_gid,
                    disk_reserved_bytes=0,
                    error_code=None,
                    error_message=None,
                    completed_at_ms=timestamp,
                    updated_at_ms=timestamp,
                )
                .returning(global_downloads)
            )
        ).mappings().first()
        if row is None:
            return None
        await conn.execute(
            update(user_tasks)
            .where(
                user_tasks.c.global_download_id == download_id,
                user_tasks.c.status.in_(ACTIVE_USER_TASK_STATUSES),
            )
            .values(
                status="completed",
                error_message=None,
                finished_at_ms=timestamp,
                updated_at_ms=timestamp,
            )
        )
    return dict(row)


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


async def claim_attempt_terminal(
    *,
    attempt_id: int,
    expected_gid: str | None,
    terminal_status: str,
    error_code: str | None,
    error_message: str | None,
    expected_statuses: Sequence[str],
    writer_gids: Sequence[str] | None = None,
    result_gids: Sequence[str] | None = None,
) -> TerminalizationClaim | None:
    """Conditionally transition an attempt to a terminal state (spec §10.2).

    Returns a ``TerminalizationClaim`` on success or ``None`` when the CAS
    does not match (stale GID, already terminal, completed_file_id set).
    Does not clear ``aria2_gid`` — it is preserved for residual cleanup fencing.
    """
    timestamp = now_ms()
    gid_condition = (
        global_downloads.c.aria2_gid.is_(None)
        if expected_gid is None
        else global_downloads.c.aria2_gid == expected_gid
    )
    async with transaction() as conn:
        row = (
            (
                await conn.execute(
                    update(global_downloads)
                    .where(
                        global_downloads.c.id == attempt_id,
                        gid_condition,
                        global_downloads.c.status.in_(
                            tuple(expected_statuses)
                        ),
                        global_downloads.c.completed_file_id.is_(None),
                    )
                    .values(
                        status=terminal_status,
                        disk_reserved_bytes=0,
                        error_code=error_code,
                        error_message=error_message,
                        updated_at_ms=timestamp,
                    )
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
                        user_tasks.c.global_download_id == attempt_id,
                        user_tasks.c.status.in_(ACTIVE_USER_TASK_STATUSES),
                    )
                )
            )
            .mappings()
            .all()
        )
        for task in active_tasks:
            await _terminate_active_task_row(
                conn,
                task,
                terminal_status=terminal_status,
                message=error_message or "",
                timestamp=timestamp,
            )

    if writer_gids is not None:
        w_gids = tuple(writer_gids)
    else:
        w_gids = (expected_gid,) if expected_gid is not None else ()
    if result_gids is not None:
        r_gids = tuple(result_gids)
    else:
        r_gids = (expected_gid,) if expected_gid is not None else ()
    return make_terminalization_claim(
        attempt_id=attempt_id,
        expected_current_gid=expected_gid,
        writer_gids=w_gids,
        result_gids=r_gids,
        terminal_status=terminal_status,
        claim_timestamp=timestamp,
        error_code=error_code,
        error_message=error_message,
    )


async def claim_terminal_reclaim(
    *,
    attempt_id: int,
    expected_gid: str | None,
) -> RepairClaim | None:
    """CAS-confirm an already-terminal attempt for physical reclaim (§10.4).

    Does not change business terminal state.  Returns a ``RepairClaim`` when
    the attempt is still ``failed``/``cancelled`` with an unchanged GID, or
    ``None`` otherwise.
    """
    timestamp = now_ms()
    gid_condition = (
        global_downloads.c.aria2_gid.is_(None)
        if expected_gid is None
        else global_downloads.c.aria2_gid == expected_gid
    )
    async with transaction() as conn:
        row = (
            (
                await conn.execute(
                    select(
                        global_downloads.c.id,
                        global_downloads.c.status,
                        global_downloads.c.aria2_gid,
                    ).where(
                        global_downloads.c.id == attempt_id,
                        gid_condition,
                        global_downloads.c.status.in_(
                            ("failed", "cancelled")
                        ),
                        global_downloads.c.completed_file_id.is_(None),
                    )
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            return None

    repair_gids = (
        (expected_gid,) if expected_gid is not None else ()
    )
    return make_repair_claim(
        attempt_id=attempt_id,
        expected_current_gid=expected_gid,
        writer_gids=repair_gids,
        result_gids=repair_gids,
        terminal_status=str(row["status"]),
        claim_timestamp=timestamp,
    )
