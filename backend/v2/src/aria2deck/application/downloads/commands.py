from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager, nullcontext
from dataclasses import replace
from typing import Protocol, cast

from ...domain.downloads import (
    ADMISSION_PAUSED,
    LIVE_ATTEMPT_STATUSES,
    METADATA_ADMISSION_PAUSED,
    RegistrationOutcome,
    SubscriptionStatus,
    allocate_download_display_name,
    decide_registration,
    effective_task_status,
    normalize_download_options,
    normalize_resource,
    parse_allow_rename_policy,
    require_retryable,
    resource_display_name,
)
from ...domain.errors import DomainError, ErrorCode
from ...domain.quota import PhysicalDownload, PhysicalPack, available_machine_bytes
from ...domain.settings import SystemSettings
from ...domain.torrents import TorrentMetadata, parse_torrent
from ..library.models import UserFileRecord
from ..ports import UnitOfWork
from ..settings.rate_limit import RateLimiter
from .models import (
    AttemptRecord,
    BackendSubmissionResult,
    CancellationResult,
    DownloadRecord,
    DownloadWriteResult,
    PreparedDownload,
    RegistrationResult,
    SourceRecord,
    Submission,
    UriDownloadInput,
)

UowFactory = Callable[[], AbstractAsyncContextManager[UnitOfWork]]


class ContentView(Protocol):
    @property
    def size(self) -> int: ...


class CommandRepository(Protocol):
    async def find_latest_subscription(
        self, *, resource_key: str, user_id: int
    ) -> DownloadRecord | None: ...

    async def find_live_attempt(self, resource_key: str) -> AttemptRecord | None: ...

    async def list_active_physical(self) -> list[PhysicalDownload]: ...

    async def find_completed_attempt(self, resource_key: str) -> AttemptRecord | None: ...

    async def list_occupied_display_names(self, user_id: int) -> set[str]: ...

    async def find_source(self, resource_key: str) -> SourceRecord | None: ...

    async def create_source(
        self,
        *,
        kind: str,
        resource_key: str,
        payload_json: str,
        options_json: str,
        selected_file_indexes_json: str | None,
        display_name: str,
        display_uri: str | None,
        now_ms: int,
    ) -> int: ...

    async def rebuild_source(
        self,
        *,
        source_id: int,
        resource_key: str,
        payload_json: str,
        options_json: str,
        selected_file_indexes_json: str | None,
        display_name: str,
        display_uri: str | None,
    ) -> None: ...

    async def create_attempt(
        self,
        *,
        source_id: int,
        resource_key: str,
        gid: str,
        now_ms: int,
        total_length: int | None = None,
        physical_reserved_bytes: int = 0,
        error_code: str | None = None,
    ) -> int: ...

    async def set_planned_gid(self, attempt_id: int, gid: str) -> None: ...

    async def create_subscription(
        self,
        *,
        attempt_id: int,
        user_id: int,
        reserved_bytes: int,
        display_name: str,
        display_uri: str | None,
        now_ms: int,
        status: SubscriptionStatus = SubscriptionStatus.QUEUED,
    ) -> int: ...

    async def get_attempt(self, attempt_id: int) -> AttemptRecord: ...

    async def get_submission(self, attempt_id: int) -> Submission: ...

    async def mark_submitted(self, attempt_id: int) -> None: ...

    async def mark_uncertain(self, attempt_id: int) -> None: ...

    async def fail_attempt(self, attempt_id: int, *, error_code: str, now_ms: int) -> None: ...

    async def get_task(self, *, user_id: int, task_id: int) -> DownloadRecord: ...

    async def get_source_for_attempt(self, attempt_id: int) -> SourceRecord: ...

    async def cancel_subscription(
        self, *, user_id: int, task_id: int, now_ms: int
    ) -> CancellationResult: ...

    async def mark_stop_confirmed(self, attempt_id: int) -> None: ...

    async def mark_stop_uncertain(self, attempt_id: int) -> None: ...


