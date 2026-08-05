from __future__ import annotations

import time
from typing import Any

from sqlalchemy import delete, exists, func, insert, literal, or_, select, text, update
from sqlalchemy.exc import IntegrityError

from app.db.engine import transaction
from app.db.schema import (
    api_tokens,
    pack_tasks,
    sessions,
    share_links,
    user_files,
    user_storage_usage,
    user_tasks,
    users,
)


class DuplicateUserError(Exception):
    pass


class DuplicateCredentialError(Exception):
    pass


class AdminActorInvalidError(Exception):
    pass


class CannotMutateSelfError(Exception):
    pass


class LastAdminError(Exception):
    pass


class UsernamePasswordRequiredError(Exception):
    pass


class AdminMutationConflictError(Exception):
    pass


class QuotaBelowUsageError(Exception):
    pass


API_TOKEN_LAST_USED_WRITE_INTERVAL_MS = 5 * 60 * 1000


def now_ms() -> int:
    return int(time.time() * 1000)


async def has_any_user() -> bool:
    async with transaction() as conn:
        row = (
            await conn.execute(
                select(users.c.id).where(users.c.pending_delete == 0).limit(1)
            )
        ).first()
    return row is not None


async def count_admins() -> int:
    async with transaction() as conn:
        count = (
            await conn.execute(
                select(func.count()).select_from(users).where(
                    users.c.is_admin == 1,
                    users.c.pending_delete == 0,
                )
            )
        ).scalar_one()
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
        try:
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
        except IntegrityError:
            raise DuplicateUserError from None
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
        WHERE NOT EXISTS (SELECT 1 FROM users WHERE pending_delete = 0)
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
                        "is_admin": 1,
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
        row = (
            await conn.execute(
                select(users).where(
                    users.c.id == user_id,
                    users.c.pending_delete == 0,
                )
            )
        ).mappings().first()
    return dict(row) if row else None


async def get_user_by_id_any(user_id: int) -> dict[str, Any] | None:
    async with transaction() as conn:
        row = (
            await conn.execute(select(users).where(users.c.id == user_id))
        ).mappings().first()
    return dict(row) if row else None


async def get_user_by_username(username: str) -> dict[str, Any] | None:
    async with transaction() as conn:
        row = (
            await conn.execute(
                select(users).where(
                    users.c.username == username,
                    users.c.pending_delete == 0,
                )
            )
        ).mappings().first()
    return dict(row) if row else None


async def list_users_by_rpc_secret_digests(
    digest: str, *, limit: int = 2
) -> list[dict[str, Any]]:
    async with transaction() as conn:
        rows = (
            await conn.execute(
                select(users)
                .where(
                    users.c.rpc_secret_digest == digest,
                    users.c.pending_delete == 0,
                )
                .limit(limit)
            )
        ).mappings().all()
    return [dict(row) for row in rows]


async def list_users() -> list[dict[str, Any]]:
    async with transaction() as conn:
        rows = (
            await conn.execute(
                select(users)
                .where(users.c.pending_delete == 0)
                .order_by(users.c.id)
            )
        ).mappings().all()
    return [dict(row) for row in rows]


def _normalize_user_update_fields(fields: dict[str, Any]) -> dict[str, Any]:
    values = dict(fields)
    if "is_admin" in values:
        values["is_admin"] = 1 if values["is_admin"] else 0
    if "is_initial_password" in values:
        values["is_initial_password"] = 1 if values["is_initial_password"] else 0
    if "quota" in values:
        values["quota_bytes"] = values.pop("quota")
    if "rpc_secret_created_at" in values:
        values["rpc_secret_created_at_ms"] = values.pop("rpc_secret_created_at")
    return values


async def update_user(user_id: int, **fields: Any) -> dict[str, Any] | None:
    values = _normalize_user_update_fields(fields)
    if not values:
        return await get_user_by_id(user_id)

    values["updated_at_ms"] = now_ms()
    async with transaction() as conn:
        try:
            row = (
                await conn.execute(
                    update(users)
                    .where(
                        users.c.id == user_id,
                        users.c.pending_delete == 0,
                    )
                    .values(**values)
                    .returning(users)
                )
            ).mappings().first()
        except IntegrityError:
            raise DuplicateUserError from None
    return dict(row) if row else None


