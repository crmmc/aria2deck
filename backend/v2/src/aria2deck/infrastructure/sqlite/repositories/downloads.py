from __future__ import annotations

import json
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.engine import Row, RowMapping
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql import ColumnElement, Select

from ....application.downloads.models import (
    AttemptRecord,
    CancellationResult,
    DownloadRecord,
    HistoryExpiration,
    SourceRecord,
    Submission,
)
from ....domain.content import ContentDeletionState
from ....domain.downloads import (
    ADMISSION_PAUSED,
    EXTERNAL_PAUSED,
    LIVE_ATTEMPT_STATUSES,
    LIVE_SUBSCRIPTION_STATUSES,
    TERMINAL_SUBSCRIPTION_STATUSES,
    AttemptStatus,
    SubscriptionStatus,
    effective_task_status,
    plan_cancellation,
    reconciled_total_length,
    require_gid_match,
    transition_attempt,
    transition_subscription,
)
from ....domain.errors import DomainError, ErrorCode
from ..schema import content_objects, download_attempts, download_sources, task_subscriptions, users
from .content import ContentRepository
from .quota import QuotaRepository

# 排序键白名单：枚举值映射到列对象，禁止任何用户输入以字符串形式拼进 SQL。
_TASK_SORT_COLUMNS: dict[str, ColumnElement[Any]] = {
    "created_at": task_subscriptions.c.created_at_ms,
    "updated_at": task_subscriptions.c.updated_at_ms,
    "name": task_subscriptions.c.display_name,
    "size": download_attempts.c.total_length,
}


