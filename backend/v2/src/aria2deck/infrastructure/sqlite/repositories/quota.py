from __future__ import annotations

from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from ....domain.downloads import LIVE_SUBSCRIPTION_STATUSES
from ....domain.errors import DomainError, ErrorCode
from ....domain.packs import PackStatus
from ....domain.quota import Quota, complete_download, rebuild_quota, release_reservation
from ..schema import (
    content_objects,
    pack_jobs,
    quota_accounts,
    task_subscriptions,
    user_files,
    users,
)


class QuotaRepository:
    def __init__(self, connection: AsyncConnection) -> None:
        self.connection = connection

    async def get(self, user_id: int) -> Quota:
        row = (
            await self.connection.execute(
                select(
                    users.c.quota_bytes,
                    quota_accounts.c.used_bytes,
                    quota_accounts.c.reserved_bytes,
                )
                .select_from(quota_accounts.join(users, users.c.id == quota_accounts.c.user_id))
                .where(quota_accounts.c.user_id == user_id)
            )
        ).first()
        if row is None:
            raise DomainError(ErrorCode.NOT_FOUND, "用户配额账目不存在")
        return Quota(int(row.quota_bytes), int(row.used_bytes), int(row.reserved_bytes))

    async def reserve(self, user_id: int, amount: int) -> Quota:
        if amount < 0:
            raise DomainError(ErrorCode.INVALID_INPUT, "预留空间不能为负数")
        result = await self.connection.execute(
            update(quota_accounts)
            .where(quota_accounts.c.user_id == user_id)
            .where(
                quota_accounts.c.used_bytes + quota_accounts.c.reserved_bytes + amount
                <= select(users.c.quota_bytes).where(users.c.id == user_id).scalar_subquery()
            )
            .values(
                reserved_bytes=quota_accounts.c.reserved_bytes + amount,
                revision=quota_accounts.c.revision + 1,
            )
        )
        if result.rowcount != 1:
            await self.get(user_id)
            raise DomainError(ErrorCode.QUOTA_EXCEEDED, "用户配额不足")
        return await self.get(user_id)

    async def release(self, user_id: int, amount: int) -> Quota:
        current = await self.get(user_id)
        target = release_reservation(current, amount)
        result = await self.connection.execute(
            update(quota_accounts)
            .where(quota_accounts.c.user_id == user_id)
            .where(quota_accounts.c.reserved_bytes == current.reserved_bytes)
            .values(reserved_bytes=target.reserved_bytes, revision=quota_accounts.c.revision + 1)
        )
        if result.rowcount != 1:
            raise DomainError(ErrorCode.INVALID_STATE, "配额账目已变化，请重试")
        return target

    async def complete_download(
        self,
        user_id: int,
        reserved_bytes: int,
        *,
        content_size: int,
        already_owned: bool,
    ) -> Quota:
        current = await self.get(user_id)
        target = complete_download(
            current,
            reserved_bytes,
            content_size=content_size,
            already_owned=already_owned,
        )
        result = await self.connection.execute(
            update(quota_accounts)
            .where(quota_accounts.c.user_id == user_id)
            .where(quota_accounts.c.used_bytes == current.used_bytes)
            .where(quota_accounts.c.reserved_bytes == current.reserved_bytes)
            .values(
                used_bytes=target.used_bytes,
                reserved_bytes=target.reserved_bytes,
                revision=quota_accounts.c.revision + 1,
            )
        )
        if result.rowcount != 1:
            raise DomainError(ErrorCode.INVALID_STATE, "配额账目已变化，请重试")
        return target

    async def rebuild(self, user_id: int) -> Quota:
        row = (
            await self.connection.execute(select(users.c.quota_bytes).where(users.c.id == user_id))
        ).first()
        if row is None:
            raise DomainError(ErrorCode.NOT_FOUND, "用户不存在")
        quota_bytes = int(row.quota_bytes)
        used = int(
            (
                await self.connection.execute(
                    select(func.coalesce(func.sum(content_objects.c.size), 0))
                    .select_from(user_files.join(content_objects, content_objects.c.id == user_files.c.content_id))
                    .where(user_files.c.user_id == user_id)
                )
            ).scalar_one()
        )
        download_reserved = int(
            (
                await self.connection.execute(
                    select(func.coalesce(func.sum(task_subscriptions.c.reserved_bytes), 0))
                    .where(task_subscriptions.c.user_id == user_id)
                    .where(task_subscriptions.c.status.in_(LIVE_SUBSCRIPTION_STATUSES))
                )
            ).scalar_one()
        )
        pack_reserved = int(
            (
                await self.connection.execute(
                    select(func.coalesce(func.sum(pack_jobs.c.reserved_bytes), 0))
                    .where(pack_jobs.c.owner_id == user_id)
                    .where(pack_jobs.c.status.in_([PackStatus.PENDING, PackStatus.PACKING]))
                )
            ).scalar_one()
        )
        target = rebuild_quota(quota_bytes, [used], [download_reserved], [pack_reserved])
        statement = sqlite_insert(quota_accounts).values(
            user_id=user_id,
            used_bytes=target.used_bytes,
            reserved_bytes=target.reserved_bytes,
        )
        await self.connection.execute(
            statement.on_conflict_do_update(
                index_elements=[quota_accounts.c.user_id],
                set_={
                    "used_bytes": target.used_bytes,
                    "reserved_bytes": target.reserved_bytes,
                    "revision": quota_accounts.c.revision + 1,
                },
            )
        )
        return target
