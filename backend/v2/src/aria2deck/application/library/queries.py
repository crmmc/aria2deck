from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Protocol

from ...domain.content import ContentKind
from ...domain.errors import DomainError, ErrorCode
from ...domain.quota import Quota
from ..ports import UnitOfWork
from .models import (
    BreadcrumbItem,
    ContentEntryRecord,
    ContentRecord,
    DirectoryEntryView,
    SearchHitView,
    StorageFileView,
    StorageObjectRecord,
    StorageReferencingUser,
    StorageReferencingUserView,
    UserFileRecord,
    UserFileSearchHit,
    UserFileView,
)


class LibraryQueryRepository(Protocol):
    async def get_object(self, content_id: int) -> ContentRecord: ...

    async def list_user_files(
        self, *, user_id: int, offset: int, limit: int
    ) -> tuple[list[UserFileRecord], int]: ...

    async def get_user_file(self, *, user_id: int, file_id: int) -> UserFileRecord: ...

    async def list_entries(
        self, *, content_id: int, parent_path: str, offset: int, limit: int
    ) -> tuple[list[ContentEntryRecord], int]: ...

    async def get_entry(self, *, content_id: int, path: str) -> ContentEntryRecord: ...

    async def search_user_files(
        self, *, user_id: int, term: str, content_id: int | None = None
    ) -> tuple[list[UserFileSearchHit], bool]: ...

    async def list_storage_objects(
        self, *, offset: int, limit: int, search: str | None = None, orphans_only: bool = False
    ) -> tuple[list[StorageObjectRecord], int]: ...

    async def list_referencing_users(self, content_id: int) -> list[StorageReferencingUser]: ...


class QuotaReader(Protocol):
    async def get(self, user_id: int) -> Quota: ...


class StoragePresence(Protocol):
    def on_disk(self, storage_path: str) -> bool: ...


RepositoryFactory = Callable[[UnitOfWork], LibraryQueryRepository]
QuotaRepositoryFactory = Callable[[UnitOfWork], QuotaReader]
UowFactory = Callable[[], AbstractAsyncContextManager[UnitOfWork]]


class LibraryQueryService:
    def __init__(
        self,
        *,
        uow_factory: UowFactory,
        repository_factory: RepositoryFactory,
        quota_repository_factory: QuotaRepositoryFactory,
        storage_presence: StoragePresence,
    ) -> None:
        self.uow_factory = uow_factory
        self.repository_factory = repository_factory
        self.quota_repository_factory = quota_repository_factory
        self.storage_presence = storage_presence

    async def list_root_files(
        self, *, user_id: int, page: int, page_size: int
    ) -> tuple[list[UserFileView], int, Quota]:
        offset = (page - 1) * page_size
        async with self.uow_factory() as uow:
            repository = self.repository_factory(uow)
            records, total = await repository.list_user_files(
                user_id=user_id, offset=offset, limit=page_size
            )
            quota = await self.quota_repository_factory(uow).get(user_id)
        return [_file_view(record) for record in records], total, quota

    async def browse_directory(
        self, *, user_id: int, file_id: int, path: str, page: int, page_size: int
    ) -> tuple[list[DirectoryEntryView], list[BreadcrumbItem], int]:
        offset = (page - 1) * page_size
        async with self.uow_factory() as uow:
            repository = self.repository_factory(uow)
            record = await repository.get_user_file(user_id=user_id, file_id=file_id)
            if record.kind is not ContentKind.DIRECTORY:
                raise DomainError(ErrorCode.INVALID_INPUT, "只能浏览目录内容")
            if path:
                entry = await repository.get_entry(content_id=record.content_id, path=path)
                if entry.entry_kind is not ContentKind.DIRECTORY:
                    raise DomainError(ErrorCode.NOT_FOUND, "目录不存在")
            entries, total = await repository.list_entries(
                content_id=record.content_id, parent_path=path, offset=offset, limit=page_size
            )
        breadcrumbs = [BreadcrumbItem(record.display_name, "")]
        cumulative = ""
        for segment in path.split("/") if path else []:
            cumulative = f"{cumulative}/{segment}" if cumulative else segment
            breadcrumbs.append(BreadcrumbItem(segment, cumulative))
        return [_entry_view(entry) for entry in entries], breadcrumbs, total

    async def search(
        self, *, user_id: int, term: str, scope_file_id: int | None
    ) -> tuple[list[SearchHitView], bool]:
        normalized = term.strip()
        if not normalized:
            raise DomainError(ErrorCode.INVALID_INPUT, "搜索词不能为空白")
        content_id: int | None = None
        if scope_file_id is not None:
            async with self.uow_factory() as uow:
                try:
                    record = await self.repository_factory(uow).get_user_file(
                        user_id=user_id, file_id=scope_file_id
                    )
                except DomainError as error:
                    if error.code is ErrorCode.NOT_FOUND:
                        raise DomainError(ErrorCode.INVALID_INPUT, "搜索范围无效") from None
                    raise  # pragma: no cover -- get_user_file 对归属查询只会抛 NOT_FOUND
            if record.kind is not ContentKind.DIRECTORY:
                raise DomainError(ErrorCode.INVALID_INPUT, "搜索范围必须是目录文件")
            content_id = record.content_id
        async with self.uow_factory() as uow:
            hits, truncated = await self.repository_factory(uow).search_user_files(
                user_id=user_id, term=normalized, content_id=content_id
            )
        return [
            SearchHitView(
                user_file_id=hit.user_file_id,
                display_name=hit.display_name,
                path=hit.path,
                kind=hit.kind,
                size=hit.size,
            )
            for hit in hits
        ], truncated

    async def list_storage_files(
        self,
        *,
        page: int,
        page_size: int,
        search: str | None = None,
        orphans_only: bool = False,
    ) -> tuple[list[StorageFileView], int]:
        offset = (page - 1) * page_size
        async with self.uow_factory() as uow:
            records, total = await self.repository_factory(uow).list_storage_objects(
                offset=offset, limit=page_size, search=search, orphans_only=orphans_only
            )
        return [
            StorageFileView(
                content_id=record.content_id,
                content_key=record.content_key,
                kind=record.kind,
                size=record.size,
                storage_path=record.storage_path,
                on_disk=self.storage_presence.on_disk(record.storage_path),
                reference_count=record.reference_count,
                deletion_state=record.deletion_state,
            )
            for record in records
        ], total

    async def list_referencing_users(self, content_id: int) -> list[StorageReferencingUserView]:
        async with self.uow_factory() as uow:
            repository = self.repository_factory(uow)
            await repository.get_object(content_id)
            users = await repository.list_referencing_users(content_id)
        return [
            StorageReferencingUserView(
                user_id=row.user_id,
                username=row.username,
                display_name=row.display_name,
                referenced_at_ms=row.referenced_at_ms,
            )
            for row in users
        ]


def _file_view(record: UserFileRecord) -> UserFileView:
    return UserFileView(
        id=record.id,
        display_name=record.display_name,
        kind=record.kind,
        size=record.size,
        created_at_ms=record.created_at_ms,
    )


def _entry_view(entry: ContentEntryRecord) -> DirectoryEntryView:
    return DirectoryEntryView(
        name=entry.name,
        path=entry.path,
        kind=entry.entry_kind,
        size=entry.size,
    )
