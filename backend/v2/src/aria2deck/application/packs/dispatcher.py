from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Protocol

from ...domain.errors import DomainError, ErrorCode
from ...domain.packs import (
    PackStatus,
    PackStep,
    PreparedOutput,
    retry_delay_ms,
)
from ...domain.quota import Quota
from ...domain.settings import SystemSettings
from ..ports import UnitOfWork
from .models import (
    ArchiveResult,
    ArchiveSource,
    PackAdmissionSource,
    PackJobRecord,
    PackSourceRecord,
)

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 1.0

_DETERMINISTIC_CODES = frozenset(
    {ErrorCode.INVALID_STATE, ErrorCode.DUPLICATE_RESOURCE, ErrorCode.INVALID_INPUT}
)


class ArchiveWriterPort(Protocol):
    async def write_archive(
        self,
        destination: Path,
        *,
        sources: list[ArchiveSource],
        pack_format: str,
        compression_level: int,
        on_progress: Callable[[int], None] | None = None,
    ) -> ArchiveResult: ...


class PackContentStorePort(Protocol):
    def physical_path(self, storage_path: str, entry_relative_path: str | None = None) -> Path: ...

    async def verify_prepared(self, storage_path: str) -> tuple[int, str]: ...

    async def remove_prepared(self, storage_path: str) -> None: ...

    async def install_prepared(self, storage_path: str, digest: str) -> str: ...


class PackRepositoryPort(Protocol):
    async def list_packing_jobs(self) -> list[PackJobRecord]: ...

    async def get_job(self, pack_id: int) -> PackJobRecord: ...

    async def claim_next_pending(self, *, now_ms: int) -> int | None: ...

    async def update_step(
        self, pack_id: int, *, from_step: PackStep, to_step: PackStep, now_ms: int
    ) -> bool: ...

    async def update_step_progress(
        self, pack_id: int, *, step: PackStep, progress: int, now_ms: int
    ) -> bool: ...

    async def set_prepared(self, pack_id: int, prepared: PreparedOutput, *, now_ms: int) -> None: ...

    async def requeue_transient(self, pack_id: int, *, delay_ms: int, now_ms: int) -> bool: ...

    async def fail_job(self, pack_id: int, *, error_code: str, now_ms: int) -> bool: ...

    async def complete_job(self, pack_id: int, *, now_ms: int) -> int: ...

    async def list_admission_sources(
        self, *, user_id: int, user_file_ids: list[int]
    ) -> list[PackAdmissionSource]: ...

    async def list_sources(self, pack_id: int) -> list[PackSourceRecord]: ...


class QuotaRepositoryPort(Protocol):
    async def release(self, user_id: int, amount: int) -> Quota: ...


class WriterControlPort(Protocol):
    def register(self, pack_id: int, writer: asyncio.Task[None]) -> None: ...

    async def stop_and_wait(self, pack_id: int) -> None: ...


RepositoryFactory = Callable[[UnitOfWork], PackRepositoryPort]
QuotaRepositoryFactory = Callable[[UnitOfWork], QuotaRepositoryPort]
UowFactory = Callable[[], AbstractAsyncContextManager[UnitOfWork]]
ClockMs = Callable[[], int]
SettingsGetter = Callable[[], SystemSettings]
Sleep = Callable[[float], Awaitable[object]]


def _part_path(pack_id: int) -> str:
    return f"packs/prepared-{pack_id}.part"


def _deterministic(error: Exception) -> bool:
    return isinstance(error, DomainError) and error.code in _DETERMINISTIC_CODES


