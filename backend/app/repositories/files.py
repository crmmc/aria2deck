from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any

from sqlalchemy import case, delete, exists, func, insert, or_, select, update
from sqlalchemy.exc import IntegrityError

from app.db.engine import transaction
from app.db.schema import (
    global_downloads,
    pack_task_sources,
    pack_tasks,
    share_links,
    stored_file_entries,
    stored_files,
    user_files,
    user_storage_usage,
    user_tasks,
    users,
)
from app.domain.shares import SHARE_ACTIVE_STATUS
from app.repositories.errors import RepositoryConflictError
from app.domain.content_identity import ContentIdentity


class PackSourceProtectedError(RuntimeError):
    pass


ENTRY_INSERT_BATCH_SIZE = 250


async def _insert_entry_templates(
    conn: Any, stored_file_id: int, entry_templates: Sequence[dict[str, Any]]
) -> None:
    for offset in range(0, len(entry_templates), ENTRY_INSERT_BATCH_SIZE):
        batch = entry_templates[offset : offset + ENTRY_INSERT_BATCH_SIZE]
        await conn.execute(
            insert(stored_file_entries),
            [{"stored_file_id": stored_file_id, **entry} for entry in batch],
        )


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
    try:
        async with transaction() as conn:
            row = (
                await conn.execute(
                    insert(stored_files).values(**row_values).returning(stored_files)
                )
            ).mappings().one()
            if entry_templates:
                await _insert_entry_templates(conn, int(row["id"]), entry_templates)
    except IntegrityError as exc:
        raise RepositoryConflictError(str(exc)) from exc
    return dict(row), len(entry_templates)


async def delete_stored_file(stored_file_id: int) -> bool:
    async with transaction() as conn:
        row = (
            await conn.execute(
                delete(stored_files)
                .where(
                    stored_files.c.id == stored_file_id,
                    stored_files.c.pending_delete == 0,
                    ~select(user_files.c.id)
                    .where(user_files.c.stored_file_id == stored_file_id)
                    .exists(),
                )
                .returning(stored_files.c.id)
            )
        ).first()
    return row is not None


async def get_stored_file_by_content_hash(content_hash: str) -> dict[str, Any] | None:
    async with transaction() as conn:
        row = (
            await conn.execute(
                select(stored_files).where(
                    stored_files.c.content_hash == content_hash,
                    stored_files.c.pending_delete == 0,
                )
            )
        ).mappings().first()
    return dict(row) if row else None


async def get_stored_file_by_identity(
    identity: ContentIdentity,
) -> dict[str, Any] | None:
    async with transaction() as conn:
        row = (
            await conn.execute(
                select(stored_files).where(
                    stored_files.c.content_hash_version == identity.version,
                    stored_files.c.content_object_kind == identity.object_kind,
                    stored_files.c.content_digest == identity.digest,
                    stored_files.c.pending_delete == 0,
                )
            )
        ).mappings().first()
    return dict(row) if row else None


async def get_stored_file_by_real_path(real_path: str) -> dict[str, Any] | None:
    async with transaction() as conn:
        row = (
            await conn.execute(
                select(stored_files).where(
                    stored_files.c.real_path == real_path,
                    stored_files.c.pending_delete == 0,
                )
            )
        ).mappings().first()
    return dict(row) if row else None


async def get_user_file_delete_identity(
    user_id: int, user_file_id: int
) -> dict[str, Any] | None:
    async with transaction() as conn:
        row = (
            await conn.execute(
                select(
                    user_files.c.id,
                    user_files.c.stored_file_id,
                    user_files.c.created_at_ms,
                    stored_files.c.content_hash,
                )
                .select_from(
                    user_files.join(stored_files).join(
                        users, users.c.id == user_files.c.user_id
                    )
                )
                .where(
                    user_files.c.id == user_file_id,
                    user_files.c.user_id == user_id,
                    users.c.pending_delete == 0,
                    stored_files.c.pending_delete == 0,
                )
            )
        ).mappings().first()
    return dict(row) if row else None


