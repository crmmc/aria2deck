from __future__ import annotations

from typing import Any

from sqlalchemy import (
    ColumnElement,
    LargeBinary,
    Row,
    case,
    cast,
    delete,
    func,
    not_,
    or_,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from ....application.packs.models import (
    PackAdmissionSource,
    PackJobRecord,
    PackSourceRecord,
)
from ....domain.content import (
    ContentDeletionState,
    ContentKind,
    validate_display_name,
)
from ....domain.errors import DomainError, ErrorCode
from ....domain.packs import (
    ArchiveEntrySummary,
    PackProgress,
    PackSourceSnapshot,
    PackStatus,
    PackStep,
    PreparedOutput,
    prepare_output,
    require_pack_output_within_budget,
    transition_pack,
    validate_prepared_values,
)
from ....domain.quota import PhysicalPack
from ..schema import (
    content_entries,
    content_objects,
    pack_jobs,
    pack_sources,
    user_files,
    users,
)
from .content import ContentRepository
from .quota import QuotaRepository


def _due(now_ms: int) -> ColumnElement[bool]:
    return or_(
        pack_jobs.c.next_attempt_at_ms.is_(None), pack_jobs.c.next_attempt_at_ms <= now_ms
    )


def _no_packing_exists() -> ColumnElement[bool]:
    return not_(select(pack_jobs.c.id).where(pack_jobs.c.status == PackStatus.PACKING).exists())


class PackRepository:
    def __init__(self, connection: AsyncConnection) -> None:
        self.connection = connection

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
    ) -> int:
        name = validate_display_name(output_name)
        if min(source_size, reserved_bytes, physical_reserved_bytes) < 0:
            raise DomainError(ErrorCode.INVALID_INPUT, "打包大小或预留不能为负数")
        owner = (
            await self.connection.execute(
                select(users.c.pending_delete).where(users.c.id == owner_id)
            )
        ).first()
        if owner is None:
            raise DomainError(ErrorCode.NOT_FOUND, "打包任务的用户不存在")
        if bool(owner.pending_delete):
            raise DomainError(ErrorCode.CONTENT_PENDING_DELETE, "账号正在删除，不能创建打包任务")
        result = await self.connection.execute(
            pack_jobs.insert().values(
                owner_id=owner_id,
                output_name=name,
                pack_format=pack_format,
                source_size=source_size,
                reserved_bytes=reserved_bytes,
                physical_reserved_bytes=physical_reserved_bytes,
                delete_source=delete_source,
                created_at_ms=now_ms,
                updated_at_ms=now_ms,
            )
        )
        return int(result.inserted_primary_key[0])

    def _job(self, row: Row[Any]) -> PackJobRecord:
        return PackJobRecord(
            id=int(row.id),
            owner_id=int(row.owner_id),
            status=PackStatus(row.status),
            step=None if row.step is None else PackStep(row.step),
            step_progress=int(row.step_progress),
            step_started_at_ms=(
                None if row.step_started_at_ms is None else int(row.step_started_at_ms)
            ),
            output_name=str(row.output_name),
            pack_format=str(row.pack_format),
            source_size=int(row.source_size),
            reserved_bytes=int(row.reserved_bytes),
            physical_reserved_bytes=int(row.physical_reserved_bytes),
            delete_source=bool(row.delete_source),
            prepared=validate_prepared_values(row.prepared_path, row.prepared_size, row.prepared_digest),
            retry_count=int(row.retry_count),
            next_attempt_at_ms=(
                None if row.next_attempt_at_ms is None else int(row.next_attempt_at_ms)
            ),
            error_code=row.error_code,
            cleanup_pending=bool(row.cleanup_pending),
            output_content_id=(
                None if row.output_content_id is None else int(row.output_content_id)
            ),
            output_content_key=row.content_key,
            created_at_ms=int(row.created_at_ms),
            updated_at_ms=int(row.updated_at_ms),
            finished_at_ms=None if row.finished_at_ms is None else int(row.finished_at_ms),
        )

    async def get_job(self, pack_id: int) -> PackJobRecord:
        row = (
            await self.connection.execute(
                select(pack_jobs, content_objects.c.content_key)
                .select_from(
                    pack_jobs.outerjoin(
                        content_objects, content_objects.c.id == pack_jobs.c.output_content_id
                    )
                )
                .where(pack_jobs.c.id == pack_id)
            )
        ).first()
        if row is None:
            raise DomainError(ErrorCode.NOT_FOUND, "打包任务不存在")
        return self._job(row)

    async def list_packing_jobs(self) -> list[PackJobRecord]:
        rows = (
            await self.connection.execute(
                select(pack_jobs, content_objects.c.content_key)
                .select_from(
                    pack_jobs.outerjoin(
                        content_objects, content_objects.c.id == pack_jobs.c.output_content_id
                    )
                )
                .where(pack_jobs.c.status == PackStatus.PACKING)
                .order_by(pack_jobs.c.id)
            )
        ).all()
        return [self._job(row) for row in rows]

    async def list_recent_jobs(self, *, user_id: int, limit: int) -> list[PackJobRecord]:
        rows = (
            await self.connection.execute(
                select(pack_jobs, content_objects.c.content_key)
                .select_from(
                    pack_jobs.outerjoin(
                        content_objects, content_objects.c.id == pack_jobs.c.output_content_id
                    )
                )
                .where(pack_jobs.c.owner_id == user_id)
                .order_by(pack_jobs.c.id.desc())
                .limit(limit)
            )
        ).all()
        return [self._job(row) for row in rows]

    async def list_admission_sources(
        self, *, user_id: int, user_file_ids: list[int]
    ) -> list[PackAdmissionSource]:
        if not user_file_ids:
            return []
        rows = (
            await self.connection.execute(
                select(
                    user_files.c.id,
                    user_files.c.content_id,
                    content_objects.c.content_key,
                    user_files.c.display_name,
                    user_files.c.created_at_ms,
                    content_objects.c.size,
                    content_objects.c.kind,
                    content_objects.c.storage_path,
                    content_objects.c.digest,
                    content_objects.c.deletion_state,
                )
                .select_from(
                    user_files.join(content_objects, content_objects.c.id == user_files.c.content_id)
                )
                .where(user_files.c.user_id == user_id)
                .where(user_files.c.id.in_(user_file_ids))
                .order_by(user_files.c.id)
            )
        ).all()
        return [
            PackAdmissionSource(
                user_file_id=int(row.id),
                content_id=int(row.content_id),
                content_key=str(row.content_key),
                display_name=str(row.display_name),
                created_at_ms=int(row.created_at_ms),
                size=int(row.size),
                kind=ContentKind(row.kind),
                storage_path=str(row.storage_path),
                digest=str(row.digest),
                deletion_state=ContentDeletionState(row.deletion_state),
            )
            for row in rows
        ]

    async def list_content_summaries(
        self, *, content_ids: list[int]
    ) -> dict[int, ArchiveEntrySummary]:
        """Aggregate directory entries per content id: one row per distinct content.

        Returns no row for a content without entries (an empty directory), so callers
        must treat a missing key as a zero summary rather than as projection drift.
        """
        if not content_ids:
            return {}
        path_bytes = func.length(cast(content_entries.c.path, LargeBinary))
        is_directory = content_entries.c.entry_kind == ContentKind.DIRECTORY
        rows = (
            await self.connection.execute(
                select(
                    content_entries.c.content_id,
                    func.count().label("entry_count"),
                    func.coalesce(func.sum(path_bytes), 0).label("path_bytes"),
                    func.coalesce(
                        func.sum(case((is_directory, 0), else_=content_entries.c.size)), 0
                    ).label("file_bytes"),
                    func.coalesce(func.sum(case((is_directory, 1), else_=0)), 0).label(
                        "directory_count"
                    ),
                    func.coalesce(
                        func.max(path_bytes + case((is_directory, 1), else_=0)), 0
                    ).label("max_path_bytes"),
                )
                .select_from(content_entries)
                .where(content_entries.c.content_id.in_(content_ids))
                .group_by(content_entries.c.content_id)
            )
        ).all()
        return {
            int(row.content_id): ArchiveEntrySummary(
                entry_count=int(row.entry_count),
                path_bytes=int(row.path_bytes),
                file_bytes=int(row.file_bytes),
                directory_count=int(row.directory_count),
                max_path_bytes=int(row.max_path_bytes),
            )
            for row in rows
        }

    async def list_active_physical(self) -> list[PhysicalPack]:
        rows = (
            await self.connection.execute(
                select(
                    pack_jobs.c.status,
                    pack_jobs.c.physical_reserved_bytes,
                    pack_jobs.c.prepared_size,
                ).where(pack_jobs.c.status.in_([PackStatus.PENDING, PackStatus.PACKING]))
            )
        ).all()
        return [
            PhysicalPack(
                status=PackStatus(row.status),
                output_reservation=int(row.physical_reserved_bytes),
                prepared_bytes=int(row.prepared_size or 0),
            )
            for row in rows
        ]

    async def claim_next_pending(self, *, now_ms: int) -> int | None:
        """Claim the oldest due pending job; a global packing job blocks new claims."""
        busy = (
            await self.connection.execute(
                select(pack_jobs.c.id).where(pack_jobs.c.status == PackStatus.PACKING).limit(1)
            )
        ).first()
        if busy is not None:
            return None
        candidate = (
            await self.connection.execute(
                select(pack_jobs.c.id)
                .where(pack_jobs.c.status == PackStatus.PENDING)
                .where(_due(now_ms))
                .order_by(pack_jobs.c.id)
                .limit(1)
            )
        ).first()
        if candidate is None:
            return None
        result = await self.connection.execute(
            update(pack_jobs)
            .where(pack_jobs.c.id == int(candidate.id))
            .where(pack_jobs.c.status == PackStatus.PENDING)
            .where(_due(now_ms))
            .where(_no_packing_exists())
            .values(
                status=PackStatus.PACKING,
                step=PackStep.VALIDATING,
                step_progress=0,
                step_started_at_ms=now_ms,
                next_attempt_at_ms=None,
                updated_at_ms=now_ms,
            )
        )
        return int(candidate.id) if result.rowcount == 1 else None

    async def update_step(
        self, pack_id: int, *, from_step: PackStep, to_step: PackStep, now_ms: int
    ) -> bool:
        result = await self.connection.execute(
            update(pack_jobs)
            .where(pack_jobs.c.id == pack_id)
            .where(pack_jobs.c.status == PackStatus.PACKING)
            .where(pack_jobs.c.step == from_step)
            .values(
                step=to_step,
                step_progress=0,
                step_started_at_ms=now_ms,
                updated_at_ms=now_ms,
            )
        )
        return result.rowcount == 1

    async def update_step_progress(
        self, pack_id: int, *, step: PackStep, progress: int, now_ms: int
    ) -> bool:
        PackProgress(PackStatus.PACKING, step, progress)  # 校验 0..100
        result = await self.connection.execute(
            update(pack_jobs)
            .where(pack_jobs.c.id == pack_id)
            .where(pack_jobs.c.status == PackStatus.PACKING)
            .where(pack_jobs.c.step == step)
            .values(step_progress=progress, updated_at_ms=now_ms)
        )
        return result.rowcount == 1

    async def set_prepared(self, pack_id: int, prepared: PreparedOutput, *, now_ms: int) -> None:
        validated = validate_prepared_values(prepared.path, prepared.size, prepared.digest)
        job = await self.get_job(pack_id)
        candidate = prepare_output(
            PackProgress(job.status, job.step, job.step_progress),
            path=validated.path,
            size=validated.size,
            digest=validated.digest,
        )
        require_pack_output_within_budget(candidate.size, job.reserved_bytes)
        if job.prepared is not None:
            if job.prepared == candidate:
                return
            raise DomainError(ErrorCode.INVALID_STATE, "prepared 输出与已登记结果不一致")
        result = await self.connection.execute(
            update(pack_jobs)
            .where(pack_jobs.c.id == pack_id)
            .where(pack_jobs.c.status == PackStatus.PACKING)
            .where(pack_jobs.c.step == PackStep.VERIFYING)
            .where(pack_jobs.c.prepared_path.is_(None))
            .values(
                prepared_path=candidate.path,
                prepared_size=candidate.size,
                prepared_digest=candidate.digest,
                updated_at_ms=now_ms,
            )
        )
        if result.rowcount != 1:
            raise DomainError(ErrorCode.INVALID_STATE, "打包状态或 prepared 输出已变化，请重试")

    async def requeue_transient(
        self,
        pack_id: int,
        *,
        delay_ms: int,
        now_ms: int,
        discard_prepared: bool = False,
    ) -> bool:
        if delay_ms < 0:
            raise DomainError(ErrorCode.INVALID_INPUT, "重试延迟不能为负数")
        values: dict[str, object] = {
            "status": PackStatus.PENDING,
            "step": None,
            "step_progress": 0,
            "step_started_at_ms": None,
            "retry_count": pack_jobs.c.retry_count + 1,
            "next_attempt_at_ms": now_ms + delay_ms,
            "updated_at_ms": now_ms,
        }
        if discard_prepared:
            # 同事务清掉不可用的 prepared 登记，下一次领取必须重新压缩。
            values |= {"prepared_path": None, "prepared_size": None, "prepared_digest": None}
        result = await self.connection.execute(
            update(pack_jobs)
            .where(pack_jobs.c.id == pack_id)
            .where(pack_jobs.c.status == PackStatus.PACKING)
            .values(**values)
        )
        return result.rowcount == 1

    async def mark_sources_kept(self, pack_id: int) -> int:
        """终态且不再删源的任务：释放来源对文件删除的占用。"""
        result = await self.connection.execute(
            update(pack_sources)
            .where(pack_sources.c.pack_id == pack_id)
            .where(pack_sources.c.cleanup_status == "pending")
            .values(cleanup_status="kept", cleanup_error_code=None)
        )
        return int(result.rowcount)

    async def fail_job(self, pack_id: int, *, error_code: str, now_ms: int) -> bool:
        result = await self.connection.execute(
            update(pack_jobs)
            .where(pack_jobs.c.id == pack_id)
            .where(pack_jobs.c.status == PackStatus.PACKING)
            .values(
                status=PackStatus.FAILED,
                step=None,
                step_progress=0,
                step_started_at_ms=None,
                reserved_bytes=0,
                physical_reserved_bytes=0,
                error_code=error_code,
                finished_at_ms=now_ms,
                updated_at_ms=now_ms,
            )
        )
        if result.rowcount == 1:
            await self.mark_sources_kept(pack_id)
        return result.rowcount == 1

    async def mark_cancelled(self, pack_id: int, *, now_ms: int) -> bool:
        result = await self.connection.execute(
            update(pack_jobs)
            .where(pack_jobs.c.id == pack_id)
            .where(pack_jobs.c.status.in_([PackStatus.PENDING, PackStatus.PACKING]))
            .values(
                status=PackStatus.CANCELLED,
                step=None,
                step_progress=0,
                step_started_at_ms=None,
                prepared_path=None,
                prepared_size=None,
                prepared_digest=None,
                reserved_bytes=0,
                physical_reserved_bytes=0,
                finished_at_ms=now_ms,
                updated_at_ms=now_ms,
            )
        )
        if result.rowcount == 1:
            await self.mark_sources_kept(pack_id)
        return result.rowcount == 1

    async def content_object_exists(self, content_key: str) -> bool:
        """只读查询：输出是否已被任何引用登记（安装后回收的安全前提）。"""
        return await ContentRepository(self.connection).find_object_by_key(content_key) is not None

    async def complete_job(self, pack_id: int, *, now_ms: int) -> int:
        """Atomically install the output reference and convert reservation to used bytes."""
        job = await self.get_job(pack_id)
        if job.status is not PackStatus.PACKING or job.prepared is None:
            raise DomainError(ErrorCode.INVALID_STATE, "打包任务尚未完成最终验收")
        require_pack_output_within_budget(job.prepared.size, job.reserved_bytes)
        digest = job.prepared.digest
        storage_path = f"objects/file-{digest}"
        content = ContentRepository(self.connection)
        existing = await content.find_object_by_key(f"file:{digest}")
        if existing is None:
            content_id = await content.create_object(
                kind="file",
                digest=digest,
                size=job.prepared.size,
                storage_path=storage_path,
                now_ms=now_ms,
            )
        else:
            if (
                existing.deletion_state is not ContentDeletionState.ACTIVE
                or existing.size != job.prepared.size
                or existing.storage_path != storage_path
            ):
                raise DomainError(ErrorCode.INVALID_STATE, "输出内容身份与现有对象冲突")
            content_id = existing.id
        already_owned = (
            await content.find_user_file(user_id=job.owner_id, content_id=content_id) is not None
        )
        if not already_owned:
            await content.add_user_file(
                user_id=job.owner_id,
                content_id=content_id,
                display_name=job.output_name,
                now_ms=now_ms,
            )
        await QuotaRepository(self.connection).complete_download(
            job.owner_id,
            job.reserved_bytes,
            content_size=job.prepared.size,
            already_owned=already_owned,
        )
        result = await self.connection.execute(
            update(pack_jobs)
            .where(pack_jobs.c.id == pack_id)
            .where(pack_jobs.c.status == PackStatus.PACKING)
            .values(
                status=PackStatus.DONE,
                step=None,
                step_progress=100,
                step_started_at_ms=None,
                reserved_bytes=0,
                physical_reserved_bytes=0,
                output_content_id=content_id,
                cleanup_pending=job.delete_source,
                finished_at_ms=now_ms,
                updated_at_ms=now_ms,
            )
        )
        if result.rowcount != 1:
            raise DomainError(ErrorCode.INVALID_STATE, "打包状态已变化，完成转换失败")
        if not job.delete_source:
            await self.mark_sources_kept(pack_id)
        return content_id

    async def add_source(self, pack_id: int, snapshot: PackSourceSnapshot) -> int:
        try:
            result = await self.connection.execute(
                pack_sources.insert().values(
                    pack_id=pack_id,
                    source_user_file_id=snapshot.user_file_id,
                    source_content_id=snapshot.content_id,
                    content_key=snapshot.content_key,
                    display_name=snapshot.display_name,
                    source_created_at_ms=snapshot.created_at_ms,
                    source_size=snapshot.size,
                )
            )
        except IntegrityError:
            raise DomainError(ErrorCode.DUPLICATE_RESOURCE, "打包来源快照重复或任务不存在") from None
        return int(result.inserted_primary_key[0])

    async def get_source(self, source_id: int) -> PackSourceRecord:
        row = (
            await self.connection.execute(select(pack_sources).where(pack_sources.c.id == source_id))
        ).first()
        if row is None:
            raise DomainError(ErrorCode.NOT_FOUND, "打包来源不存在")
        snapshot = PackSourceSnapshot(
            user_file_id=int(row.source_user_file_id),
            content_id=int(row.source_content_id),
            content_key=str(row.content_key),
            display_name=str(row.display_name),
            created_at_ms=int(row.source_created_at_ms),
            size=int(row.source_size),
        )
        return PackSourceRecord(
            int(row.id), int(row.pack_id), snapshot, str(row.cleanup_status), row.cleanup_error_code
        )

    async def list_sources(self, pack_id: int) -> list[PackSourceRecord]:
        rows = (
            await self.connection.execute(
                select(pack_sources).where(pack_sources.c.pack_id == pack_id).order_by(pack_sources.c.id)
            )
        ).all()
        records: list[PackSourceRecord] = []
        for row in rows:
            snapshot = PackSourceSnapshot(
                user_file_id=int(row.source_user_file_id),
                content_id=int(row.source_content_id),
                content_key=str(row.content_key),
                display_name=str(row.display_name),
                created_at_ms=int(row.source_created_at_ms),
                size=int(row.source_size),
            )
            records.append(
                PackSourceRecord(
                    int(row.id), int(row.pack_id), snapshot, str(row.cleanup_status), row.cleanup_error_code
                )
            )
        return records

    async def count_pending_sources(self, pack_id: int) -> int:
        return int(
            (
                await self.connection.execute(
                    select(func.count())
                    .select_from(pack_sources)
                    .where(pack_sources.c.pack_id == pack_id)
                    .where(pack_sources.c.cleanup_status == "pending")
                )
            ).scalar_one()
        )

    async def set_source_cleanup(
        self, source_id: int, *, status: str, error_code: str | None = None
    ) -> bool:
        if status not in ("deleted", "kept"):
            raise DomainError(ErrorCode.INVALID_INPUT, "来源清理状态无效")
        result = await self.connection.execute(
            update(pack_sources)
            .where(pack_sources.c.id == source_id)
            .where(pack_sources.c.cleanup_status == "pending")
            .values(cleanup_status=status, cleanup_error_code=error_code)
        )
        return result.rowcount == 1

    async def list_cleanup_pending_jobs(self) -> list[PackJobRecord]:
        rows = (
            await self.connection.execute(
                select(pack_jobs, content_objects.c.content_key)
                .select_from(
                    pack_jobs.outerjoin(
                        content_objects, content_objects.c.id == pack_jobs.c.output_content_id
                    )
                )
                .where(pack_jobs.c.cleanup_pending)
                .order_by(pack_jobs.c.id)
            )
        ).all()
        return [self._job(row) for row in rows]

    async def finish_source_cleanup(self, pack_id: int, *, now_ms: int) -> bool:
        pending = await self.count_pending_sources(pack_id)
        if pending:
            raise DomainError(ErrorCode.INVALID_STATE, "仍有来源未完成清理")
        result = await self.connection.execute(
            update(pack_jobs)
            .where(pack_jobs.c.id == pack_id)
            .where(pack_jobs.c.cleanup_pending)
            .values(cleanup_pending=False, updated_at_ms=now_ms)
        )
        return result.rowcount == 1

    async def delete_job(self, pack_id: int) -> bool:
        result = await self.connection.execute(
            delete(pack_jobs).where(pack_jobs.c.id == pack_id)
        )
        return result.rowcount == 1

    async def clear_terminal_records(self, *, user_id: int) -> int:
        """清空本人全部终态打包记录（A3）；来源清理未完成的记录保留。"""
        pending_sources = (
            select(pack_sources.c.id)
            .where(pack_sources.c.pack_id == pack_jobs.c.id)
            .where(pack_sources.c.cleanup_status == "pending")
        )
        result = await self.connection.execute(
            delete(pack_jobs)
            .where(pack_jobs.c.owner_id == user_id)
            .where(
                pack_jobs.c.status.in_(
                    (PackStatus.DONE, PackStatus.FAILED, PackStatus.CANCELLED)
                )
            )
            .where(~pending_sources.exists())
        )
        return int(result.rowcount)

    async def list_user_job_ids(self, user_id: int) -> list[int]:
        """列出该用户全部尚未删除的打包记录，含已 cancelled 的记录。"""
        rows = (
            await self.connection.execute(
                select(pack_jobs.c.id)
                .where(pack_jobs.c.owner_id == user_id)
                .order_by(pack_jobs.c.id)
            )
        ).scalars().all()
        return [int(pack_id) for pack_id in rows]

    async def mark_user_jobs_cancelled(self, *, user_id: int, now_ms: int) -> None:
        """把该用户仍在进行中的任务标 cancelled；已终态记录保持原状。"""
        if (
            await self.connection.execute(select(users.c.id).where(users.c.id == user_id))
        ).first() is None:
            return
        rows = (
            await self.connection.execute(
                select(pack_jobs.c.id, pack_jobs.c.status).where(pack_jobs.c.owner_id == user_id)
            )
        ).all()
        for row in rows:
            status = PackStatus(row.status)
            if status not in {PackStatus.PENDING, PackStatus.PACKING}:
                continue
            transition_pack(status, PackStatus.CANCELLED)
            result = await self.connection.execute(
                update(pack_jobs)
                .where(pack_jobs.c.id == row.id)
                .where(pack_jobs.c.status == status)
                .values(
                    status=PackStatus.CANCELLED,
                    step=None,
                    step_progress=0,
                    reserved_bytes=0,
                    physical_reserved_bytes=0,
                    updated_at_ms=now_ms,
                    finished_at_ms=now_ms,
                )
            )
            if result.rowcount != 1:
                raise DomainError(ErrorCode.INVALID_STATE, "打包任务已变化，请重试清理")

    async def delete_user_jobs_and_rebuild_quota(self, *, user_id: int) -> None:
        """删除该用户全部打包记录并在同一 UoW 内重建配额。"""
        if (
            await self.connection.execute(select(users.c.id).where(users.c.id == user_id))
        ).first() is None:
            return
        await self.connection.execute(delete(pack_jobs).where(pack_jobs.c.owner_id == user_id))
        await QuotaRepository(self.connection).rebuild(user_id)
