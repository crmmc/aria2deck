from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Awaitable, Callable, Coroutine
from contextlib import AbstractAsyncContextManager
from functools import partial
from pathlib import Path
from typing import Any, Protocol

from ...domain.errors import DomainError, ErrorCode
from ...domain.packs import (
    PackStatus,
    PackStep,
    PreparedOutput,
    require_pack_output_within_budget,
    retry_delay_ms,
)
from ...domain.quota import Quota
from ...domain.settings import SystemSettings
from ..ports import UnitOfWork
from .models import (
    ArchiveResult,
    ArchiveSource,
    InstallOutcome,
    PackAdmissionSource,
    PackJobRecord,
    PackSourceRecord,
)

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 1.0

_DETERMINISTIC_CODES = frozenset(
    {
        ErrorCode.INVALID_STATE,
        ErrorCode.DUPLICATE_RESOURCE,
        ErrorCode.INVALID_INPUT,
        ErrorCode.TASK_TOO_LARGE,
        ErrorCode.QUOTA_EXCEEDED,
    }
)


class ArchiveWriterPort(Protocol):
    async def write_archive(
        self,
        destination: Path,
        *,
        sources: list[ArchiveSource],
        pack_format: str,
        compression_level: int,
        max_output_bytes: int,
        min_free_bytes: int,
        cancel_event: threading.Event,
        on_progress: Callable[[int], None] | None = None,
    ) -> ArchiveResult: ...


class PackContentStorePort(Protocol):
    def physical_path(self, storage_path: str, entry_relative_path: str | None = None) -> Path: ...

    async def verify_prepared(self, storage_path: str) -> tuple[int, str]: ...

    async def remove_prepared(self, storage_path: str) -> None: ...

    async def install_prepared(self, storage_path: str, digest: str) -> InstallOutcome: ...

    async def physical_delete(self, storage_path: str) -> None: ...


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

    async def requeue_transient(
        self,
        pack_id: int,
        *,
        delay_ms: int,
        now_ms: int,
        discard_prepared: bool = False,
    ) -> bool: ...

    async def fail_job(self, pack_id: int, *, error_code: str, now_ms: int) -> bool: ...

    async def content_object_exists(self, content_key: str) -> bool: ...

    async def complete_job(self, pack_id: int, *, now_ms: int) -> int: ...

    async def list_admission_sources(
        self, *, user_id: int, user_file_ids: list[int]
    ) -> list[PackAdmissionSource]: ...

    async def list_sources(self, pack_id: int) -> list[PackSourceRecord]: ...


class QuotaRepositoryPort(Protocol):
    async def release(self, user_id: int, amount: int) -> Quota: ...


class WriterControlPort(Protocol):
    def register(
        self, pack_id: int, writer: asyncio.Task[None], cancel_event: threading.Event
    ) -> bool: ...

    async def stop_and_wait(self, pack_id: int) -> None: ...

    async def wait_for_writer(self, pack_id: int, writer: asyncio.Task[None]) -> None: ...

    def allows_resume(self, pack_id: int) -> bool: ...


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


