from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from typing import Protocol

from ...domain.content import ContentDeletionState, ContentKind, content_identity
from ...domain.downloads import (
    ADMISSION_PAUSED,
    EXTERNAL_PAUSED,
    METADATA_ADMISSION_PAUSED,
    TERMINAL_SUBSCRIPTION_STATUSES,
    AttemptStatus,
    adopted_display_name,
    magnet_missing_name_fallback,
)
from ...domain.errors import DomainError, ErrorCode
from ...domain.settings import SystemSettings
from ..ports import UnitOfWork
from .broadcast import BroadcastPort, task_message
from .commands import SubmissionBackend
from .models import (
    AttemptRecord,
    ContentPreparation,
    DownloadObservation,
    ManifestEntry,
    SourceRecord,
    TaskProjection,
)

logger = logging.getLogger(__name__)
UowFactory = Callable[[], AbstractAsyncContextManager[UnitOfWork]]
Sleep = Callable[[float], Awaitable[None]]


class LifecycleRepository(Protocol):
    async def find_attempt_by_gid(self, gid: str) -> AttemptRecord | None: ...

    async def list_subscription_owners(self, attempt_id: int) -> list[tuple[int, int]]: ...

    async def get_attempt(self, attempt_id: int) -> AttemptRecord: ...

    async def get_source_for_attempt(self, attempt_id: int) -> SourceRecord: ...

    async def list_live_attempts(self, *, submitted_only: bool = False) -> list[AttemptRecord]: ...

    async def list_completion_intents(self) -> list[AttemptRecord]: ...

    async def record_manifest(
        self, attempt_id: int, *, entries: tuple[ManifestEntry, ...], now_ms: int
    ) -> None: ...

    async def get_manifest(self, attempt_id: int) -> tuple[ManifestEntry, ...] | None: ...

    async def apply_observation(
        self,
        attempt_id: int,
        *,
        expected_gid: str,
        status: AttemptStatus,
        completed_length: int,
        total_length: int | None,
        now_ms: int,
        clear_pause: bool = False,
        external_pause: bool = False,
    ) -> bool: ...

    async def adopt_display_name(
        self, attempt_id: int, *, fallback_name: str, display_name: str, now_ms: int
    ) -> None: ...

    async def admit_size(
        self,
        *,
        attempt_id: int,
        observed_bytes: int,
        known_total: bool,
        disk_free_bytes: int,
        min_free_bytes: int,
        max_task_bytes: int,
        now_ms: int,
    ) -> DomainError | None: ...

    async def handoff_attempt(
        self, attempt_id: int, *, expected_gid: str, new_gid: str, now_ms: int
    ) -> None: ...

    async def start_ingestion(
        self, attempt_id: int, *, expected_gid: str, now_ms: int
    ) -> None: ...

    async def begin_completion(
        self, attempt_id: int, *, expected_gid: str, content_id: int, now_ms: int
    ) -> None: ...

    async def finalize_completion(
        self, attempt_id: int, *, expected_gid: str, now_ms: int
    ) -> bool: ...

    async def fail_attempt(
        self,
        attempt_id: int,
        *,
        error_code: str,
        now_ms: int,
        expected_gid: str | None = None,
    ) -> None: ...

    async def mark_stop_uncertain(self, attempt_id: int) -> None: ...

    async def list_terminal_cleanup(self) -> list[AttemptRecord]: ...

    async def finish_terminal_cleanup(
        self, attempt_id: int, *, expected_gid: str, now_ms: int
    ) -> None: ...

    async def list_all_gids(self) -> list[str]: ...


class ContentView(Protocol):
    @property
    def id(self) -> int: ...

    @property
    def kind(self) -> ContentKind: ...

    @property
    def digest(self) -> str: ...

    @property
    def size(self) -> int: ...

    @property
    def storage_path(self) -> str: ...

    @property
    def deletion_state(self) -> ContentDeletionState: ...


class LifecycleContentRepository(Protocol):
    async def find_object_by_key(self, content_key: str) -> ContentView | None: ...

    async def create_object(
        self, *, kind: str, digest: str, size: int, storage_path: str, now_ms: int
    ) -> int: ...

    async def add_entry(
        self,
        *,
        content_id: int,
        parent_path: str,
        name: str,
        entry_kind: str,
        size: int,
        now_ms: int,
    ) -> int: ...

    async def get_object(self, content_id: int) -> ContentView: ...


