from __future__ import annotations

from typing import Any

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError

from app.db.engine import transaction
from app.db.schema import share_links, stored_files, user_files
from app.domain.shares import SHARE_ACTIVE_STATUS, SHARE_REVOKED_STATUS


def share_select():
    return select(
        share_links,
        user_files.c.display_name.label("file_name"),
        user_files.c.stored_file_id,
        stored_files.c.content_hash,
        stored_files.c.real_path,
        stored_files.c.size_bytes,
        stored_files.c.is_directory,
    ).select_from(
        share_links.join(user_files, share_links.c.user_file_id == user_files.c.id)
        .join(stored_files, user_files.c.stored_file_id == stored_files.c.id)
    )


async def get_owned_file(user_id: int, user_file_id: int) -> dict[str, Any] | None:
    stmt = (
        select(
            user_files.c.id.label("user_file_id"),
            user_files.c.display_name,
            stored_files.c.size_bytes,
        )
        .select_from(
            user_files.join(stored_files, user_files.c.stored_file_id == stored_files.c.id)
        )
        .where(user_files.c.id == user_file_id, user_files.c.user_id == user_id)
    )
    async with transaction() as conn:
        row = (await conn.execute(stmt)).mappings().first()
    return dict(row) if row else None


async def count_effective_active_shares(user_file_id: int, timestamp_ms: int) -> int:
    async with transaction() as conn:
        value = (
            await conn.execute(
                select(func.count()).select_from(share_links).where(
                    share_links.c.user_file_id == user_file_id,
                    share_links.c.status == SHARE_ACTIVE_STATUS,
                    (
                        share_links.c.expires_at_ms.is_(None)
                        | (share_links.c.expires_at_ms > timestamp_ms)
                    ),
                    (
                        share_links.c.max_downloads.is_(None)
                        | (share_links.c.download_count < share_links.c.max_downloads)
                    ),
                )
            )
        ).scalar_one()
    return int(value or 0)


async def create_share_row(values: dict[str, Any]) -> dict[str, Any]:
    async with transaction() as conn:
        row = (
            await conn.execute(insert(share_links).values(**values).returning(share_links))
        ).mappings().one()
    return dict(row)


async def create_share_row_in_existing_conn(conn, values: dict[str, Any]) -> dict[str, Any]:
    row = (
        await conn.execute(insert(share_links).values(**values).returning(share_links))
    ).mappings().one()
    return dict(row)


async def create_share_with_retry(
    *,
    values_factory,
    max_attempts: int,
) -> dict[str, Any] | None:
    async with transaction() as conn:
        for attempt in range(max_attempts):
            try:
                return await create_share_row_in_existing_conn(conn, values_factory())
            except IntegrityError:
                if attempt == max_attempts - 1:
                    raise
    return None


async def list_shares(owner_id: int) -> list[dict[str, Any]]:
    async with transaction() as conn:
        rows = (
            await conn.execute(
                share_select()
                .where(share_links.c.owner_id == owner_id)
                .order_by(share_links.c.id.desc())
            )
        ).mappings().all()
    return [dict(row) for row in rows]


async def get_share_status_for_owner(share_id: int, owner_id: int) -> str | None:
    async with transaction() as conn:
        row = (
            await conn.execute(
                select(share_links.c.status).where(
                    share_links.c.id == share_id,
                    share_links.c.owner_id == owner_id,
                )
            )
        ).first()
    return str(row[0]) if row else None


async def revoke_share(share_id: int, owner_id: int) -> None:
    async with transaction() as conn:
        await conn.execute(
            update(share_links)
            .where(share_links.c.id == share_id, share_links.c.owner_id == owner_id)
            .values(status=SHARE_REVOKED_STATUS)
        )


async def delete_share(share_id: int, owner_id: int) -> bool:
    async with transaction() as conn:
        result = await conn.execute(
            delete(share_links).where(
                share_links.c.id == share_id,
                share_links.c.owner_id == owner_id,
            )
        )
    return bool(result.rowcount)


async def revoke_all_shares(owner_id: int) -> int:
    async with transaction() as conn:
        result = await conn.execute(
            update(share_links)
            .where(
                share_links.c.owner_id == owner_id,
                share_links.c.status == SHARE_ACTIVE_STATUS,
            )
            .values(status=SHARE_REVOKED_STATUS)
        )
    return int(result.rowcount or 0)


async def get_share_with_file(code: str) -> tuple[dict[str, Any] | None, bool]:
    async with transaction() as conn:
        share = (
            await conn.execute(share_select().where(share_links.c.share_code == code))
        ).mappings().first()
        if share:
            return dict(share), True
        existing = (
            await conn.execute(select(share_links.c.id).where(share_links.c.share_code == code))
        ).first()
    return None, existing is not None


async def touch_and_maybe_count_download(
    share_id: int,
    *,
    timestamp_ms: int,
    max_downloads: int | None,
    should_count_download: bool,
) -> bool:
    conditions = [
        share_links.c.id == share_id,
        share_links.c.status == SHARE_ACTIVE_STATUS,
        (
            share_links.c.expires_at_ms.is_(None)
            | (share_links.c.expires_at_ms > timestamp_ms)
        ),
    ]
    if max_downloads is not None:
        conditions.append(share_links.c.download_count < share_links.c.max_downloads)

    values: dict[str, Any] = {"last_accessed_at_ms": timestamp_ms}
    if should_count_download:
        values["download_count"] = share_links.c.download_count + 1

    async with transaction() as conn:
        result = await conn.execute(
            update(share_links).where(*conditions).values(**values)
        )
    return bool(result.rowcount)


async def touch_share(share_id: int, timestamp_ms: int) -> None:
    async with transaction() as conn:
        await conn.execute(
            update(share_links)
            .where(share_links.c.id == share_id)
            .values(last_accessed_at_ms=timestamp_ms)
        )
