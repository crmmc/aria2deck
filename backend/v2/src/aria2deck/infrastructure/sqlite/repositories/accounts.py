from __future__ import annotations

from typing import cast

from sqlalchemy import delete, func, select, update
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from ....domain.accounts import ApiToken, Session, User, validate_new_user
from ....domain.errors import DomainError, ErrorCode
from ....domain.quota import Quota
from ..schema import api_tokens, quota_accounts, sessions, users


class AccountRepository:
    def __init__(self, connection: AsyncConnection) -> None:
        self.connection = connection

    async def create_user(
        self,
        *,
        username: str,
        password_hash: str,
        is_admin: bool,
        quota_bytes: int | None,
        is_initial_password: bool = False,
        now_ms: int,
    ) -> int:
        existing = int((await self.connection.execute(select(func.count(users.c.id)))).scalar_one())
        new_user = validate_new_user(username, quota_bytes, is_admin, existing)
        try:
            result = await self.connection.execute(
                users.insert().values(
                    username=new_user.username,
                    username_key=new_user.username_key,
                    password_hash=password_hash,
                    is_admin=new_user.is_admin,
                    quota_bytes=new_user.quota_bytes,
                    is_initial_password=is_initial_password,
                    created_at_ms=now_ms,
                    updated_at_ms=now_ms,
                )
            )
            user_id = int(result.inserted_primary_key[0])
            await self.connection.execute(
                quota_accounts.insert().values(user_id=user_id, updated_at_ms=now_ms)
            )
        except IntegrityError:
            raise DomainError(ErrorCode.DUPLICATE_RESOURCE, "用户名已存在") from None
        return user_id

    @staticmethod
    def _user(row: RowMapping) -> User:
        return User(
            id=int(cast(int, row["id"])),
            username=str(row["username"]),
            username_key=str(row["username_key"]),
            password_hash=str(row["password_hash"]),
            is_admin=bool(row["is_admin"]),
            quota_bytes=int(cast(int, row["quota_bytes"])),
            is_initial_password=bool(row["is_initial_password"]),
            pending_delete=bool(row["pending_delete"]),
            credential_generation=int(cast(int, row["credential_generation"])),
            created_at_ms=int(cast(int, row["created_at_ms"])),
            updated_at_ms=int(cast(int, row["updated_at_ms"])),
            rpc_secret_ciphertext=cast(str | None, row["rpc_secret_ciphertext"]),
            rpc_secret_prefix=cast(str | None, row["rpc_secret_prefix"]),
        )

    async def count_users(self) -> int:
        return int((await self.connection.execute(select(func.count(users.c.id)))).scalar_one())

    async def count_admins(self) -> int:
        result = await self.connection.execute(
            select(func.count(users.c.id)).where(users.c.is_admin.is_(True))
        )
        return int(result.scalar_one())

    async def get_user(self, user_id: int) -> User:
        row = (await self.connection.execute(select(users).where(users.c.id == user_id))).mappings().first()
        if row is None:
            raise DomainError(ErrorCode.NOT_FOUND, "用户不存在")
        return self._user(row)

    async def find_user_by_username(self, username: str) -> User | None:
        row = (
            await self.connection.execute(select(users).where(users.c.username_key == username.casefold()))
        ).mappings().first()
        return self._user(row) if row is not None else None

    async def list_users(self, *, offset: int, limit: int) -> list[User]:
        result = await self.connection.execute(
            select(users).order_by(users.c.id).offset(offset).limit(limit)
        )
        return [self._user(row) for row in result.mappings().all()]

    async def get_quota(self, user_id: int) -> Quota:
        row = (
            await self.connection.execute(
                select(
                    users.c.quota_bytes,
                    quota_accounts.c.used_bytes,
                    quota_accounts.c.reserved_bytes,
                ).select_from(
                    users.join(quota_accounts, users.c.id == quota_accounts.c.user_id)
                ).where(users.c.id == user_id)
            )
        ).first()
        if row is None:
            raise DomainError(ErrorCode.NOT_FOUND, "用户配额账目不存在")
        return Quota(int(row.quota_bytes), int(row.used_bytes), int(row.reserved_bytes))


    async def update_user(
        self,
        *,
        user_id: int,
        username: str,
        username_key: str,
        password_hash: str,
        is_admin: bool,
        quota_bytes: int,
        is_initial_password: bool,
        now_ms: int,
    ) -> None:
        try:
            result = await self.connection.execute(
                update(users)
                .where(users.c.id == user_id)
                .values(
                    username=username,
                    username_key=username_key,
                    password_hash=password_hash,
                    is_admin=is_admin,
                    quota_bytes=quota_bytes,
                    is_initial_password=is_initial_password,
                    updated_at_ms=now_ms,
                )
            )
        except IntegrityError:
            raise DomainError(ErrorCode.DUPLICATE_RESOURCE, "用户名已存在") from None
        if result.rowcount != 1:
            raise DomainError(ErrorCode.NOT_FOUND, "用户不存在")

    async def change_password(
        self,
        *,
        user_id: int,
        expected_generation: int,
        password_hash: str,
        is_initial_password: bool,
        now_ms: int,
    ) -> bool:
        result = await self.connection.execute(
            update(users)
            .where(users.c.id == user_id)
            .where(users.c.credential_generation == expected_generation)
            .values(
                password_hash=password_hash,
                is_initial_password=is_initial_password,
                credential_generation=users.c.credential_generation + 1,
                updated_at_ms=now_ms,
            )
        )
        return result.rowcount == 1

    async def mark_pending_delete(self, *, user_id: int, now_ms: int) -> None:
        result = await self.connection.execute(
            update(users)
            .where(users.c.id == user_id)
            .values(
                pending_delete=True,
                rpc_secret_ciphertext=None,
                rpc_secret_prefix=None,
                updated_at_ms=now_ms,
            )
        )
        if result.rowcount != 1:
            raise DomainError(ErrorCode.NOT_FOUND, "用户不存在")
        await self.connection.execute(delete(api_tokens).where(api_tokens.c.user_id == user_id))
        await self.connection.execute(
            update(sessions)
            .where(sessions.c.user_id == user_id)
            .where(sessions.c.revoked_at_ms.is_(None))
            .values(revoked_at_ms=now_ms)
        )

    async def list_pending_users(self) -> list[int]:
        result = await self.connection.execute(
            select(users.c.id).where(users.c.pending_delete.is_(True)).order_by(users.c.id).limit(16)
        )
        return [int(value) for value in result.scalars().all()]

    async def hard_delete_user(self, user_id: int) -> bool:
        result = await self.connection.execute(
            delete(users).where(users.c.id == user_id).where(users.c.pending_delete.is_(True))
        )
        return result.rowcount == 1


    async def create_session(
        self,
        *,
        session_id: str,
        user_id: int,
        credential_generation: int,
        now_ms: int,
        expires_at_ms: int,
    ) -> None:
        await self.connection.execute(
            sessions.insert().values(
                id=session_id,
                user_id=user_id,
                credential_generation=credential_generation,
                created_at_ms=now_ms,
                expires_at_ms=expires_at_ms,
            )
        )

    async def get_session_user(self, session_id: str) -> tuple[Session, User] | None:
        row = (
            await self.connection.execute(
                select(
                    sessions.c.id.label("session_id"),
                    sessions.c.user_id.label("session_user_id"),
                    sessions.c.credential_generation.label("session_generation"),
                    sessions.c.created_at_ms.label("session_created_at_ms"),
                    sessions.c.expires_at_ms,
                    sessions.c.revoked_at_ms,
                    users,
                )
                .select_from(sessions.join(users, sessions.c.user_id == users.c.id))
                .where(sessions.c.id == session_id)
            )
        ).mappings().first()
        if row is None:
            return None
        session = Session(
            id=str(row["session_id"]),
            user_id=int(cast(int, row["session_user_id"])),
            credential_generation=int(cast(int, row["session_generation"])),
            created_at_ms=int(cast(int, row["session_created_at_ms"])),
            expires_at_ms=int(cast(int, row["expires_at_ms"])),
            revoked_at_ms=cast(int | None, row["revoked_at_ms"]),
        )
        return session, self._user(row)

    async def revoke_session(self, *, session_id: str, now_ms: int) -> None:
        await self.connection.execute(
            update(sessions)
            .where(sessions.c.id == session_id)
            .where(sessions.c.revoked_at_ms.is_(None))
            .values(revoked_at_ms=now_ms)
        )

    async def revoke_user_sessions(self, *, user_id: int, now_ms: int) -> None:
        await self.connection.execute(
            update(sessions)
            .where(sessions.c.user_id == user_id)
            .where(sessions.c.revoked_at_ms.is_(None))
            .values(revoked_at_ms=now_ms)
        )


    @staticmethod
    def _token(row: RowMapping) -> ApiToken:
        return ApiToken(
            id=int(cast(int, row["id"])),
            user_id=int(cast(int, row["user_id"])),
            name=str(row["name"]),
            prefix=str(row["prefix"]),
            created_at_ms=int(cast(int, row["created_at_ms"])),
            last_used_at_ms=cast(int | None, row["last_used_at_ms"]),
        )

    async def create_api_token(
        self,
        *,
        user_id: int,
        name: str,
        token_digest: str,
        prefix: str,
        now_ms: int,
    ) -> int:
        result = await self.connection.execute(
            api_tokens.insert().values(
                user_id=user_id,
                name=name,
                token_digest=token_digest,
                prefix=prefix,
                created_at_ms=now_ms,
            )
        )
        return int(result.inserted_primary_key[0])

    async def get_api_token_user(self, token_digest: str) -> tuple[ApiToken, User] | None:
        row = (
            await self.connection.execute(
                select(
                    api_tokens.c.id.label("token_id"),
                    api_tokens.c.user_id.label("token_user_id"),
                    api_tokens.c.name.label("token_name"),
                    api_tokens.c.prefix.label("token_prefix"),
                    api_tokens.c.created_at_ms.label("token_created_at_ms"),
                    api_tokens.c.last_used_at_ms,
                    users,
                )
                .select_from(api_tokens.join(users, api_tokens.c.user_id == users.c.id))
                .where(api_tokens.c.token_digest == token_digest)
            )
        ).mappings().first()
        if row is None:
            return None
        token = ApiToken(
            id=int(cast(int, row["token_id"])),
            user_id=int(cast(int, row["token_user_id"])),
            name=str(row["token_name"]),
            prefix=str(row["token_prefix"]),
            created_at_ms=int(cast(int, row["token_created_at_ms"])),
            last_used_at_ms=cast(int | None, row["last_used_at_ms"]),
        )
        return token, self._user(row)

    async def list_api_tokens(self, *, user_id: int, offset: int, limit: int) -> list[ApiToken]:
        result = await self.connection.execute(
            select(api_tokens)
            .where(api_tokens.c.user_id == user_id)
            .order_by(api_tokens.c.id)
            .offset(offset)
            .limit(limit)
        )
        return [self._token(row) for row in result.mappings().all()]

    async def count_api_tokens(self, user_id: int) -> int:
        result = await self.connection.execute(
            select(func.count(api_tokens.c.id)).where(api_tokens.c.user_id == user_id)
        )
        return int(result.scalar_one())

    async def touch_api_token(self, *, token_id: int, now_ms: int) -> None:
        await self.connection.execute(
            update(api_tokens)
            .where(api_tokens.c.id == token_id)
            .values(last_used_at_ms=now_ms)
        )

    async def delete_api_token(self, *, token_id: int, user_id: int) -> None:
        result = await self.connection.execute(
            delete(api_tokens)
            .where(api_tokens.c.id == token_id)
            .where(api_tokens.c.user_id == user_id)
        )
        if result.rowcount != 1:
            raise DomainError(ErrorCode.NOT_FOUND, "Token 不存在")

    async def set_rpc_secret(
        self,
        *,
        user_id: int,
        ciphertext: str | None,
        prefix: str | None,
        now_ms: int,
    ) -> None:
        result = await self.connection.execute(
            update(users)
            .where(users.c.id == user_id)
            .values(
                rpc_secret_ciphertext=ciphertext,
                rpc_secret_prefix=prefix,
                updated_at_ms=now_ms,
            )
        )
        if result.rowcount != 1:
            raise DomainError(ErrorCode.NOT_FOUND, "用户不存在")

    async def invalidate_credentials(self) -> tuple[int, int]:
        token_count = int(
            (await self.connection.execute(select(func.count(api_tokens.c.id)))).scalar_one()
        )
        rpc_count = int(
            (
                await self.connection.execute(
                    select(func.count(users.c.id)).where(users.c.rpc_secret_ciphertext.is_not(None))
                )
            ).scalar_one()
        )
        await self.connection.execute(delete(api_tokens))
        await self.connection.execute(
            update(users).values(rpc_secret_ciphertext=None, rpc_secret_prefix=None)
        )
        return token_count, rpc_count
