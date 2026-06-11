from __future__ import annotations

import time
from typing import Any

from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.exc import IntegrityError

from app.db.engine import transaction
from app.db.schema import api_tokens, pack_tasks, sessions, user_files, user_storage_usage, user_tasks, users


def now_ms() -> int:
    return int(time.time() * 1000)


async def has_any_user() -> bool:
    async with transaction() as conn:
        row = (await conn.execute(select(users.c.id).limit(1))).first()
    return row is not None


async def count_admins() -> int:
    async with transaction() as conn:
        count = (await conn.execute(select(func.count()).select_from(users).where(users.c.is_admin == 1))).scalar_one()
    return int(count)


async def create_user(
    *,
    username: str,
    password_hash: str,
    is_admin: bool,
    quota_bytes: int,
    is_initial_password: bool,
) -> dict[str, Any]:
    timestamp = now_ms()
    async with transaction() as conn:
        row = (
            await conn.execute(
                insert(users)
                .values(
                    username=username,
                    password_hash=password_hash,
                    is_admin=1 if is_admin else 0,
                    quota_bytes=quota_bytes,
                    is_initial_password=1 if is_initial_password else 0,
                    created_at_ms=timestamp,
                    updated_at_ms=timestamp,
                )
                .returning(users)
            )
        ).mappings().one()
        await conn.execute(
            insert(user_storage_usage).values(
                user_id=row["id"],
                used_bytes=0,
                reserved_bytes=0,
                updated_at_ms=timestamp,
            )
        )
    return dict(row)


async def create_first_user_if_none(
    *,
    username: str,
    password_hash: str,
    is_admin: bool,
    quota_bytes: int,
    is_initial_password: bool,
) -> dict[str, Any] | None:
    timestamp = now_ms()
    stmt = text(
        """
        INSERT INTO users (
            username, password_hash, is_admin, quota_bytes,
            is_initial_password, created_at_ms, updated_at_ms
        )
        SELECT
            :username, :password_hash, :is_admin, :quota_bytes,
            :is_initial_password, :created_at_ms, :updated_at_ms
        WHERE NOT EXISTS (SELECT 1 FROM users)
        RETURNING *
        """
    )
    async with transaction() as conn:
        try:
            row = (
                await conn.execute(
                    stmt,
                    {
                        "username": username,
                        "password_hash": password_hash,
                        "is_admin": 1 if is_admin else 0,
                        "quota_bytes": quota_bytes,
                        "is_initial_password": 1 if is_initial_password else 0,
                        "created_at_ms": timestamp,
                        "updated_at_ms": timestamp,
                    },
                )
            ).mappings().first()
        except IntegrityError:
            return None
        if row is None:
            return None
        await conn.execute(
            insert(user_storage_usage).values(
                user_id=row["id"],
                used_bytes=0,
                reserved_bytes=0,
                updated_at_ms=timestamp,
            )
        )
    return dict(row)


async def get_user_by_id(user_id: int) -> dict[str, Any] | None:
    async with transaction() as conn:
        row = (await conn.execute(select(users).where(users.c.id == user_id))).mappings().first()
    return dict(row) if row else None


async def get_user_by_username(username: str) -> dict[str, Any] | None:
    async with transaction() as conn:
        row = (await conn.execute(select(users).where(users.c.username == username))).mappings().first()
    return dict(row) if row else None


async def list_users_by_rpc_secret(secret: str, *, limit: int = 2) -> list[dict[str, Any]]:
    async with transaction() as conn:
        rows = (
            await conn.execute(
                select(users)
                .where(users.c.rpc_secret == secret)
                .limit(limit)
            )
        ).mappings().all()
    return [dict(row) for row in rows]


async def list_users() -> list[dict[str, Any]]:
    async with transaction() as conn:
        rows = (await conn.execute(select(users).order_by(users.c.id))).mappings().all()
    return [dict(row) for row in rows]


async def update_user(user_id: int, **fields: Any) -> dict[str, Any] | None:
    values = dict(fields)
    if "is_admin" in values:
        values["is_admin"] = 1 if values["is_admin"] else 0
    if "is_initial_password" in values:
        values["is_initial_password"] = 1 if values["is_initial_password"] else 0
    if "quota" in values:
        values["quota_bytes"] = values.pop("quota")
    if "rpc_secret_created_at" in values:
        values["rpc_secret_created_at_ms"] = values.pop("rpc_secret_created_at")
    if not values:
        return await get_user_by_id(user_id)

    values["updated_at_ms"] = now_ms()
    async with transaction() as conn:
        row = (
            await conn.execute(update(users).where(users.c.id == user_id).values(**values).returning(users))
        ).mappings().first()
    return dict(row) if row else None


async def delete_user(user_id: int) -> bool:
    async with transaction() as conn:
        result = await conn.execute(delete(users).where(users.c.id == user_id))
    return bool(result.rowcount)


async def delete_user_owned_rows(user_id: int) -> None:
    async with transaction() as conn:
        await conn.execute(delete(sessions).where(sessions.c.user_id == user_id))
        await conn.execute(delete(user_tasks).where(user_tasks.c.user_id == user_id))
        await conn.execute(delete(pack_tasks).where(pack_tasks.c.user_id == user_id))
        await conn.execute(delete(user_files).where(user_files.c.user_id == user_id))
        await conn.execute(delete(api_tokens).where(api_tokens.c.user_id == user_id))


async def create_session(session_id: str, user_id: int, expires_at_ms: int) -> str:
    async with transaction() as conn:
        await conn.execute(
            insert(sessions).values(
                id=session_id,
                user_id=user_id,
                expires_at_ms=expires_at_ms,
                created_at_ms=now_ms(),
            )
        )
    return session_id


async def delete_session(session_id: str) -> bool:
    async with transaction() as conn:
        result = await conn.execute(delete(sessions).where(sessions.c.id == session_id))
    return bool(result.rowcount)


async def delete_user_sessions(user_id: int) -> int:
    async with transaction() as conn:
        result = await conn.execute(delete(sessions).where(sessions.c.user_id == user_id))
    return int(result.rowcount or 0)


async def get_session_user(session_id: str) -> dict[str, Any] | None:
    stmt = (
        select(
            users,
            sessions.c.expires_at_ms.label("session_expires_at_ms"),
        )
        .select_from(sessions.join(users, sessions.c.user_id == users.c.id))
        .where(sessions.c.id == session_id)
    )
    async with transaction() as conn:
        row = (await conn.execute(stmt)).mappings().first()
    return dict(row) if row else None


async def list_api_tokens(user_id: int) -> list[dict[str, Any]]:
    async with transaction() as conn:
        rows = (
            await conn.execute(
                select(api_tokens).where(api_tokens.c.user_id == user_id).order_by(api_tokens.c.created_at_ms.desc())
            )
        ).mappings().all()
    return [dict(row) for row in rows]


async def create_api_token(user_id: int, token: str, name: str | None) -> dict[str, Any]:
    async with transaction() as conn:
        row = (
            await conn.execute(
                insert(api_tokens)
                .values(
                    user_id=user_id,
                    token=token,
                    name=name,
                    created_at_ms=now_ms(),
                )
                .returning(api_tokens)
            )
        ).mappings().one()
    return dict(row)


async def delete_api_token(user_id: int, token_id: int) -> bool:
    async with transaction() as conn:
        result = await conn.execute(delete(api_tokens).where(api_tokens.c.id == token_id, api_tokens.c.user_id == user_id))
    return bool(result.rowcount)