async def set_rpc_secret(
    user_id: int,
    digest: str | None,
    prefix: str | None,
    created_at_ms: int | None,
    *,
    require_enabled: bool = False,
) -> bool:
    values = {
        "rpc_secret_digest": digest,
        "rpc_secret_prefix": prefix,
        "rpc_secret_created_at_ms": created_at_ms,
        "updated_at_ms": now_ms(),
    }
    conditions = [users.c.id == user_id, users.c.pending_delete == 0]
    if require_enabled:
        conditions.append(users.c.rpc_secret_digest.is_not(None))
    async with transaction() as conn:
        try:
            row = (
                await conn.execute(
                    update(users).where(*conditions).values(**values).returning(users.c.id)
                )
            ).first()
        except IntegrityError:
            raise DuplicateCredentialError from None
    return row is not None


async def update_user_as_admin(
    *,
    actor_id: int,
    user_id: int,
    expected_username: str,
    **fields: Any,
) -> dict[str, Any] | None:
    values = _normalize_user_update_fields(fields)
    demoting = values.get("is_admin") == 0
    if (
        "username" in values
        and "password_hash" not in values
        and str(values["username"]).lower() != expected_username.lower()
    ):
        raise UsernamePasswordRequiredError

    actor = users.alias("admin_actor")
    conditions = [
        users.c.id == user_id,
        users.c.pending_delete == 0,
        users.c.username == expected_username,
        exists(
            select(actor.c.id).where(
                actor.c.id == actor_id,
                actor.c.is_admin == 1,
                actor.c.pending_delete == 0,
            )
        ),
    ]
    if "quota_bytes" in values:
        conditions.append(
            exists(
                select(user_storage_usage.c.user_id).where(
                    user_storage_usage.c.user_id == users.c.id,
                    user_storage_usage.c.used_bytes
                    + user_storage_usage.c.reserved_bytes
                    <= int(values["quota_bytes"]),
                )
            )
        )
    if demoting:
        other_admin = users.alias("other_admin")
        conditions.extend(
            [
                users.c.id != actor_id,
                or_(
                    users.c.is_admin == 0,
                    exists(
                        select(other_admin.c.id)
                        .where(
                            other_admin.c.is_admin == 1,
                            other_admin.c.pending_delete == 0,
                            other_admin.c.id != users.c.id,
                        )
                        .correlate(users)
                    ),
                ),
            ]
        )

    values["updated_at_ms"] = now_ms()
    async with transaction() as conn:
        try:
            row = (
                await conn.execute(
                    update(users)
                    .where(*conditions)
                    .values(**values)
                    .returning(users)
                )
            ).mappings().first()
        except IntegrityError:
            raise DuplicateUserError from None
        if row is not None:
            if "password_hash" in values:
                await conn.execute(delete(sessions).where(sessions.c.user_id == user_id))
            return dict(row)

        actor_row = (
            await conn.execute(
                select(users.c.is_admin, users.c.pending_delete).where(
                    users.c.id == actor_id
                )
            )
        ).first()
        if (
            actor_row is None
            or not bool(actor_row.is_admin)
            or bool(actor_row.pending_delete)
        ):
            raise AdminActorInvalidError
        target = (
            await conn.execute(select(users).where(users.c.id == user_id))
        ).mappings().first()
        if target is None:
            return None
        if "quota_bytes" in values:
            usage = (
                await conn.execute(
                    select(
                        user_storage_usage.c.used_bytes,
                        user_storage_usage.c.reserved_bytes,
                    ).where(user_storage_usage.c.user_id == user_id)
                )
            ).first()
            if usage is not None and int(usage.used_bytes) + int(
                usage.reserved_bytes
            ) > int(values["quota_bytes"]):
                raise QuotaBelowUsageError
        if demoting and actor_id == user_id:
            raise CannotMutateSelfError
        if demoting and bool(target["is_admin"]):
            other_admin = users.alias("remaining_admin")
            remaining = (
                await conn.execute(
                    select(other_admin.c.id).where(
                        other_admin.c.is_admin == 1,
                        other_admin.c.pending_delete == 0,
                        other_admin.c.id != user_id,
                    )
                )
            ).first()
            if remaining is None:
                raise LastAdminError
        raise AdminMutationConflictError


async def delete_user(user_id: int) -> bool:
    async with transaction() as conn:
        result = await conn.execute(
            delete(users).where(
                users.c.id == user_id,
                users.c.pending_delete == 1,
                ~exists(select(user_files.c.id).where(user_files.c.user_id == user_id)),
                ~exists(select(user_tasks.c.id).where(user_tasks.c.user_id == user_id)),
                ~exists(select(pack_tasks.c.id).where(pack_tasks.c.user_id == user_id)),
            )
        )
    return bool(result.rowcount)


