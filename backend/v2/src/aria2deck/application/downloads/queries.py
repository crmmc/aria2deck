from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import replace
from typing import Protocol

from ...domain.content import ContentKind
from ...domain.downloads import SubscriptionStatus
from ...domain.quota import (
    PhysicalDownload,
    PhysicalPack,
    available_machine_bytes,
    visible_space,
)
from ...domain.settings import SystemSettings
from ..library.models import ContentEntryRecord, ContentRecord
from ..ports import AccountRepositoryPort, UnitOfWork
from .models import DownloadRecord, ObservedFile, TaskPage, TaskProjection, TaskStats
from .projection import TaskProjector


class DownloadReadRepository(Protocol):
    async def list_tasks(
        self,
        *,
        user_id: int,
        sort: str = "updated_at",
        order: str = "desc",
    ) -> list[DownloadRecord]: ...

    async def get_task(self, *, user_id: int, task_id: int) -> DownloadRecord: ...

    async def list_active_physical(self) -> list[PhysicalDownload]: ...


class ContentReadRepository(Protocol):
    async def get_object(self, content_id: int) -> ContentRecord: ...

    async def list_files(self, content_id: int) -> list[ContentEntryRecord]: ...


class PackReadRepository(Protocol):
    async def list_active_physical(self) -> list[PhysicalPack]: ...


class MachineSpacePort(Protocol):
    def machine_free_bytes(self) -> int: ...


RepositoryFactory = Callable[[UnitOfWork], DownloadReadRepository]
ContentRepositoryFactory = Callable[[UnitOfWork], ContentReadRepository]
PackRepositoryFactory = Callable[[UnitOfWork], PackReadRepository]
AccountRepositoryFactory = Callable[[UnitOfWork], AccountRepositoryPort]
UowFactory = Callable[[], AbstractAsyncContextManager[UnitOfWork]]


class DownloadQueryService:
    def __init__(
        self,
        *,
        uow_factory: UowFactory,
        repository_factory: RepositoryFactory,
        content_repository_factory: ContentRepositoryFactory,
        pack_repository_factory: PackRepositoryFactory,
        account_repository_factory: AccountRepositoryFactory,
        projector: TaskProjector,
        machine_space: MachineSpacePort,
        settings: Callable[[], SystemSettings],
    ) -> None:
        self.uow_factory = uow_factory
        self.repository_factory = repository_factory
        self.content_repository_factory = content_repository_factory
        self.pack_repository_factory = pack_repository_factory
        self.account_repository_factory = account_repository_factory
        self.projector = projector
        self.machine_space = machine_space
        self.settings = settings

    async def list_tasks(
        self,
        *,
        user_id: int,
        status: SubscriptionStatus | None,
        search: str,
        page: int,
        page_size: int,
        sort: str = "created_at",
        order: str = "desc",
    ) -> TaskPage:
        async with self.uow_factory() as uow:
            records = await self.repository_factory(uow).list_tasks(
                user_id=user_id, sort=sort, order=order
            )
        tasks = [self.projector.project(record) for record in records]
        if status is not None:
            tasks = [task for task in tasks if task.status is status]
        normalized_search = search.strip().casefold()
        if normalized_search:
            tasks = [
                task
                for task in tasks
                if normalized_search in task.name.casefold()
                or (
                    task.display_uri is not None
                    and normalized_search in task.display_uri.casefold()
                )
            ]
        start = (page - 1) * page_size
        return TaskPage(
            items=tasks[start : start + page_size],
            total=len(tasks),
            page=page,
            page_size=page_size,
        )

    async def get_task(self, *, user_id: int, task_id: int) -> TaskProjection:
        async with self.uow_factory() as uow:
            record = await self.repository_factory(uow).get_task(
                user_id=user_id, task_id=task_id
            )
            projection = self.projector.project(record)
            if projection.files or record.completed_content_id is None:
                return projection
            contents = self.content_repository_factory(uow)
            content = await contents.get_object(record.completed_content_id)
            files: tuple[ObservedFile, ...]
            if content.kind is ContentKind.FILE:
                files = (
                    ObservedFile(
                        name=projection.name,
                        total_bytes=content.size,
                        completed_bytes=content.size,
                        selected=True,
                    ),
                )
            else:
                files = tuple(
                    ObservedFile(
                        name=entry.path,
                        total_bytes=entry.size,
                        completed_bytes=entry.size,
                        selected=True,
                    )
                    for entry in await contents.list_files(content.id)
                )
        return replace(projection, files=files)

    async def list_user_records(self, *, user_id: int) -> list[DownloadRecord]:
        """RPC/WS 适配用的原始记录视图（含真实 GID，仅在服务端解析，绝不外发）。"""
        async with self.uow_factory() as uow:
            return await self.repository_factory(uow).list_tasks(user_id=user_id)

    async def stats(self, *, user_id: int) -> TaskStats:
        async with self.uow_factory() as uow:
            records = await self.repository_factory(uow).list_tasks(user_id=user_id)
            quota = await self.account_repository_factory(uow).get_quota(user_id)
            downloads = await self.repository_factory(uow).list_active_physical()
            packs = await self.pack_repository_factory(uow).list_active_physical()
        tasks = [self.projector.project(record) for record in records]
        counts = {status: 0 for status in SubscriptionStatus}
        for task in tasks:
            counts[task.status] += 1
        headroom = available_machine_bytes(
            self.machine_space.machine_free_bytes(),
            self.settings().min_free_disk,
            downloads,
            packs,
        )
        return TaskStats(
            counts=counts,
            total=len(tasks),
            download_speed=sum(task.download_speed for task in tasks),
            upload_speed=sum(task.upload_speed for task in tasks),
            quota=quota,
            disk=visible_space(quota, machine_available_bytes=headroom),
        )
