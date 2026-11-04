from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.engine import Row
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from ....domain.errors import DomainError, ErrorCode
from ....domain.shares import (
    Share,
    ShareStatus,
    ensure_share_capacity,
    share_status,
)
from ..schema import content_objects, share_links, user_files


@dataclass(frozen=True, slots=True)
class ShareRecord:
    id: int
    code: str
    owner_id: int
    user_file_id: int | None
    file_name: str
    file_size: int
    file_kind: str
    content_key: str
    password_hash: str | None
    expires_at_ms: int | None
    revoked_at_ms: int | None
    max_downloads: int | None
    download_count: int
    file_pending_delete: bool

    def as_domain(self) -> Share:
        return Share(
            expires_at_ms=self.expires_at_ms,
            revoked_at_ms=self.revoked_at_ms,
            max_downloads=self.max_downloads,
            download_count=self.download_count,
            file_pending_delete=self.file_pending_delete,
        )


class ShareRepository:
    def __init__(self, connection: AsyncConnection) -> None:
        self.connection = connection

    def _record(self, row: Row[Any]) -> ShareRecord:
        record = row._mapping
        return ShareRecord(
            id=int(record["id"]),
            code=str(record["code"]),
            owner_id=int(record["owner_id"]),
            user_file_id=(
                None if record["user_file_id"] is None else int(record["user_file_id"])
            ),
            file_name=str(record["file_name"]),
            file_size=int(record["file_size"]),
            file_kind=str(record["file_kind"]),
            content_key=str(record["content_key"]),
            password_hash=record["password_hash"],
            expires_at_ms=record["expires_at_ms"],
            revoked_at_ms=record["revoked_at_ms"],
            max_downloads=record["max_downloads"],
            download_count=int(record["download_count"]),
            file_pending_delete=record["deletion_state"] != "active",
        )

    async def get_share(self, share_id: int) -> ShareRecord:
        row = (
            await self.connection.execute(
                select(
                    share_links,
                    content_objects.c.deletion_state,
                )
                .select_from(
                    share_links.outerjoin(
                        user_files, user_files.c.id == share_links.c.user_file_id
                    ).outerjoin(content_objects, content_objects.c.id == user_files.c.content_id)
                )
                .where(share_links.c.id == share_id)
            )
        ).first()
        if row is None:
            raise DomainError(ErrorCode.NOT_FOUND, "分享不存在")
        return self._record(row)

    async def create_share(
        self,
        *,
        user_id: int,
        user_file_id: int,
        code: str,
        password_hash: str | None,
        expires_at_ms: int | None,
        max_downloads: int | None,
        now_ms: int,
    ) -> int:
        owned = (
            await self.connection.execute(
                select(
                    user_files.c.display_name,
                    content_objects.c.size,
                    content_objects.c.kind,
                    content_objects.c.content_key,
                )
                .select_from(
                    user_files.join(
                        content_objects, content_objects.c.id == user_files.c.content_id
                    )
                )
                .where(user_files.c.id == user_file_id)
                .where(user_files.c.user_id == user_id)
            )
        ).first()
        if owned is None:
            raise DomainError(ErrorCode.NOT_FOUND, "用户文件不存在")
        rows = (
            await self.connection.execute(
                select(
                    share_links.c.expires_at_ms,
                    share_links.c.revoked_at_ms,
                    share_links.c.max_downloads,
                    share_links.c.download_count,
                    content_objects.c.deletion_state,
                )
                .select_from(
                    share_links.join(user_files, user_files.c.id == share_links.c.user_file_id)
                    .join(content_objects, content_objects.c.id == user_files.c.content_id)
                )
                .where(share_links.c.user_file_id == user_file_id)
            )
        ).all()
        active_count = sum(
            share_status(
                Share(
                    expires_at_ms=row.expires_at_ms,
                    revoked_at_ms=row.revoked_at_ms,
                    max_downloads=row.max_downloads,
                    download_count=int(row.download_count),
                    file_pending_delete=row.deletion_state != "active",
                ),
                now_ms,
            )
            is ShareStatus.ACTIVE
            for row in rows
        )
        ensure_share_capacity(active_count)
        try:
            result = await self.connection.execute(
                share_links.insert().values(
                    code=code,
                    owner_id=user_id,
                    user_file_id=user_file_id,
                    file_name=owned.display_name,
                    file_size=owned.size,
                    file_kind=owned.kind,
                    content_key=owned.content_key,
                    password_hash=password_hash,
                    expires_at_ms=expires_at_ms,
                    max_downloads=max_downloads,
                    created_at_ms=now_ms,
                )
            )
        except IntegrityError:
            raise DomainError(ErrorCode.DUPLICATE_RESOURCE, "分享码已存在或文件引用无效") from None
        return int(result.inserted_primary_key[0])

    async def list_shares(self, owner_id: int) -> list[ShareRecord]:
        rows = (
            await self.connection.execute(
                select(share_links, content_objects.c.deletion_state)
                .select_from(
                    share_links.outerjoin(
                        user_files, user_files.c.id == share_links.c.user_file_id
                    ).outerjoin(content_objects, content_objects.c.id == user_files.c.content_id)
                )
                .where(share_links.c.owner_id == owner_id)
                .order_by(share_links.c.id)
            )
        ).all()
        return [self._record(row) for row in rows]

    async def increment_download_count(self, share_id: int, *, expected_count: int) -> None:
        result = await self.connection.execute(
            update(share_links)
            .where(share_links.c.id == share_id)
            .where(share_links.c.download_count == expected_count)
            .where(
                (share_links.c.max_downloads.is_(None))
                | (share_links.c.download_count < share_links.c.max_downloads)
            )
            .values(download_count=share_links.c.download_count + 1)
        )
        if result.rowcount == 1:
            return
        current = await self.get_share(share_id)
        if current.max_downloads is not None and current.download_count >= current.max_downloads:
            raise DomainError(ErrorCode.SHARE_EXHAUSTED, "分享下载次数已用尽")
        raise DomainError(ErrorCode.INVALID_STATE, "分享计数已变化，请重试")