async def delete_user_as_admin(
    *, actor_id: int, user_id: int
) -> dict[str, Any] | None:
    timestamp = now_ms()
    actor = users.alias("delete_actor")
    other_admin = users.alias("delete_other_admin")
    conditions = [
        users.c.id == user_id,
        users.c.pending_delete == 0,
        users.c.id != actor_id,
        exists(
            select(actor.c.id).where(
                actor.c.id == actor_id,
                actor.c.is_admin == 1,
                actor.c.pending_delete == 0,
            )
        ),
        or_(
            users.c.is_admin == 0,
            exists(
                select(other_admin.c.id)
                .where(
                    other_admin.c.is_admin == 1,
                    other_admin.c.pending_delete == 0,
                    other_admin.c.id != users.c.id,
                )
                .correlate(users)
            ),
        ),
    ]
    async with transaction() as conn:
        queued = (
            await conn.execute(
                update(users)
                .where(*conditions)
                .values(
                    pending_delete=1,
                    delete_attempts=0,
                    delete_next_retry_at_ms=timestamp,
                    delete_lease_token=None,
                    delete_lease_expires_at_ms=None,
                    delete_error=None,
                    rpc_secret_digest=None,
                    rpc_secret_prefix=None,
                    rpc_secret_created_at_ms=None,
                    updated_at_ms=timestamp,
                )
                .returning(users)
            )
        ).mappings().first()
        if queued is not None:
            await conn.execute(delete(sessions).where(sessions.c.user_id == user_id))
            await conn.execute(delete(api_tokens).where(api_tokens.c.user_id == user_id))
            await conn.execute(
                update(share_links)
                .where(share_links.c.owner_id == user_id)
                .values(status="revoked")
            )
            return dict(queued)

        actor_row = (
            await conn.execute(
                select(users.c.is_admin, users.c.pending_delete).where(
                    users.c.id == actor_id
                )
            )
        ).first()
        if actor_row is None or not bool(actor_row.is_admin) or bool(actor_row.pending_delete):
            raise AdminActorInvalidError
        target = (
            await conn.execute(select(users).where(users.c.id == user_id))
        ).mappings().first()
        if target is None:
            return None
        if actor_id == user_id:
            raise CannotMutateSelfError
        if bool(target["pending_delete"]):
            return dict(target)
        if bool(target["is_admin"]):
            remaining = (
                await conn.execute(
                    select(other_admin.c.id).where(
                        other_admin.c.is_admin == 1,
                        other_admin.c.pending_delete == 0,
                        other_admin.c.id != user_id,
                    )
                )
            ).first()
            if remaining is None:
                raise LastAdminError
        raise AdminMutationConflictError


async def create_session(session_id: str, user_id: int, expires_at_ms: int) -> str:
    timestamp = now_ms()
    async with transaction() as conn:
        result = await conn.execute(
            insert(sessions).from_select(
                ("id", "user_id", "expires_at_ms", "created_at_ms"),
                select(
                    literal(session_id),
                    users.c.id,
                    literal(expires_at_ms),
                    literal(timestamp),
                ).where(
                    users.c.id == user_id,
                    users.c.pending_delete == 0,
                ),
            )
        )
        if not result.rowcount:
            raise ValueError("用户不可用")
    return session_id


async def change_password_and_replace_session(
    *,
    user_id: int,
    expected_password_hash: str,
    new_password_hash: str,
    session_id: str,
    expires_at_ms: int,
) -> bool:
    timestamp = now_ms()
    async with transaction() as conn:
        changed = (
            await conn.execute(
                update(users)
                .where(
                    users.c.id == user_id,
                    users.c.password_hash == expected_password_hash,
                    users.c.pending_delete == 0,
                )
                .values(
                    password_hash=new_password_hash,
                    is_initial_password=0,
                    updated_at_ms=timestamp,
                )
                .returning(users.c.id)
            )
        ).first()
        if changed is None:
            return False
        await conn.execute(delete(sessions).where(sessions.c.user_id == user_id))
        await conn.execute(
            insert(sessions).values(
                id=session_id,
                user_id=user_id,
                expires_at_ms=expires_at_ms,
                created_at_ms=timestamp,
            )
        )
    return True


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
        .where(
            sessions.c.id == session_id,
            users.c.pending_delete == 0,
        )
    )
    async with transaction() as conn:
        row = (await conn.execute(stmt)).mappings().first()
    return dict(row) if row else None


