from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Protocol

from ...domain.errors import DomainError, ErrorCode
from ..library.models import ContentRecord, UserFileRecord
from ..ports import UnitOfWork
from .models import PackJobRecord, PackOutputFileView, PackView


class PackRepositoryPort(Protocol):
    async def get_job(self, pack_id: int) -> PackJobRecord: ...

    async def list_recent_jobs(self, *, user_id: int, limit: int) -> list[PackJobRecord]: ...


class ContentRepositoryPort(Protocol):
    async def get_object(self, content_id: int) -> ContentRecord: ...

    async def find_user_file(self, *, user_id: int, content_id: int) -> int | None: ...

    async def get_user_file(self, *, user_id: int, file_id: int) -> UserFileRecord: ...


RepositoryFactory = Callable[[UnitOfWork], PackRepositoryPort]
ContentRepositoryFactory = Callable[[UnitOfWork], ContentRepositoryPort]
UowFactory = Callable[[], AbstractAsyncContextManager[UnitOfWork]]


class PackProgressService:
    """Pack progress views; DB PackStatus.DONE maps 1:1 to the public "done" value."""

    def __init__(
        self,
        *,
        uow_factory: UowFactory,
        repository_factory: RepositoryFactory,
        content_repository_factory: ContentRepositoryFactory,
    ) -> None:
        self.uow_factory = uow_factory
        self.repository_factory = repository_factory
        self.content_repository_factory = content_repository_factory

    async def get_pack(self, user_id: int, pack_id: int) -> PackView:
        async with self.uow_factory() as uow:
            job = await self.repository_factory(uow).get_job(pack_id)
            if job.owner_id != user_id:
                raise DomainError(ErrorCode.NOT_FOUND, "打包任务不存在")
            output = await self._output(uow, job)
        return self._view(job, output)

    async def list_packs(self, user_id: int, limit: int) -> list[PackView]:
        """Recent jobs of the user (newest first) — the pack task card source (F1)."""
        async with self.uow_factory() as uow:
            repository = self.repository_factory(uow)
            jobs = await repository.list_recent_jobs(user_id=user_id, limit=limit)
            outputs = [await self._output(uow, job) for job in jobs]
        return [self._view(job, output) for job, output in zip(jobs, outputs, strict=True)]

    def _view(self, job: PackJobRecord, output: PackOutputFileView | None) -> PackView:
        return PackView(
            id=job.id,
            status=job.status,
            step=job.step,
            step_progress=job.step_progress,
            step_started_at_ms=job.step_started_at_ms,
            created_at_ms=job.created_at_ms,
            updated_at_ms=job.updated_at_ms,
            finished_at_ms=job.finished_at_ms,
            error_code=job.error_code,
            delete_source=job.delete_source,
            output_name=job.output_name,
            output=output,
        )

    async def _output(self, uow: UnitOfWork, job: PackJobRecord) -> PackOutputFileView | None:
        if job.output_content_id is None:
            return None
        content = self.content_repository_factory(uow)
        file_id = await content.find_user_file(user_id=job.owner_id, content_id=job.output_content_id)
        if file_id is None:
            return None
        record = await content.get_user_file(user_id=job.owner_id, file_id=file_id)
        return PackOutputFileView(
            user_file_id=record.id, display_name=record.display_name, size=record.size
        )
