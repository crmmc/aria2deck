from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any

from sqlalchemy import case, delete, func, insert, select, update

from app.db.engine import transaction
from app.db.schema import (
    global_downloads,
    pack_tasks,
    share_links,
    stored_file_entries,
    stored_files,
    user_files,
    user_storage_usage,
    user_tasks,
)
from app.domain.shares import SHARE_ACTIVE_STATUS


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


def file_select():
    return select(
        user_files.c.id.label("user_file_id"),
        user_files.c.user_id,
        user_files.c.stored_file_id,
        user_files.c.display_name,
        user_files.c.created_at_ms.label("user_file_created_at_ms"),
        user_files.c.updated_at_ms.label("user_file_updated_at_ms"),
        stored_files.c.content_hash,
        stored_files.c.real_path,
        stored_files.c.size_bytes,
        stored_files.c.is_directory,
        stored_files.c.original_name,
        stored_files.c.created_at_ms.label("stored_file_created_at_ms"),
    ).select_from(
        user_files.join(stored_files, user_files.c.stored_file_id == stored_files.c.id)
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
                    .select_from(user_files)
                    .where(user_files.c.user_id == user_id)
                )
            ).scalar_one()
            or 0
        )
        rows = (
            (
                await conn.execute(
                    file_select()
                    .where(user_files.c.user_id == user_id)
                    .order_by(user_files.c.created_at_ms.desc())
                    .offset(offset)
                    .limit(limit)
                )
            )
            .mappings()
            .all()
        )
    return total, [dict(row) for row in rows]


async def directory_entries(
    stored_file_id: int, parent_path: str
) -> tuple[bool | None, list[dict[str, Any]]]:
    async with transaction() as conn:
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


async def delete_user_file_reference(
    user_id: int,
    user_file_id: int,
) -> tuple[bool, list[int], str | None]:
    async with transaction() as conn:
        row = (
            (
                await conn.execute(
                    select(
                        user_files.c.stored_file_id,
                        stored_files.c.size_bytes,
                        stored_files.c.real_path,
                    )
                    .select_from(
                        user_files.join(
                            stored_files,
                            user_files.c.stored_file_id == stored_files.c.id,
                        )
                    )
                    .where(
                        user_files.c.id == user_file_id, user_files.c.user_id == user_id
                    )
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            return False, [], None

        deleted = await conn.execute(
            delete(user_files).where(
                user_files.c.id == user_file_id,
                user_files.c.user_id == user_id,
            )
        )
        if not deleted.rowcount:
            return False, [], None

        used_expr = user_storage_usage.c.used_bytes - int(row["size_bytes"] or 0)
        await conn.execute(
            update(user_storage_usage)
            .where(user_storage_usage.c.user_id == user_id)
            .values(
                used_bytes=case((used_expr < 0, 0), else_=used_expr),
                updated_at_ms=now_ms(),
            )
        )
        refs = (
            await conn.execute(
                select(func.count())
                .select_from(user_files)
                .where(user_files.c.stored_file_id == row["stored_file_id"])
            )
        ).scalar_one()
        if int(refs or 0) > 0:
            return True, [], None

        timestamp = now_ms()
        affected_downloads = (
            await conn.execute(
                update(global_downloads)
                .where(global_downloads.c.completed_file_id == row["stored_file_id"])
                .values(
                    status="cancelled",
                    aria2_gid=None,
                    completed_file_id=None,
                    completed_bytes=0,
                    completed_at_ms=None,
                    error_code="stored_file_deleted",
                    error_message="Stored file was deleted",
                    updated_at_ms=timestamp,
                )
                .returning(global_downloads.c.id)
            )
        ).all()
        affected_download_ids = [int(item[0]) for item in affected_downloads]
        if affected_download_ids:
            await conn.execute(
                update(user_tasks)
                .where(
                    user_tasks.c.global_download_id.in_(affected_download_ids),
                    user_tasks.c.status == "completed",
                )
                .values(
                    status="cancelled",
                    error_message="Stored file was deleted",
                    updated_at_ms=timestamp,
                    finished_at_ms=timestamp,
                )
            )
        await conn.execute(
            update(pack_tasks)
            .where(pack_tasks.c.output_stored_file_id == row["stored_file_id"])
            .values(output_stored_file_id=None, updated_at_ms=timestamp)
        )
        await conn.execute(
            delete(stored_file_entries).where(
                stored_file_entries.c.stored_file_id == row["stored_file_id"]
            )
        )
        await conn.execute(
            delete(stored_files).where(stored_files.c.id == row["stored_file_id"])
        )
        return True, affected_download_ids, str(row["real_path"])


async def count_active_shares_for_user_file(user_file_id: int) -> int:
    timestamp = now_ms()
    async with transaction() as conn:
        count = (
            await conn.execute(
                select(func.count())
                .select_from(share_links)
                .where(
                    share_links.c.user_file_id == user_file_id,
                    share_links.c.status == SHARE_ACTIVE_STATUS,
                    (
                        share_links.c.expires_at_ms.is_(None)
                        | (share_links.c.expires_at_ms > timestamp)
                    ),
                    (
                        share_links.c.max_downloads.is_(None)
                        | (share_links.c.download_count < share_links.c.max_downloads)
                    ),
                )
            )
        ).scalar_one()
    return int(count or 0)


async def rename_user_file_by_hash(user_id: int, file_hash: str, name: str) -> bool:
    async with transaction() as conn:
        result = await conn.execute(
            update(user_files)
            .where(
                user_files.c.id
                == select(user_files.c.id)
                .select_from(
                    user_files.join(
                        stored_files, user_files.c.stored_file_id == stored_files.c.id
                    )
                )
                .where(
                    stored_files.c.content_hash == file_hash,
                    user_files.c.user_id == user_id,
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