class ContentCommands(Protocol):
    async def find_user_file(self, *, user_id: int, content_id: int) -> int | None: ...

    async def get_user_file(self, *, user_id: int, file_id: int) -> UserFileRecord: ...

    async def add_user_file(
        self, *, user_id: int, content_id: int, display_name: str, now_ms: int
    ) -> int: ...

    async def get_object(self, content_id: int) -> ContentView: ...


class QuotaCommands(Protocol):
    async def reserve(self, user_id: int, amount: int) -> object: ...

    async def complete_download(
        self,
        user_id: int,
        reserved_bytes: int,
        *,
        content_size: int,
        already_owned: bool,
    ) -> object: ...


class PackPhysicalRepository(Protocol):
    async def list_active_physical(self) -> list[PhysicalPack]: ...


class MachineSpacePort(Protocol):
    def machine_free_bytes(self) -> int: ...


class SubmissionBackend(Protocol):
    async def submit_many(
        self, submissions: list[Submission], *, trackers: tuple[str, ...]
    ) -> list[BackendSubmissionResult]: ...

    async def confirm(self, *, gid: str) -> bool: ...

    async def stop(self, *, gid: str) -> None: ...


class TrackerProvider(Protocol):
    async def current(self) -> tuple[str, ...]: ...


RepositoryFactory = Callable[[UnitOfWork], CommandRepository]
ContentRepositoryFactory = Callable[[UnitOfWork], ContentCommands]
QuotaRepositoryFactory = Callable[[UnitOfWork], QuotaCommands]
PackRepositoryFactory = Callable[[UnitOfWork], PackPhysicalRepository]


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def prepare_uri_download(
    uri: str, options: Mapping[str, object]
) -> PreparedDownload:
    resource = normalize_resource(uri)
    allow_rename = parse_allow_rename_policy(options.get("allow_rename"))
    # 策略只服务本次注册：不进入资源身份、不持久化到共享 source、不交给 aria2。
    normalized_options, resource_key = normalize_download_options(
        resource, {key: value for key, value in options.items() if key != "allow_rename"}
    )
    payload: dict[str, object]
    if resource.kind == "http":
        payload = {
            "uris": [
                resource.canonical_uri,
                *cast(list[str], normalized_options["mirrors"]),
            ]
        }
    else:
        # Magnet identity and submission deliberately discard user-supplied trackers.
        payload = {"uri": resource.canonical_uri}
    display_name = resource_display_name(resource, uri.strip())
    if isinstance(normalized_options.get("out"), str):
        # 用户显式指定的输出名优先（A9）；display_name 是 V2 文件命名的唯一来源。
        display_name = str(normalized_options["out"])
    return PreparedDownload(
        kind=resource.kind,
        resource_key=resource_key,
        payload=payload,
        options=normalized_options,
        display_name=display_name,
        display_uri=resource.display_uri,
        allow_rename=allow_rename,
    )


def prepare_torrent_download(
    metadata: TorrentMetadata,
    *,
    allow_rename: bool = True,
) -> PreparedDownload:
    return PreparedDownload(
        kind="torrent",
        resource_key=metadata.resource_key,
        payload={"torrent": metadata.payload_base64},
        selected_file_indexes=metadata.selected_file_indexes,
        display_name=metadata.name,
        display_uri=f"torrent:{metadata.info_hash}",
        total_bytes=metadata.selected_size,
        allow_rename=allow_rename,
    )


def _from_source(source: SourceRecord) -> PreparedDownload:
    if source.detached_at_ms is not None:
        raise DomainError(ErrorCode.INVALID_STATE, "历史下载载荷已清理，不能重试")
    if source.kind == "torrent":
        payload = source.payload.get("torrent")
        if not isinstance(payload, str):
            raise DomainError(ErrorCode.INVALID_STATE, "种子来源数据无效")
        return prepare_torrent_download(parse_torrent(payload, source.selected_file_indexes))
    if source.kind not in {"http", "magnet"}:
        raise DomainError(ErrorCode.INVALID_STATE, "下载来源类型无效")
    if not source.payload:
        raise DomainError(ErrorCode.INVALID_STATE, "历史下载载荷已清理，不能重试")
    return PreparedDownload(
        kind=source.kind,
        resource_key=source.resource_key,
        payload=source.payload,
        options=source.options,
        selected_file_indexes=source.selected_file_indexes,
        display_name=source.display_name,
        display_uri=source.display_uri,
    )


