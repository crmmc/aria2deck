from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Protocol

from ...domain.content import ContentKind, RangeSpec
from ...domain.errors import DomainError, ErrorCode
from ...domain.shares import plan_share_download, require_password, require_share_available
from ..library.download import LibraryDownloadService
from ..library.leases import ReadLeaseManager
from ..library.models import ContentEntryRecord, DownloadTarget, UserFileRecord
from ..ports import UnitOfWork
from .models import (
    PublicShareDownload,
    PublicShareEntryView,
    PublicShareInfoView,
    ShareEntryBreadcrumb,
    ShareRecord,
)
from .tokens import ShareAccessTokenStore


class ShareRepositoryPort(Protocol):
    async def find_share_by_code(self, code: str) -> ShareRecord | None: ...

    async def increment_download_count(
        self, share_id: int, *, expected_count: int, now_ms: int
    ) -> None: ...


class ContentRepositoryPort(Protocol):
    async def get_user_file(self, *, user_id: int, file_id: int) -> UserFileRecord: ...

    async def get_entry(self, *, content_id: int, path: str) -> ContentEntryRecord: ...

    async def list_entries(
        self, *, content_id: int, parent_path: str, offset: int, limit: int
    ) -> tuple[list[ContentEntryRecord], int]: ...


class ContentPathPort(Protocol):
    def physical_path(self, storage_path: str, entry_relative_path: str | None = None) -> Path: ...


RepositoryFactory = Callable[[UnitOfWork], ShareRepositoryPort]
ContentRepositoryFactory = Callable[[UnitOfWork], ContentRepositoryPort]
UowFactory = Callable[[], AbstractAsyncContextManager[UnitOfWork]]
ClockMs = Callable[[], int]


