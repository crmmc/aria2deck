from __future__ import annotations

import json
import time
from typing import Any

from sqlalchemy import and_, delete, exists, func, insert, select, update
from sqlalchemy.exc import IntegrityError

from app.db.engine import transaction
from app.db.schema import (
    pack_task_sources,
    pack_tasks,
    stored_file_entries,
    stored_files,
    user_files,
    user_storage_usage,
    users,
)
from app.domain.error_text import fmt_gb
from app.domain.pack import PACK_ACTIVE_STATUSES, PACK_TERMINAL_STATUSES
from app.repositories.task.downloads import active_physical_commitment_bytes
from app.repositories.errors import RepositoryConflictError
from app.domain.content_identity import content_identity_from_content_hash


class PackAdmissionError(ValueError):
    def __init__(self, reason: str, message: str | None = None):
        self.reason = reason
        self.message = message
        super().__init__(reason)


def now_ms() -> int:
    return int(time.time() * 1000)


def _active_pack_user() -> Any:
    return exists(
        select(users.c.id).where(
            users.c.id == pack_tasks.c.user_id,
            users.c.pending_delete == 0,
        )
    )


async def _release_reservation_locked(
    conn: Any,
    task: dict[str, Any],
    *,
    timestamp: int,
) -> None:
    reserved = int(task["reserved_bytes"] or 0)
    if reserved == 0:
        return
    released = (
        await conn.execute(
            update(user_storage_usage)
            .where(
                user_storage_usage.c.user_id == task["user_id"],
                user_storage_usage.c.reserved_bytes >= reserved,
            )
            .values(
                reserved_bytes=user_storage_usage.c.reserved_bytes - reserved,
                updated_at_ms=timestamp,
            )
            .returning(user_storage_usage.c.user_id)
        )
    ).first()
    if released is None:
        raise RepositoryConflictError("pack reservation drift")