async def list_stored_file_content_hashes() -> set[str]:
    async with transaction() as conn:
        rows = (await conn.execute(select(stored_files.c.content_hash))).all()
    return {str(row[0]) for row in rows}


async def list_stored_file_real_paths() -> set[str]:
    async with transaction() as conn:
        rows = (await conn.execute(select(stored_files.c.real_path))).all()
    return {str(row[0]) for row in rows}


async def list_pending_user_file_ids(user_id: int, *, limit: int) -> list[int]:
    async with transaction() as conn:
        rows = (
            await conn.execute(
                select(user_files.c.id)
                .select_from(user_files.join(users))
                .where(
                    user_files.c.user_id == user_id,
                    users.c.pending_delete == 1,
                )
                .order_by(user_files.c.id)
                .limit(limit)
            )
        ).all()
    return [int(row[0]) for row in rows]


async def get_pending_user_file_delete_identity(
    user_id: int, user_file_id: int
) -> dict[str, Any] | None:
    async with transaction() as conn:
        row = (
            await conn.execute(
                select(
                    user_files.c.id,
                    user_files.c.stored_file_id,
                    user_files.c.created_at_ms,
                    stored_files.c.content_hash,
                )
                .select_from(
                    user_files.join(stored_files).join(
                        users, users.c.id == user_files.c.user_id
                    )
                )
                .where(
                    user_files.c.id == user_file_id,
                    user_files.c.user_id == user_id,
                    users.c.pending_delete == 1,
                    stored_files.c.pending_delete == 0,
                )
            )
        ).mappings().first()
    return dict(row) if row else None


async def claim_due_stored_files(
    *,
    lease_token: str,
    timestamp_ms: int,
    lease_expires_at_ms: int,
    limit: int,
) -> list[dict[str, Any]]:
    due = (
        stored_files.c.pending_delete == 1,
        or_(
            stored_files.c.delete_next_retry_at_ms.is_(None),
            stored_files.c.delete_next_retry_at_ms <= timestamp_ms,
        ),
        or_(
            stored_files.c.delete_lease_expires_at_ms.is_(None),
            stored_files.c.delete_lease_expires_at_ms <= timestamp_ms,
        ),
        ~exists(
            select(user_files.c.id).where(
                user_files.c.stored_file_id == stored_files.c.id
            )
        ),
    )
    async with transaction() as conn:
        ids = [
            int(row[0])
            for row in (
                await conn.execute(
                    select(stored_files.c.id)
                    .where(*due)
                    .order_by(stored_files.c.id)
                    .limit(limit)
                )
            ).all()
        ]
        if not ids:
            return []
        rows = (
            await conn.execute(
                update(stored_files)
                .where(stored_files.c.id.in_(ids), *due)
                .values(
                    delete_attempts=stored_files.c.delete_attempts + 1,
                    delete_lease_token=lease_token,
                    delete_lease_expires_at_ms=lease_expires_at_ms,
                )
                .returning(stored_files)
            )
        ).mappings().all()
    return [dict(row) for row in rows]


async def retry_claimed_stored_file_delete(
    *,
    stored_file_id: int,
    lease_token: str,
    next_retry_at_ms: int,
    error: str,
) -> bool:
    async with transaction() as conn:
        row = (
            await conn.execute(
                update(stored_files)
                .where(
                    stored_files.c.id == stored_file_id,
                    stored_files.c.pending_delete == 1,
                    stored_files.c.delete_lease_token == lease_token,
                )
                .values(
                    delete_next_retry_at_ms=next_retry_at_ms,
                    delete_lease_token=None,
                    delete_lease_expires_at_ms=None,
                    delete_error=error[:1000],
                )
                .returning(stored_files.c.id)
            )
        ).first()
    return row is not None


