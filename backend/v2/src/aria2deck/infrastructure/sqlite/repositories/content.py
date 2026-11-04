from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from ....domain.content import (
    ContentDeletionState,
    ContentKind,
    content_identity,
    join_relative_path,
    normalize_relative_path,
    validate_display_name,
)
from ....domain.errors import DomainError, ErrorCode
from ..schema import content_entries, content_objects, pack_sources, user_files, users
from .quota import QuotaRepository


@dataclass(frozen=True, slots=True)
class ContentRecord:
    id: int
    content_key: str
    kind: ContentKind
    size: int
    storage_path: str
    digest: str
    deletion_state: ContentDeletionState


class ContentRepository:
    def __init__(self, connection: AsyncConnection) -> None:
        self.connection = connection

    async def create_object(
        self,
        *,
        kind: str,
        digest: str,
        size: int,
        storage_path: str,
        now_ms: int,
    ) -> int:
        try:
            content_kind = ContentKind(kind)
        except ValueError:
            raise DomainError(ErrorCode.INVALID_INPUT, "内容对象类型无效") from None
        identity = content_identity(content_kind, digest)
        if size < 0:
            raise DomainError(ErrorCode.INVALID_INPUT, "内容大小不能为负数")
        path = normalize_relative_path(storage_path)
        try:
            result = await self.connection.execute(
                content_objects.insert().values(
                    content_key=identity.key,
                    kind=identity.kind,
                    digest=identity.digest,
                    size=size,
                    storage_path=path,
                    created_at_ms=now_ms,
                    updated_at_ms=now_ms,
                )
            )
        except IntegrityError:
            raise DomainError(ErrorCode.DUPLICATE_RESOURCE, "内容身份或存储路径已存在") from None
        return int(result.inserted_primary_key[0])

    async def get_object(self, content_id: int) -> ContentRecord:
        row = (
            await self.connection.execute(
                select(content_objects).where(content_objects.c.id == content_id)
            )
        ).first()
        if row is None:
            raise DomainError(ErrorCode.NOT_FOUND, "内容对象不存在")
        return ContentRecord(
            id=int(row.id),
            content_key=str(row.content_key),
            kind=ContentKind(row.kind),
            size=int(row.size),
            storage_path=str(row.storage_path),
            digest=str(row.digest),
            deletion_state=ContentDeletionState(row.deletion_state),
        )

    async def find_object_by_key(self, content_key: str) -> ContentRecord | None:
        row = (
            await self.connection.execute(
                select(content_objects).where(content_objects.c.content_key == content_key)
            )
        ).first()
        return None if row is None else await self.get_object(int(row.id))

    async def add_user_file(
        self,
        *,
        user_id: int,
        content_id: int,
        display_name: str,
        now_ms: int,
    ) -> int:
        name = validate_display_name(display_name)
        content = await self.get_object(content_id)
        if content.deletion_state is not ContentDeletionState.ACTIVE:
            raise DomainError(ErrorCode.CONTENT_PENDING_DELETE, "内容正在删除，不能建立新引用")
        try:
            result = await self.connection.execute(
                user_files.insert().values(
                    user_id=user_id,
                    content_id=content_id,
                    display_name=name,
                    created_at_ms=now_ms,
                )
            )
        except IntegrityError:
            raise DomainError(ErrorCode.DUPLICATE_RESOURCE, "用户文件引用或文件名已存在") from None
        return int(result.inserted_primary_key[0])

    async def find_user_file(self, *, user_id: int, content_id: int) -> int | None:
        value = (
            await self.connection.execute(
                select(user_files.c.id)
                .where(user_files.c.user_id == user_id)
                .where(user_files.c.content_id == content_id)
            )
        ).scalar_one_or_none()
        return None if value is None else int(value)

    async def add_entry(
        self,
        *,
        content_id: int,
        parent_path: str,
        name: str,
        entry_kind: str,
        size: int,
        now_ms: int,
    ) -> int:
        parent = normalize_relative_path(parent_path, allow_root=True)
        entry_name = validate_display_name(name)
        try:
            kind = ContentKind(entry_kind)
        except ValueError:
            raise DomainError(ErrorCode.INVALID_INPUT, "目录条目类型无效") from None
        if size < 0:
            raise DomainError(ErrorCode.INVALID_INPUT, "目录条目大小不能为负数")
        try:
            result = await self.connection.execute(
                content_entries.insert().values(
                    content_id=content_id,
                    parent_path=parent,
                    name=entry_name,
                    path=join_relative_path(parent, entry_name),
                    entry_kind=kind,
                    size=size,
                    modified_at_ms=now_ms,
                )
            )
        except IntegrityError:
            raise DomainError(ErrorCode.DUPLICATE_RESOURCE, "目录条目路径已存在") from None
        return int(result.inserted_primary_key[0])

    async def claim_orphan_delete(self, content_id: int, *, now_ms: int) -> bool:
        result = await self.connection.execute(
            update(content_objects)
            .where(content_objects.c.id == content_id)
            .where(content_objects.c.deletion_state == ContentDeletionState.ACTIVE)
            .where(~select(user_files.c.id).where(user_files.c.content_id == content_id).exists())
            .where(
                ~select(pack_sources.c.id)
                .where(pack_sources.c.source_content_id == content_id)
                .where(pack_sources.c.cleanup_status == "pending")
                .exists()
            )
            .values(deletion_state=ContentDeletionState.PENDING_DELETE, updated_at_ms=now_ms)
        )
        return result.rowcount == 1


    async def cleanup_user(self, *, user_id: int, now_ms: int) -> None:
        if (
            await self.connection.execute(select(users.c.id).where(users.c.id == user_id))
        ).first() is None:
            return
        content_ids = (
            await self.connection.execute(
                select(user_files.c.content_id).where(user_files.c.user_id == user_id)
            )
        ).scalars().all()
        await self.connection.execute(delete(user_files).where(user_files.c.user_id == user_id))
        for content_id in content_ids:
            await self.claim_orphan_delete(int(content_id), now_ms=now_ms)
        await QuotaRepository(self.connection).rebuild(user_id)
