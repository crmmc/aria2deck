from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Protocol

from ...domain.content import (
    ContentDeletionState,
    plan_reference_removal,
    validate_display_name,
)
from ...domain.errors import DomainError, ErrorCode
from ...domain.quota import Quota
from ..ports import UnitOfWork
from .models import ContentRecord, StorageDeleteOutcome, UserFileRecord, UserFileView

logger = logging.getLogger(__name__)


class LibraryCommandRepository(Protocol):
    async def get_object(self, content_id: int) -> ContentRecord: ...

    async def get_user_file(self, *, user_id: int, file_id: int) -> UserFileRecord: ...

    async def rename_user_file(self, *, user_id: int, file_id: int, new_name: str) -> None: ...

    async def count_references(self, content_id: int) -> int: ...

    async def has_pending_pack_sources(self, content_id: int) -> bool: ...

    async def remove_user_file(self, *, user_id: int, file_id: int) -> int: ...

    async def revoke_shares_for_user_file(self, *, user_file_id: int, now_ms: int) -> int: ...

    async def claim_orphan_delete(
        self, content_id: int, *, now_ms: int, lease_owner: str, lease_ttl_ms: int
    ) -> bool: ...


class QuotaRebuilder(Protocol):
    async def rebuild(self, user_id: int) -> Quota: ...


RepositoryFactory = Callable[[UnitOfWork], LibraryCommandRepository]
QuotaRepositoryFactory = Callable[[UnitOfWork], QuotaRebuilder]
UowFactory = Callable[[], AbstractAsyncContextManager[UnitOfWork]]
ClockMs = Callable[[], int]


class LibraryCommandService:
    def __init__(
        self,
        *,
        uow_factory: UowFactory,
        repository_factory: RepositoryFactory,
        quota_repository_factory: QuotaRepositoryFactory,
        clock_ms: ClockMs,
        lease_owner: str,
        lease_ttl_ms: int,
    ) -> None:
        self.uow_factory = uow_factory
        self.repository_factory = repository_factory
        self.quota_repository_factory = quota_repository_factory
        self.clock_ms = clock_ms
        self.lease_owner = lease_owner
        self.lease_ttl_ms = lease_ttl_ms

    async def rename(self, *, user_id: int, file_id: int, new_name: str) -> UserFileView:
        name = validate_display_name(new_name)
        async with self.uow_factory() as uow:
            repository = self.repository_factory(uow)
            await repository.rename_user_file(user_id=user_id, file_id=file_id, new_name=name)
            return _view(await repository.get_user_file(user_id=user_id, file_id=file_id))

    async def delete_user_file(self, *, user_id: int, file_id: int) -> None:
        async with self.uow_factory() as uow:
            repository = self.repository_factory(uow)
            record = await repository.get_user_file(user_id=user_id, file_id=file_id)
            decision = plan_reference_removal(
                reference_count=await repository.count_references(record.content_id),
                pending_pack_sources=await repository.has_pending_pack_sources(record.content_id),
            )
            # 先撤销分享再删引用行：user_files 外键是 SET NULL，行删除后无法再按 user_file_id 定位。
            await repository.revoke_shares_for_user_file(
                user_file_id=file_id, now_ms=self.clock_ms()
            )
            await repository.remove_user_file(user_id=user_id, file_id=file_id)
            await self.quota_repository_factory(uow).rebuild(user_id)
            if decision.schedule_physical_delete:
                claimed = await repository.claim_orphan_delete(
                    record.content_id,
                    now_ms=self.clock_ms(),
                    lease_owner=self.lease_owner,
                    lease_ttl_ms=self.lease_ttl_ms,
                )
                if not claimed:
                    logger.info(
                        "最后引用删除后未认领物理删除 content_id=%s", record.content_id
                    )

    async def delete_user_files(
        self, *, user_id: int, file_ids: list[int]
    ) -> list[StorageDeleteOutcome]:
        """批量删除本人文件引用（A1）：逐项受理，单项失败不阻断其余。"""
        outcomes: list[StorageDeleteOutcome] = []
        for file_id in dict.fromkeys(file_ids):
            try:
                await self.delete_user_file(user_id=user_id, file_id=file_id)
                outcomes.append(StorageDeleteOutcome(file_id, True))
            except DomainError as error:
                outcomes.append(
                    StorageDeleteOutcome(file_id, False, error.code.value, str(error))
                )
        return outcomes

    async def storage_delete_orphans(self, content_ids: list[int]) -> list[StorageDeleteOutcome]:
        outcomes: list[StorageDeleteOutcome] = []
        for content_id in content_ids:
            try:
                async with self.uow_factory() as uow:
                    repository = self.repository_factory(uow)
                    content = await repository.get_object(content_id)
                    accepted = await repository.claim_orphan_delete(
                        content_id,
                        now_ms=self.clock_ms(),
                        lease_owner=self.lease_owner,
                        lease_ttl_ms=self.lease_ttl_ms,
                    )
                if accepted:
                    outcomes.append(StorageDeleteOutcome(content_id, True))
                elif content.deletion_state is not ContentDeletionState.ACTIVE:
                    outcomes.append(
                        StorageDeleteOutcome(
                            content_id,
                            False,
                            ErrorCode.CONTENT_PENDING_DELETE.value,
                            "内容已在删除流程中",
                        )
                    )
                else:
                    outcomes.append(
                        StorageDeleteOutcome(
                            content_id,
                            False,
                            ErrorCode.CONTENT_BUSY.value,
                            "内容仍有用户引用",
                        )
                    )
            except DomainError as error:
                outcomes.append(
                    StorageDeleteOutcome(content_id, False, error.code.value, str(error))
                )
        return outcomes


def _view(record: UserFileRecord) -> UserFileView:
    return UserFileView(
        id=record.id,
        display_name=record.display_name,
        kind=record.kind,
        size=record.size,
        created_at_ms=record.created_at_ms,
    )