async def list_api_tokens(user_id: int) -> list[dict[str, Any]]:
    async with transaction() as conn:
        rows = (
            await conn.execute(
                select(api_tokens)
                .where(
                    api_tokens.c.user_id == user_id,
                    exists(
                        select(users.c.id).where(
                            users.c.id == user_id,
                            users.c.pending_delete == 0,
                        )
                    ),
                )
                .order_by(api_tokens.c.created_at_ms.desc())
            )
        ).mappings().all()
    return [dict(row) for row in rows]


async def create_api_token(
    user_id: int, token_digest: str, token_prefix: str, name: str | None
) -> dict[str, Any]:
    timestamp = now_ms()
    async with transaction() as conn:
        try:
            row = (
                await conn.execute(
                    insert(api_tokens)
                    .from_select(
                        ("user_id", "token_digest", "token_prefix", "name", "created_at_ms"),
                        select(
                            users.c.id,
                            literal(token_digest),
                            literal(token_prefix),
                            literal(name),
                            literal(timestamp),
                        ).where(
                            users.c.id == user_id,
                            users.c.pending_delete == 0,
                        ),
                    )
                    .returning(api_tokens)
                )
            ).mappings().first()
        except IntegrityError:
            raise DuplicateCredentialError from None
        if row is None:
            raise ValueError("用户不可用")
    return dict(row)


async def use_api_token_digest(
    digest: str,
    *,
    timestamp_ms: int | None = None,
) -> dict[str, Any] | None:
    timestamp = now_ms() if timestamp_ms is None else timestamp_ms
    write_before = timestamp - API_TOKEN_LAST_USED_WRITE_INTERVAL_MS
    lookup = (
        select(
            users,
            api_tokens.c.id.label("api_token_id"),
            api_tokens.c.token_digest.label("api_token_digest"),
            api_tokens.c.last_used_at_ms.label("api_token_last_used_at_ms"),
        )
        .select_from(api_tokens.join(users, api_tokens.c.user_id == users.c.id))
        .where(api_tokens.c.token_digest == digest, users.c.pending_delete == 0)
    )
    async with transaction() as conn:
        rows = (await conn.execute(lookup.limit(2))).mappings().all()
        if len(rows) != 1:
            return None
        row = dict(rows[0])
        should_touch = (
            row["api_token_last_used_at_ms"] is None
            or int(row["api_token_last_used_at_ms"]) <= write_before
        )
        if should_touch:
            updated = (
                await conn.execute(
                    update(api_tokens)
                    .where(
                        api_tokens.c.id == row["api_token_id"],
                        api_tokens.c.token_digest == digest,
                    )
                    .values(last_used_at_ms=timestamp)
                    .returning(api_tokens.c.id)
                )
            ).first()
            if updated is None:
                row = dict(
                    (
                        await conn.execute(
                            lookup.where(api_tokens.c.id == row["api_token_id"])
                        )
                    ).mappings().first()
                    or {}
                )
                if not row:
                    return None
        return row


async def delete_api_token(user_id: int, token_id: int) -> bool:
    async with transaction() as conn:
        result = await conn.execute(
            delete(api_tokens).where(
                api_tokens.c.id == token_id,
                api_tokens.c.user_id == user_id,
            )
        )
    return bool(result.rowcount)


async def invalidate_all_credential_digests() -> dict[str, int]:
    """Delete all API token digests and clear every user RPC secret digest.

    Used after rotating ARIA2DECK_CREDENTIAL_PEPPER so stale digests cannot be
    mistaken for still-valid credentials.
    """
    async with transaction() as conn:
        token_count = int(
            (
                await conn.execute(select(func.count()).select_from(api_tokens))
            ).scalar_one()
        )
        rpc_count = int(
            (
                await conn.execute(
                    select(func.count())
                    .select_from(users)
                    .where(users.c.rpc_secret_digest.is_not(None))
                )
            ).scalar_one()
        )
        if token_count:
            await conn.execute(delete(api_tokens))
        if rpc_count:
            await conn.execute(
                update(users)
                .where(users.c.rpc_secret_digest.is_not(None))
                .values(
                    rpc_secret_digest=None,
                    rpc_secret_prefix=None,
                    rpc_secret_created_at_ms=None,
                    updated_at_ms=now_ms(),
                )
            )
    return {
        "api_token_count": token_count,
        "rpc_secret_count": rpc_count,
    }


