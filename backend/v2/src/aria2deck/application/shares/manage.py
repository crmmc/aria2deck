from __future__ import annotations

import secrets
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Protocol

from ...domain.errors import DomainError, ErrorCode
from ...domain.shares import ShareStatus, hash_share_password, share_status, validate_share_options
from ..ports import UnitOfWork
from .models import ShareCreatedView, ShareRecord, ShareView


class ShareRepositoryPort(Protocol):
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
    ) -> int: ...

    async def get_share(self, share_id: int) -> ShareRecord: ...

    async def page_shares(
        self,
        owner_id: int,
        *,
        now_ms: int,
        status: ShareStatus | None = None,
        offset: int = 0,
        limit: int | None = None,
    ) -> tuple[list[ShareRecord], int]: ...

    async def revoke_share(self, share_id: int, *, owner_id: int, now_ms: int) -> bool: ...

    async def revoke_all_shares(self, owner_id: int, *, now_ms: int) -> int: ...

    async def delete_share(self, share_id: int, *, owner_id: int) -> bool: ...


@dataclass(frozen=True, slots=True)
class ShareBatchItem:
    share_id: int
    ok: bool
    already_revoked: bool = False
    error_code: str | None = None
    message: str | None = None


RepositoryFactory = Callable[[UnitOfWork], ShareRepositoryPort]
UowFactory = Callable[[], AbstractAsyncContextManager[UnitOfWork]]
ClockMs = Callable[[], int]


def _view(record: ShareRecord, now_ms: int) -> ShareView:
    return ShareView(
        id=record.id,
        code=record.code,
        file_name=record.file_name,
        file_size=record.file_size,
        file_kind=record.file_kind,
        status=share_status(record.as_domain(), now_ms),
        expires_at_ms=record.expires_at_ms,
        max_downloads=record.max_downloads,
        download_count=record.download_count,
        created_at_ms=record.created_at_ms,
        has_password=record.password_hash is not None,
    )


class ShareManageService:
    def __init__(
        self,
        *,
        uow_factory: UowFactory,
        repository_factory: RepositoryFactory,
        clock_ms: ClockMs,
    ) -> None:
        self.uow_factory = uow_factory
        self.repository_factory = repository_factory
        self.clock_ms = clock_ms

    async def create(
        self,
        user_id: int,
        *,
        user_file_id: int,
        expires_in_seconds: int | None,
        max_downloads: int | None,
        password: str | None,
    ) -> ShareCreatedView:
        validate_share_options(expires_in_seconds, max_downloads, password)
        now_ms = self.clock_ms()
        expires_at_ms = (
            None if expires_in_seconds is None else now_ms + expires_in_seconds * 1000
        )
        async with self.uow_factory() as uow:
            repository = self.repository_factory(uow)
            # 归属校验由 create_share 的联查完成：他人/不存在都返回 404。
            code = self._new_code()
            password_hash = (
                None if password is None else hash_share_password(code, password)
            )
            share_id = await repository.create_share(
                user_id=user_id,
                user_file_id=user_file_id,
                code=code,
                password_hash=password_hash,
                expires_at_ms=expires_at_ms,
                max_downloads=max_downloads,
                now_ms=now_ms,
            )
            record = await repository.get_share(share_id)
        return ShareCreatedView(code=code, view=_view(record, now_ms))

    async def list_shares(
        self,
        user_id: int,
        *,
        status: ShareStatus | None,
        page: int,
        page_size: int,
    ) -> tuple[list[ShareView], int]:
        now_ms = self.clock_ms()
        async with self.uow_factory() as uow:
            records, total = await self.repository_factory(uow).page_shares(
                user_id,
                now_ms=now_ms,
                status=status,
                offset=(page - 1) * page_size,
                limit=page_size,
            )
        return [_view(record, now_ms) for record in records], total

    async def revoke(self, user_id: int, share_ids: list[int]) -> list[ShareBatchItem]:
        results: list[ShareBatchItem] = []
        for share_id in share_ids:
            async with self.uow_factory() as uow:
                repository = self.repository_factory(uow)
                try:
                    record = await repository.get_share(share_id)
                    if record.owner_id != user_id:
                        raise DomainError(ErrorCode.NOT_FOUND, "分享不存在")
                    already = record.revoked_at_ms is not None
                    if not already:
                        await repository.revoke_share(
                            share_id, owner_id=user_id, now_ms=self.clock_ms()
                        )
                except DomainError as error:
                    results.append(
                        ShareBatchItem(
                            share_id, False, error_code=error.code.value, message=str(error)
                        )
                    )
                    continue
            results.append(ShareBatchItem(share_id, True, already_revoked=already))
        return results

    async def delete(self, user_id: int, share_ids: list[int]) -> list[ShareBatchItem]:
        results: list[ShareBatchItem] = []
        for share_id in share_ids:
            async with self.uow_factory() as uow:
                repository = self.repository_factory(uow)
                try:
                    record = await repository.get_share(share_id)
                    if record.owner_id != user_id:
                        raise DomainError(ErrorCode.NOT_FOUND, "分享不存在")
                    already = record.revoked_at_ms is not None
                    # 删除含先撤销语义：行删除后公开链路一律 404。
                    if not already:
                        await repository.revoke_share(
                            share_id, owner_id=user_id, now_ms=self.clock_ms()
                        )
                    deleted = await repository.delete_share(share_id, owner_id=user_id)
                except DomainError as error:
                    results.append(
                        ShareBatchItem(
                            share_id, False, error_code=error.code.value, message=str(error)
                        )
                    )
                    continue
            results.append(ShareBatchItem(share_id, deleted, already_revoked=already))
        return results

    async def revoke_all(self, user_id: int) -> int:
        async with self.uow_factory() as uow:
            return await self.repository_factory(uow).revoke_all_shares(
                user_id, now_ms=self.clock_ms()
            )

    @staticmethod
    def _new_code() -> str:
        return secrets.token_urlsafe(12)