class DownloadRepository:
    def __init__(self, connection: AsyncConnection) -> None:
        self.connection = connection

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
    ) -> int:
        try:
            result = await self.connection.execute(
                download_sources.insert().values(
                    kind=kind,
                    resource_key=resource_key,
                    payload_json=payload_json,
                    options_json=options_json,
                    selected_file_indexes_json=selected_file_indexes_json,
                    display_name=display_name,
                    display_uri=display_uri,
                    created_at_ms=now_ms,
                )
            )
        except IntegrityError:
            raise DomainError(ErrorCode.DUPLICATE_RESOURCE, "下载资源已存在") from None
        return int(result.inserted_primary_key[0])

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
    ) -> None:
        result = await self.connection.execute(
            update(download_sources)
            .where(download_sources.c.id == source_id)
            .where(download_sources.c.resource_key == resource_key)
            .values(
                payload_json=payload_json,
                options_json=options_json,
                selected_file_indexes_json=selected_file_indexes_json,
                display_name=display_name,
                display_uri=display_uri,
                detached_at_ms=None,
            )
        )
        if result.rowcount != 1:
            raise DomainError(ErrorCode.INVALID_STATE, "下载来源已变化，请重试")

    def _attempt(self, row: Row[Any]) -> AttemptRecord:
        record = row._mapping
        return AttemptRecord(
            id=int(record["id"]),
            source_id=int(record["source_id"]),
            resource_key=str(record["resource_key"]),
            gid=str(record["gid"]),
            status=AttemptStatus(record["status"]),
            completed_content_id=(
                None
                if record["completed_content_id"] is None
                else int(record["completed_content_id"])
            ),
            submission_state=str(record["submission_state"]),
            total_length=None if record["total_length"] is None else int(record["total_length"]),
            completed_length=int(record["completed_length"]),
            physical_reserved_bytes=int(record["physical_reserved_bytes"]),
            result_consumed_at_ms=(
                None
                if record["result_consumed_at_ms"] is None
                else int(record["result_consumed_at_ms"])
            ),
            error_code=record["error_code"],
        )

    async def create_attempt(
        self,
        *,
        source_id: int,
        resource_key: str,
        gid: str,
        now_ms: int,
        status: AttemptStatus = AttemptStatus.QUEUED,
        total_length: int | None = None,
        physical_reserved_bytes: int = 0,
        error_code: str | None = None,
    ) -> int:
        try:
            result = await self.connection.execute(
                download_attempts.insert().values(
                    source_id=source_id,
                    resource_key=resource_key,
                    gid=gid,
                    status=status,
                    total_length=total_length,
                    physical_reserved_bytes=physical_reserved_bytes,
                    error_code=error_code,
                    created_at_ms=now_ms,
                    updated_at_ms=now_ms,
                )
            )
        except IntegrityError:
            raise DomainError(ErrorCode.DUPLICATE_RESOURCE, "活动下载或 GID 已存在") from None
        return int(result.inserted_primary_key[0])

    async def get_attempt(self, attempt_id: int) -> AttemptRecord:
        row = (
            await self.connection.execute(
                select(download_attempts).where(download_attempts.c.id == attempt_id)
            )
        ).first()
        if row is None:
            raise DomainError(ErrorCode.NOT_FOUND, "下载尝试不存在")
        return self._attempt(row)

    async def find_live_attempt(self, resource_key: str) -> AttemptRecord | None:
        row = (
            await self.connection.execute(
                select(download_attempts)
                .where(download_attempts.c.resource_key == resource_key)
                .where(download_attempts.c.status.in_(LIVE_ATTEMPT_STATUSES))
            )
        ).first()
        return None if row is None else self._attempt(row)

    async def complete_attempt(
        self, attempt_id: int, *, content_id: int, now_ms: int
    ) -> None:
        attempt = await self.get_attempt(attempt_id)
        if attempt.status is AttemptStatus.COMPLETED:
            if attempt.completed_content_id == content_id:
                return
            raise DomainError(ErrorCode.INVALID_STATE, "完成尝试不能改写内容身份")
        transition_attempt(attempt.status, AttemptStatus.COMPLETED)
        available_content = (
            await self.connection.execute(
                select(content_objects.c.id)
                .where(content_objects.c.id == content_id)
                .where(content_objects.c.deletion_state == ContentDeletionState.ACTIVE)
            )
        ).first()
        if available_content is None:
            raise DomainError(ErrorCode.NOT_FOUND, "完成内容不存在或不可用")
        await self.connection.execute(
            update(download_attempts)
            .where(download_attempts.c.id == attempt_id)
            .values(
                status=AttemptStatus.COMPLETED,
                completed_content_id=content_id,
                updated_at_ms=now_ms,
                finished_at_ms=now_ms,
            )
        )
        await self.connection.execute(
            update(task_subscriptions)
            .where(task_subscriptions.c.attempt_id == attempt_id)
            .where(task_subscriptions.c.status.in_(LIVE_SUBSCRIPTION_STATUSES))
            .values(
                status=SubscriptionStatus.COMPLETED,
                updated_at_ms=now_ms,
                finished_at_ms=now_ms,
            )
        )

    async def find_completed_content_id(self, resource_key: str) -> int | None:
        value = (
            await self.connection.execute(
                select(download_attempts.c.completed_content_id)
                .join(
                    content_objects,
                    content_objects.c.id == download_attempts.c.completed_content_id,
                )
                .where(download_attempts.c.resource_key == resource_key)
                .where(download_attempts.c.status == AttemptStatus.COMPLETED)
                .where(content_objects.c.deletion_state == ContentDeletionState.ACTIVE)
                .order_by(download_attempts.c.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return None if value is None else int(value)

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
    ) -> int:
        try:
            result = await self.connection.execute(
                task_subscriptions.insert().values(
                    attempt_id=attempt_id,
                    user_id=user_id,
                    status=status,
                    reserved_bytes=reserved_bytes,
                    display_name=display_name,
                    display_uri=display_uri,
                    created_at_ms=now_ms,
                    updated_at_ms=now_ms,
                    finished_at_ms=(
                        now_ms if status in TERMINAL_SUBSCRIPTION_STATUSES else None
                    ),
                )
            )
        except IntegrityError:
            raise DomainError(ErrorCode.DUPLICATE_RESOURCE, "用户任务或关联对象无效") from None
        return int(result.inserted_primary_key[0])

    @staticmethod
    def _task_query() -> Select[Any]:
        return (
            select(
                task_subscriptions.c.id.label("task_id"),
                task_subscriptions.c.attempt_id,
                task_subscriptions.c.status.label("subscription_status"),
                task_subscriptions.c.reserved_bytes,
                task_subscriptions.c.display_name,
                task_subscriptions.c.display_uri,
                task_subscriptions.c.created_at_ms.label("subscription_created_at_ms"),
                task_subscriptions.c.updated_at_ms.label("subscription_updated_at_ms"),
                task_subscriptions.c.finished_at_ms.label("subscription_finished_at_ms"),
                download_attempts.c.gid,
                download_attempts.c.status.label("attempt_status"),
                download_attempts.c.total_length,
                download_attempts.c.completed_length,
                download_attempts.c.completed_content_id,
                func.coalesce(
                    task_subscriptions.c.error_code, download_attempts.c.error_code
                ).label("error_code"),
            )
            .select_from(
                task_subscriptions.join(
                    download_attempts,
                    download_attempts.c.id == task_subscriptions.c.attempt_id,
                )
            )
        )

    @staticmethod
    def _task(row: RowMapping) -> DownloadRecord:
        return DownloadRecord(
            id=int(row["task_id"]),
            attempt_id=int(row["attempt_id"]),
            gid=str(row["gid"]),
            subscription_status=SubscriptionStatus(row["subscription_status"]),
            attempt_status=AttemptStatus(row["attempt_status"]),
            name=str(row["display_name"]),
            display_uri=row["display_uri"],
            total_bytes=(
                None if row["total_length"] is None else int(row["total_length"])
            ),
            completed_bytes=int(row["completed_length"]),
            reserved_bytes=int(row["reserved_bytes"]),
            error_code=row["error_code"],
            created_at_ms=int(row["subscription_created_at_ms"]),
            updated_at_ms=int(row["subscription_updated_at_ms"]),
            finished_at_ms=row["subscription_finished_at_ms"],
            ingestion_pending=(
                row["completed_content_id"] is not None
                and AttemptStatus(row["attempt_status"]) in LIVE_ATTEMPT_STATUSES
            ),
        )

    async def list_tasks(
        self,
        *,
        user_id: int,
        sort: str = "updated_at",
        order: str = "desc",
    ) -> list[DownloadRecord]:
        column = _TASK_SORT_COLUMNS[sort]
        ascending = order == "asc"
        result = await self.connection.execute(
            self._task_query()
            .where(task_subscriptions.c.user_id == user_id)
            .order_by(
                column.asc() if ascending else column.desc(),
                task_subscriptions.c.id.asc() if ascending else task_subscriptions.c.id.desc(),
            )
        )
        return [self._task(row) for row in result.mappings().all()]

    async def get_task(self, *, user_id: int, task_id: int) -> DownloadRecord:
        row = (
            await self.connection.execute(
                self._task_query()
                .where(task_subscriptions.c.user_id == user_id)
                .where(task_subscriptions.c.id == task_id)
            )
        ).mappings().first()
        if row is None:
            raise DomainError(ErrorCode.NOT_FOUND, "任务不存在")
        return self._task(row)

    async def list_subscription_owners(
        self, attempt_id: int
    ) -> list[tuple[int, int]]:
        rows = (
            await self.connection.execute(
                select(task_subscriptions.c.id, task_subscriptions.c.user_id)
                .where(task_subscriptions.c.attempt_id == attempt_id)
                .order_by(task_subscriptions.c.id)
            )
        ).all()
        return [(int(row.id), int(row.user_id)) for row in rows]

    async def count_live_subscriptions(self, attempt_id: int) -> int:
        result = await self.connection.execute(
            select(func.count(task_subscriptions.c.id))
            .where(task_subscriptions.c.attempt_id == attempt_id)
            .where(task_subscriptions.c.status.in_(LIVE_SUBSCRIPTION_STATUSES))
        )
        return int(result.scalar_one())


    async def list_attempts_to_stop(self, user_id: int) -> list[str]:
        rows = (
            await self.connection.execute(
                select(
                    task_subscriptions.c.attempt_id,
                    task_subscriptions.c.status,
                    task_subscriptions.c.reserved_bytes,
                    download_attempts.c.gid,
                )
                .join(
                    download_attempts,
                    download_attempts.c.id == task_subscriptions.c.attempt_id,
                )
                .where(task_subscriptions.c.user_id == user_id)
                .where(task_subscriptions.c.status.in_(LIVE_SUBSCRIPTION_STATUSES))
                .where(download_attempts.c.status.in_(LIVE_ATTEMPT_STATUSES))
            )
        ).all()
        gids: list[str] = []
        for row in rows:
            decision = plan_cancellation(
                SubscriptionStatus(row.status),
                remaining_subscriptions=await self.count_live_subscriptions(row.attempt_id) - 1,
                reserved_bytes=int(row.reserved_bytes),
            )
            if decision.stop_attempt:
                gids.append(str(row.gid))
        return gids

    async def cleanup_user(self, *, user_id: int, now_ms: int) -> None:
        if (
            await self.connection.execute(select(users.c.id).where(users.c.id == user_id))
        ).first() is None:
            return
        rows = (
            await self.connection.execute(
                select(
                    task_subscriptions.c.attempt_id,
                    task_subscriptions.c.status,
                    task_subscriptions.c.reserved_bytes,
                    download_attempts.c.status.label("attempt_status"),
                )
                .join(
                    download_attempts,
                    download_attempts.c.id == task_subscriptions.c.attempt_id,
                )
                .where(task_subscriptions.c.user_id == user_id)
            )
        ).all()
        for row in rows:
            status = SubscriptionStatus(row.status)
            if status not in LIVE_SUBSCRIPTION_STATUSES:
                continue
            decision = plan_cancellation(
                status,
                remaining_subscriptions=await self.count_live_subscriptions(row.attempt_id) - 1,
                reserved_bytes=int(row.reserved_bytes),
            )
            await QuotaRepository(self.connection).release(user_id, decision.release_reserved)
            if decision.stop_attempt:
                attempt_status = AttemptStatus(row.attempt_status)
                transition_attempt(attempt_status, AttemptStatus.CANCELLED)
                result = await self.connection.execute(
                    update(download_attempts)
                    .where(download_attempts.c.id == row.attempt_id)
                    .where(download_attempts.c.status == attempt_status)
                    .values(
                        status=AttemptStatus.CANCELLED,
                        physical_reserved_bytes=0,
                        updated_at_ms=now_ms,
                        finished_at_ms=now_ms,
                    )
                )
                if result.rowcount != 1:
                    raise DomainError(ErrorCode.INVALID_STATE, "下载尝试已变化，请重试清理")
        await self.connection.execute(
            delete(task_subscriptions).where(task_subscriptions.c.user_id == user_id)
        )


    @staticmethod
    def _json_object(value: object) -> dict[str, object]:
        try:
            parsed: object = json.loads(str(value))
        except json.JSONDecodeError:
            raise DomainError(ErrorCode.INVALID_STATE, "下载来源数据无效") from None
        if not isinstance(parsed, dict):
            raise DomainError(ErrorCode.INVALID_STATE, "下载来源数据无效")
        return parsed

    def _source(self, row: RowMapping) -> SourceRecord:
        raw_selected = row["selected_file_indexes_json"]
        selected: tuple[int, ...] | None = None
        if raw_selected is not None:
            try:
                parsed: object = json.loads(str(raw_selected))
            except json.JSONDecodeError:
                raise DomainError(ErrorCode.INVALID_STATE, "种子选择数据无效") from None
            if not isinstance(parsed, list) or not all(
                isinstance(index, int) and not isinstance(index, bool) for index in parsed
            ):
                raise DomainError(ErrorCode.INVALID_STATE, "种子选择数据无效")
            selected = tuple(parsed)
        return SourceRecord(
            id=int(row["id"]),
            kind=str(row["kind"]),
            resource_key=str(row["resource_key"]),
            payload=self._json_object(row["payload_json"]),
            options=self._json_object(row["options_json"]),
            selected_file_indexes=selected,
            display_name=str(row["display_name"]),
            display_uri=row["display_uri"],
            detached_at_ms=row["detached_at_ms"],
        )

    async def find_source(self, resource_key: str) -> SourceRecord | None:
        row = (
            await self.connection.execute(
                select(download_sources).where(download_sources.c.resource_key == resource_key)
            )
        ).mappings().first()
        return None if row is None else self._source(row)

    async def get_source(self, source_id: int) -> SourceRecord:
        row = (
            await self.connection.execute(
                select(download_sources).where(download_sources.c.id == source_id)
            )
        ).mappings().first()
        if row is None:
            raise DomainError(ErrorCode.NOT_FOUND, "下载来源不存在")
        return self._source(row)

    async def get_source_for_attempt(self, attempt_id: int) -> SourceRecord:
        attempt = await self.get_attempt(attempt_id)
        return await self.get_source(attempt.source_id)

    async def find_latest_subscription(
        self, *, resource_key: str, user_id: int
    ) -> DownloadRecord | None:
        row = (
            await self.connection.execute(
                self._task_query()
                .where(download_attempts.c.resource_key == resource_key)
                .where(task_subscriptions.c.user_id == user_id)
                .order_by(task_subscriptions.c.id.desc())
                .limit(1)
            )
        ).mappings().first()
        return None if row is None else self._task(row)

    async def find_completed_attempt(self, resource_key: str) -> AttemptRecord | None:
        row = (
            await self.connection.execute(
                select(download_attempts)
                .join(
                    content_objects,
                    content_objects.c.id == download_attempts.c.completed_content_id,
                )
                .where(download_attempts.c.resource_key == resource_key)
                .where(download_attempts.c.status == AttemptStatus.COMPLETED)
                .where(content_objects.c.deletion_state == ContentDeletionState.ACTIVE)
                .order_by(download_attempts.c.id.desc())
                .limit(1)
            )
        ).first()
        return None if row is None else self._attempt(row)

    async def set_planned_gid(self, attempt_id: int, gid: str) -> None:
        result = await self.connection.execute(
            update(download_attempts)
            .where(download_attempts.c.id == attempt_id)
            .where(download_attempts.c.submission_state == "planned")
            .values(gid=gid)
        )
        if result.rowcount != 1:
            raise DomainError(ErrorCode.INVALID_STATE, "计划 GID 写入失败")

    async def get_submission(self, attempt_id: int) -> Submission:
        attempt = await self.get_attempt(attempt_id)
        return Submission(
            attempt_id=attempt.id,
            gid=attempt.gid,
            source=await self.get_source(attempt.source_id),
        )

    async def list_recovery_submissions(self) -> list[Submission]:
        rows = (
            await self.connection.execute(
                select(download_attempts.c.id)
                .where(download_attempts.c.status.in_(LIVE_ATTEMPT_STATUSES))
                .where(download_attempts.c.submission_state.in_(("planned", "uncertain")))
                .order_by(download_attempts.c.id)
            )
        ).scalars().all()
        return [await self.get_submission(int(attempt_id)) for attempt_id in rows]

    async def list_uncertain_stops(self) -> list[str]:
        rows = (
            await self.connection.execute(
                select(download_attempts.c.gid)
                .where(download_attempts.c.status.in_((AttemptStatus.CANCELLED, AttemptStatus.FAILED)))
                .where(download_attempts.c.submission_state == "uncertain")
                .order_by(download_attempts.c.id)
            )
        ).scalars().all()
        return [str(gid) for gid in rows]

    async def mark_submitted(self, attempt_id: int) -> None:
        await self.connection.execute(
            update(download_attempts)
            .where(download_attempts.c.id == attempt_id)
            .where(download_attempts.c.status.in_(LIVE_ATTEMPT_STATUSES))
            .values(submission_state="submitted")
        )

    async def mark_uncertain(self, attempt_id: int) -> None:
        await self.connection.execute(
            update(download_attempts)
            .where(download_attempts.c.id == attempt_id)
            .where(download_attempts.c.status.in_(LIVE_ATTEMPT_STATUSES))
            .values(submission_state="uncertain")
        )

    async def mark_stop_uncertain(self, attempt_id: int) -> None:
        await self.connection.execute(
            update(download_attempts)
            .where(download_attempts.c.id == attempt_id)
            .where(download_attempts.c.status.in_((AttemptStatus.CANCELLED, AttemptStatus.FAILED)))
            .values(submission_state="uncertain")
        )

    async def mark_stop_confirmed(self, attempt_id: int) -> None:
        await self.connection.execute(
            update(download_attempts)
            .where(download_attempts.c.id == attempt_id)
            .where(download_attempts.c.status.in_((AttemptStatus.CANCELLED, AttemptStatus.FAILED)))
            .values(submission_state="submitted")
        )

    async def confirm_stopped_gid(self, gid: str) -> None:
        result = await self.connection.execute(
            update(download_attempts)
            .where(download_attempts.c.gid == gid)
            .where(download_attempts.c.status == AttemptStatus.CANCELLED)
            .where(download_attempts.c.submission_state == "uncertain")
            .values(submission_state="submitted")
        )
        if result.rowcount != 1:
            raise DomainError(ErrorCode.INVALID_STATE, "取消恢复目标已变化")

    async def fail_attempt(
        self,
        attempt_id: int,
        *,
        error_code: str,
        now_ms: int,
        expected_gid: str | None = None,
    ) -> None:
        attempt = await self.get_attempt(attempt_id)
        if expected_gid is not None:
            require_gid_match(attempt_id, expected=expected_gid, observed=attempt.gid)
        if attempt.status is AttemptStatus.FAILED:
            return
        if attempt.status in {AttemptStatus.COMPLETED, AttemptStatus.CANCELLED}:
            raise DomainError(ErrorCode.STALE_EVENT, "下载已结束，忽略旧失败事件")
        transition_attempt(attempt.status, AttemptStatus.FAILED)
        rows = (
            await self.connection.execute(
                select(task_subscriptions)
                .where(task_subscriptions.c.attempt_id == attempt_id)
                .where(task_subscriptions.c.status.in_(LIVE_SUBSCRIPTION_STATUSES))
            )
        ).all()
        for row in rows:
            status = SubscriptionStatus(row.status)
            transition_subscription(status, SubscriptionStatus.FAILED)
            reserved = int(row.reserved_bytes)
            if reserved:
                await QuotaRepository(self.connection).release(int(row.user_id), reserved)
            await self.connection.execute(
                update(task_subscriptions)
                .where(task_subscriptions.c.id == row.id)
                .where(task_subscriptions.c.status == status)
                .values(
                    status=SubscriptionStatus.FAILED,
                    reserved_bytes=0,
                    error_code=error_code,
                    updated_at_ms=now_ms,
                    finished_at_ms=now_ms,
                )
            )
        await self.connection.execute(
            update(download_attempts)
            .where(download_attempts.c.id == attempt_id)
            .values(
                status=AttemptStatus.FAILED,
                physical_reserved_bytes=0,
                error_code=error_code,
                updated_at_ms=now_ms,
                finished_at_ms=now_ms,
            )
        )

    async def cancel_subscription(
        self, *, user_id: int, task_id: int, now_ms: int
    ) -> CancellationResult:
        record = await self.get_task(user_id=user_id, task_id=task_id)
        effective = effective_task_status(record.subscription_status, record.attempt_status)
        remaining = await self.count_live_subscriptions(record.attempt_id) - 1
        decision = plan_cancellation(
            effective,
            remaining_subscriptions=remaining,
            reserved_bytes=record.reserved_bytes,
        )
        updated = await self.connection.execute(
            update(task_subscriptions)
            .where(task_subscriptions.c.id == task_id)
            .where(task_subscriptions.c.status == record.subscription_status)
            .values(
                status=SubscriptionStatus.CANCELLED,
                reserved_bytes=0,
                updated_at_ms=now_ms,
                finished_at_ms=now_ms,
            )
        )
        if updated.rowcount != 1:
            raise DomainError(ErrorCode.INVALID_STATE, "任务已变化，请重试取消")
        if decision.release_reserved:
            await QuotaRepository(self.connection).release(user_id, decision.release_reserved)
        attempt = await self.get_attempt(record.attempt_id)
        stop_attempt = decision.stop_attempt and attempt.submission_state != "planned"
        if decision.stop_attempt:
            transition_attempt(attempt.status, AttemptStatus.CANCELLED)
            stopped = await self.connection.execute(
                update(download_attempts)
                .where(download_attempts.c.id == attempt.id)
                .where(download_attempts.c.status == attempt.status)
                .values(
                    status=AttemptStatus.CANCELLED,
                    physical_reserved_bytes=0,
                    submission_state=(
                        "uncertain" if stop_attempt else attempt.submission_state
                    ),
                    updated_at_ms=now_ms,
                    finished_at_ms=now_ms,
                )
            )
            if stopped.rowcount != 1:
                raise DomainError(ErrorCode.INVALID_STATE, "下载尝试已变化，请重试取消")
        return CancellationResult(
            task_id=task_id,
            attempt_id=record.attempt_id,
            gid=record.gid,
            stop_attempt=stop_attempt,
            submission_state=attempt.submission_state,
        )

    async def delete_history(self, *, user_id: int, task_id: int, now_ms: int) -> bool:
        record = await self.get_task(user_id=user_id, task_id=task_id)
        status = effective_task_status(record.subscription_status, record.attempt_status)
        if status not in TERMINAL_SUBSCRIPTION_STATUSES:
            raise DomainError(ErrorCode.INVALID_STATE, "进行中的任务不能作为历史删除")
        result = await self.connection.execute(
            delete(task_subscriptions)
            .where(task_subscriptions.c.id == task_id)
            .where(task_subscriptions.c.user_id == user_id)
        )
        if result.rowcount != 1:
            raise DomainError(ErrorCode.INVALID_STATE, "历史任务已变化，请重试")
        await self.detach_unused_sources(now_ms=now_ms)
        return True

    async def detach_unused_sources(self, *, now_ms: int) -> int:
        attempts_for_source = (
            select(download_attempts.c.id)
            .join(
                task_subscriptions,
                task_subscriptions.c.attempt_id == download_attempts.c.id,
            )
            .where(download_attempts.c.source_id == download_sources.c.id)
        )
        live_history = attempts_for_source.where(
            task_subscriptions.c.status != SubscriptionStatus.EXPIRED
        )
        result = await self.connection.execute(
            update(download_sources)
            .where(attempts_for_source.exists())
            .where(~live_history.exists())
            .where(download_sources.c.detached_at_ms.is_(None))
            .values(
                payload_json="{}",
                options_json="{}",
                selected_file_indexes_json=None,
                display_uri=None,
                detached_at_ms=now_ms,
            )
        )
        return int(result.rowcount)

    async def expire_history(self, *, cutoff_ms: int, now_ms: int) -> HistoryExpiration:
        terminal = (
            SubscriptionStatus.COMPLETED,
            SubscriptionStatus.FAILED,
            SubscriptionStatus.CANCELLED,
        )
        result = await self.connection.execute(
            update(task_subscriptions)
            .where(task_subscriptions.c.status.in_(terminal))
            .where(task_subscriptions.c.finished_at_ms.is_not(None))
            .where(task_subscriptions.c.finished_at_ms <= cutoff_ms)
            .values(
                status=SubscriptionStatus.EXPIRED,
                reserved_bytes=0,
                expired_at_ms=now_ms,
                updated_at_ms=now_ms,
            )
        )
        detached = await self.detach_unused_sources(now_ms=now_ms)
        return HistoryExpiration(int(result.rowcount), detached)


    async def find_attempt_by_gid(self, gid: str) -> AttemptRecord | None:
        row = (
            await self.connection.execute(
                select(download_attempts).where(download_attempts.c.gid == gid)
            )
        ).first()
        return None if row is None else self._attempt(row)

    async def list_live_attempts(self, *, submitted_only: bool = False) -> list[AttemptRecord]:
        statement = select(download_attempts).where(
            download_attempts.c.status.in_(LIVE_ATTEMPT_STATUSES)
        )
        if submitted_only:
            statement = statement.where(download_attempts.c.submission_state == "submitted")
        rows = (await self.connection.execute(statement.order_by(download_attempts.c.id))).all()
        return [self._attempt(row) for row in rows]

    async def list_completion_intents(self) -> list[AttemptRecord]:
        rows = (
            await self.connection.execute(
                select(download_attempts)
                .where(download_attempts.c.status.in_(LIVE_ATTEMPT_STATUSES))
                .where(download_attempts.c.completed_content_id.is_not(None))
                .order_by(download_attempts.c.id)
            )
        ).all()
        return [self._attempt(row) for row in rows]

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
    ) -> bool:
        attempt = await self.get_attempt(attempt_id)
        require_gid_match(attempt_id, expected=expected_gid, observed=attempt.gid)
        if attempt.status not in LIVE_ATTEMPT_STATUSES:
            return False
        transition_attempt(attempt.status, status)
        total = reconciled_total_length(attempt.total_length, total_length)
        completed = max(attempt.completed_length, completed_length)
        if completed < 0 or total is not None and completed > total:
            raise DomainError(ErrorCode.INVALID_STATE, "aria2 返回了不一致的下载进度")
        values: dict[str, object] = {
            "status": status,
            "total_length": total,
            "completed_length": completed,
            "updated_at_ms": now_ms,
        }
        if clear_pause:
            values["error_code"] = None
        elif external_pause:
            values["error_code"] = EXTERNAL_PAUSED
        result = await self.connection.execute(
            update(download_attempts)
            .where(download_attempts.c.id == attempt_id)
            .where(download_attempts.c.gid == expected_gid)
            .where(download_attempts.c.status == attempt.status)
            .values(**values)
        )
        if result.rowcount != 1:
            raise DomainError(ErrorCode.STALE_EVENT, "下载代际已变化，忽略旧观察")
        rows = (
            await self.connection.execute(
                select(task_subscriptions)
                .where(task_subscriptions.c.attempt_id == attempt_id)
                .where(task_subscriptions.c.status.in_(LIVE_SUBSCRIPTION_STATUSES))
            )
        ).all()
        target = SubscriptionStatus(status.value)
        for row in rows:
            current = SubscriptionStatus(row.status)
            transition_subscription(current, target)
            await self.connection.execute(
                update(task_subscriptions)
                .where(task_subscriptions.c.id == row.id)
                .where(task_subscriptions.c.status == current)
                .values(status=target, updated_at_ms=now_ms)
            )
        return True

    async def handoff_attempt(
        self, attempt_id: int, *, expected_gid: str, new_gid: str, now_ms: int
    ) -> None:
        if not new_gid or new_gid == expected_gid:
            raise DomainError(ErrorCode.INVALID_STATE, "磁链接交接 GID 无效")
        attempt = await self.get_attempt(attempt_id)
        require_gid_match(attempt_id, expected=expected_gid, observed=attempt.gid)
        if attempt.status not in LIVE_ATTEMPT_STATUSES:
            return
        try:
            result = await self.connection.execute(
                update(download_attempts)
                .where(download_attempts.c.id == attempt_id)
                .where(download_attempts.c.gid == expected_gid)
                .where(download_attempts.c.status.in_(LIVE_ATTEMPT_STATUSES))
                .values(
                    gid=new_gid,
                    status=AttemptStatus.WAITING,
                    total_length=None,
                    completed_length=0,
                    physical_reserved_bytes=0,
                    error_code=ADMISSION_PAUSED,
                    updated_at_ms=now_ms,
                )
            )
        except IntegrityError:
            raise DomainError(ErrorCode.STALE_EVENT, "交接目标已属于其他下载") from None
        if result.rowcount != 1:
            raise DomainError(ErrorCode.STALE_EVENT, "交接源已变化")
        rows = (
            await self.connection.execute(
                select(task_subscriptions)
                .where(task_subscriptions.c.attempt_id == attempt_id)
                .where(task_subscriptions.c.status.in_(LIVE_SUBSCRIPTION_STATUSES))
            )
        ).all()
        for row in rows:
            current = SubscriptionStatus(row.status)
            transition_subscription(current, SubscriptionStatus.WAITING)
            await self.connection.execute(
                update(task_subscriptions)
                .where(task_subscriptions.c.id == row.id)
                .where(task_subscriptions.c.status == current)
                .values(status=SubscriptionStatus.WAITING, updated_at_ms=now_ms)
            )

    async def begin_completion(
        self, attempt_id: int, *, expected_gid: str, content_id: int, now_ms: int
    ) -> None:
        attempt = await self.get_attempt(attempt_id)
        require_gid_match(attempt_id, expected=expected_gid, observed=attempt.gid)
        if attempt.status not in LIVE_ATTEMPT_STATUSES:
            return
        if attempt.completed_content_id not in {None, content_id}:
            raise DomainError(ErrorCode.INVALID_STATE, "入库意图已指向其他内容")
        result = await self.connection.execute(
            update(download_attempts)
            .where(download_attempts.c.id == attempt_id)
            .where(download_attempts.c.gid == expected_gid)
            .where(download_attempts.c.status.in_(LIVE_ATTEMPT_STATUSES))
            .values(completed_content_id=content_id, updated_at_ms=now_ms)
        )
        if result.rowcount != 1:
            raise DomainError(ErrorCode.STALE_EVENT, "入库目标已变化")

    async def finalize_completion(
        self, attempt_id: int, *, expected_gid: str, now_ms: int
    ) -> bool:
        attempt = await self.get_attempt(attempt_id)
        require_gid_match(attempt_id, expected=expected_gid, observed=attempt.gid)
        content_id = attempt.completed_content_id
        if content_id is None:
            raise DomainError(ErrorCode.INVALID_STATE, "下载尚未建立入库意图")
        if attempt.status is AttemptStatus.COMPLETED:
            return False
        if attempt.status not in LIVE_ATTEMPT_STATUSES:
            raise DomainError(ErrorCode.STALE_EVENT, "下载已结束，忽略旧完成事件")
        transition_attempt(attempt.status, AttemptStatus.COMPLETED)
        content_repository = ContentRepository(self.connection)
        content = await content_repository.get_object(content_id)
        if content.deletion_state is not ContentDeletionState.ACTIVE:
            raise DomainError(ErrorCode.CONTENT_PENDING_DELETE, "完成内容不可用")
        rows = (
            await self.connection.execute(
                select(task_subscriptions)
                .where(task_subscriptions.c.attempt_id == attempt_id)
                .where(task_subscriptions.c.status.in_(LIVE_SUBSCRIPTION_STATUSES))
                .order_by(task_subscriptions.c.id)
            )
        ).all()
        quota = QuotaRepository(self.connection)
        for row in rows:
            user_id = int(row.user_id)
            current = SubscriptionStatus(row.status)
            transition_subscription(current, SubscriptionStatus.COMPLETED)
            existing = await content_repository.find_user_file(
                user_id=user_id, content_id=content_id
            )
            if existing is None:
                await content_repository.add_user_file(
                    user_id=user_id,
                    content_id=content_id,
                    display_name=str(row.display_name),
                    now_ms=now_ms,
                )
            await quota.complete_download(
                user_id,
                int(row.reserved_bytes),
                content_size=content.size,
                already_owned=existing is not None,
            )
            updated = await self.connection.execute(
                update(task_subscriptions)
                .where(task_subscriptions.c.id == row.id)
                .where(task_subscriptions.c.status == current)
                .values(
                    status=SubscriptionStatus.COMPLETED,
                    reserved_bytes=0,
                    error_code=None,
                    updated_at_ms=now_ms,
                    finished_at_ms=now_ms,
                )
            )
            if updated.rowcount != 1:
                raise DomainError(ErrorCode.STALE_EVENT, "用户订阅已变化")
        result = await self.connection.execute(
            update(download_attempts)
            .where(download_attempts.c.id == attempt_id)
            .where(download_attempts.c.gid == expected_gid)
            .where(download_attempts.c.status == attempt.status)
            .values(
                status=AttemptStatus.COMPLETED,
                total_length=content.size,
                completed_length=content.size,
                physical_reserved_bytes=0,
                error_code=None,
                updated_at_ms=now_ms,
                finished_at_ms=now_ms,
            )
        )
        if result.rowcount != 1:
            raise DomainError(ErrorCode.STALE_EVENT, "完成目标已变化")
        return True

    async def list_terminal_cleanup(self) -> list[AttemptRecord]:
        rows = (
            await self.connection.execute(
                select(download_attempts)
                .where(
                    download_attempts.c.status.in_(
                        (AttemptStatus.COMPLETED, AttemptStatus.FAILED, AttemptStatus.CANCELLED)
                    )
                )
                .where(download_attempts.c.result_consumed_at_ms.is_(None))
                .where(download_attempts.c.submission_state != "uncertain")
                .order_by(download_attempts.c.id)
            )
        ).all()
        return [self._attempt(row) for row in rows]

    async def mark_result_consumed(
        self, attempt_id: int, *, expected_gid: str, now_ms: int
    ) -> None:
        result = await self.connection.execute(
            update(download_attempts)
            .where(download_attempts.c.id == attempt_id)
            .where(download_attempts.c.gid == expected_gid)
            .where(
                download_attempts.c.status.in_(
                    (AttemptStatus.COMPLETED, AttemptStatus.FAILED, AttemptStatus.CANCELLED)
                )
            )
            .values(result_consumed_at_ms=now_ms)
        )
        if result.rowcount != 1:
            raise DomainError(ErrorCode.STALE_EVENT, "结果清理目标已变化")

    async def list_all_gids(self) -> list[str]:
        rows = (await self.connection.execute(select(download_attempts.c.gid))).scalars().all()
        return [str(gid) for gid in rows]

    async def list_quota_user_ids(self) -> list[int]:
        rows = (await self.connection.execute(select(users.c.id).order_by(users.c.id))).scalars().all()
        return [int(user_id) for user_id in rows]

    async def admit_size(
        self,
        *,
        attempt_id: int,
        observed_bytes: int,
        known_total: bool,
        now_ms: int,
    ) -> DomainError | None:
        if observed_bytes < 0:
            raise DomainError(ErrorCode.INVALID_INPUT, "下载大小不能为负数")
        attempt = await self.get_attempt(attempt_id)
        if attempt.status not in LIVE_ATTEMPT_STATUSES:
            raise DomainError(ErrorCode.DOWNLOAD_UNAVAILABLE, "下载已结束")
        target = max(attempt.total_length or 0, observed_bytes)
        rows = (
            await self.connection.execute(
                select(task_subscriptions)
                .where(task_subscriptions.c.attempt_id == attempt_id)
                .where(task_subscriptions.c.status.in_(LIVE_SUBSCRIPTION_STATUSES))
                .order_by(task_subscriptions.c.id)
            )
        ).all()
        if not rows:
            raise DomainError(ErrorCode.DOWNLOAD_UNAVAILABLE, "下载已没有有效订阅")
        first_failure: DomainError | None = None
        for row in rows:
            user_id = int(row.user_id)
            reserved = int(row.reserved_bytes)
            desired = max(reserved, target)
            delta = desired - reserved
            try:
                if delta:
                    await QuotaRepository(self.connection).reserve(user_id, delta)
            except DomainError as error:
                if error.code not in {
                    ErrorCode.QUOTA_EXCEEDED,
                    ErrorCode.PHYSICAL_SPACE_EXCEEDED,
                }:
                    raise
                if reserved:
                    await QuotaRepository(self.connection).release(user_id, reserved)
                status = SubscriptionStatus(row.status)
                transition_subscription(status, SubscriptionStatus.FAILED)
                await self.connection.execute(
                    update(task_subscriptions)
                    .where(task_subscriptions.c.id == row.id)
                    .values(
                        status=SubscriptionStatus.FAILED,
                        reserved_bytes=0,
                        error_code=error.code.value,
                        updated_at_ms=now_ms,
                        finished_at_ms=now_ms,
                    )
                )
                first_failure = first_failure or error
            else:
                await self.connection.execute(
                    update(task_subscriptions)
                    .where(task_subscriptions.c.id == row.id)
                    .values(reserved_bytes=desired, updated_at_ms=now_ms)
                )
        remaining = await self.count_live_subscriptions(attempt_id)
        if remaining == 0:
            failure = first_failure or DomainError(ErrorCode.QUOTA_EXCEEDED, "用户配额不足")
            transition_attempt(attempt.status, AttemptStatus.FAILED)
            await self.connection.execute(
                update(download_attempts)
                .where(download_attempts.c.id == attempt_id)
                .values(
                    status=AttemptStatus.FAILED,
                    physical_reserved_bytes=0,
                    error_code=failure.code.value,
                    updated_at_ms=now_ms,
                    finished_at_ms=now_ms,
                )
            )
            return failure
        values: dict[str, object] = {
            "physical_reserved_bytes": max(attempt.physical_reserved_bytes, target),
            "updated_at_ms": now_ms,
        }
        if known_total:
            values["total_length"] = target
        await self.connection.execute(
            update(download_attempts).where(download_attempts.c.id == attempt_id).values(**values)
        )
        return None