async def create_pending_pack_with_reservation(
    *,
    user_id: int,
    source_user_file_ids_json: str,
    source_size_bytes: int,
    reserved_bytes: int,
    output_name: str | None,
    delete_source: bool,
    disk_available_bytes: int,
) -> dict[str, Any]:
    if source_size_bytes <= 0 or reserved_bytes <= 0:
        raise PackAdmissionError("source")
    try:
        values = json.loads(source_user_file_ids_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise PackAdmissionError("source") from exc
    if (
        not isinstance(values, list)
        or not values
        or any(
            not isinstance(value, int) or isinstance(value, bool) or value <= 0
            for value in values
        )
        or len(values) != len(set(values))
    ):
        raise PackAdmissionError("source")
    source_ids = sorted(values)
    source_user_file_ids_json = json.dumps(source_ids, separators=(",", ":"))
    timestamp = now_ms()
    try:
        async with transaction() as conn:
            completed = (
                await conn.execute(
                    select(pack_tasks.c.id)
                    .select_from(
                        pack_tasks.join(
                            user_files,
                            (user_files.c.user_id == pack_tasks.c.user_id)
                            & (
                                user_files.c.stored_file_id
                                == pack_tasks.c.output_stored_file_id
                            ),
                        )
                    )
                    .where(
                        pack_tasks.c.user_id == user_id,
                        pack_tasks.c.source_user_file_ids_json
                        == source_user_file_ids_json,
                        pack_tasks.c.status == "completed",
                        pack_tasks.c.output_stored_file_id.is_not(None),
                    )
                )
            ).first()
            if completed is not None:
                raise PackAdmissionError("completed")
            quota = (
                await conn.execute(
                    select(users.c.quota_bytes).where(
                        users.c.id == user_id,
                        users.c.pending_delete == 0,
                    )
                )
            ).scalar_one_or_none()
            if quota is None:
                raise PackAdmissionError("user_missing")
            usage = (
                await conn.execute(
                    update(user_storage_usage)
                    .where(
                        user_storage_usage.c.user_id == user_id,
                        user_storage_usage.c.used_bytes
                        + user_storage_usage.c.reserved_bytes
                        + reserved_bytes
                        <= int(quota),
                    )
                    .values(
                        reserved_bytes=(
                            user_storage_usage.c.reserved_bytes + reserved_bytes
                        ),
                        updated_at_ms=timestamp,
                    )
                    .returning(user_storage_usage.c.user_id)
                )
            ).first()
            if usage is None:
                used_reserved = (
                    await conn.execute(
                        select(
                            func.coalesce(user_storage_usage.c.used_bytes, 0)
                            + func.coalesce(user_storage_usage.c.reserved_bytes, 0)
                        ).where(user_storage_usage.c.user_id == user_id)
                    )
                ).scalar()
                available = max(0, int(quota) - int(used_reserved or 0))
                raise PackAdmissionError(
                    "quota",
                    f"打包需冻结 {fmt_gb(reserved_bytes)}，"
                    f"超过剩余配额 {fmt_gb(available)}",
                )
            source_rows = (
                await conn.execute(
                    select(
                        user_files.c.id,
                        user_files.c.stored_file_id,
                        user_files.c.created_at_ms,
                        stored_files.c.content_hash,
                    )
                    .select_from(
                        user_files.join(
                            stored_files,
                            stored_files.c.id == user_files.c.stored_file_id,
                        ).join(users, users.c.id == user_files.c.user_id)
                    )
                    .where(
                        user_files.c.user_id == user_id,
                        user_files.c.id.in_(source_ids),
                        stored_files.c.pending_delete == 0,
                        users.c.pending_delete == 0,
                    )
                )
            ).mappings().all()
            sources_by_id = {int(source["id"]): source for source in source_rows}
            if len(sources_by_id) != len(source_ids):
                raise PackAdmissionError("source")
            commitment = await active_physical_commitment_bytes(conn)
            if commitment + reserved_bytes > max(0, disk_available_bytes):
                raise PackAdmissionError(
                    "disk",
                    f"打包需 {fmt_gb(reserved_bytes)}，"
                    f"磁盘可用 {fmt_gb(max(0, disk_available_bytes - commitment))} 不足",
                )
            row = (
                await conn.execute(
                    insert(pack_tasks)
                    .values(
                        user_id=user_id,
                        source_user_file_ids_json=source_user_file_ids_json,
                        source_size_bytes=source_size_bytes,
                        reserved_bytes=reserved_bytes,
                        output_name=output_name,
                        delete_source=int(delete_source),
                        status="pending",
                        progress=0,
                        created_at_ms=timestamp,
                        updated_at_ms=timestamp,
                    )
                    .returning(pack_tasks)
                )
            ).mappings().one()
            await conn.execute(
                insert(pack_task_sources),
                [
                    {
                        "task_id": row["id"],
                        "ordinal": ordinal,
                        "original_user_file_id": source_id,
                        "stored_file_id": sources_by_id[source_id]["stored_file_id"],
                        "user_file_created_at_ms": sources_by_id[source_id]["created_at_ms"],
                        "content_hash": sources_by_id[source_id]["content_hash"],
                        "cleanup_state": "pending" if delete_source else "retained",
                    }
                    for ordinal, source_id in enumerate(source_ids)
                ],
            )
    except IntegrityError as exc:
        raise PackAdmissionError("duplicate") from exc
    return dict(row)


async def get_pack_task_row(task_id: int) -> dict[str, Any] | None:
    async with transaction() as conn:
        row = (
            await conn.execute(
                select(pack_tasks)
                .select_from(pack_tasks.join(users))
                .where(
                    pack_tasks.c.id == task_id,
                    users.c.pending_delete == 0,
                )
            )
        ).mappings().first()
    return dict(row) if row else None


async def get_pack_task_row_any(task_id: int) -> dict[str, Any] | None:
    async with transaction() as conn:
        row = (
            await conn.execute(select(pack_tasks).where(pack_tasks.c.id == task_id))
        ).mappings().first()
    return dict(row) if row else None


async def get_user_pack_task_row(
    user_id: int,
    task_id: int,
) -> dict[str, Any] | None:
    async with transaction() as conn:
        row = (
            (
                await conn.execute(
                    select(pack_tasks)
                    .select_from(pack_tasks.join(users))
                    .where(
                        pack_tasks.c.id == task_id,
                        pack_tasks.c.user_id == user_id,
                        users.c.pending_delete == 0,
                    )
                )
            )
            .mappings()
            .first()
        )
    return dict(row) if row else None


async def get_pack_output_user_file_id(task_id: int) -> int | None:
    async with transaction() as conn:
        row = (
            await conn.execute(
                select(user_files.c.id)
                .select_from(
                    pack_tasks.join(
                        user_files,
                        (user_files.c.user_id == pack_tasks.c.user_id)
                        & (
                            user_files.c.stored_file_id
                            == pack_tasks.c.output_stored_file_id
                        ),
                    )
                )
                .where(pack_tasks.c.id == task_id)
            )
        ).first()
    return int(row[0]) if row else None


async def list_pack_task_source_rows(task_id: int) -> list[dict[str, Any]]:
    async with transaction() as conn:
        rows = (
            await conn.execute(
                select(pack_task_sources)
                .where(pack_task_sources.c.task_id == task_id)
                .order_by(pack_task_sources.c.ordinal)
            )
        ).mappings().all()
    return [dict(row) for row in rows]


async def resolve_pack_task_source_rows(task_id: int) -> list[dict[str, Any]]:
    identity_match = and_(
        user_files.c.id == pack_task_sources.c.original_user_file_id,
        user_files.c.user_id == pack_tasks.c.user_id,
        user_files.c.stored_file_id == pack_task_sources.c.stored_file_id,
        user_files.c.created_at_ms == pack_task_sources.c.user_file_created_at_ms,
    )
    async with transaction() as conn:
        rows = (
            await conn.execute(
                select(
                    pack_task_sources,
                    user_files.c.display_name,
                    stored_files.c.real_path,
                    stored_files.c.size_bytes,
                )
                .select_from(
                    pack_task_sources.join(pack_tasks)
                    .join(users, users.c.id == pack_tasks.c.user_id)
                    .join(user_files, identity_match)
                    .join(
                        stored_files,
                        and_(
                            stored_files.c.id == pack_task_sources.c.stored_file_id,
                            stored_files.c.content_hash
                            == pack_task_sources.c.content_hash,
                        ),
                    )
                )
                .where(
                    pack_task_sources.c.task_id == task_id,
                    users.c.pending_delete == 0,
                    stored_files.c.pending_delete == 0,
                )
                .order_by(pack_task_sources.c.ordinal)
            )
        ).mappings().all()
    return [dict(row) for row in rows]


async def set_pack_materialized_bytes(task_id: int, size_bytes: int) -> None:
    async with transaction() as conn:
        await conn.execute(
            update(pack_tasks)
            .where(
                pack_tasks.c.id == task_id,
                pack_tasks.c.status.in_(PACK_ACTIVE_STATUSES),
                _active_pack_user(),
            )
            .values(materialized_bytes=max(0, size_bytes), updated_at_ms=now_ms())
        )


async def reserve_pack_install_bytes(
    task_id: int,
    size_bytes: int,
    disk_available_bytes: int,
) -> bool:
    if size_bytes <= 0:
        return True
    async with transaction() as conn:
        task = (
            await conn.execute(
                update(pack_tasks)
                .where(
                    pack_tasks.c.id == task_id,
                    pack_tasks.c.status == "packing",
                    pack_tasks.c.prepared_content_hash.is_not(None),
                    pack_tasks.c.install_reserved_bytes == 0,
                    _active_pack_user(),
                )
                .values(updated_at_ms=pack_tasks.c.updated_at_ms)
                .returning(pack_tasks.c.id)
            )
        ).first()
        if task is None:
            return False
        commitment = await active_physical_commitment_bytes(conn)
        if commitment + size_bytes > max(0, disk_available_bytes):
            return False
        reserved = (
            await conn.execute(
                update(pack_tasks)
                .where(
                    pack_tasks.c.id == task_id,
                    pack_tasks.c.status == "packing",
                    pack_tasks.c.install_reserved_bytes == 0,
                    _active_pack_user(),
                )
                .values(
                    install_reserved_bytes=size_bytes,
                    updated_at_ms=now_ms(),
                )
                .returning(pack_tasks.c.id)
            )
        ).first()
    return reserved is not None


async def clear_pack_install_reservation(task_id: int) -> None:
    async with transaction() as conn:
        await conn.execute(
            update(pack_tasks)
            .where(pack_tasks.c.id == task_id)
            .values(install_reserved_bytes=0, updated_at_ms=now_ms())
        )


async def schedule_pack_retry(
    task_id: int,
    *,
    retry_count: int,
    next_retry_at_ms: int,
) -> bool:
    eligible = (
        pack_tasks.c.status.in_(PACK_ACTIVE_STATUSES)
        | (
            (pack_tasks.c.status == "completed")
            & (pack_tasks.c.source_cleanup_pending == 1)
        )
    )
    async with transaction() as conn:
        row = (
            await conn.execute(
                update(pack_tasks)
                .where(
                    pack_tasks.c.id == task_id,
                    eligible,
                    _active_pack_user(),
                )
                .values(
                    retry_count=max(0, retry_count),
                    next_retry_at_ms=max(0, next_retry_at_ms),
                    updated_at_ms=now_ms(),
                )
                .returning(pack_tasks.c.id)
            )
        ).first()
    return row is not None


async def mark_pack_source_cleanup_error(
    task_id: int, ordinal: int, error: str
) -> None:
    async with transaction() as conn:
        await conn.execute(
            update(pack_task_sources)
            .where(
                pack_task_sources.c.task_id == task_id,
                pack_task_sources.c.ordinal == ordinal,
                pack_task_sources.c.cleanup_state == "pending",
            )
            .values(cleanup_error=error[:1000])
        )


async def get_pack_task_status(task_id: int) -> str | None:
    async with transaction() as conn:
        row = (
            await conn.execute(
                select(pack_tasks.c.status).where(pack_tasks.c.id == task_id)
            )
        ).first()
    return str(row[0]) if row else None


async def mark_pack_task_packing_if_pending(task_id: int) -> bool:
    async with transaction() as conn:
        row = (
            await conn.execute(
                update(pack_tasks)
                .where(
                    pack_tasks.c.id == task_id,
                    pack_tasks.c.status == "pending",
                    _active_pack_user(),
                )
                .values(status="packing", updated_at_ms=now_ms())
                .returning(pack_tasks.c.id)
            )
        ).first()
    return row is not None


async def update_pack_task_progress(task_id: int, progress: int) -> None:
    async with transaction() as conn:
        await conn.execute(
            update(pack_tasks)
            .where(
                pack_tasks.c.id == task_id,
                pack_tasks.c.status == "packing",
                _active_pack_user(),
            )
            .values(progress=progress, updated_at_ms=now_ms())
        )


async def persist_pack_prepared(
    task_id: int,
    *,
    content_hash: str,
    size_bytes: int,
    filename: str,
) -> bool:
    async with transaction() as conn:
        row = (
            await conn.execute(
                update(pack_tasks)
                .where(
                    pack_tasks.c.id == task_id,
                    pack_tasks.c.status == "packing",
                    pack_tasks.c.prepared_content_hash.is_(None),
                    _active_pack_user(),
                )
                .values(
                    prepared_content_hash=content_hash,
                    prepared_size_bytes=size_bytes,
                    prepared_filename=filename,
                    materialized_bytes=size_bytes,
                    updated_at_ms=now_ms(),
                )
                .returning(pack_tasks.c.id)
            )
        ).first()
    return row is not None


async def requeue_interrupted_pack_task(task_id: int) -> bool:
    async with transaction() as conn:
        row = (
            await conn.execute(
                update(pack_tasks)
                .where(
                    pack_tasks.c.id == task_id,
                    pack_tasks.c.status == "packing",
                    pack_tasks.c.prepared_content_hash.is_(None),
                    _active_pack_user(),
                )
                .values(
                    status="pending",
                    progress=0,
                    materialized_bytes=0,
                    install_reserved_bytes=0,
                    updated_at_ms=now_ms(),
                )
                .returning(pack_tasks.c.id)
            )
        ).first()
    return row is not None


async def fail_active_pack_task(task_id: int, error: str) -> dict[str, Any] | None:
    timestamp = now_ms()
    async with transaction() as conn:
        task_row = (
            await conn.execute(
                update(pack_tasks)
                .where(
                    pack_tasks.c.id == task_id,
                    pack_tasks.c.status.in_(PACK_ACTIVE_STATUSES),
                )
                .values(updated_at_ms=pack_tasks.c.updated_at_ms)
                .returning(pack_tasks)
            )
        ).mappings().first()
        if task_row is None:
            return None
        task = dict(task_row)
        await _release_reservation_locked(conn, task, timestamp=timestamp)
        await conn.execute(
            update(pack_task_sources)
            .where(
                pack_task_sources.c.task_id == task_id,
                pack_task_sources.c.cleanup_state == "pending",
            )
            .values(cleanup_state="retained", cleanup_error=None)
        )
        failed = (
            await conn.execute(
                update(pack_tasks)
                .where(
                    pack_tasks.c.id == task_id,
                    pack_tasks.c.status.in_(PACK_ACTIVE_STATUSES),
                )
                .values(
                    status="failed",
                    error_message=error,
                    reserved_bytes=0,
                    materialized_bytes=0,
                    install_reserved_bytes=0,
                    retry_count=0,
                    next_retry_at_ms=None,
                    prepared_content_hash=None,
                    prepared_size_bytes=None,
                    prepared_filename=None,
                    updated_at_ms=timestamp,
                    finished_at_ms=timestamp,
                )
                .returning(pack_tasks.c.id)
            )
        ).first()
        if failed is None:
            raise RepositoryConflictError("pack state changed during failure")
    return task


async def _ensure_prepared_stored_file(
    conn: Any,
    *,
    content_hash: str,
    real_path: str,
    size_bytes: int,
    filename: str,
    timestamp: int,
) -> dict[str, Any]:
    identity = content_identity_from_content_hash(content_hash)
    stored = (
        await conn.execute(
            select(stored_files).where(
                stored_files.c.content_hash_version == identity.version,
                stored_files.c.content_object_kind == identity.object_kind,
                (
                    stored_files.c.content_digest == identity.digest
                    if identity.version != "v1"
                    else (
                        (stored_files.c.content_digest == identity.digest)
                        | (
                            stored_files.c.content_digest.is_(None)
                            & (stored_files.c.content_hash == content_hash)
                        )
                    )
                ),
            )
        )
    ).mappings().first()
    if stored is not None:
        if bool(stored["pending_delete"]):
            raise RepositoryConflictError("prepared content is pending deletion")
        if int(stored["size_bytes"]) != size_bytes:
            raise RepositoryConflictError("prepared content size mismatch")
        if str(stored["real_path"]) != real_path:
            stored = (
                await conn.execute(
                    update(stored_files)
                    .where(stored_files.c.id == stored["id"])
                    .values(real_path=real_path)
                    .returning(stored_files)
                )
            ).mappings().one()
        return dict(stored)
    stored = (
        await conn.execute(
            insert(stored_files)
            .values(
                content_hash=content_hash,
                content_hash_version=identity.version,
                content_object_kind=identity.object_kind,
                content_digest=identity.digest,
                real_path=real_path,
                size_bytes=size_bytes,
                is_directory=0,
                original_name=filename,
                created_at_ms=timestamp,
            )
            .returning(stored_files)
        )
    ).mappings().one()
    await conn.execute(
        insert(stored_file_entries).values(
            stored_file_id=stored["id"],
            relative_path=".",
            parent_path="",
            name=filename,
            size_bytes=size_bytes,
            is_dir=0,
            mtime_ms=timestamp,
            sort_key=f"\x001\x00{filename.lower()}",
        )
    )
    return dict(stored)


async def finalize_prepared_pack_task(
    task_id: int,
    *,
    content_hash: str,
    size_bytes: int,
    filename: str,
    real_path: str,
) -> dict[str, Any] | None:
    timestamp = now_ms()
    async with transaction() as conn:
        task_row = (
            await conn.execute(
                update(pack_tasks)
                .where(
                    pack_tasks.c.id == task_id,
                    pack_tasks.c.status == "packing",
                    pack_tasks.c.prepared_content_hash == content_hash,
                    pack_tasks.c.prepared_size_bytes == size_bytes,
                    pack_tasks.c.prepared_filename == filename,
                    _active_pack_user(),
                )
                .values(updated_at_ms=pack_tasks.c.updated_at_ms)
                .returning(pack_tasks)
            )
        ).mappings().first()
        if task_row is None:
            return None
        task = dict(task_row)
        stored = await _ensure_prepared_stored_file(
            conn,
            content_hash=content_hash,
            real_path=real_path,
            size_bytes=size_bytes,
            filename=filename,
            timestamp=timestamp,
        )
        existing_ref = (
            await conn.execute(
                select(user_files.c.id).where(
                    user_files.c.user_id == task["user_id"],
                    user_files.c.stored_file_id == stored["id"],
                )
            )
        ).first()
        reserved = int(task["reserved_bytes"] or 0)
        if size_bytes > reserved:
            raise RepositoryConflictError("prepared output exceeds reservation")
        usage_values: dict[str, Any] = {
            "reserved_bytes": user_storage_usage.c.reserved_bytes - reserved,
            "updated_at_ms": timestamp,
        }
        if existing_ref is None:
            usage_values["used_bytes"] = (
                user_storage_usage.c.used_bytes + size_bytes
            )
        usage = (
            await conn.execute(
                update(user_storage_usage)
                .where(
                    user_storage_usage.c.user_id == task["user_id"],
                    user_storage_usage.c.reserved_bytes >= reserved,
                    exists(
                        select(users.c.id).where(
                            users.c.id == task["user_id"],
                            users.c.pending_delete == 0,
                        )
                    ),
                )
                .values(**usage_values)
                .returning(user_storage_usage.c.user_id)
            )
        ).first()
        if usage is None:
            raise RepositoryConflictError("pack reservation drift during finalize")
        if existing_ref is None:
            await conn.execute(
                insert(user_files).values(
                    user_id=task["user_id"],
                    stored_file_id=stored["id"],
                    display_name=filename,
                    created_at_ms=timestamp,
                    updated_at_ms=timestamp,
                )
            )
        completed = (
            await conn.execute(
                update(pack_tasks)
                .where(
                    pack_tasks.c.id == task_id,
                    pack_tasks.c.status == "packing",
                    pack_tasks.c.prepared_content_hash == content_hash,
                    pack_tasks.c.prepared_size_bytes == size_bytes,
                    pack_tasks.c.prepared_filename == filename,
                    _active_pack_user(),
                )
                .values(
                    status="completed",
                    progress=100,
                    output_stored_file_id=stored["id"],
                    reserved_bytes=0,
                    materialized_bytes=0,
                    install_reserved_bytes=0,
                    retry_count=0,
                    next_retry_at_ms=None,
                    prepared_content_hash=None,
                    prepared_size_bytes=None,
                    prepared_filename=None,
                    source_cleanup_pending=task["delete_source"],
                    error_message=None,
                    updated_at_ms=timestamp,
                    finished_at_ms=timestamp,
                )
                .returning(pack_tasks)
            )
        ).mappings().first()
        if completed is None:
            raise RepositoryConflictError("pack state changed during finalize")
    result = dict(completed)
    result["created_user_file"] = existing_ref is None
    result["stored_real_path"] = stored["real_path"]
    return result


async def list_pack_recovery_rows() -> list[dict[str, Any]]:
    async with transaction() as conn:
        rows = (
            await conn.execute(
                select(pack_tasks)
                .select_from(pack_tasks.join(users))
                .where(
                    (
                        pack_tasks.c.status.in_(PACK_ACTIVE_STATUSES)
                        | (pack_tasks.c.source_cleanup_pending == 1)
                    ),
                    users.c.pending_delete == 0,
                )
                .order_by(pack_tasks.c.id)
            )
        ).mappings().all()
    return [dict(row) for row in rows]


async def list_pending_pack_task_ids() -> list[int]:
    async with transaction() as conn:
        rows = (
            await conn.execute(
                select(pack_tasks.c.id)
                .select_from(pack_tasks.join(users))
                .where(
                    pack_tasks.c.status == "pending",
                    users.c.pending_delete == 0,
                )
                .order_by(pack_tasks.c.id)
            )
        ).all()
    return [int(row[0]) for row in rows]


async def list_pack_dispatch_task_ids(
    *,
    limit: int | None = None,
    due_at_ms: int | None = None,
) -> list[int]:
    due = now_ms() if due_at_ms is None else due_at_ms
    eligible = (
        (pack_tasks.c.status == "pending")
        | (
            (pack_tasks.c.status == "packing")
            & pack_tasks.c.prepared_content_hash.is_not(None)
        )
        | (
            (pack_tasks.c.status == "completed")
            & (pack_tasks.c.source_cleanup_pending == 1)
        )
    )
    query = (
        select(pack_tasks.c.id)
        .select_from(pack_tasks.join(users))
        .where(
            eligible,
            users.c.pending_delete == 0,
            pack_tasks.c.next_retry_at_ms.is_(None)
            | (pack_tasks.c.next_retry_at_ms <= max(0, due)),
        )
        .order_by(pack_tasks.c.id)
    )
    if limit is not None:
        query = query.limit(max(0, limit))
    async with transaction() as conn:
        rows = (await conn.execute(query)).all()
    return [int(row[0]) for row in rows]


async def mark_source_cleanup_complete(task_id: int) -> bool:
    async with transaction() as conn:
        row = (
            await conn.execute(
                update(pack_tasks)
                .where(
                    pack_tasks.c.id == task_id,
                    pack_tasks.c.status == "completed",
                    pack_tasks.c.source_cleanup_pending == 1,
                    ~exists(
                        select(pack_task_sources.c.ordinal).where(
                            pack_task_sources.c.task_id == task_id,
                            (pack_task_sources.c.cleanup_state == "pending")
                            | pack_task_sources.c.cleanup_real_path.is_not(None),
                        )
                    ),
                )
                .values(
                    source_cleanup_pending=0,
                    retry_count=0,
                    next_retry_at_ms=None,
                    updated_at_ms=now_ms(),
                )
                .returning(pack_tasks.c.id)
            )
        ).first()
    return row is not None


async def physical_budget_remaining_bytes(disk_available_bytes: int) -> int:
    async with transaction() as conn:
        commitment = await active_physical_commitment_bytes(conn)
    return max(0, disk_available_bytes - commitment)


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
            await conn.execute(
                select(users.c.quota_bytes).where(
                    users.c.id == user_id,
                    users.c.pending_delete == 0,
                )
            )
        ).first()
    return int(row[0]) if row else 0