class PublicShareService:
    """Public share access chain: info, verify, browse, download (no authentication)."""

    def __init__(
        self,
        *,
        uow_factory: UowFactory,
        repository_factory: RepositoryFactory,
        content_repository_factory: ContentRepositoryFactory,
        tokens: ShareAccessTokenStore,
        leases: ReadLeaseManager,
        content_paths: ContentPathPort,
        streamer: LibraryDownloadService,
        clock_ms: ClockMs,
    ) -> None:
        self.uow_factory = uow_factory
        self.repository_factory = repository_factory
        self.content_repository_factory = content_repository_factory
        self.tokens = tokens
        self.leases = leases
        self.content_paths = content_paths
        self.streamer = streamer
        self.clock_ms = clock_ms

    async def info(self, code: str) -> PublicShareInfoView:
        record = await self._active_share(code)
        return PublicShareInfoView(
            file_name=record.file_name,
            file_size=record.file_size,
            file_kind=record.file_kind,
            has_password=record.password_hash is not None,
            expires_at_ms=record.expires_at_ms,
            max_downloads=record.max_downloads,
            download_count=record.download_count,
        )

    async def verify(self, code: str, password: str | None) -> tuple[str, int]:
        record = await self._active_share(code)
        require_password(record.password_hash, code, password)
        return self.tokens.issue(code)

    async def browse(
        self, code: str, token: str | None, path: str, page: int, page_size: int
    ) -> tuple[list[PublicShareEntryView], list[ShareEntryBreadcrumb], int]:
        record = await self._active_share(code)
        self._require_token(record, token)
        async with self.uow_factory() as uow:
            content = self.content_repository_factory(uow)
            file_record = await self._owned_file(content, record)
            if file_record is None or file_record.kind is not ContentKind.DIRECTORY:
                raise DomainError(ErrorCode.INVALID_INPUT, "单文件分享不支持目录浏览")
            if path:
                entry = await content.get_entry(content_id=file_record.content_id, path=path)
                if entry.entry_kind is not ContentKind.DIRECTORY:
                    raise DomainError(ErrorCode.NOT_FOUND, "目录不存在")
            entries, total = await content.list_entries(
                content_id=file_record.content_id,
                parent_path=path,
                offset=(page - 1) * page_size,
                limit=page_size,
            )
        breadcrumbs = [ShareEntryBreadcrumb(record.file_name, "")]
        cumulative = ""
        for segment in path.split("/") if path else []:
            cumulative = f"{cumulative}/{segment}" if cumulative else segment
            breadcrumbs.append(ShareEntryBreadcrumb(segment, cumulative))
        return [
            PublicShareEntryView(
                name=entry.name, path=entry.path, kind=entry.entry_kind, size=entry.size
            )
            for entry in entries
        ], breadcrumbs, total

    async def check_access(self, code: str, token: str | None) -> ShareRecord:
        """校验分享可用性与访问令牌（不解析目标、不计数）。

        供匿名连接限流在占用连接档之前做准入判断：无效分享码/令牌的请求
        不应消耗匿名连接额度（旧版 check_share_access 同位职责）。
        """
        record = await self._active_share(code)
        self._require_token(record, token)
        return record

    async def download(
        self,
        code: str,
        token: str | None,
        path: str | None,
        range_header: str | None,
    ) -> PublicShareDownload:
        now_ms = self.clock_ms()
        record = await self._active_share(code)
        self._require_token(record, token)
        async with self.uow_factory() as uow:
            content = self.content_repository_factory(uow)
            file_record = await self._owned_file(content, record)
            if file_record is None:
                raise DomainError(ErrorCode.NOT_FOUND, "分享文件不存在")
            entry_relative_path: str | None = None
            if file_record.kind is ContentKind.FILE:
                if path:
                    raise DomainError(ErrorCode.INVALID_INPUT, "文件分享不支持子路径")
                display_name = file_record.display_name
                total_size = file_record.size
            else:
                if not path:
                    raise DomainError(ErrorCode.INVALID_INPUT, "目录不能整体下载")
                entry = await content.get_entry(content_id=file_record.content_id, path=path)
                if entry.entry_kind is not ContentKind.FILE:
                    raise DomainError(ErrorCode.INVALID_INPUT, "目录条目不能整体下载")
                entry_relative_path = entry.path
                display_name = entry.name
                total_size = entry.size
            content_id = file_record.content_id
            storage_path = file_record.storage_path
        decision = plan_share_download(
            record.as_domain(), now_ms=now_ms, size=total_size, range_header=range_header
        )
        await self.leases.acquire(content_id)
        try:
            if decision.count_download:
                await self._count_download(record)
            else:
                await self._active_share(record.code)
        except BaseException:
            # 租约后的任何准入异常（含协程取消）都必须释放读租约；原样抛出，不得吞掉取消。
            await self.leases.release(content_id)
            raise
        return PublicShareDownload(
            decision=decision,
            content_id=content_id,
            storage_path=storage_path,
            entry_relative_path=entry_relative_path,
            physical_path=self.content_paths.physical_path(storage_path, entry_relative_path),
            display_name=display_name,
            total_size=total_size,
        )

    async def iter_chunks(self, result: PublicShareDownload) -> AsyncIterator[bytes]:
        async for chunk in self.streamer.iter_chunks(self._as_target(result)):
            yield chunk

    async def release(self, result: PublicShareDownload) -> None:
        await self.streamer.release(self._as_target(result))

    @staticmethod
    def _as_target(result: PublicShareDownload) -> DownloadTarget:
        range_spec = (
            None
            if result.decision.status_code == 200
            else RangeSpec(result.decision.start, result.decision.end)
        )
        return DownloadTarget(
            content_id=result.content_id,
            storage_path=result.storage_path,
            entry_relative_path=result.entry_relative_path,
            physical_path=result.physical_path,
            display_name=result.display_name,
            total_size=result.total_size,
            range_spec=range_spec,
        )

    async def _active_share(self, code: str) -> ShareRecord:
        async with self.uow_factory() as uow:
            record = await self.repository_factory(uow).find_share_by_code(code)
        if record is None:
            raise DomainError(ErrorCode.NOT_FOUND, "分享不存在")
        require_share_available(record.as_domain(), self.clock_ms())
        return record

    def _require_token(self, record: ShareRecord, token: str | None) -> None:
        # 无密码分享凭分享码直接访问：token 是「过密码门」的证明，无门则无需证明。
        if record.password_hash is None:
            return
        if not token or not self.tokens.verify(record.code, token):
            raise DomainError(ErrorCode.UNAUTHENTICATED, "分享访问令牌无效或已过期")

    async def _owned_file(
        self, content: ContentRepositoryPort, record: ShareRecord
    ) -> UserFileRecord | None:
        if record.user_file_id is None:
            return None
        return await content.get_user_file(user_id=record.owner_id, file_id=record.user_file_id)

    async def _count_download(self, record: ShareRecord) -> None:
        # 条件计数 + 失败重读重试：并发完整下载恰好到达 max 次时全部成功，越界即 410。
        for _ in range(5):
            try:
                async with self.uow_factory() as uow:
                    await self.repository_factory(uow).increment_download_count(
                        record.id,
                        expected_count=record.download_count,
                        now_ms=self.clock_ms(),
                    )
                return
            except DomainError:
                fresh = await self._find_share(record.code)
                require_share_available(fresh.as_domain(), self.clock_ms())
                record = fresh
        raise DomainError(ErrorCode.INVALID_STATE, "分享计数冲突，请重试")

    async def _find_share(self, code: str) -> ShareRecord:
        async with self.uow_factory() as uow:
            record = await self.repository_factory(uow).find_share_by_code(code)
        if record is None:
            raise DomainError(ErrorCode.NOT_FOUND, "分享不存在")
        return record