async def hard_delete_claimed_stored_file(
    stored_file_id: int, lease_token: str
) -> bool:
    async with transaction() as conn:
        row = (
            await conn.execute(
                delete(stored_files)
                .where(
                    stored_files.c.id == stored_file_id,
                    stored_files.c.pending_delete == 1,
                    stored_files.c.delete_lease_token == lease_token,
                    ~exists(
                        select(user_files.c.id).where(
                            user_files.c.stored_file_id == stored_file_id
                        )
                    ),
                )
                .returning(stored_files.c.id)
            )
        ).first()
    return row is not None


async def ensure_stored_file_with_user_ref(
    *,
    user_id: int,
    content_hash: str,
    real_path: str,
    size_bytes: int,
    is_directory: bool,
    original_name: str,
    entry_templates: Sequence[dict[str, Any]],
    content_hash_version: str = "v1",
    content_object_kind: str = "legacy",
    content_digest: str | None = None,
) -> tuple[int, int | None]:
    timestamp = now_ms()
    async with transaction() as conn:
        active_user = (
            await conn.execute(
                select(users.c.id).where(
                    users.c.id == user_id,
                    users.c.pending_delete == 0,
                )
            )
        ).first()
        if active_user is None:
            raise RepositoryConflictError("用户正在删除")
        stored = (
            (
                await conn.execute(
                    select(stored_files).where(
                        stored_files.c.content_hash_version == content_hash_version,
                        stored_files.c.content_object_kind == content_object_kind,
                        stored_files.c.content_digest == (content_digest or content_hash),
                    )
                )
            )
            .mappings()
            .first()
        )
        if stored is not None and bool(stored["pending_delete"]):
            raise RepositoryConflictError("相同内容正在清理")
        if stored is None:
            stored = (
                (
                    await conn.execute(
                        insert(stored_files)
                        .values(
                            content_hash=content_hash,
                            content_hash_version=content_hash_version,
                            content_object_kind=content_object_kind,
                            content_digest=content_digest or content_hash,
                            real_path=real_path,
                            size_bytes=size_bytes,
                            is_directory=1 if is_directory else 0,
                            original_name=original_name,
                            created_at_ms=timestamp,
                        )
                        .returning(stored_files)
                    )
                )
                .mappings()
                .one()
            )
            if entry_templates:
                await _insert_entry_templates(conn, int(stored["id"]), entry_templates)

        existing_ref = (
            await conn.execute(
                select(user_files.c.id).where(
                    user_files.c.user_id == user_id,
                    user_files.c.stored_file_id == stored["id"],
                )
            )
        ).first()
        if existing_ref:
            return int(stored["id"]), None

        user_file = (
            await conn.execute(
                insert(user_files)
                .values(
                    user_id=user_id,
                    stored_file_id=stored["id"],
                    display_name=original_name,
                    created_at_ms=timestamp,
                    updated_at_ms=timestamp,
                )
                .returning(user_files.c.id)
            )
        ).first()
    return int(stored["id"]), int(user_file[0]) if user_file else None


def file_select():
    return select(
        user_files.c.id.label("user_file_id"),
        user_files.c.user_id,
        user_files.c.stored_file_id,
        user_files.c.display_name,
        user_files.c.created_at_ms.label("user_file_created_at_ms"),
        user_files.c.updated_at_ms.label("user_file_updated_at_ms"),
        stored_files.c.content_hash,
        stored_files.c.content_hash_version,
        stored_files.c.content_object_kind,
        stored_files.c.content_digest,
        stored_files.c.real_path,
        stored_files.c.size_bytes,
        stored_files.c.is_directory,
        stored_files.c.original_name,
        stored_files.c.created_at_ms.label("stored_file_created_at_ms"),
    ).select_from(
        user_files.join(
            stored_files, user_files.c.stored_file_id == stored_files.c.id
        ).join(users, users.c.id == user_files.c.user_id)
    ).where(
        stored_files.c.pending_delete == 0,
        users.c.pending_delete == 0,
    )