def _raise_if_stopped(cancel_event: threading.Event) -> None:
    """停止请求是取消，不是失败：直接离开当前阶段，不落库伪造终态。"""
    if cancel_event.is_set():
        raise asyncio.CancelledError


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
            await self._resume_packing_jobs()
            pack_id = await self._claim()
            if pack_id is None:
                return
            await self._run_job(pack_id, partial(self._execute, pack_id))

    async def _run_job(
        self,
        pack_id: int,
        runner: Callable[[threading.Event], Coroutine[Any, Any, None]],
    ) -> None:
        """Create the per-job stop event and task, register both, then await the task."""
        cancel_event = threading.Event()
        task = asyncio.create_task(self._run_registered_job(pack_id, runner, cancel_event))
        try:
            self.writer_control.register(pack_id, task, cancel_event)
        except BaseException:
            # 注册失败也不得留下未等待的孤儿任务。
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise
        await self._supervise(pack_id, task)

    async def _run_registered_job(
        self,
        pack_id: int,
        runner: Callable[[threading.Event], Coroutine[Any, Any, None]],
        cancel_event: threading.Event,
    ) -> None:
        """业务执行与失败收尾同属注册任务：停止等待和 ID 复用都必须等它结束。"""
        try:
            await runner(cancel_event)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self._handle_failure(pack_id, error)

    async def _supervise(self, pack_id: int, task: asyncio.Task[None]) -> None:
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                # 外部关停：只请求一次有上限的停止，再继续传播原取消。
                await self._stop_for_shutdown(pack_id)
                raise
            # 执行任务内部的停止：状态由取消端点落库，这里不重复失败处理。
        finally:
            # 只观察已有等待：不再请求停止，也不取消执行任务。
            await self.writer_control.wait_for_writer(pack_id, task)

    async def _stop_for_shutdown(self, pack_id: int) -> None:
        try:
            await self.writer_control.stop_and_wait(pack_id)
        except DomainError as error:
            if error.code is not ErrorCode.RECOVERY_UNSAFE:
                raise
            logger.warning("打包写入者停止超时 pack_id=%s 保留现场等待重试或重启", pack_id)
        except asyncio.CancelledError:
            logger.warning("关停期间的停止等待被中断 pack_id=%s 保留现场", pack_id)

    async def _claim(self) -> int | None:
        async with self.uow_factory() as uow:
            return await self.repository_factory(uow).claim_next_pending(now_ms=self.clock_ms())

    async def _resume_packing_jobs(self) -> None:
        async with self.uow_factory() as uow:
            jobs = await self.repository_factory(uow).list_packing_jobs()
        for job in jobs:
            if not self.writer_control.allows_resume(job.id):
                # 本进程仍在停止/已请求停止：保持 packing 与单活跃槽位，不得自动重排。
                continue
            await self._run_job(job.id, partial(self._resume, job))

    async def _resume(self, job: PackJobRecord, cancel_event: threading.Event) -> None:
        """Restart recovery for a packing job left behind by a crash."""
        _raise_if_stopped(cancel_event)
        if job.prepared is None:
            # 压缩未完成：丢弃半成品 part 文件并暂态重排。
            await self.content_store.remove_prepared(_part_path(job.id))
            await self._requeue(job)
            return
        prepared = job.prepared
        canonical = f"objects/file-{prepared.digest}"
        part_state = await self._probe(prepared.path)
        if part_state is None:
            canonical_state = await self._probe(canonical)
            if canonical_state is None:
                # part 与规范位都不在：旧登记无法证明可用，清空后重新压缩。
                await self._requeue(job, discard_prepared=True)
                return
            if canonical_state != (prepared.size, prepared.digest):
                # 持久性内容冲突：重压缩修不好，确定性失败并保留 canonical。
                raise DomainError(ErrorCode.INVALID_STATE, "规范位内容与登记结果不一致")
        elif part_state != (prepared.size, prepared.digest):
            await self.content_store.remove_prepared(_part_path(job.id))
            await self._requeue(job, discard_prepared=True)
            return
        _raise_if_stopped(cancel_event)
        await self._install_and_complete(job, cancel_event)

    async def _requeue(self, job: PackJobRecord, *, discard_prepared: bool = False) -> None:
        # transient=True 时 retry_delay_ms 必有返回值。
        delay = retry_delay_ms(job.retry_count + 1, transient=True) or 0
        async with self.uow_factory() as uow:
            await self.repository_factory(uow).requeue_transient(
                job.id,
                delay_ms=delay,
                now_ms=self.clock_ms(),
                discard_prepared=discard_prepared,
            )

    async def _execute(self, pack_id: int, cancel_event: threading.Event) -> None:
        _raise_if_stopped(cancel_event)
        async with self.uow_factory() as uow:
            job = await self.repository_factory(uow).get_job(pack_id)
        if job.status is not PackStatus.PACKING:
            return  # 已被取消/完成：晚到的执行不得推进状态。
        if job.prepared is not None:
            # 已有 prepared：走同一恢复/安装路径，不重新压缩、不重复注册。
            await self._resume(job, cancel_event)
            return
        _raise_if_stopped(cancel_event)
        sources = await self._validate_sources(job)
        _raise_if_stopped(cancel_event)
        if not await self._advance(pack_id, PackStep.VALIDATING, PackStep.COMPRESSING):
            return
        _raise_if_stopped(cancel_event)
        await self._compress(job, sources, cancel_event)
        _raise_if_stopped(cancel_event)
        if not await self._advance(pack_id, PackStep.COMPRESSING, PackStep.VERIFYING):
            return
        _raise_if_stopped(cancel_event)
        await self._verify(pack_id)
        await self._install(pack_id, cancel_event=cancel_event)

    async def _advance(self, pack_id: int, source: PackStep, target: PackStep) -> bool:
        async with self.uow_factory() as uow:
            return await self.repository_factory(uow).update_step(
                pack_id, from_step=source, to_step=target, now_ms=self.clock_ms()
            )

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

    async def _compress(
        self,
        job: PackJobRecord,
        sources: list[PackAdmissionSource],
        cancel_event: threading.Event,
    ) -> None:
        archive_sources = [
            ArchiveSource(
                display_name=source.display_name,
                root=self.content_store.physical_path(source.storage_path),
                kind=source.kind,
            )
            for source in sources
        ]
        loop = asyncio.get_running_loop()
        settings = self.settings()

        def save_progress(percent: int) -> None:
            # 写入线程内同步回调：桥接回事件循环落库。
            asyncio.run_coroutine_threadsafe(
                self._save_progress(job.id, percent), loop
            ).result()

        try:
            await self.writer.write_archive(
                self.content_store.physical_path(_part_path(job.id)),
                sources=archive_sources,
                pack_format=job.pack_format,
                compression_level=settings.pack_compression_level,
                max_output_bytes=job.reserved_bytes,
                min_free_bytes=settings.min_free_disk,
                cancel_event=cancel_event,
                on_progress=save_progress,
            )
        except InterruptedError:
            # 只有停止事件已设置才视为取消；普通 OSError 继续走暂态失败分类。
            if cancel_event.is_set():
                raise asyncio.CancelledError from None
            raise

    async def _save_progress(self, pack_id: int, percent: int) -> None:
        async with self.uow_factory() as uow:
            await self.repository_factory(uow).update_step_progress(
                pack_id, step=PackStep.COMPRESSING, progress=percent, now_ms=self.clock_ms()
            )

    async def _verify(self, pack_id: int) -> None:
        async with self.uow_factory() as uow:
            job = await self.repository_factory(uow).get_job(pack_id)
        size, digest = await self.content_store.verify_prepared(_part_path(pack_id))
        require_pack_output_within_budget(size, job.reserved_bytes)
        async with self.uow_factory() as uow:
            await self.repository_factory(uow).set_prepared(
                pack_id, PreparedOutput(_part_path(pack_id), size, digest), now_ms=self.clock_ms()
            )

    async def _install(
        self, pack_id: int, *, cancel_event: threading.Event | None = None
    ) -> None:
        async with self.uow_factory() as uow:
            job = await self.repository_factory(uow).get_job(pack_id)
        await self._install_and_complete(job, cancel_event or threading.Event())

    async def _install_and_complete(
        self, job: PackJobRecord, cancel_event: threading.Event
    ) -> None:
        """校验、安装并结算一个 prepared 输出；正常执行与恢复共用同一条安装路径。"""
        _raise_if_stopped(cancel_event)
        prepared = job.prepared
        if prepared is None:
            raise DomainError(ErrorCode.INVALID_STATE, "prepared 输出缺失，不能安装")
        canonical = f"objects/file-{prepared.digest}"
        part_state = await self._probe(prepared.path)
        outcome: InstallOutcome | None = None
        try:
            if part_state is None:
                # part 已不在：只能补登记 canonical，本次没有安装产生的新建归属。
                canonical_state = await self._probe(canonical)
                if canonical_state is None:
                    raise DomainError(ErrorCode.INVALID_STATE, "prepared 输出文件不存在")
                if canonical_state != (prepared.size, prepared.digest):
                    raise DomainError(ErrorCode.INVALID_STATE, "规范位内容与登记结果不一致")
                require_pack_output_within_budget(canonical_state[0], job.reserved_bytes)
            else:
                if part_state != (prepared.size, prepared.digest):
                    raise DomainError(ErrorCode.INVALID_STATE, "prepared 输出与登记结果不一致")
                require_pack_output_within_budget(part_state[0], job.reserved_bytes)
                existing = await self._probe(canonical)
                if existing is not None and existing != (prepared.size, prepared.digest):
                    raise DomainError(ErrorCode.INVALID_STATE, "规范位内容与登记结果不一致")
                outcome, cancelled = await self._install_shielded(prepared)
                if cancelled:
                    raise asyncio.CancelledError
            # 无论直接安装或 canonical 补登记，完成前都再次确认停止事件。
            _raise_if_stopped(cancel_event)
            async with self.uow_factory() as uow:
                await self.repository_factory(uow).complete_job(job.id, now_ms=self.clock_ms())
        except asyncio.CancelledError:
            # 取消：回收本次新建、且回滚后仍无内容对象的 canonical。
            await self._reclaim_canonical_if_owned(prepared, outcome)
            raise
        except Exception:
            # complete 失败或确定性失败都按 ownership 回收；已有/共享内容不受影响。
            await self._reclaim_canonical_if_owned(prepared, outcome)
            raise

    async def _install_shielded(
        self, prepared: PreparedOutput
    ) -> tuple[InstallOutcome, bool]:
        """shield 真实安装，并返回 (安装结果, 是否在等待中收到取消)。

        外层取消时必须继续等到真实 rename 结束，否则会丢掉 created 归属信息。
        """
        installing = asyncio.create_task(
            self.content_store.install_prepared(prepared.path, prepared.digest)
        )
        cancelled = False
        while not installing.done():
            try:
                await asyncio.shield(installing)
            except asyncio.CancelledError:
                # 第二次取消也不能提前返回：同一真实安装只有一个结果。
                cancelled = True
        if installing.cancelled():
            raise asyncio.CancelledError
        return installing.result(), cancelled

    async def _reclaim_canonical_if_owned(
        self, prepared: PreparedOutput, outcome: InstallOutcome | None
    ) -> None:
        """回收本次安装新建、且仍无内容对象的 canonical；共享或历史内容一律保留。"""
        if outcome is None or not outcome.created_by_this_attempt:
            return
        try:
            async with self.uow_factory() as uow:
                registered = await self.repository_factory(uow).content_object_exists(
                    f"file:{prepared.digest}"
                )
            if registered:
                return
            await self.content_store.physical_delete(outcome.storage_path)
        except Exception:
            # 不能因为回收失败就改写原来的取消/失败语义。
            logger.warning(
                "打包安装后的 canonical 回收失败，保留现场 storage_path=%s",
                outcome.storage_path,
                exc_info=True,
            )

    async def _probe(self, storage_path: str) -> tuple[int, str] | None:
        """返回 (size, digest)；只有真实不存在返回 None，目录/符号链接等错误如实上抛。"""
        try:
            return await self.content_store.verify_prepared(storage_path)
        except DomainError as error:
            if error.code is not ErrorCode.NOT_FOUND:
                raise
            return None

    async def _handle_failure(self, pack_id: int, error: Exception) -> None:
        part = _part_path(pack_id)
        try:
            async with self.uow_factory() as uow:
                job = await self.repository_factory(uow).get_job(pack_id)
                if job.status is not PackStatus.PACKING:
                    return  # 已被取消或完成：不覆盖外部状态。
        except DomainError as read_error:
            if read_error.code is not ErrorCode.NOT_FOUND:
                raise
            # 行已被外部清理删除：防御性返回，绝不触碰复用 ID 的新任务。
            return
        if not _deterministic(error):
            logger.warning(
                "打包任务暂态失败 pack_id=%s error_type=%s", pack_id, type(error).__name__
            )
            try:
                # 仍在 packing 时先清现场；删除失败保留 PACKING 与预留，交给恢复重试。
                await self.content_store.remove_prepared(part)
            except OSError:
                logger.warning(
                    "打包失败后的现场清理失败，保留 packing 待恢复重试 pack_id=%s",
                    pack_id,
                    exc_info=True,
                )
                return
            await self._requeue(job)
            return
        logger.warning(
            "打包任务失败 pack_id=%s error_type=%s deterministic=True",
            pack_id,
            type(error).__name__,
        )
        try:
            # 仍在 packing 时先清现场；删除失败不得提交 FAILED，否则物理清理再无入口。
            await self.content_store.remove_prepared(part)
        except OSError:
            logger.warning(
                "打包失败后的现场清理失败，保留 packing 待恢复重试 pack_id=%s",
                pack_id,
                exc_info=True,
            )
            return
        async with self.uow_factory() as uow:
            # 同一 BEGIN IMMEDIATE UoW 内重读：取消与失败竞态只允许一方结算。
            try:
                fresh = await self.repository_factory(uow).get_job(pack_id)
            except DomainError as read_error:
                if read_error.code is not ErrorCode.NOT_FOUND:
                    raise
                return  # 行已被外部清理删除：不做任何旧收尾。
            if fresh.status is not PackStatus.PACKING:
                return  # 现场已清，状态由对方结算。
            try:
                await self.quota_repository_factory(uow).release(
                    fresh.owner_id, fresh.reserved_bytes
                )
            except DomainError as quota_error:
                if quota_error.code is not ErrorCode.QUOTA_EXCEEDED:
                    raise
                # 账目已损坏：不修补、不重复释放，只清该任务的预留并落终态。
                logger.error(
                    "打包失败结算跳过预留释放：配额账目已损坏 pack_id=%s user_id=%s",
                    pack_id,
                    fresh.owner_id,
                )
            await self.repository_factory(uow).fail_job(
                pack_id,
                error_code=str(error.code) if isinstance(error, DomainError) else "internal",
                now_ms=self.clock_ms(),
            )