class LifecycleBackend(SubmissionBackend, Protocol):
    async def version(self) -> tuple[str, list[str]]: ...

    async def snapshot(self, *, gid: str, attempt_id: int) -> DownloadObservation | None: ...

    async def unpause(self, *, gid: str) -> None: ...

    async def remove_result(self, *, gid: str) -> None: ...

    async def list_live_gids(self) -> list[str]: ...

    async def listen(self, on_gid: Callable[[str], Awaitable[None]]) -> None: ...


class ObservationSink(Protocol):
    def put(self, observation: DownloadObservation) -> None: ...

    def discard(self, gid: str) -> None: ...


class ContentStore(Protocol):
    def machine_free_bytes(self) -> int: ...

    async def collect(
        self, attempt_id: int, manifest: tuple[ManifestEntry, ...]
    ) -> ContentPreparation: ...

    async def install(
        self,
        attempt_id: int,
        expected: ContentPreparation,
        manifest: tuple[ManifestEntry, ...],
    ) -> None: ...

    async def cleanup_attempt(self, attempt_id: int) -> None: ...


class TaskQueryPort(Protocol):
    async def get_task(self, *, user_id: int, task_id: int) -> TaskProjection: ...


class Connectivity(Protocol):
    def record_success(self) -> None: ...

    def record_failure(self) -> None: ...


RepositoryFactory = Callable[[UnitOfWork], LifecycleRepository]
ContentRepositoryFactory = Callable[[UnitOfWork], LifecycleContentRepository]


def _manifest_entries(snapshot: DownloadObservation) -> tuple[ManifestEntry, ...]:
    """完成清单的唯一口径：只收选中项（未选中文件 aria2 不落盘），路径用 attempt 内相对路径。"""
    return tuple(
        ManifestEntry(path=file.relpath, size=file.total_bytes)
        for file in snapshot.files
        if file.selected
    )


