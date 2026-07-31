from __future__ import annotations

import time
from typing import Any

from sqlalchemy import delete, func, select, update

from app.db.engine import transaction
from app.db.schema import (
    global_downloads,
    pack_tasks,
    stored_file_entries,
    stored_files,
    user_files,
    user_tasks,
    users,
)


def now_ms() -> int:
    return int(time.time() * 1000)


async def list_stored_files(
    search: str,
    orphan_only: bool,
    *,
    offset: int,
    limit: int,
) -> tuple[int, list[dict[str, Any]]]:
    ref_counts = (
        select(
            user_files.c.stored_file_id.label("stored_file_id"),
            func.count(user_files.c.id).label("ref_count"),
        )
        .group_by(user_files.c.stored_file_id)
        .subquery()
    )
    ref_count = func.coalesce(ref_counts.c.ref_count, 0)
    source = stored_files.outerjoin(
        ref_counts, stored_files.c.id == ref_counts.c.stored_file_id
    )
    filters = []
    if search:
        filters.append(stored_files.c.original_name.contains(search))
    if orphan_only:
        filters.append(ref_count <= 0)

    count_stmt = select(func.count()).select_from(source).where(*filters)
    page_stmt = (
        select(stored_files, ref_count.label("ref_count"))
        .select_from(source)
        .where(*filters)
        .order_by(stored_files.c.created_at_ms.desc(), stored_files.c.id.desc())
        .offset(offset)
        .limit(limit)
    )
    async with transaction() as conn:
        total = int((await conn.execute(count_stmt)).scalar_one() or 0)
        rows = (await conn.execute(page_stmt)).mappings().all()
    return total, [dict(row) for row in rows]


async def stored_file_exists(file_id: int) -> bool:
    async with transaction() as conn:
        row = (
            await conn.execute(select(stored_files.c.id).where(stored_files.c.id == file_id))
        ).first()
    return row is not None


async def list_file_users(file_id: int) -> list[dict[str, Any]]:
    async with transaction() as conn:
        rows = (
            (
                await conn.execute(
                    select(
                        user_files.c.user_id,
                        users.c.username,
                        user_files.c.display_name,
                    )
                    .select_from(
                        user_files.join(users, user_files.c.user_id == users.c.id)
                    )
                    .where(user_files.c.stored_file_id == file_id)
                )
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


async def delete_orphan_stored_file(
    file_id: int,
) -> tuple[str, str, list[int]] | None:
    async with transaction() as conn:
        stored_file = (
            (
                await conn.execute(select(stored_files).where(stored_files.c.id == file_id))
            )
            .mappings()
            .first()
        )
        if not stored_file:
            return None
        ref_count = (
            await conn.execute(
                select(func.count())
                .select_from(user_files)
                .where(user_files.c.stored_file_id == file_id)
            )
        ).scalar_one()
        if int(ref_count or 0) > 0:
            raise ValueError(f"文件 {file_id} 仍有 {ref_count} 个引用，无法删除")
        timestamp = now_ms()
        affected_downloads = (
            await conn.execute(
                update(global_downloads)
                .where(global_downloads.c.completed_file_id == file_id)
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
            .where(pack_tasks.c.output_stored_file_id == file_id)
            .values(output_stored_file_id=None, updated_at_ms=timestamp)
        )
        await conn.execute(
            delete(stored_file_entries).where(
                stored_file_entries.c.stored_file_id == file_id
            )
        )
        await conn.execute(delete(stored_files).where(stored_files.c.id == file_id))
        return (
            str(stored_file["content_hash"]),
            str(stored_file["real_path"]),
            affected_download_ids,
        )