class PackDispatcher:
    """Single driver of pack jobs: claim → validate → compress → verify → install → done."""

    def __init__(
        self,
        *,
        uow_factory: UowFactory,
        repository_factory: RepositoryFactory,
        quota_repository_factory: QuotaRepositoryFactory,
        writer: ArchiveWriterPort,
        content_store: PackContentStorePort,
        writer_control: WriterControlPort,
        settings: SettingsGetter,
        clock_ms: ClockMs,
        sleep: Sleep = asyncio.sleep,
        poll_interval_seconds: float = POLL_INTERVAL_SECONDS,
    ) -> None:
        self.uow_factory = uow_factory
        self.repository_factory = repository_factory
        self.quota_repository_factory = quota_repository_factory
        self.writer = writer
        self.content_store = content_store
        self.writer_control = writer_control
        self.settings = settings
        self.clock_ms = clock_ms
        self.sleep = sleep
        self.poll_interval_seconds = poll_interval_seconds
        # dispatcher 是唯一推进者：串行化 step_once，避免把同进程内在途的 packing 任务
        # 误判为崩溃残留（恢复逻辑会清 part 并重排）。
        self._step_lock = asyncio.Lock()

    async def run_worker(self) -> None:
        while True:
            await self.step_once()
            await self.sleep(self.poll_interval_seconds)

    async def step_once(self) -> None:
        """One scheduling tick: resume stale packing jobs first, then claim at most one job."""
        async with self._step_lock:
            await self._step_once_locked()

    async def _step_once_locked(self) -> None:
        await self._resume_packing_jobs()
        pack_id = await self._claim()
        if pack_id is None:
            return
        task = asyncio.create_task(self._execute(pack_id))
        self.writer_control.register(pack_id, task)
        current = asyncio.current_task()
        try:
            await task
        except asyncio.CancelledError:
            # 取消端点只取消写入者；supervisor 关闭会沿 await 链同时取消两者。
            # 当前任务自身处于取消中即为外部关闭：必须继续上抛，否则吞掉关停信号。
            if current is not None and current.cancelling():
                raise
        except Exception as error:
            await self._handle_failure(pack_id, error)
        finally:
            await self.writer_control.stop_and_wait(pack_id)

    async def _claim(self) -> int | None:
        async with self.uow_factory() as uow:
            return await self.repository_factory(uow).claim_next_pending(now_ms=self.clock_ms())

    async def _resume_packing_jobs(self) -> None:
        async with self.uow_factory() as uow:
            jobs = await self.repository_factory(uow).list_packing_jobs()
        for job in jobs:
            try:
                await self._resume(job)
            except Exception as error:
                await self._handle_failure(job.id, error)

    async def _resume(self, job: PackJobRecord) -> None:
        """Restart recovery for a packing job left behind by a crash."""
        if job.prepared is None:
            # 压缩未完成：丢弃半成品 part 文件并暂态重排。
            await self.content_store.remove_prepared(_part_path(job.id))
            await self._requeue(job)
            return
        try:
            size, digest = await self.content_store.verify_prepared(job.prepared.path)
        except DomainError:
            # part 缺失：可能崩溃于「移动到规范位之后、DB 完成之前」。
            canonical = f"objects/file-{job.prepared.digest}"
            try:
                canonical_size, canonical_digest = await self.content_store.verify_prepared(canonical)
            except DomainError:
                await self.content_store.remove_prepared(job.prepared.path)
                await self._requeue(job)
                return
            if (canonical_size, canonical_digest) != (job.prepared.size, job.prepared.digest):
                await self._requeue(job)
                return
        else:
            if (size, digest) != (job.prepared.size, job.prepared.digest):
                await self.content_store.remove_prepared(job.prepared.path)
                await self._requeue(job)
                return
            await self.content_store.install_prepared(job.prepared.path, job.prepared.digest)
        async with self.uow_factory() as uow:
            await self.repository_factory(uow).complete_job(job.id, now_ms=self.clock_ms())

    async def _requeue(self, job: PackJobRecord) -> None:
        # transient=True 时 retry_delay_ms 必有返回值。
        delay = retry_delay_ms(job.retry_count + 1, transient=True) or 0
        async with self.uow_factory() as uow:
            await self.repository_factory(uow).requeue_transient(
                job.id, delay_ms=delay, now_ms=self.clock_ms()
            )

    async def _execute(self, pack_id: int) -> None:
        async with self.uow_factory() as uow:
            job = await self.repository_factory(uow).get_job(pack_id)
        if job.status is not PackStatus.PACKING:
            return
        sources = await self._validate_sources(job)
        async with self.uow_factory() as uow:
            await self.repository_factory(uow).update_step(
                pack_id,
                from_step=PackStep.VALIDATING,
                to_step=PackStep.COMPRESSING,
                now_ms=self.clock_ms(),
            )
        await self._compress(job, sources)
        async with self.uow_factory() as uow:
            await self.repository_factory(uow).update_step(
                pack_id,
                from_step=PackStep.COMPRESSING,
                to_step=PackStep.VERIFYING,
                now_ms=self.clock_ms(),
            )
        await self._verify(pack_id)
        await self._install(pack_id)

    async def _validate_sources(self, job: PackJobRecord) -> list[PackAdmissionSource]:
        async with self.uow_factory() as uow:
            records = await self.repository_factory(uow).list_sources(job.id)
            sources = await self.repository_factory(uow).list_admission_sources(
                user_id=job.owner_id,
                user_file_ids=[record.snapshot.user_file_id for record in records],
            )
            snapshots = {record.snapshot.user_file_id: record.snapshot for record in records}
        by_file = {source.user_file_id: source for source in sources}
        for file_id, snapshot in snapshots.items():
            current = by_file.get(file_id)
            if current is None or current.snapshot() != snapshot:
                raise DomainError(ErrorCode.INVALID_STATE, "来源快照与当前状态不一致")
        return sources

    async def _compress(self, job: PackJobRecord, sources: list[PackAdmissionSource]) -> None:
        archive_sources = [
            ArchiveSource(
                display_name=source.display_name,
                root=self.content_store.physical_path(source.storage_path),
                kind=source.kind,
            )
            for source in sources
        ]
        loop = asyncio.get_running_loop()

        def save_progress(percent: int) -> None:
            # 写入线程内同步回调：桥接回事件循环落库。
            asyncio.run_coroutine_threadsafe(
                self._save_progress(job.id, percent), loop
            ).result()

        await self.writer.write_archive(
            self.content_store.physical_path(_part_path(job.id)),
            sources=archive_sources,
            pack_format=job.pack_format,
            compression_level=self.settings().pack_compression_level,
            on_progress=save_progress,
        )

    async def _save_progress(self, pack_id: int, percent: int) -> None:
        async with self.uow_factory() as uow:
            await self.repository_factory(uow).update_step_progress(
                pack_id, step=PackStep.COMPRESSING, progress=percent, now_ms=self.clock_ms()
            )

    async def _verify(self, pack_id: int) -> None:
        size, digest = await self.content_store.verify_prepared(_part_path(pack_id))
        async with self.uow_factory() as uow:
            await self.repository_factory(uow).set_prepared(
                pack_id, PreparedOutput(_part_path(pack_id), size, digest), now_ms=self.clock_ms()
            )

    async def _install(self, pack_id: int) -> None:
        async with self.uow_factory() as uow:
            job = await self.repository_factory(uow).get_job(pack_id)
        if job.prepared is None:
            raise DomainError(ErrorCode.INVALID_STATE, "prepared 输出缺失，不能安装")
        size, digest = await self.content_store.verify_prepared(job.prepared.path)
        if (size, digest) != (job.prepared.size, job.prepared.digest):
            raise DomainError(ErrorCode.INVALID_STATE, "prepared 输出与登记结果不一致")
        await self.content_store.install_prepared(job.prepared.path, digest)
        async with self.uow_factory() as uow:
            await self.repository_factory(uow).complete_job(pack_id, now_ms=self.clock_ms())

    async def _handle_failure(self, pack_id: int, error: Exception) -> None:
        async with self.uow_factory() as uow:
            job = await self.repository_factory(uow).get_job(pack_id)
        if job.status is not PackStatus.PACKING:
            return  # 已被取消或完成：不覆盖外部状态。
        logger.warning(
            "打包任务失败 pack_id=%s error_type=%s deterministic=%s",
            pack_id,
            type(error).__name__,
            _deterministic(error),
        )
        if _deterministic(error):
            async with self.uow_factory() as uow:
                await self.quota_repository_factory(uow).release(job.owner_id, job.reserved_bytes)
                await self.repository_factory(uow).fail_job(
                    pack_id,
                    error_code=str(error.code) if isinstance(error, DomainError) else "internal",
                    now_ms=self.clock_ms(),
                )
            await self.content_store.remove_prepared(_part_path(pack_id))
            return
        await self.content_store.remove_prepared(_part_path(pack_id))
        await self._requeue(job)