class DownloadLifecycleService:
    """Own every lifecycle write; transport workers only obtain and submit snapshots."""

    def __init__(
        self,
        *,
        uow_factory: UowFactory,
        repository_factory: RepositoryFactory,
        content_repository_factory: ContentRepositoryFactory,
        backend: LifecycleBackend,
        content_store: ContentStore,
        observations: ObservationSink,
        connectivity: Connectivity,
        settings: Callable[[], SystemSettings],
        clock_ms: Callable[[], int],
        poll_interval_seconds: float = 2.0,
        probe_timeout_seconds: float = 2.0,
        reconnect_max_seconds: float = 30.0,
        missing_grace_ms: int = 10_000,
        ingestion_timeout_ms: int = 60_000,
        jitter: Callable[[], float] = lambda: 0.0,
        sleep: Sleep = asyncio.sleep,
        broadcast: BroadcastPort | None = None,
        queries: TaskQueryPort | None = None,
        terminal_broadcast_capacity: int = 10_000,
    ) -> None:
        self.uow_factory = uow_factory
        self.repository_factory = repository_factory
        self.content_repository_factory = content_repository_factory
        self.backend = backend
        self.content_store = content_store
        self.observations = observations
        self.connectivity = connectivity
        self.settings = settings
        self.clock_ms = clock_ms
        self.poll_interval_seconds = poll_interval_seconds
        self.probe_timeout_seconds = probe_timeout_seconds
        self.reconnect_max_seconds = reconnect_max_seconds
        self.missing_grace_ms = missing_grace_ms
        self.ingestion_timeout_ms = ingestion_timeout_ms
        self.jitter = jitter
        self.sleep = sleep
        self.broadcast = broadcast
        self.queries = queries
        self._locks: dict[int, asyncio.Lock] = {}
        self._missing_since: dict[int, int] = {}
        # 终态帧幂等：粒度 (attempt_id, task_id)，按插入顺序有界淘汰；淘汰后由前端与 REST 重读兜底。
        self._terminal_broadcast: OrderedDict[tuple[int, int], None] = OrderedDict()
        self._terminal_broadcast_capacity = terminal_broadcast_capacity

    async def _attempt_and_source(
        self, attempt_id: int, gid: str
    ) -> tuple[AttemptRecord, SourceRecord]:
        async with self.uow_factory() as uow:
            repository = self.repository_factory(uow)
            attempt = await repository.get_attempt(attempt_id)
            if attempt.gid != gid:
                raise DomainError(ErrorCode.STALE_EVENT, "下载事件属于旧 GID")
            return attempt, await repository.get_source_for_attempt(attempt_id)

    async def reconcile(self, snapshot: DownloadObservation) -> None:
        async with self.uow_factory() as uow:
            attempt = await self.repository_factory(uow).find_attempt_by_gid(snapshot.gid)
        if attempt is None:
            return
        lock = self._locks.setdefault(attempt.id, asyncio.Lock())
        async with lock:
            self._missing_since.pop(attempt.id, None)
            try:
                await self._reconcile(snapshot, attempt.id)
            except DomainError as error:
                if error.code in {ErrorCode.STALE_EVENT, ErrorCode.NOT_FOUND}:
                    self.observations.discard(snapshot.gid)
                    return
                raise
            finally:
                # 单一广播挂接点：决策结束后推送 DB 权威状态（快照缺失也不清空）。
                await self._broadcast_attempt(attempt.id)

    async def broadcast_attempt(self, attempt_id: int) -> None:
        """公开广播入口：已持有该 attempt 锁的调用方不得再调用（asyncio.Lock 不可重入）。"""
        lock = self._locks.setdefault(attempt_id, asyncio.Lock())
        async with lock:
            await self._broadcast_attempt(attempt_id)

    async def _broadcast_attempt(self, attempt_id: int) -> None:
        """统一广播出口：每个订阅进入终态时推送一次自己的终态帧（幂等）。"""
        if self.broadcast is None or self.queries is None:
            return
        try:
            async with self.uow_factory() as uow:
                subscriptions = await self.repository_factory(uow).list_subscription_owners(
                    attempt_id
                )
            for task_id, user_id in subscriptions:
                try:
                    projection = await self.queries.get_task(user_id=user_id, task_id=task_id)
                except DomainError as error:
                    if error.code is ErrorCode.NOT_FOUND:
                        continue  # 订阅已删除或内容已清理：只跳过当前订阅。
                    raise
                if projection.status in TERMINAL_SUBSCRIPTION_STATUSES:
                    key = (attempt_id, task_id)
                    if key in self._terminal_broadcast:
                        continue  # 该订阅的终态帧只发一次。
                    self._terminal_broadcast[key] = None
                    while len(self._terminal_broadcast) > self._terminal_broadcast_capacity:
                        self._terminal_broadcast.popitem(last=False)
                self.broadcast.publish([user_id], task_message(projection))
        except Exception as error:  # noqa: BLE001  # 广播失败不阻断生命周期决策
            logger.warning(
                "任务广播失败 error_type=%s code=%s",
                type(error).__name__,
                getattr(error, "code", None),
            )

    async def _reconcile(self, snapshot: DownloadObservation, attempt_id: int) -> None:
        attempt, source = await self._attempt_and_source(attempt_id, snapshot.gid)
        if attempt.status not in {
            AttemptStatus.QUEUED,
            AttemptStatus.ACTIVE,
            AttemptStatus.WAITING,
            AttemptStatus.PAUSED,
        }:
            return
        await self._adopt_observed_name(attempt, source, snapshot)
        if attempt.completed_content_id is not None or attempt.ingestion_started_at_ms is not None:
            await self._ingest(
                attempt, snapshot if snapshot.status == "complete" else None
            )
            return
        if snapshot.followed_by:
            await self._handoff(attempt, snapshot)
            return
        if snapshot.status in {"error", "removed"}:
            await self._fail(attempt, f"aria2_{snapshot.status}", cleanup=True)
            self.observations.put(snapshot)
            return
        if snapshot.status == "complete":
            if source.kind == "magnet" and attempt.error_code == METADATA_ADMISSION_PAUSED:
                self.observations.put(snapshot)
                return
            await self._ingest(attempt, snapshot)
            return
        target = {
            "active": AttemptStatus.ACTIVE,
            "waiting": AttemptStatus.WAITING,
            "paused": AttemptStatus.PAUSED,
        }.get(snapshot.status)
        if target is None:
            raise DomainError(ErrorCode.INVALID_STATE, "aria2 快照状态无效")
        if source.kind == "magnet" and attempt.error_code == METADATA_ADMISSION_PAUSED:
            if target is AttemptStatus.PAUSED:
                await self._resume(attempt, snapshot, admit=False)
            else:
                await self._apply(attempt, snapshot, target)
            return
        if target is AttemptStatus.PAUSED and attempt.error_code is None:
            await self._apply(attempt, snapshot, target, external_pause=True)
            return
        if target is AttemptStatus.PAUSED and attempt.error_code in {
            ADMISSION_PAUSED,
            METADATA_ADMISSION_PAUSED,
        }:
            await self._resume(attempt, snapshot, admit=True)
            return
        failure = await self._admit(attempt, snapshot)
        if failure is not None:
            await self._stop_failed(attempt, failure)
            self.observations.put(snapshot)
            return
        clear_pause = attempt.error_code in {ADMISSION_PAUSED, EXTERNAL_PAUSED}
        await self._apply(attempt, snapshot, target, clear_pause=clear_pause)

    async def _adopt_observed_name(
        self, attempt: AttemptRecord, source: SourceRecord, snapshot: DownloadObservation
    ) -> None:
        """无 dn 磁力任务在元数据到达后改用种子真名。

        显示名只存在 task_subscriptions.display_name 一处，改名后 REST/WS/RPC 与
        完成入库的文件名同一次生效；用户自定义名（out / dn）不在此列。
        """
        canonical_uri = source.display_uri or ""
        name = adopted_display_name(
            kind=source.kind, canonical_uri=canonical_uri, observed_name=snapshot.name
        )
        if name is None:
            return
        async with self.uow_factory() as uow:
            await self.repository_factory(uow).adopt_display_name(
                attempt.id,
                fallback_name=magnet_missing_name_fallback(canonical_uri),
                display_name=name,
                now_ms=self.clock_ms(),
            )

    async def _apply(
        self,
        attempt: AttemptRecord,
        snapshot: DownloadObservation,
        target: AttemptStatus,
        *,
        clear_pause: bool = False,
        external_pause: bool = False,
    ) -> None:
        async with self.uow_factory() as uow:
            applied = await self.repository_factory(uow).apply_observation(
                attempt.id,
                expected_gid=snapshot.gid,
                status=target,
                completed_length=snapshot.completed_length,
                total_length=snapshot.total_length,
                now_ms=self.clock_ms(),
                clear_pause=clear_pause,
                external_pause=external_pause,
            )
        if applied:
            self.observations.put(snapshot)

    async def _admit(
        self, attempt: AttemptRecord, snapshot: DownloadObservation
    ) -> DomainError | None:
        async with self.uow_factory() as uow:
            return await self.repository_factory(uow).admit_size(
                attempt_id=attempt.id,
                observed_bytes=(
                    snapshot.total_length
                    if snapshot.total_length is not None
                    else snapshot.completed_length
                ),
                known_total=snapshot.total_length is not None,
                disk_free_bytes=self.content_store.machine_free_bytes(),
                min_free_bytes=self.settings().min_free_disk,
                max_task_bytes=self.settings().max_task_size,
                now_ms=self.clock_ms(),
            )

    async def _resume(
        self, attempt: AttemptRecord, snapshot: DownloadObservation, *, admit: bool
    ) -> None:
        if admit:
            failure = await self._admit(attempt, snapshot)
            if failure is not None:
                await self._stop_failed(attempt, failure)
                return
        try:
            await self.backend.unpause(gid=snapshot.gid)
            confirmed = await self.backend.snapshot(
                gid=snapshot.gid, attempt_id=attempt.id
            )
        except DomainError:
            self.observations.discard(snapshot.gid)
            self.connectivity.record_failure()
            return
        if confirmed is None:
            self.observations.discard(snapshot.gid)
            return
        self.connectivity.record_success()
        if confirmed.status in {"active", "waiting"}:
            target = (
                AttemptStatus.ACTIVE
                if confirmed.status == "active"
                else AttemptStatus.WAITING
            )
            await self._apply(attempt, confirmed, target, clear_pause=admit)
        elif confirmed.status == "paused":
            await self._apply(attempt, confirmed, AttemptStatus.PAUSED)
        else:
            await self._reconcile(confirmed, attempt.id)

    async def _handoff(self, attempt: AttemptRecord, snapshot: DownloadObservation) -> None:
        targets = tuple(dict.fromkeys(snapshot.followed_by))
        if len(targets) != 1:
            raise DomainError(ErrorCode.INVALID_STATE, "下载交接目标不唯一")
        new_gid = targets[0]
        self._missing_since.pop(attempt.id, None)
        async with self.uow_factory() as uow:
            await self.repository_factory(uow).handoff_attempt(
                attempt.id, expected_gid=snapshot.gid, new_gid=new_gid, now_ms=self.clock_ms()
            )
        self.observations.discard(snapshot.gid)
        try:
            await self.backend.remove_result(gid=snapshot.gid)
        except DomainError:
            logger.warning("交接父任务结果暂未清理 error_type=DomainError")
        try:
            following = await self.backend.snapshot(gid=new_gid, attempt_id=attempt.id)
        except DomainError:
            self.observations.discard(new_gid)
            self.connectivity.record_failure()
            return
        if following is not None:
            await self._reconcile(following, attempt.id)

    async def _prepare_content(
        self, attempt: AttemptRecord, manifest: tuple[ManifestEntry, ...]
    ) -> ContentPreparation:
        if attempt.completed_content_id is not None:
            async with self.uow_factory() as uow:
                content = await self.content_repository_factory(uow).get_object(
                    attempt.completed_content_id
                )
            return ContentPreparation(
                content_identity(content.kind, content.digest),
                content.size,
                content.storage_path,
                (),
            )
        preparation = await self.content_store.collect(attempt.id, manifest)
        async with self.uow_factory() as uow:
            repository = self.repository_factory(uow)
            content_repository = self.content_repository_factory(uow)
            existing = await content_repository.find_object_by_key(preparation.identity.key)
            if existing is None:
                content_id = await content_repository.create_object(
                    kind=preparation.identity.kind.value,
                    digest=preparation.identity.digest,
                    size=preparation.size,
                    storage_path=preparation.storage_path,
                    now_ms=self.clock_ms(),
                )
                for entry in preparation.entries:
                    parent, _, name = entry.path.rpartition("/")
                    await content_repository.add_entry(
                        content_id=content_id,
                        parent_path=parent,
                        name=name,
                        entry_kind=entry.kind.value,
                        size=entry.size,
                        now_ms=self.clock_ms(),
                    )
            else:
                if (
                    existing.deletion_state is not ContentDeletionState.ACTIVE
                    or existing.size != preparation.size
                    or existing.storage_path != preparation.storage_path
                ):
                    raise DomainError(ErrorCode.INVALID_STATE, "现有内容身份与入库结果冲突")
                content_id = existing.id
            await repository.begin_completion(
                attempt.id,
                expected_gid=attempt.gid,
                content_id=content_id,
                now_ms=self.clock_ms(),
            )
        return preparation

    async def _manifest_or_restore(
        self, attempt: AttemptRecord
    ) -> tuple[ManifestEntry, ...] | None:
        """读完成清单；缺清单就向 aria2 重观测补写，补不回来按 manifest_unavailable 收尾。

        没有任何“扫磁盘猜内容”的退路：清单是入库的唯一事实来源。
        返回 None 表示本轮不得继续入库：要么终态已经写死（文件留在盘上等人工处理），
        要么 aria2 暂时查不到这个任务、还在丢失宽限期内。
        """
        async with self.uow_factory() as uow:
            manifest = await self.repository_factory(uow).get_manifest(attempt.id)
        if manifest is not None:
            return manifest
        try:
            snapshot = await self.backend.snapshot(gid=attempt.gid, attempt_id=attempt.id)
        except DomainError as error:
            # 连不上 aria2 不等于 aria2 没有这个任务：清单可能还在，保持可重试，
            # 由恢复循环的入库超时判死（错误码仍取 manifest_unavailable，文案才准）。
            raise DomainError(
                ErrorCode.MANIFEST_UNAVAILABLE, "下载完成清单暂时无法核对"
            ) from error
        if snapshot is None:
            # 与 _confirm_missing 同一口径：aria2 重启 reload 会话的瞬间也会查不到任务，
            # 首次查不到起算宽限（_missing_since），宽限内只重试，到期才判死。
            first_seen = self._missing_since.setdefault(attempt.id, self.clock_ms())
            if self.clock_ms() - first_seen < self.missing_grace_ms:
                return None
            await self._fail(attempt, ErrorCode.MANIFEST_UNAVAILABLE.value, cleanup=False)
            return None
        entries = _manifest_entries(snapshot)
        if not entries:
            # aria2 有这条任务却报不出可信文件：重观测同样拿不到清单，不再重试。
            await self._fail(attempt, ErrorCode.MANIFEST_UNAVAILABLE.value, cleanup=False)
            return None
        self._missing_since.pop(attempt.id, None)
        async with self.uow_factory() as uow:
            await self.repository_factory(uow).record_manifest(
                attempt.id, entries=entries, now_ms=self.clock_ms()
            )
        return entries

    async def _ingest(
        self, attempt: AttemptRecord, snapshot: DownloadObservation | None
    ) -> None:
        now_ms = self.clock_ms()
        async with self.uow_factory() as uow:
            repository = self.repository_factory(uow)
            await repository.start_ingestion(
                attempt.id, expected_gid=attempt.gid, now_ms=now_ms
            )
            # 清单与完成意图同一事务：清单写入失败会连同意图一起回滚，
            # 不存在“意图在、清单丢”。
            # 空条目不落行：完成态的 aria2 报告必然有文件，凑不出选中项只能说明“不知道”，
            # 留给恢复循环重观测补写（补不出来按 manifest_unavailable 收尾）。
            entries = _manifest_entries(snapshot) if snapshot is not None else ()
            if entries:
                await repository.record_manifest(
                    attempt.id, entries=entries, now_ms=now_ms
                )
        if snapshot is not None:
            self.observations.put(snapshot)
        try:
            manifest = await self._manifest_or_restore(attempt)
            if manifest is None:
                return
            preparation = await self._prepare_content(attempt, manifest)
        except DomainError as error:
            if error.code is ErrorCode.INVALID_INPUT:
                await self._fail(attempt, error.code.value, cleanup=True)
                return
            if error.code in {ErrorCode.STALE_EVENT, ErrorCode.NOT_FOUND}:
                await self._abort_stale_ingest(attempt, error)
                return
            raise
        await self.content_store.install(attempt.id, preparation, manifest)
        try:
            async with self.uow_factory() as uow:
                await self.repository_factory(uow).finalize_completion(
                    attempt.id, expected_gid=attempt.gid, now_ms=self.clock_ms()
                )
        except DomainError as error:
            if error.code in {ErrorCode.STALE_EVENT, ErrorCode.NOT_FOUND}:
                await self._abort_stale_ingest(attempt, error)
                return
            await self._fail(attempt, error.code.value, cleanup=True)
            return
        self._missing_since.pop(attempt.id, None)
        await self._cleanup_terminal(attempt.id)

    async def _abort_stale_ingest(
        self, attempt: AttemptRecord, error: DomainError
    ) -> None:
        """建立意图或最终确认遇到 STALE/NOT_FOUND：锁内重读最新 attempt 后再决定处置。"""
        async with self.uow_factory() as uow:
            current = await self.repository_factory(uow).get_attempt(attempt.id)
        if current.gid != attempt.gid or current.status not in {
            AttemptStatus.COMPLETED,
            AttemptStatus.FAILED,
            AttemptStatus.CANCELLED,
        }:
            raise error  # GID 变化或仍 live：保留原异常，不按取消清理
        if current.submission_state == "uncertain":
            return  # 停止未确认：不清目录、不删结果、不标 consumed，交停止恢复链
        await self._cleanup_terminal(attempt.id)

    async def _fail(self, attempt: AttemptRecord, error_code: str, *, cleanup: bool) -> None:
        self._missing_since.pop(attempt.id, None)
        async with self.uow_factory() as uow:
            await self.repository_factory(uow).fail_attempt(
                attempt.id,
                error_code=error_code,
                now_ms=self.clock_ms(),
                expected_gid=attempt.gid,
            )
        if cleanup:
            await self._cleanup_terminal(attempt.id)
            return
        # 不清理：清单核对不出来的下载文件留在盘上等人工处理。终态收尾必须同轮记下，
        # 否则轮询里的 cleanup_terminal 下一轮照样把目录删掉，“文件已保留”就是空话。
        async with self.uow_factory() as uow:
            await self.repository_factory(uow).finish_terminal_cleanup(
                attempt.id, expected_gid=attempt.gid, now_ms=self.clock_ms()
            )

    async def _stop_failed(self, attempt: AttemptRecord, failure: DomainError) -> None:
        try:
            await self.backend.stop(gid=attempt.gid)
        except DomainError:
            async with self.uow_factory() as uow:
                await self.repository_factory(uow).mark_stop_uncertain(attempt.id)
            self.observations.discard(attempt.gid)
            self.connectivity.record_failure()
            return
        await self._cleanup_terminal(attempt.id)

    async def _cleanup_terminal(self, attempt_id: int) -> None:
        """终态清理：仅供已持有该 attempt _locks 锁的调用方使用（asyncio.Lock 不可重入）。"""
        async with self.uow_factory() as uow:
            attempt = await self.repository_factory(uow).get_attempt(attempt_id)
        if attempt.status not in {
            AttemptStatus.COMPLETED,
            AttemptStatus.FAILED,
            AttemptStatus.CANCELLED,
        }:
            return  # pragma: no cover -- 第二道复核：list_terminal_cleanup 查询只返回终态，锁内竞争单实例下不可达
        if attempt.result_consumed_at_ms is not None:
            return  # pragma: no cover -- 第二道复核：list_terminal_cleanup 查询已过滤 consumed，锁内竞争单实例下不可达
        if attempt.submission_state == "uncertain":
            # 停止未确认：不认领、不清目录、不删 aria2 结果、不标 consumed。
            return  # pragma: no cover -- 第二道复核：list_terminal_cleanup 查询已排除 uncertain，锁内竞争单实例下不可达
        await self.content_store.cleanup_attempt(attempt.id)
        if attempt.submission_state != "planned":
            await self.backend.remove_result(gid=attempt.gid)
        async with self.uow_factory() as uow:
            await self.repository_factory(uow).finish_terminal_cleanup(
                attempt.id, expected_gid=attempt.gid, now_ms=self.clock_ms()
            )

    async def _confirm_missing(self, attempt: AttemptRecord) -> None:
        lock = self._locks.setdefault(attempt.id, asyncio.Lock())
        async with lock:
            try:
                async with self.uow_factory() as uow:
                    current = await self.repository_factory(uow).get_attempt(attempt.id)
                if current.status not in {
                    AttemptStatus.QUEUED,
                    AttemptStatus.ACTIVE,
                    AttemptStatus.WAITING,
                    AttemptStatus.PAUSED,
                } or current.submission_state != "submitted":
                    self._missing_since.pop(attempt.id, None)
                    return
                if (
                    current.completed_content_id is not None
                    or current.ingestion_started_at_ms is not None
                ):
                    await self._ingest(current, None)
                    self._missing_since.pop(attempt.id, None)
                    return
                confirmed = await self.backend.snapshot(
                    gid=current.gid, attempt_id=current.id
                )
                if confirmed is not None:
                    self._missing_since.pop(attempt.id, None)
                    await self._reconcile(confirmed, current.id)
                    return
                await self._fail(current, "aria2_gid_missing", cleanup=True)
            finally:
                await self._broadcast_attempt(attempt.id)

    async def reconcile_live(self) -> bool | None:
        async with self.uow_factory() as uow:
            attempts = await self.repository_factory(uow).list_live_attempts(submitted_only=True)
        if not attempts:
            return None
        complete = True
        for attempt in attempts:
            try:
                snapshot = await self.backend.snapshot(
                    gid=attempt.gid, attempt_id=attempt.id
                )
                if snapshot is None:
                    self.observations.discard(attempt.gid)
                    complete = False
                    first_seen = self._missing_since.setdefault(attempt.id, self.clock_ms())
                    if self.clock_ms() - first_seen >= self.missing_grace_ms:
                        await self._confirm_missing(attempt)
                    continue
                self._missing_since.pop(attempt.id, None)
                await self.reconcile(snapshot)
            except Exception as error:  # noqa: BLE001  # 单轮单任务故障必须隔离
                complete = False
                self.observations.discard(attempt.gid)
                logger.warning(
                    "生命周期轮询单项失败 error_type=%s code=%s",
                    type(error).__name__,
                    getattr(error, "code", None),
                )
        return complete

    async def recover_ingestions(self) -> bool:
        async with self.uow_factory() as uow:
            attempts = await self.repository_factory(uow).list_completion_intents()
        complete = True
        for candidate in attempts:
            lock = self._locks.setdefault(candidate.id, asyncio.Lock())
            try:
                async with lock:
                    async with self.uow_factory() as uow:
                        attempt = await self.repository_factory(uow).get_attempt(candidate.id)
                    if attempt.status not in {
                        AttemptStatus.QUEUED,
                        AttemptStatus.ACTIVE,
                        AttemptStatus.WAITING,
                        AttemptStatus.PAUSED,
                    } or (
                        attempt.completed_content_id is None
                        and attempt.ingestion_started_at_ms is None
                    ):
                        continue
                    try:
                        await self._ingest(attempt, None)
                    except Exception as error:  # noqa: BLE001  # 入库失败保留到下轮或到期终结
                        complete = False
                        started = attempt.ingestion_started_at_ms
                        if (
                            started is not None
                            and self.clock_ms() - started >= self.ingestion_timeout_ms
                        ):
                            # 清单核不出来导致的超时用 manifest_unavailable（并保留文件），
                            # 其余（collect/install/finalize 反复失败）仍是入库超时。
                            manifest_missing = (
                                isinstance(error, DomainError)
                                and error.code is ErrorCode.MANIFEST_UNAVAILABLE
                            )
                            await self._fail(
                                attempt,
                                (
                                    ErrorCode.MANIFEST_UNAVAILABLE.value
                                    if manifest_missing
                                    else "ingestion_timeout"
                                ),
                                cleanup=not manifest_missing,
                            )
                        else:
                            logger.warning(
                                "入库恢复单项失败 error_type=%s code=%s",
                                type(error).__name__,
                                getattr(error, "code", None),
                            )
                    finally:
                        await self._broadcast_attempt(attempt.id)
            except Exception as error:  # noqa: BLE001  # 保留其他恢复候选并禁止孤儿清理
                complete = False
                logger.warning(
                    "入库恢复单项失败 error_type=%s code=%s",
                    type(error).__name__,
                    getattr(error, "code", None),
                )
        return complete

    async def cleanup_terminal(self) -> bool:
        async with self.uow_factory() as uow:
            attempts = await self.repository_factory(uow).list_terminal_cleanup()
        complete = True
        for candidate in attempts:
            # 与 reconcile/recover_ingestions 共用同一 attempt 锁：
            # install 阻塞期间并发清理只能等待，不能提前认领或删目录。
            lock = self._locks.setdefault(candidate.id, asyncio.Lock())
            try:
                async with lock:
                    await self._cleanup_terminal(candidate.id)
            except Exception as error:  # noqa: BLE001  # 清理失败保留记录供下轮重试
                complete = False
                logger.warning(
                    "终态清理单项失败 error_type=%s code=%s message=%s",
                    type(error).__name__,
                    getattr(error, "code", None),
                    error,
                )
        return complete

    async def purge_orphans(self) -> None:
        async with self.uow_factory() as uow:
            known = set(await self.repository_factory(uow).list_all_gids())
        for gid in await self.backend.list_live_gids():
            if gid not in known:
                await self.backend.stop(gid=gid)

    async def _on_gid(self, gid: str) -> None:
        try:
            async with self.uow_factory() as uow:
                attempt = await self.repository_factory(uow).find_attempt_by_gid(gid)
            if attempt is None:
                self.observations.discard(gid)
                return
            # 观测文件的相对路径需要 attempt 上下文（目录前缀），所以先定位归属再取快照。
            snapshot = await self.backend.snapshot(gid=gid, attempt_id=attempt.id)
            if snapshot is None:
                self.observations.discard(gid)
                return
            await self.reconcile(snapshot)
            self.connectivity.record_success()
        except Exception as error:  # noqa: BLE001  # 单个通知不得中断监听连接
            self.observations.discard(gid)
            self.connectivity.record_failure()
            logger.warning(
                "生命周期通知单项失败 error_type=%s code=%s",
                type(error).__name__,
                getattr(error, "code", None),
            )

    async def _probe_backend(self) -> None:
        """空闲探测：没有任务观测可依据时，仍让健康计数跟随 aria2 真实状态。"""
        try:
            await asyncio.wait_for(
                self.backend.version(), timeout=self.probe_timeout_seconds
            )
        except Exception as error:  # noqa: BLE001  # 探测只服务横幅，失败不得中断轮询
            logger.debug("空闲探测失败 error_type=%s", type(error).__name__)
            self.connectivity.record_failure()
            return
        self.connectivity.record_success()

    async def run_polling(self) -> None:
        while True:
            ingestions_complete = await self.recover_ingestions()
            result = await self.reconcile_live()
            if result is None and ingestions_complete:
                # 空闲没有任务观测可记：不探测则计数只能等新任务经过放行环节才归零，横幅会永久卡亮。
                await self._probe_backend()
            elif ingestions_complete and result is not False:
                self.connectivity.record_success()
            else:
                self.connectivity.record_failure()
            await self.cleanup_terminal()
            await self.sleep(self.poll_interval_seconds)

    async def run_listener(self) -> None:
        delay = 1.0
        while True:
            try:
                await self.backend.listen(self._on_gid)
            except asyncio.CancelledError:
                raise
            except DomainError:
                self.connectivity.record_failure()
            else:
                self.connectivity.record_failure()
            await self.sleep(delay + self.jitter())
            delay = min(delay * 2, self.reconnect_max_seconds)
