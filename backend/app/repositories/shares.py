from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy import delete, exists, func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db.engine import get_engine, transaction
from app.db.schema import share_links, stored_files, user_files, users
from app.domain.shares import SHARE_ACTIVE_STATUS, SHARE_REVOKED_STATUS
from app.repositories.errors import RepositoryConflictError


class ShareTargetInactiveError(RuntimeError):
    pass


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
        .join(users, users.c.id == share_links.c.owner_id)
    ).where(
        stored_files.c.pending_delete == 0,
        users.c.pending_delete == 0,
    )


async def get_owned_file(user_id: int, user_file_id: int) -> dict[str, Any] | None:
    stmt = (
        select(
            user_files.c.id.label("user_file_id"),
            user_files.c.display_name,
            stored_files.c.size_bytes,
        )
        .select_from(
            user_files.join(
                stored_files, user_files.c.stored_file_id == stored_files.c.id
            ).join(users, users.c.id == user_files.c.user_id)
        )
        .where(
            user_files.c.id == user_file_id,
            user_files.c.user_id == user_id,
            stored_files.c.pending_delete == 0,
            users.c.pending_delete == 0,
        )
    )
    async with transaction() as conn:
        row = (await conn.execute(stmt)).mappings().first()
    return dict(row) if row else None


def _effective_active_conditions(user_file_id: int, timestamp_ms: int) -> tuple[Any, ...]:
    return (
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


async def create_share_row_in_existing_conn(
    conn: AsyncConnection,
    values: dict[str, Any],
) -> dict[str, Any]:
    row = (
        await conn.execute(insert(share_links).values(**values).returning(share_links))
    ).mappings().one()
    return dict(row)


async def create_share_with_retry(
    *,
    user_file_id: int,
    timestamp_ms: int,
    max_active_shares: int,
    values_factory: Callable[[], dict[str, Any]],
    max_attempts: int,
) -> dict[str, Any] | None:
    async with get_engine().connect() as conn:
        await conn.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            owned = (
                await conn.execute(
                    select(user_files.c.id)
                    .select_from(
                        user_files.join(stored_files).join(
                            users, users.c.id == user_files.c.user_id
                        )
                    )
                    .where(
                        user_files.c.id == user_file_id,
                        stored_files.c.pending_delete == 0,
                        users.c.pending_delete == 0,
                    )
                )
            ).first()
            if owned is None:
                raise ShareTargetInactiveError
            active_count = (
                await conn.execute(
                    select(func.count())
                    .select_from(share_links)
                    .where(*_effective_active_conditions(user_file_id, timestamp_ms))
                )
            ).scalar_one()
            if int(active_count or 0) >= max_active_shares:
                await conn.rollback()
                return None

            for attempt in range(max_attempts):
                try:
                    share = await create_share_row_in_existing_conn(
                        conn, values_factory()
                    )
                    await conn.commit()
                    return share
                except IntegrityError:
                    if attempt == max_attempts - 1:
                        raise RepositoryConflictError(
                            "share code collision"
                        ) from None
        except BaseException:
            await conn.rollback()
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
    should_count_download: bool,
) -> bool:
    active_target = exists(
        select(user_files.c.id)
        .select_from(
            user_files.join(stored_files).join(
                users, users.c.id == user_files.c.user_id
            )
        )
        .where(
            user_files.c.id == share_links.c.user_file_id,
            stored_files.c.pending_delete == 0,
            users.c.pending_delete == 0,
        )
    )
    values: dict[str, Any] = {"last_accessed_at_ms": timestamp_ms}
    if should_count_download:
        values["download_count"] = share_links.c.download_count + 1
    async with transaction() as conn:
        result = await conn.execute(
            update(share_links)
            .where(
                share_links.c.id == share_id,
                share_links.c.status == SHARE_ACTIVE_STATUS,
                active_target,
                (
                    share_links.c.expires_at_ms.is_(None)
                    | (share_links.c.expires_at_ms > timestamp_ms)
                ),
                (
                    share_links.c.max_downloads.is_(None)
                    | (share_links.c.download_count < share_links.c.max_downloads)
                ),
            )
            .values(**values)
        )
    return bool(result.rowcount)


async def consume_share_download(
    share_id: int,
    *,
    timestamp_ms: int,
) -> bool:
    return await touch_and_maybe_count_download(
        share_id,
        timestamp_ms=timestamp_ms,
        should_count_download=True,
    )

async def touch_share(share_id: int, timestamp_ms: int) -> None:
    async with transaction() as conn:
        await conn.execute(
            update(share_links)
            .where(share_links.c.id == share_id)
            .values(last_accessed_at_ms=timestamp_ms)
        )