def _pack_task_select():
    return select(
        pack_tasks,
        stored_files.c.size_bytes.label("output_size"),
    ).select_from(
        pack_tasks.outerjoin(
            stored_files,
            (pack_tasks.c.output_stored_file_id == stored_files.c.id)
            & (stored_files.c.pending_delete == 0),
        ).join(users, users.c.id == pack_tasks.c.user_id)
    ).where(users.c.pending_delete == 0)


async def list_user_pack_cleanup_rows(user_id: int) -> list[dict[str, Any]]:
    async with transaction() as conn:
        rows = (
            await conn.execute(
                select(pack_tasks)
                .where(pack_tasks.c.user_id == user_id)
                .order_by(pack_tasks.c.id)
            )
        ).mappings().all()
    return [dict(row) for row in rows]


async def clear_terminal_pack_tasks(user_id: int) -> int:
    async with transaction() as conn:
        result = await conn.execute(
            delete(pack_tasks)
            .where(
                pack_tasks.c.user_id == user_id,
                pack_tasks.c.status.in_(PACK_TERMINAL_STATUSES),
                pack_tasks.c.source_cleanup_pending == 0,
                ~exists(
                    select(pack_task_sources.c.ordinal).where(
                        pack_task_sources.c.task_id == pack_tasks.c.id,
                        (pack_task_sources.c.cleanup_state.in_(("pending", "unknown")))
                        | pack_task_sources.c.cleanup_real_path.is_not(None),
                    )
                ),
            )
            .returning(pack_tasks.c.id)
        )
        rows = result.all()
    return len(rows)