async def get_user_file_by_hash(
    user_id: int, content_hash: str
) -> dict[str, Any] | None:
    stmt = (
        file_select()
        .where(
            stored_files.c.content_hash == content_hash,
            user_files.c.user_id == user_id,
        )
        .order_by(user_files.c.id.asc())
    )
    async with transaction() as conn:
        row = (await conn.execute(stmt)).mappings().first()
    return dict(row) if row else None


async def list_user_file_rows(
    user_id: int,
    *,
    offset: int,
    limit: int,
) -> tuple[int, list[dict[str, Any]]]:
    async with transaction() as conn:
        total = int(
            (
                await conn.execute(
                    select(func.count())
                    .select_from(
                        user_files.join(
                            stored_files,
                            user_files.c.stored_file_id == stored_files.c.id,
                        ).join(users, users.c.id == user_files.c.user_id)
                    )
                    .where(
                        user_files.c.user_id == user_id,
                        stored_files.c.pending_delete == 0,
                        users.c.pending_delete == 0,
                    )
                )
            ).scalar_one()
            or 0
        )
        rows = (
            (
                await conn.execute(
                    file_select()
                    .where(user_files.c.user_id == user_id)
                    .order_by(
                        user_files.c.created_at_ms.desc(), user_files.c.id.desc()
                    )
                    .offset(offset)
                    .limit(limit)
                )
            )
            .mappings()
            .all()
        )
    return total, [dict(row) for row in rows]


async def list_all_user_file_rows(user_id: int) -> list[dict[str, Any]]:
    stmt = (
        file_select()
        .where(user_files.c.user_id == user_id)
        .order_by(user_files.c.created_at_ms.desc(), user_files.c.id.desc())
    )
    async with transaction() as conn:
        rows = (await conn.execute(stmt)).mappings().all()
    return [dict(row) for row in rows]


async def search_stored_file_entries(
    stored_file_ids: list[int],
    *,
    path_prefix: str = "",
) -> list[dict[str, Any]]:
    conditions = [
        stored_file_entries.c.stored_file_id.in_(stored_file_ids),
        stored_file_entries.c.relative_path != ".",
    ]
    if path_prefix:
        conditions.append(
            or_(
                stored_file_entries.c.relative_path == path_prefix,
                stored_file_entries.c.relative_path.startswith(
                    path_prefix + "/", autoescape=True
                ),
            )
        )
    stmt = (
        select(stored_file_entries).where(*conditions).order_by(stored_file_entries.c.id)
    )
    async with transaction() as conn:
        rows = (await conn.execute(stmt)).mappings().all()
    return [dict(row) for row in rows]


async def directory_entries(
    stored_file_id: int, parent_path: str
) -> tuple[bool | None, list[dict[str, Any]]]:
    async with transaction() as conn:
        active = (
            await conn.execute(
                select(stored_files.c.id).where(
                    stored_files.c.id == stored_file_id,
                    stored_files.c.pending_delete == 0,
                )
            )
        ).first()
        if active is None:
            return None, []
        parent_is_dir: bool | None = True
        if parent_path:
            parent = (
                await conn.execute(
                    select(stored_file_entries.c.is_dir)
                    .where(
                        stored_file_entries.c.stored_file_id == stored_file_id,
                        stored_file_entries.c.relative_path == parent_path,
                    )
                    .limit(1)
                )
            ).first()
            if parent is None:
                return None, []
            parent_is_dir = bool(parent[0])
            if not parent_is_dir:
                return False, []

        rows = (
            (
                await conn.execute(
                    select(stored_file_entries)
                    .where(
                        stored_file_entries.c.stored_file_id == stored_file_id,
                        stored_file_entries.c.parent_path == parent_path,
                        stored_file_entries.c.relative_path != ".",
                    )
                    .order_by(
                        stored_file_entries.c.sort_key, stored_file_entries.c.name
                    )
                )
            )
            .mappings()
            .all()
        )
    return parent_is_dir, [dict(row) for row in rows]


