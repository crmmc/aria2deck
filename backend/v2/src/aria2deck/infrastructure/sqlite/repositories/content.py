from __future__ import annotations

from typing import Any

from sqlalchemy import ColumnElement, Row, and_, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from ....application.library.models import (
    ContentEntryRecord,
    ContentRecord,
    DeletionCandidate,
    EntryDiff,
    StorageObjectRecord,
    StorageReferencingUser,
    UserFileRecord,
    UserFileSearchHit,
)
from ....domain.content import (
    ContentDeletionState,
    ContentEntryIdentity,
    ContentKind,
    content_identity,
    join_relative_path,
    normalize_relative_path,
    validate_display_name,
)
from ....domain.errors import DomainError, ErrorCode
from ....domain.packs import (
    PACK_OUTPUT_DELETED_ERROR_CODE,
    PackStatus,
    mark_output_deleted,
)
from ..schema import (
    content_entries,
    content_objects,
    pack_jobs,
    pack_sources,
    share_links,
    user_files,
    users,
)
from .quota import QuotaRepository

DELETE_LEASE_OWNER = "content-deletion"
DELETE_LEASE_TTL_MS = 300_000
SEARCH_LIMIT = 200


def _like_pattern(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


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
            read_leases=int(row.read_leases),
            delete_lease_owner=row.delete_lease_owner,
            delete_lease_expires_at_ms=(
                None
                if row.delete_lease_expires_at_ms is None
                else int(row.delete_lease_expires_at_ms)
            ),
            tombstone_path=row.tombstone_path,
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

    def _user_file(self, row: Row[Any]) -> UserFileRecord:
        return UserFileRecord(
            id=int(row.id),
            user_id=int(row.user_id),
            content_id=int(row.content_id),
            display_name=str(row.display_name),
            created_at_ms=int(row.created_at_ms),
            kind=ContentKind(row.kind),
            size=int(row.size),
            deletion_state=ContentDeletionState(row.deletion_state),
            storage_path=str(row.storage_path),
            digest=str(row.digest),
        )

    def _user_file_columns(self) -> tuple[ColumnElement[Any], ...]:
        return (
            user_files.c.id,
            user_files.c.user_id,
            user_files.c.content_id,
            user_files.c.display_name,
            user_files.c.created_at_ms,
            content_objects.c.kind,
            content_objects.c.size,
            content_objects.c.deletion_state,
            content_objects.c.storage_path,
            content_objects.c.digest,
        )

    async def list_user_files(
        self, *, user_id: int, offset: int, limit: int
    ) -> tuple[list[UserFileRecord], int]:
        join = user_files.join(content_objects, content_objects.c.id == user_files.c.content_id)
        total = int(
            (
                await self.connection.execute(
                    select(func.count()).select_from(join).where(user_files.c.user_id == user_id)
                )
            ).scalar_one()
        )
        rows = (
            await self.connection.execute(
                select(*self._user_file_columns())
                .select_from(join)
                .where(user_files.c.user_id == user_id)
                .order_by(content_objects.c.kind, user_files.c.display_name)
                .offset(offset)
                .limit(limit)
            )
        ).all()
        return [self._user_file(row) for row in rows], total

    async def get_user_file(self, *, user_id: int, file_id: int) -> UserFileRecord:
        row = (
            await self.connection.execute(
                select(*self._user_file_columns())
                .select_from(user_files.join(content_objects, content_objects.c.id == user_files.c.content_id))
                .where(user_files.c.id == file_id)
                .where(user_files.c.user_id == user_id)
            )
        ).first()
        if row is None:
            raise DomainError(ErrorCode.NOT_FOUND, "用户文件不存在")
        return self._user_file(row)

    async def rename_user_file(self, *, user_id: int, file_id: int, new_name: str) -> None:
        name = validate_display_name(new_name)
        try:
            result = await self.connection.execute(
                update(user_files)
                .where(user_files.c.id == file_id)
                .where(user_files.c.user_id == user_id)
                .where(
                    ~select(user_files.c.id)
                    .where(user_files.c.user_id == user_id)
                    .where(user_files.c.display_name == name)
                    .where(user_files.c.id != file_id)
                    .exists()
                )
                .values(display_name=name)
            )
        except IntegrityError:  # pragma: no cover -- 写事务内 NOT EXISTS 已排除并发同名，兜底不可达
            raise DomainError(ErrorCode.DUPLICATE_RESOURCE, "同名文件已存在") from None
        if result.rowcount == 1:
            return
        exists = (
            await self.connection.execute(select(user_files.c.id).where(user_files.c.id == file_id))
        ).first()
        if exists is None:
            raise DomainError(ErrorCode.NOT_FOUND, "用户文件不存在")
        raise DomainError(ErrorCode.DUPLICATE_RESOURCE, "同名文件已存在")

    async def count_references(self, content_id: int) -> int:
        return int(
            (
                await self.connection.execute(
                    select(func.count())
                    .select_from(user_files)
                    .where(user_files.c.content_id == content_id)
                )
            ).scalar_one()
        )

    async def has_pending_pack_sources(self, content_id: int) -> bool:
        row = (
            await self.connection.execute(
                select(pack_sources.c.id)
                .where(pack_sources.c.source_content_id == content_id)
                .where(pack_sources.c.cleanup_status == "pending")
                .limit(1)
            )
        ).first()
        return row is not None

    async def remove_user_file(self, *, user_id: int, file_id: int) -> int:
        """Delete the reference row and return the content's reference count before deletion."""
        row = (
            await self.connection.execute(
                select(user_files.c.content_id)
                .where(user_files.c.id == file_id)
                .where(user_files.c.user_id == user_id)
            )
        ).first()
        if row is None:
            raise DomainError(ErrorCode.NOT_FOUND, "用户文件不存在")
        before = await self.count_references(int(row.content_id))
        await self.connection.execute(delete(user_files).where(user_files.c.id == file_id))
        return before

    async def revoke_shares_for_user_file(self, *, user_file_id: int, now_ms: int) -> int:
        result = await self.connection.execute(
            update(share_links)
            .where(share_links.c.user_file_id == user_file_id)
            .where(share_links.c.revoked_at_ms.is_(None))
            .values(revoked_at_ms=now_ms)
        )
        return int(result.rowcount)

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

    def _entry(self, row: Row[Any]) -> ContentEntryRecord:
        return ContentEntryRecord(
            id=int(row.id),
            content_id=int(row.content_id),
            parent_path=str(row.parent_path),
            name=str(row.name),
            path=str(row.path),
            entry_kind=ContentKind(row.entry_kind),
            size=int(row.size),
            modified_at_ms=None if row.modified_at_ms is None else int(row.modified_at_ms),
        )

    async def list_entries(
        self, *, content_id: int, parent_path: str, offset: int, limit: int
    ) -> tuple[list[ContentEntryRecord], int]:
        parent = normalize_relative_path(parent_path, allow_root=True)
        filters = and_(
            content_entries.c.content_id == content_id,
            content_entries.c.parent_path == parent,
        )
        total = int(
            (
                await self.connection.execute(
                    select(func.count()).select_from(content_entries).where(filters)
                )
            ).scalar_one()
        )
        rows = (
            await self.connection.execute(
                select(content_entries)
                .where(filters)
                .order_by(content_entries.c.entry_kind, content_entries.c.name)
                .offset(offset)
                .limit(limit)
            )
        ).all()
        return [self._entry(row) for row in rows], total

    async def list_files(self, content_id: int) -> list[ContentEntryRecord]:
        rows = (
            await self.connection.execute(
                select(content_entries)
                .where(content_entries.c.content_id == content_id)
                .where(content_entries.c.entry_kind == ContentKind.FILE)
                .order_by(content_entries.c.path)
            )
        ).all()
        return [self._entry(row) for row in rows]

    async def get_entry(self, *, content_id: int, path: str) -> ContentEntryRecord:
        normalized = normalize_relative_path(path)
        row = (
            await self.connection.execute(
                select(content_entries)
                .where(content_entries.c.content_id == content_id)
                .where(content_entries.c.path == normalized)
            )
        ).first()
        if row is None:
            raise DomainError(ErrorCode.NOT_FOUND, "目录条目不存在")
        return self._entry(row)

    async def verify_entries(
        self, *, content_id: int, entries: list[ContentEntryIdentity]
    ) -> EntryDiff:
        """Compare content_entries against a physical scan; directories carry size 0."""
        rows = (
            await self.connection.execute(
                select(content_entries.c.path, content_entries.c.size)
                .where(content_entries.c.content_id == content_id)
            )
        ).all()
        stored = {str(row.path): int(row.size) for row in rows}
        scanned = {entry.path: entry.size for entry in entries}
        return EntryDiff(
            missing=tuple(sorted(set(scanned) - set(stored))),
            unexpected=tuple(sorted(set(stored) - set(scanned))),
            size_mismatch=tuple(
                sorted(
                    path
                    for path in set(stored) & set(scanned)
                    if stored[path] != scanned[path]
                )
            ),
        )

    async def rebuild_entries(
        self, *, content_id: int, entries: list[ContentEntryIdentity], now_ms: int
    ) -> None:
        """Replace all entries of the content with a physical scan, in one transaction."""
        await self.get_object(content_id)
        await self.connection.execute(
            delete(content_entries).where(content_entries.c.content_id == content_id)
        )
        for entry in sorted(entries, key=lambda item: item.path):
            parent, _, name = entry.path.rpartition("/")
            await self.add_entry(
                content_id=content_id,
                parent_path=parent,
                name=name,
                entry_kind=entry.kind.value,
                size=entry.size,
                now_ms=now_ms,
            )

    async def search_user_files(
        self, *, user_id: int, term: str, content_id: int | None = None
    ) -> tuple[list[UserFileSearchHit], bool]:
        pattern = _like_pattern(term)
        cap = SEARCH_LIMIT + 1
        root_filters = [
            user_files.c.user_id == user_id,
            user_files.c.display_name.like(pattern, escape="\\"),
        ]
        entry_filters = [
            user_files.c.user_id == user_id,
            content_entries.c.path.like(pattern, escape="\\"),
        ]
        if content_id is not None:
            root_filters.append(user_files.c.content_id == content_id)
            entry_filters.append(user_files.c.content_id == content_id)
        join = user_files.join(content_objects, content_objects.c.id == user_files.c.content_id)
        root_rows = (
            await self.connection.execute(
                select(
                    user_files.c.id,
                    user_files.c.display_name,
                    content_objects.c.kind,
                    content_objects.c.size,
                )
                .select_from(join)
                .where(and_(*root_filters))
                .order_by(user_files.c.id)
                .limit(cap)
            )
        ).all()
        entry_rows = (
            await self.connection.execute(
                select(
                    user_files.c.id,
                    user_files.c.display_name,
                    content_entries.c.path,
                    content_entries.c.entry_kind,
                    content_entries.c.size,
                )
                .select_from(
                    join.join(content_entries, content_entries.c.content_id == content_objects.c.id)
                )
                .where(and_(*entry_filters))
                .order_by(user_files.c.id, content_entries.c.path)
                .limit(cap)
            )
        ).all()
        hits = [
            UserFileSearchHit(
                user_file_id=int(row.id),
                display_name=str(row.display_name),
                path="",
                kind=ContentKind(row.kind),
                size=int(row.size),
            )
            for row in root_rows
        ] + [
            UserFileSearchHit(
                user_file_id=int(row.id),
                display_name=str(row.display_name),
                path=str(row.path),
                kind=ContentKind(row.entry_kind),
                size=int(row.size),
            )
            for row in entry_rows
        ]
        truncated = len(hits) > SEARCH_LIMIT
        return hits[:SEARCH_LIMIT], truncated

    async def acquire_read_lease(self, content_id: int) -> None:
        result = await self.connection.execute(
            update(content_objects)
            .where(content_objects.c.id == content_id)
            .where(content_objects.c.deletion_state == ContentDeletionState.ACTIVE)
            .values(read_leases=content_objects.c.read_leases + 1)
        )
        if result.rowcount == 1:
            return
        exists = (
            await self.connection.execute(
                select(content_objects.c.id).where(content_objects.c.id == content_id)
            )
        ).first()
        if exists is None:
            raise DomainError(ErrorCode.NOT_FOUND, "内容对象不存在")
        raise DomainError(ErrorCode.CONTENT_PENDING_DELETE, "内容正在删除，不能开始新读取")

    async def release_read_lease(self, content_id: int) -> None:
        await self.connection.execute(
            update(content_objects)
            .where(content_objects.c.id == content_id)
            .where(content_objects.c.read_leases > 0)
            .values(read_leases=content_objects.c.read_leases - 1)
        )

    async def reset_all_read_leases(self) -> int:
        result = await self.connection.execute(
            update(content_objects)
            .where(content_objects.c.read_leases > 0)
            .values(read_leases=0)
        )
        return int(result.rowcount)

    async def claim_orphan_delete(
        self,
        content_id: int,
        *,
        now_ms: int,
        lease_owner: str,
        lease_ttl_ms: int,
    ) -> bool:
        if lease_ttl_ms < 0:
            raise DomainError(ErrorCode.INVALID_INPUT, "删除租约时长不能为负数")
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
            .values(
                deletion_state=ContentDeletionState.PENDING_DELETE,
                delete_lease_owner=lease_owner,
                delete_lease_expires_at_ms=now_ms + lease_ttl_ms,
                updated_at_ms=now_ms,
            )
        )
        return result.rowcount == 1

    async def list_deletion_candidates(
        self, *, owner: str, now_ms: int
    ) -> list[DeletionCandidate]:
        references = self._reference_count_subquery()
        rows = (
            await self.connection.execute(
                select(
                    content_objects.c.id,
                    content_objects.c.deletion_state,
                    content_objects.c.storage_path,
                    content_objects.c.tombstone_path,
                    content_objects.c.read_leases,
                    references.label("reference_count"),
                ).where(
                    or_(
                        content_objects.c.deletion_state == ContentDeletionState.TOMBSTONED,
                        and_(
                            content_objects.c.deletion_state == ContentDeletionState.PENDING_DELETE,
                            or_(
                                content_objects.c.delete_lease_owner == owner,
                                content_objects.c.delete_lease_owner.is_(None),
                                content_objects.c.delete_lease_expires_at_ms <= now_ms,
                            ),
                        ),
                    )
                )
            )
        ).all()
        return [
            DeletionCandidate(
                content_id=int(row.id),
                deletion_state=ContentDeletionState(row.deletion_state),
                storage_path=str(row.storage_path),
                tombstone_path=row.tombstone_path,
                read_leases=int(row.read_leases),
                reference_count=int(row.reference_count),
            )
            for row in rows
        ]

    async def acquire_or_reclaim(
        self,
        content_id: int,
        *,
        now_ms: int,
        lease_owner: str,
        lease_ttl_ms: int,
    ) -> bool:
        if lease_ttl_ms < 0:
            raise DomainError(ErrorCode.INVALID_INPUT, "删除租约时长不能为负数")
        result = await self.connection.execute(
            update(content_objects)
            .where(content_objects.c.id == content_id)
            .where(content_objects.c.deletion_state == ContentDeletionState.PENDING_DELETE)
            .where(
                or_(
                    content_objects.c.delete_lease_owner == lease_owner,
                    content_objects.c.delete_lease_owner.is_(None),
                    content_objects.c.delete_lease_expires_at_ms <= now_ms,
                )
            )
            .where(~select(user_files.c.id).where(user_files.c.content_id == content_id).exists())
            .values(
                delete_lease_owner=lease_owner,
                delete_lease_expires_at_ms=now_ms + lease_ttl_ms,
                updated_at_ms=now_ms,
            )
        )
        return result.rowcount == 1

    async def write_tombstone(self, content_id: int, *, owner: str, now_ms: int) -> bool:
        result = await self.connection.execute(
            update(content_objects)
            .where(content_objects.c.id == content_id)
            .where(content_objects.c.deletion_state == ContentDeletionState.PENDING_DELETE)
            .where(~select(user_files.c.id).where(user_files.c.content_id == content_id).exists())
            .where(content_objects.c.delete_lease_owner == owner)
            .where(content_objects.c.delete_lease_expires_at_ms > now_ms)
            .values(
                deletion_state=ContentDeletionState.TOMBSTONED,
                tombstone_path=content_objects.c.storage_path,
                updated_at_ms=now_ms,
            )
        )
        return result.rowcount == 1

    async def finalize_tombstoned(self, content_id: int) -> bool:
        tombstoned = (
            select(content_objects.c.id)
            .where(content_objects.c.id == content_id)
            .where(content_objects.c.deletion_state == ContentDeletionState.TOMBSTONED)
            .exists()
        )
        source_status = PackStatus.DONE
        deleted_status = mark_output_deleted(source_status)
        await self.connection.execute(
            update(pack_jobs)
            .where(pack_jobs.c.output_content_id == content_id)
            .where(pack_jobs.c.status == source_status)
            .where(tombstoned)
            .values(
                status=deleted_status,
                error_code=PACK_OUTPUT_DELETED_ERROR_CODE,
                output_content_id=None,
            )
        )
        result = await self.connection.execute(
            delete(content_objects)
            .where(content_objects.c.id == content_id)
            .where(content_objects.c.deletion_state == ContentDeletionState.TOMBSTONED)
        )
        return result.rowcount == 1

    def _reference_count_subquery(self) -> ColumnElement[int]:
        return (
            select(func.count())
            .select_from(user_files)
            .where(user_files.c.content_id == content_objects.c.id)
            .scalar_subquery()
        )

    async def list_storage_objects(
        self,
        *,
        offset: int,
        limit: int,
        search: str | None = None,
        orphans_only: bool = False,
    ) -> tuple[list[StorageObjectRecord], int]:
        references = self._reference_count_subquery()
        filters: list[ColumnElement[bool]] = []
        if search:
            filters.append(content_objects.c.storage_path.like(_like_pattern(search), escape="\\"))
        if orphans_only:
            filters.append(content_objects.c.deletion_state == ContentDeletionState.ACTIVE)
            filters.append(references == 0)
        condition = and_(*filters) if filters else None
        count_query = select(func.count()).select_from(content_objects)
        rows_query = select(
            content_objects.c.id,
            content_objects.c.content_key,
            content_objects.c.kind,
            content_objects.c.size,
            content_objects.c.storage_path,
            content_objects.c.deletion_state,
            content_objects.c.read_leases,
            content_objects.c.created_at_ms,
            content_objects.c.updated_at_ms,
            references.label("reference_count"),
        )
        if condition is not None:
            count_query = count_query.where(condition)
            rows_query = rows_query.where(condition)
        total = int((await self.connection.execute(count_query)).scalar_one())
        rows = (
            await self.connection.execute(
                rows_query.order_by(content_objects.c.id).offset(offset).limit(limit)
            )
        ).all()
        return [
            StorageObjectRecord(
                content_id=int(row.id),
                content_key=str(row.content_key),
                kind=ContentKind(row.kind),
                size=int(row.size),
                storage_path=str(row.storage_path),
                deletion_state=ContentDeletionState(row.deletion_state),
                reference_count=int(row.reference_count),
                read_leases=int(row.read_leases),
                created_at_ms=int(row.created_at_ms),
                updated_at_ms=int(row.updated_at_ms),
            )
            for row in rows
        ], total

    async def list_storage_file_names(
        self, content_ids: list[int]
    ) -> dict[int, list[str]]:
        """每个内容对象关联的用户文件名（按最早引用顺序，供存储页辨认文件）。"""
        if not content_ids:
            return {}
        rows = (
            await self.connection.execute(
                select(user_files.c.content_id, user_files.c.display_name)
                .where(user_files.c.content_id.in_(content_ids))
                .order_by(user_files.c.id)
            )
        ).all()
        names: dict[int, list[str]] = {}
        for row in rows:
            names.setdefault(int(row.content_id), []).append(str(row.display_name))
        return names

    async def list_referencing_users(self, content_id: int) -> list[StorageReferencingUser]:
        rows = (
            await self.connection.execute(
                select(
                    users.c.id,
                    users.c.username,
                    user_files.c.display_name,
                    user_files.c.created_at_ms,
                )
                .select_from(user_files.join(users, users.c.id == user_files.c.user_id))
                .where(user_files.c.content_id == content_id)
                .order_by(user_files.c.id)
            )
        ).all()
        return [
            StorageReferencingUser(
                user_id=int(row.id),
                username=str(row.username),
                display_name=str(row.display_name),
                referenced_at_ms=int(row.created_at_ms),
            )
            for row in rows
        ]

    async def accept_orphan_deletes(
        self,
        content_ids: list[int],
        *,
        now_ms: int,
        lease_owner: str,
        lease_ttl_ms: int,
    ) -> list[tuple[int, bool]]:
        results: list[tuple[int, bool]] = []
        for content_id in content_ids:
            accepted = await self.claim_orphan_delete(
                content_id,
                now_ms=now_ms,
                lease_owner=lease_owner,
                lease_ttl_ms=lease_ttl_ms,
            )
            results.append((content_id, accepted))
        return results

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
            await self.claim_orphan_delete(
                int(content_id),
                now_ms=now_ms,
                lease_owner=DELETE_LEASE_OWNER,
                lease_ttl_ms=DELETE_LEASE_TTL_MS,
            )
        await QuotaRepository(self.connection).rebuild(user_id)