async def settle_user_pack_markers(task_id: int, user_id: int) -> bool:
    async with transaction() as conn:
        task = (
            await conn.execute(
                select(pack_tasks.c.id).where(
                    pack_tasks.c.id == task_id,
                    pack_tasks.c.user_id == user_id,
                    pack_tasks.c.status.in_(PACK_TERMINAL_STATUSES),
                    ~exists(
                        select(pack_task_sources.c.ordinal).where(
                            pack_task_sources.c.task_id == task_id,
                            pack_task_sources.c.cleanup_real_path.is_not(None),
                        )
                    ),
                )
            )
        ).first()
        if task is None:
            return False
        await conn.execute(
            update(pack_task_sources)
            .where(
                pack_task_sources.c.task_id == task_id,
                pack_task_sources.c.cleanup_state.in_(("pending", "unknown")),
            )
            .values(
                cleanup_state="retained",
                cleanup_error=None,
                cleaned_at_ms=now_ms(),
            )
        )
        await conn.execute(
            update(pack_tasks)
            .where(pack_tasks.c.id == task_id)
            .values(
                source_cleanup_pending=0,
                retry_count=0,
                next_retry_at_ms=None,
                updated_at_ms=now_ms(),
            )
        )
    return True


async def cancel_active_pack_task(user_id: int, task_id: int) -> dict[str, Any] | None:
    timestamp = now_ms()
    async with transaction() as conn:
        task_row = (
            await conn.execute(
                update(pack_tasks)
                .where(
                    pack_tasks.c.id == task_id,
                    pack_tasks.c.user_id == user_id,
                    pack_tasks.c.status.in_(PACK_ACTIVE_STATUSES),
                )
                .values(updated_at_ms=pack_tasks.c.updated_at_ms)
                .returning(pack_tasks)
            )
        ).mappings().first()
        if task_row is None:
            return None
        task = dict(task_row)
        await _release_reservation_locked(conn, task, timestamp=timestamp)
        await conn.execute(
            update(pack_task_sources)
            .where(
                pack_task_sources.c.task_id == task_id,
                pack_task_sources.c.cleanup_state == "pending",
            )
            .values(cleanup_state="retained", cleanup_error=None)
        )
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
                    materialized_bytes=0,
                    install_reserved_bytes=0,
                    retry_count=0,
                    next_retry_at_ms=None,
                    prepared_content_hash=None,
                    prepared_size_bytes=None,
                    prepared_filename=None,
                    updated_at_ms=timestamp,
                    finished_at_ms=timestamp,
                )
                .returning(pack_tasks)
            )
        ).mappings().first()
        if row is None:
            raise RepositoryConflictError("pack state changed during cancellation")
    return dict(row)


async def delete_user_pack_task(user_id: int, task_id: int) -> bool:
    async with transaction() as conn:
        row = (
            await conn.execute(
                delete(pack_tasks)
                .where(
                    pack_tasks.c.id == task_id,
                    pack_tasks.c.user_id == user_id,
                    pack_tasks.c.source_cleanup_pending == 0,
                    ~exists(
                        select(pack_task_sources.c.ordinal).where(
                            pack_task_sources.c.task_id == pack_tasks.c.id,
                            (pack_task_sources.c.cleanup_state.in_(("pending", "unknown")))
                            | pack_task_sources.c.cleanup_real_path.is_not(None),
                        )
                    ),
                )
                .returning(pack_tasks.c.id)
            )
        ).first()
    return row is not None


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