async def list_pending_user_ids() -> list[int]:
    async with transaction() as conn:
        rows = (
            await conn.execute(
                select(users.c.id)
                .where(users.c.pending_delete == 1)
                .order_by(users.c.id)
            )
        ).all()
    return [int(row[0]) for row in rows]


async def claim_due_users(
    *,
    lease_token: str,
    timestamp_ms: int,
    lease_expires_at_ms: int,
    limit: int,
) -> list[dict[str, Any]]:
    due = (
        users.c.pending_delete == 1,
        or_(
            users.c.delete_next_retry_at_ms.is_(None),
            users.c.delete_next_retry_at_ms <= timestamp_ms,
        ),
        or_(
            users.c.delete_lease_expires_at_ms.is_(None),
            users.c.delete_lease_expires_at_ms <= timestamp_ms,
        ),
    )
    async with transaction() as conn:
        ids = [
            int(row[0])
            for row in (
                await conn.execute(
                    select(users.c.id).where(*due).order_by(users.c.id).limit(limit)
                )
            ).all()
        ]
        if not ids:
            return []
        rows = (
            await conn.execute(
                update(users)
                .where(users.c.id.in_(ids), *due)
                .values(
                    delete_attempts=users.c.delete_attempts + 1,
                    delete_lease_token=lease_token,
                    delete_lease_expires_at_ms=lease_expires_at_ms,
                )
                .returning(users)
            )
        ).mappings().all()
    return [dict(row) for row in rows]


async def retry_claimed_user_delete(
    *,
    user_id: int,
    lease_token: str,
    next_retry_at_ms: int,
    error: str | None,
) -> bool:
    async with transaction() as conn:
        row = (
            await conn.execute(
                update(users)
                .where(
                    users.c.id == user_id,
                    users.c.pending_delete == 1,
                    users.c.delete_lease_token == lease_token,
                )
                .values(
                    delete_next_retry_at_ms=next_retry_at_ms,
                    delete_lease_token=None,
                    delete_lease_expires_at_ms=None,
                    delete_error=error[:1000] if error else None,
                )
                .returning(users.c.id)
            )
        ).first()
    return row is not None


async def renew_claimed_user_delete(
    *, user_id: int, lease_token: str, lease_expires_at_ms: int
) -> bool:
    async with transaction() as conn:
        row = (
            await conn.execute(
                update(users)
                .where(
                    users.c.id == user_id,
                    users.c.pending_delete == 1,
                    users.c.delete_lease_token == lease_token,
                )
                .values(delete_lease_expires_at_ms=lease_expires_at_ms)
                .returning(users.c.id)
            )
        ).first()
    return row is not None


async def delete_terminal_user_tasks_for_cleanup(user_id: int) -> bool:
    active_statuses = ("queued", "active", "waiting", "paused")
    async with transaction() as conn:
        active = (
            await conn.execute(
                select(user_tasks.c.id)
                .where(
                    user_tasks.c.user_id == user_id,
                    user_tasks.c.status.in_(active_statuses),
                )
                .limit(1)
            )
        ).first()
        if active is not None:
            return False
        await conn.execute(delete(user_tasks).where(user_tasks.c.user_id == user_id))
        return True


async def hard_delete_claimed_user(user_id: int, lease_token: str) -> bool:
    async with transaction() as conn:
        await conn.execute(delete(sessions).where(sessions.c.user_id == user_id))
        await conn.execute(delete(api_tokens).where(api_tokens.c.user_id == user_id))
        await conn.execute(delete(share_links).where(share_links.c.owner_id == user_id))
        row = (
            await conn.execute(
                delete(users)
                .where(
                    users.c.id == user_id,
                    users.c.pending_delete == 1,
                    users.c.delete_lease_token == lease_token,
                    ~exists(
                        select(user_files.c.id).where(user_files.c.user_id == user_id)
                    ),
                    ~exists(
                        select(user_tasks.c.id).where(user_tasks.c.user_id == user_id)
                    ),
                    ~exists(
                        select(pack_tasks.c.id).where(pack_tasks.c.user_id == user_id)
                    ),
                )
                .returning(users.c.id)
            )
        ).first()
    return row is not None