async def resolve_user_file_ids(
    user_id: int,
    file_ids: list[int],
) -> list[dict[str, Any]]:
    requested_ids = list(dict.fromkeys(file_ids))
    stmt = file_select().where(
        user_files.c.id.in_(requested_ids),
        user_files.c.user_id == user_id,
    )
    async with transaction() as conn:
        rows = (await conn.execute(stmt)).mappings().all()
    by_id = {int(row["user_file_id"]): dict(row) for row in rows}
    return [by_id[file_id] for file_id in requested_ids if file_id in by_id]


async def _finish_user_file_reference_delete(
    conn: Any,
    *,
    user_id: int,
    row: Any,
    adjust_usage: bool,
    timestamp: int,
) -> tuple[list[int], str | None]:
    if adjust_usage:
        used_expr = user_storage_usage.c.used_bytes - int(row["size_bytes"] or 0)
        await conn.execute(
            update(user_storage_usage)
            .where(user_storage_usage.c.user_id == user_id)
            .values(
                used_bytes=case((used_expr < 0, 0), else_=used_expr),
                updated_at_ms=timestamp,
            )
        )
    await conn.execute(
        update(pack_tasks)
        .where(
            pack_tasks.c.user_id == user_id,
            pack_tasks.c.output_stored_file_id == row["stored_file_id"],
        )
        .values(output_stored_file_id=None, updated_at_ms=timestamp)
    )
    refs = (
        await conn.execute(
            select(func.count())
            .select_from(user_files)
            .where(user_files.c.stored_file_id == row["stored_file_id"])
        )
    ).scalar_one()
    if int(refs or 0) > 0:
        return [], None
    affected = (
        await conn.execute(
            update(global_downloads)
            .where(global_downloads.c.completed_file_id == row["stored_file_id"])
            .values(
                status="cancelled", aria2_gid=None, completed_file_id=None,
                completed_bytes=0, completed_at_ms=None,
                error_code="stored_file_deleted",
                error_message="Stored file was deleted", updated_at_ms=timestamp,
            )
            .returning(global_downloads.c.id)
        )
    ).all()
    download_ids = [int(item[0]) for item in affected]
    if download_ids:
        await conn.execute(
            update(user_tasks)
            .where(
                user_tasks.c.global_download_id.in_(download_ids),
                user_tasks.c.status == "completed",
            )
            .values(
                status="cancelled", error_message="Stored file was deleted",
                updated_at_ms=timestamp, finished_at_ms=timestamp,
            )
        )
    await conn.execute(
        update(pack_tasks)
        .where(pack_tasks.c.output_stored_file_id == row["stored_file_id"])
        .values(output_stored_file_id=None, updated_at_ms=timestamp)
    )
    await conn.execute(
        update(stored_files)
        .where(
            stored_files.c.id == row["stored_file_id"],
            stored_files.c.pending_delete == 0,
            ~exists(
                select(user_files.c.id).where(
                    user_files.c.stored_file_id == row["stored_file_id"]
                )
            ),
        )
        .values(
            pending_delete=1,
            delete_attempts=0,
            delete_next_retry_at_ms=timestamp,
            delete_lease_token=None,
            delete_lease_expires_at_ms=None,
            delete_error=None,
        )
    )
    return download_ids, str(row["real_path"])