class DownloadCommandService:
    def __init__(
        self,
        *,
        uow_factory: UowFactory,
        repository_factory: RepositoryFactory,
        content_repository_factory: ContentRepositoryFactory,
        quota_repository_factory: QuotaRepositoryFactory,
        pack_repository_factory: PackRepositoryFactory,
        backend: SubmissionBackend,
        trackers: TrackerProvider,
        rate_limiter: RateLimiter,
        machine_space: MachineSpacePort,
        settings: Callable[[], SystemSettings],
        clock_ms: Callable[[], int],
        planned_gid: Callable[[int], str],
        submission_lock: asyncio.Lock | None = None,
    ) -> None:
        self.uow_factory = uow_factory
        self.repository_factory = repository_factory
        self.content_repository_factory = content_repository_factory
        self.quota_repository_factory = quota_repository_factory
        self.pack_repository_factory = pack_repository_factory
        self.backend = backend
        self.trackers = trackers
        self.rate_limiter = rate_limiter
        self.machine_space = machine_space
        self.settings = settings
        self.clock_ms = clock_ms
        self.planned_gid = planned_gid
        self.submission_lock = submission_lock or asyncio.Lock()

    async def _register(self, *, user_id: int, prepared: PreparedDownload) -> RegistrationResult:
        now_ms = self.clock_ms()
        async with self.uow_factory() as uow:
            repository = self.repository_factory(uow)
            latest = await repository.find_latest_subscription(
                resource_key=prepared.resource_key, user_id=user_id
            )
            live = await repository.find_live_attempt(prepared.resource_key)
            completed = await repository.find_completed_attempt(prepared.resource_key)
            subscription_status = (
                None
                if latest is None
                else effective_task_status(latest.subscription_status, latest.attempt_status)
            )
            decision = decide_registration(
                attempt_status=None if live is None else live.status,
                subscription_status=subscription_status,
                content_available=completed is not None,
            )
            if decision.outcome is RegistrationOutcome.DUPLICATE:
                existing_task = cast(DownloadRecord, latest)
                return RegistrationResult(
                    existing_task.id, existing_task.attempt_id, existing_task.gid, decision.outcome, False
                )
            occupied_names = await repository.list_occupied_display_names(user_id)
            if decision.outcome is RegistrationOutcome.ATTACHED_COMPLETED:
                completed_attempt = cast(AttemptRecord, completed)
                content_id = cast(int, completed_attempt.completed_content_id)
                content = self.content_repository_factory(uow)
                existing_file = await content.find_user_file(user_id=user_id, content_id=content_id)
                if existing_file is not None:
                    # 已拥有内容：订阅直接显示该文件实际名称，不分配新名、不加引用。
                    attached_name = (
                        await content.get_user_file(user_id=user_id, file_id=existing_file)
                    ).display_name
                else:
                    attached_name = allocate_download_display_name(
                        prepared.display_name, occupied_names, prepared.allow_rename
                    )
                    await content.add_user_file(
                        user_id=user_id,
                        content_id=content_id,
                        display_name=attached_name,
                        now_ms=now_ms,
                    )
                content_view = await content.get_object(content_id)
                await self.quota_repository_factory(uow).complete_download(
                    user_id,
                    0,
                    content_size=content_view.size,
                    already_owned=existing_file is not None,
                )
                task_id = await repository.create_subscription(
                    attempt_id=completed_attempt.id,
                    user_id=user_id,
                    reserved_bytes=0,
                    display_name=attached_name,
                    display_uri=prepared.display_uri,
                    now_ms=now_ms,
                    status=SubscriptionStatus.COMPLETED,
                )
                return RegistrationResult(
                    task_id, completed_attempt.id, completed_attempt.gid, decision.outcome, False
                )
            if decision.outcome is RegistrationOutcome.JOINED_LIVE:
                live_attempt = cast(AttemptRecord, live)
                joined_name = allocate_download_display_name(
                    prepared.display_name, occupied_names, prepared.allow_rename
                )
                reserved = live_attempt.total_length or 0
                if reserved:
                    await self.quota_repository_factory(uow).reserve(user_id, reserved)
                task_id = await repository.create_subscription(
                    attempt_id=live_attempt.id,
                    user_id=user_id,
                    reserved_bytes=reserved,
                    display_name=joined_name,
                    display_uri=prepared.display_uri,
                    now_ms=now_ms,
                )
                return RegistrationResult(task_id, live_attempt.id, live_attempt.gid, decision.outcome, False)
            # CREATED：在任何 source/attempt/配额写入前分配最终名并核对已知大小（严格冲突/超限整笔回滚）。
            final_name = allocate_download_display_name(
                prepared.display_name, occupied_names, prepared.allow_rename
            )
            maximum = self.settings().max_task_size
            if (
                prepared.total_bytes is not None
                and maximum > 0
                and prepared.total_bytes > maximum
            ):
                raise DomainError(
                    ErrorCode.TASK_TOO_LARGE, "下载超过系统单任务大小上限"
                )
            source = await repository.find_source(prepared.resource_key)
            if source is None:
                source_id = await repository.create_source(
                    kind=prepared.kind,
                    resource_key=prepared.resource_key,
                    payload_json=_json(prepared.payload),
                    options_json=_json(prepared.options),
                    selected_file_indexes_json=(
                        None
                        if prepared.selected_file_indexes is None
                        else _json(prepared.selected_file_indexes)
                    ),
                    display_name=prepared.display_name,
                    display_uri=prepared.display_uri,
                    now_ms=now_ms,
                )
            elif source.detached_at_ms is not None or not source.payload:
                await repository.rebuild_source(
                    source_id=source.id,
                    resource_key=prepared.resource_key,
                    payload_json=_json(prepared.payload),
                    options_json=_json(prepared.options),
                    selected_file_indexes_json=(
                        None
                        if prepared.selected_file_indexes is None
                        else _json(prepared.selected_file_indexes)
                    ),
                    display_name=prepared.display_name,
                    display_uri=prepared.display_uri,
                )
                source_id = source.id
            else:
                source_id = source.id
            reserved = prepared.total_bytes or 0
            downloads = await repository.list_active_physical()
            packs = await self.pack_repository_factory(uow).list_active_physical()
            machine_available = available_machine_bytes(
                self.machine_space.machine_free_bytes(),
                self.settings().min_free_disk,
                downloads,
                packs,
            )
            if reserved > machine_available:
                raise DomainError(ErrorCode.PHYSICAL_SPACE_EXCEEDED, "机器可用空间不足")
            if reserved:
                await self.quota_repository_factory(uow).reserve(user_id, reserved)
            attempt_id = await repository.create_attempt(
                source_id=source_id,
                resource_key=prepared.resource_key,
                gid=uuid.uuid4().hex[:16],
                now_ms=now_ms,
                total_length=prepared.total_bytes,
                physical_reserved_bytes=reserved,
                error_code=(
                    METADATA_ADMISSION_PAUSED
                    if prepared.kind == "magnet"
                    else ADMISSION_PAUSED
                ),
            )
            gid = self.planned_gid(attempt_id)
            await repository.set_planned_gid(attempt_id, gid)
            task_id = await repository.create_subscription(
                attempt_id=attempt_id,
                user_id=user_id,
                reserved_bytes=reserved,
                display_name=final_name,
                display_uri=prepared.display_uri,
                now_ms=now_ms,
            )
            return RegistrationResult(task_id, attempt_id, gid, decision.outcome, True)

    async def _submit(self, pending: dict[int, DownloadWriteResult]) -> None:
        async with self.submission_lock:
            await self._submit_locked(pending)

    async def _submit_locked(self, pending: dict[int, DownloadWriteResult]) -> None:
        if not pending:
            return
        submissions: list[Submission] = []
        async with self.uow_factory() as uow:
            repository = self.repository_factory(uow)
            for attempt_id in pending:
                attempt = await repository.get_attempt(attempt_id)
                if attempt.status in LIVE_ATTEMPT_STATUSES and attempt.submission_state == "planned":
                    submissions.append(await repository.get_submission(attempt_id))
        if not submissions:
            return
        try:
            trackers = await self.trackers.current()
            replies = await self.backend.submit_many(submissions, trackers=trackers)
            if len(replies) != len(submissions):
                raise DomainError(ErrorCode.SUBMISSION_UNCERTAIN, "下载提交结果数量不完整")
            for submission, reply in zip(submissions, replies, strict=True):
                if reply.accepted and reply.gid != submission.gid:
                    raise DomainError(ErrorCode.SUBMISSION_UNCERTAIN, "aria2 返回的 GID 不匹配")
            for submission, reply in zip(submissions, replies, strict=True):
                if not reply.accepted:
                    await self._reject_submission(submission, reply, pending)
                    continue
                if not await self.backend.confirm(gid=submission.gid):
                    raise DomainError(ErrorCode.SUBMISSION_UNCERTAIN, "下载提交后无法确认")
                async with self.uow_factory() as uow:
                    await self.repository_factory(uow).mark_submitted(submission.attempt_id)
        except DomainError as error:
            if error.code is not ErrorCode.SUBMISSION_UNCERTAIN:
                raise
            async with self.uow_factory() as uow:
                repository = self.repository_factory(uow)
                for submission in submissions:
                    await repository.mark_uncertain(submission.attempt_id)
            raise DomainError(ErrorCode.SUBMISSION_UNCERTAIN, "下载结果暂无法确认，将在恢复时核对") from None

    async def _reject_submission(
        self,
        submission: Submission,
        reply: BackendSubmissionResult,
        pending: dict[int, DownloadWriteResult],
    ) -> None:
        async with self.uow_factory() as uow:
            await self.repository_factory(uow).fail_attempt(
                submission.attempt_id,
                error_code=reply.error_code or "aria2_rejected",
                now_ms=self.clock_ms(),
            )
        pending[submission.attempt_id] = replace(
            pending[submission.attempt_id],
            accepted=False,
            error_code=reply.error_code or "aria2_rejected",
            message="aria2 拒绝了下载提交",
        )

    async def create_uris(
        self, *, user_id: int, items: list[UriDownloadInput], submit: bool = True
    ) -> list[DownloadWriteResult]:
        """`submit=False` 只登记不提交（RPC 面“登记即返回”），提交交给恢复循环。

        登记并发由数据库事务裁决，不需要提交锁；锁保护的是 aria2 提交本身。
        """
        if not 1 <= len(items) <= 30:
            raise DomainError(ErrorCode.INVALID_INPUT, "每批必须包含 1 到 30 个下载")
        lock = self.submission_lock if submit else nullcontext()
        async with lock:
            results: list[DownloadWriteResult] = []
            pending: dict[int, DownloadWriteResult] = {}
            seen: set[str] = set()
            for item in items:
                try:
                    prepared = prepare_uri_download(item.uri, item.options)
                    if prepared.resource_key not in seen:
                        self.rate_limiter.check_create_task(user_id=user_id)
                        seen.add(prepared.resource_key)
                    registered = await self._register(user_id=user_id, prepared=prepared)
                    result = DownloadWriteResult(
                        accepted=True,
                        task_id=registered.task_id,
                        attempt_id=registered.attempt_id,
                        outcome=registered.outcome,
                    )
                    if registered.should_submit:
                        pending[registered.attempt_id] = result
                except DomainError as error:
                    result = DownloadWriteResult(
                        accepted=False, error_code=error.code.value, message=str(error)
                    )
                results.append(result)
            if submit:
                await self._submit_locked(pending)
            return [
                pending.get(result.attempt_id, result)
                if result.attempt_id is not None and result.outcome is RegistrationOutcome.CREATED
                else result
                for result in results
            ]

    def preview_torrent(self, *, user_id: int, payload_base64: str) -> TorrentMetadata:
        self.rate_limiter.check_create_torrent(user_id=user_id)
        return parse_torrent(payload_base64)

    async def create_torrent(
        self,
        *,
        user_id: int,
        payload_base64: str,
        selected_file_indexes: list[int] | None,
        allow_rename: bool = True,
        submit: bool = True,
    ) -> DownloadWriteResult:
        """`submit=False` 的语义见 `create_uris`。"""
        self.rate_limiter.check_create_torrent(user_id=user_id)
        metadata = parse_torrent(payload_base64, selected_file_indexes)
        lock = self.submission_lock if submit else nullcontext()
        async with lock:
            registered = await self._register(
                user_id=user_id,
                prepared=prepare_torrent_download(metadata, allow_rename=allow_rename),
            )
            result = DownloadWriteResult(
                accepted=True,
                task_id=registered.task_id,
                attempt_id=registered.attempt_id,
                outcome=registered.outcome,
            )
            if registered.should_submit and submit:
                pending = {registered.attempt_id: result}
                await self._submit_locked(pending)
                return pending[registered.attempt_id]
            return result

    async def retry(
        self, *, user_id: int, task_id: int, allow_rename: bool = True
    ) -> DownloadWriteResult:
        self.rate_limiter.check_create_task(user_id=user_id)
        async with self.uow_factory() as uow:
            repository = self.repository_factory(uow)
            record = await repository.get_task(user_id=user_id, task_id=task_id)
            require_retryable(effective_task_status(record.subscription_status, record.attempt_status))
            source = await repository.get_source_for_attempt(record.attempt_id)
        # 重试以当前任务名为本用户请求名，并注入本次调用策略（D1/D7）。
        prepared = replace(
            _from_source(source),
            display_name=record.name,
            total_bytes=record.total_bytes,
            allow_rename=allow_rename,
        )
        async with self.submission_lock:
            registered = await self._register(user_id=user_id, prepared=prepared)
            result = DownloadWriteResult(
                accepted=True,
                task_id=registered.task_id,
                attempt_id=registered.attempt_id,
                outcome=registered.outcome,
            )
            if registered.should_submit:
                pending = {registered.attempt_id: result}
                await self._submit_locked(pending)
                return pending[registered.attempt_id]
            return result

    async def cancel(
        self, *, user_id: int, task_ids: list[int]
    ) -> list[DownloadWriteResult]:
        if not 1 <= len(task_ids) <= 1000:
            raise DomainError(ErrorCode.INVALID_INPUT, "每批必须包含 1 到 1000 个任务")
        async with self.submission_lock:
            results: list[DownloadWriteResult] = []
            for task_id in task_ids:
                try:
                    now_ms = self.clock_ms()
                    async with self.uow_factory() as uow:
                        cancellation = await self.repository_factory(uow).cancel_subscription(
                            user_id=user_id, task_id=task_id, now_ms=now_ms
                        )
                    stop_confirmed = True
                    if cancellation.stop_attempt:
                        try:
                            await self.backend.stop(gid=cancellation.gid)
                        except DomainError:
                            stop_confirmed = False
                        async with self.uow_factory() as uow:
                            repository = self.repository_factory(uow)
                            if stop_confirmed:
                                await repository.mark_stop_confirmed(cancellation.attempt_id)
                            else:
                                await repository.mark_stop_uncertain(cancellation.attempt_id)
                    results.append(
                        DownloadWriteResult(
                            accepted=True, task_id=task_id, stop_confirmed=stop_confirmed
                        )
                    )
                except DomainError as error:
                    results.append(
                        DownloadWriteResult(
                            accepted=False,
                            task_id=task_id,
                            error_code=error.code.value,
                            message=str(error),
                        )
                    )
            return results
