from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Protocol

from ...domain.content import ContentDeletionState, validate_display_name
from ...domain.errors import DomainError, ErrorCode
from ...domain.quota import (
    PhysicalDownload,
    PhysicalPack,
    Quota,
    available_machine_bytes,
    available_user_bytes,
)
from ...domain.settings import SystemSettings
from ..ports import UnitOfWork
from .models import (
    PackAdmissionSource,
    PackSourceSnapshot,
    PrecomputeItemView,
    PrecomputeResultView,
)

MAX_PACK_SOURCES = 100


class DownloadRepositoryPort(Protocol):
    async def list_active_physical(self) -> list[PhysicalDownload]: ...


class PackRepositoryPort(Protocol):
    async def list_admission_sources(
        self, *, user_id: int, user_file_ids: list[int]
    ) -> list[PackAdmissionSource]: ...

    async def list_active_physical(self) -> list[PhysicalPack]: ...

    async def create_job(
        self,
        *,
        owner_id: int,
        output_name: str,
        pack_format: str,
        source_size: int,
        reserved_bytes: int,
        physical_reserved_bytes: int,
        delete_source: bool,
        now_ms: int,
    ) -> int: ...

    async def add_source(self, pack_id: int, snapshot: PackSourceSnapshot) -> int: ...


class QuotaRepositoryPort(Protocol):
    async def get(self, user_id: int) -> Quota: ...

    async def reserve(self, user_id: int, amount: int) -> Quota: ...


class MachineSpacePort(Protocol):
    def machine_free_bytes(self) -> int: ...


RepositoryFactory = Callable[[UnitOfWork], PackRepositoryPort]
DownloadRepositoryFactory = Callable[[UnitOfWork], DownloadRepositoryPort]
QuotaRepositoryFactory = Callable[[UnitOfWork], QuotaRepositoryPort]
UowFactory = Callable[[], AbstractAsyncContextManager[UnitOfWork]]
ClockMs = Callable[[], int]
SettingsGetter = Callable[[], SystemSettings]


class PackAdmissionService:
    def __init__(
        self,
        *,
        uow_factory: UowFactory,
        repository_factory: RepositoryFactory,
        download_repository_factory: DownloadRepositoryFactory,
        quota_repository_factory: QuotaRepositoryFactory,
        machine_space: MachineSpacePort,
        settings: SettingsGetter,
        clock_ms: ClockMs,
    ) -> None:
        self.uow_factory = uow_factory
        self.repository_factory = repository_factory
        self.download_repository_factory = download_repository_factory
        self.quota_repository_factory = quota_repository_factory
        self.machine_space = machine_space
        self.settings = settings
        self.clock_ms = clock_ms

    async def precompute(self, user_id: int, user_file_ids: list[int]) -> PrecomputeResultView:
        unique_ids = list(dict.fromkeys(user_file_ids))
        async with self.uow_factory() as uow:
            sources = {
                source.user_file_id: source
                for source in await self.repository_factory(uow).list_admission_sources(
                    user_id=user_id, user_file_ids=unique_ids
                )
            }
            quota = await self.quota_repository_factory(uow).get(user_id)
            physical = await self._machine_available(uow)
        items: list[PrecomputeItemView] = []
        total = 0
        for file_id in unique_ids:
            source = sources.get(file_id)
            if source is None:
                items.append(
                    PrecomputeItemView(file_id, False, "not_found", "来源文件不存在")
                )
                continue
            if source.deletion_state is not ContentDeletionState.ACTIVE:
                items.append(
                    PrecomputeItemView(
                        file_id, False, "content_pending_delete", "来源正在删除，不能打包"
                    )
                )
                continue
            total += source.size
            items.append(
                PrecomputeItemView(
                    file_id, True, display_name=source.display_name, size=source.size
                )
            )
        available = min(available_user_bytes(quota), physical)
        return PrecomputeResultView(items=items, total_size=total, available_bytes=available)

    async def create(
        self,
        user_id: int,
        *,
        user_file_ids: list[int],
        output_name: str,
        delete_source: bool,
    ) -> int:
        if not 1 <= len(user_file_ids) <= MAX_PACK_SOURCES:
            raise DomainError(ErrorCode.INVALID_INPUT, "打包来源数量必须在 1 到 100 之间")
        validate_display_name(output_name)
        unique_ids = list(dict.fromkeys(user_file_ids))
        now_ms = self.clock_ms()
        async with self.uow_factory() as uow:
            repository = self.repository_factory(uow)
            sources = await repository.list_admission_sources(
                user_id=user_id, user_file_ids=unique_ids
            )
            if len(sources) != len(unique_ids):
                raise DomainError(ErrorCode.NOT_FOUND, "存在无效的打包来源")
            for source in sources:
                if source.deletion_state is not ContentDeletionState.ACTIVE:
                    raise DomainError(ErrorCode.CONTENT_PENDING_DELETE, "来源正在删除，不能打包")
            total = sum(source.size for source in sources)
            machine_available = await self._machine_available(uow)
            if total > machine_available:
                raise DomainError(ErrorCode.PHYSICAL_SPACE_EXCEEDED, "机器可用空间不足")
            pack_id = await repository.create_job(
                owner_id=user_id,
                output_name=output_name,
                pack_format=self.settings().pack_format,
                source_size=total,
                reserved_bytes=total,
                physical_reserved_bytes=total,
                delete_source=delete_source,
                now_ms=now_ms,
            )
            for source in sources:
                await repository.add_source(pack_id, source.snapshot())
            await self.quota_repository_factory(uow).reserve(user_id, total)
        return pack_id

    async def _machine_available(self, uow: UnitOfWork) -> int:
        downloads = await self.download_repository_factory(uow).list_active_physical()
        packs = await self.repository_factory(uow).list_active_physical()
        return available_machine_bytes(
            self.machine_space.machine_free_bytes(),
            self.settings().min_free_disk,
            downloads,
            packs,
        )