async def delete_user_file_reference(
    user_id: int,
    user_file_id: int,
    *,
    adjust_usage: bool = True,
    expected_stored_file_id: int | None = None,
    expected_created_at_ms: int | None = None,
    cleanup_pending_user: bool = False,
) -> tuple[bool, list[int], str | None]:
    async with transaction() as conn:
        row = (
            (
                await conn.execute(
                    select(
                        user_files.c.stored_file_id,
                        user_files.c.created_at_ms,
                        stored_files.c.size_bytes,
                        stored_files.c.real_path,
                    )
                    .select_from(
                        user_files.join(
                            stored_files,
                            user_files.c.stored_file_id == stored_files.c.id,
                        ).join(users, users.c.id == user_files.c.user_id)
                    )
                    .where(
                        user_files.c.id == user_file_id,
                        user_files.c.user_id == user_id,
                        users.c.pending_delete == (1 if cleanup_pending_user else 0),
                        stored_files.c.pending_delete == 0,
                    )
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            return False, [], None
        if (
            expected_stored_file_id is not None
            and int(row["stored_file_id"]) != expected_stored_file_id
        ) or (
            expected_created_at_ms is not None
            and int(row["created_at_ms"]) != expected_created_at_ms
        ):
            return False, [], None

        protected = (
            select(pack_task_sources.c.ordinal)
            .select_from(pack_task_sources.join(pack_tasks))
            .where(
                pack_task_sources.c.original_user_file_id == user_file_id,
                pack_task_sources.c.stored_file_id == row["stored_file_id"],
                pack_task_sources.c.user_file_created_at_ms == row["created_at_ms"],
                or_(
                    pack_tasks.c.status.in_(("pending", "packing")),
                    pack_task_sources.c.cleanup_state.in_(("pending", "unknown")),
                ),
            )
            .exists()
        )
        await conn.execute(
            update(share_links)
            .where(
                share_links.c.user_file_id == user_file_id,
                share_links.c.status == SHARE_ACTIVE_STATUS,
                ~protected,
            )
            .values(status="revoked")
        )
        deleted = await conn.execute(
            delete(user_files).where(
                user_files.c.id == user_file_id,
                user_files.c.user_id == user_id,
                user_files.c.stored_file_id == row["stored_file_id"],
                user_files.c.created_at_ms == row["created_at_ms"],
                ~protected,
            )
        )
        if not deleted.rowcount:
            if (await conn.execute(select(protected))).scalar_one():
                raise PackSourceProtectedError
            return False, [], None

        timestamp = now_ms()
        affected_download_ids, real_path = await _finish_user_file_reference_delete(
            conn,
            user_id=user_id,
            row=row,
            adjust_usage=adjust_usage,
            timestamp=timestamp,
        )
        return True, affected_download_ids, real_path


async def cleanup_pack_source_reference(
    task_id: int, ordinal: int
) -> tuple[str, list[int], str | None]:
    timestamp = now_ms()
    async with transaction() as conn:
        source = (
            await conn.execute(
                select(pack_task_sources, pack_tasks.c.user_id,
                       pack_tasks.c.output_stored_file_id)
                .select_from(pack_task_sources.join(pack_tasks))
                .where(
                    pack_task_sources.c.task_id == task_id,
                    pack_task_sources.c.ordinal == ordinal,
                    pack_task_sources.c.cleanup_state == "pending",
                    pack_tasks.c.status == "completed",
                    pack_tasks.c.source_cleanup_pending == 1,
                )
            )
        ).mappings().first()
        if source is None:
            return "noop", [], None
        current = (
            await conn.execute(
                select(
                    user_files.c.id, user_files.c.stored_file_id,
                    user_files.c.created_at_ms, stored_files.c.size_bytes,
                    stored_files.c.real_path,
                )
                .select_from(user_files.join(stored_files))
                .where(
                    user_files.c.id == source["original_user_file_id"],
                    user_files.c.user_id == source["user_id"],
                    user_files.c.stored_file_id == source["stored_file_id"],
                    user_files.c.created_at_ms == source["user_file_created_at_ms"],
                    stored_files.c.content_hash == source["content_hash"],
                )
            )
        ).mappings().first()
        if current is None:
            await conn.execute(
                update(pack_task_sources)
                .where(
                    pack_task_sources.c.task_id == task_id,
                    pack_task_sources.c.ordinal == ordinal,
                    pack_task_sources.c.cleanup_state == "pending",
                )
                .values(
                    cleanup_state="identity_mismatch",
                    cleanup_error="源文件身份已变化，安全跳过删除",
                    cleaned_at_ms=timestamp,
                )
            )
            return "identity_mismatch", [], None
        if current["stored_file_id"] == source["output_stored_file_id"]:
            await conn.execute(
                update(pack_task_sources)
                .where(
                    pack_task_sources.c.task_id == task_id,
                    pack_task_sources.c.ordinal == ordinal,
                    pack_task_sources.c.cleanup_state == "pending",
                )
                .values(cleanup_state="retained_output", cleaned_at_ms=timestamp)
            )
            return "retained_output", [], None
        deleted = await conn.execute(
            delete(user_files).where(
                user_files.c.id == current["id"],
                user_files.c.user_id == source["user_id"],
                user_files.c.stored_file_id == current["stored_file_id"],
                user_files.c.created_at_ms == current["created_at_ms"],
            )
        )
        if not deleted.rowcount:
            return "noop", [], None
        download_ids, real_path = await _finish_user_file_reference_delete(
            conn, user_id=int(source["user_id"]), row=current,
            adjust_usage=True, timestamp=timestamp,
        )
        await conn.execute(
            update(pack_task_sources)
            .where(
                pack_task_sources.c.task_id == task_id,
                pack_task_sources.c.ordinal == ordinal,
                pack_task_sources.c.cleanup_state == "pending",
            )
            .values(
                cleanup_state="cleaned",
                cleaned_at_ms=timestamp,
                cleanup_real_path=real_path,
                cleanup_error="等待物理文件清理" if real_path else None,
            )
        )
    return "cleaned", download_ids, real_path


async def set_pack_source_cleanup_real_path(
    task_id: int, ordinal: int, real_path: str
) -> bool:
    async with transaction() as conn:
        row = (
            await conn.execute(
                update(pack_task_sources)
                .where(
                    pack_task_sources.c.task_id == task_id,
                    pack_task_sources.c.ordinal == ordinal,
                    pack_task_sources.c.cleanup_state == "cleaned",
                    pack_task_sources.c.cleanup_real_path.is_not(None),
                )
                .values(
                    cleanup_real_path=real_path,
                    cleanup_error="等待物理文件清理",
                )
                .returning(pack_task_sources.c.task_id)
            )
        ).first()
    return row is not None


async def finish_pack_source_physical_cleanup(
    task_id: int, ordinal: int, error: str | None
) -> None:
    async with transaction() as conn:
        await conn.execute(
            update(pack_task_sources)
            .where(
                pack_task_sources.c.task_id == task_id,
                pack_task_sources.c.ordinal == ordinal,
                pack_task_sources.c.cleanup_state == "cleaned",
            )
            .values(
                cleanup_real_path=None if error is None else pack_task_sources.c.cleanup_real_path,
                cleanup_error=error[:1000] if error else None,
            )
        )


async def rename_user_file_by_hash(user_id: int, file_hash: str, name: str) -> bool:
    async with transaction() as conn:
        result = await conn.execute(
            update(user_files)
            .where(
                user_files.c.id
                == select(user_files.c.id)
                .select_from(
                    user_files.join(
                        stored_files,
                        user_files.c.stored_file_id == stored_files.c.id,
                    ).join(users, users.c.id == user_files.c.user_id)
                )
                .where(
                    stored_files.c.content_hash == file_hash,
                    stored_files.c.pending_delete == 0,
                    user_files.c.user_id == user_id,
                    users.c.pending_delete == 0,
                )
                .order_by(user_files.c.id.asc())
                .limit(1)
                .scalar_subquery()
            )
            .values(display_name=name, updated_at_ms=now_ms())
            .returning(user_files.c.id)
        )
        row = result.first()
    return row is not None
