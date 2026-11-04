from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from ....domain.content import validate_display_name
from ....domain.errors import DomainError, ErrorCode
from ....domain.packs import (
    PackProgress,
    PackSourceSnapshot,
    PackStatus,
    PackStep,
    PreparedOutput,
    prepare_output,
    transition_pack,
    validate_prepared_values,
)
from ..schema import pack_jobs, pack_sources, users
from .quota import QuotaRepository


@dataclass(frozen=True, slots=True)
class PackRecord:
    id: int
    owner_id: int
    status: PackStatus
    step: PackStep | None
    step_progress: int
    output_name: str
    prepared: PreparedOutput | None


@dataclass(frozen=True, slots=True)
class PackSourceRecord:
    id: int
    pack_id: int
    snapshot: PackSourceSnapshot
    cleanup_status: str


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

    async def get_job(self, pack_id: int) -> PackRecord:
        row = (
            await self.connection.execute(select(pack_jobs).where(pack_jobs.c.id == pack_id))
        ).first()
        if row is None:
            raise DomainError(ErrorCode.NOT_FOUND, "打包任务不存在")
        return PackRecord(
            id=int(row.id),
            owner_id=int(row.owner_id),
            status=PackStatus(row.status),
            step=None if row.step is None else PackStep(row.step),
            step_progress=int(row.step_progress),
            output_name=str(row.output_name),
            prepared=validate_prepared_values(row.prepared_path, row.prepared_size, row.prepared_digest),
        )

    async def set_prepared(self, pack_id: int, prepared: PreparedOutput, *, now_ms: int) -> None:
        validated = validate_prepared_values(prepared.path, prepared.size, prepared.digest)
        job = await self.get_job(pack_id)
        candidate = prepare_output(
            PackProgress(job.status, job.step, job.step_progress),
            path=validated.path,
            size=validated.size,
            digest=validated.digest,
        )
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
        return PackSourceRecord(int(row.id), int(row.pack_id), snapshot, str(row.cleanup_status))


    async def list_active_jobs(self, user_id: int) -> list[int]:
        rows = (
            await self.connection.execute(
                select(pack_jobs.c.id)
                .where(pack_jobs.c.owner_id == user_id)
                .where(pack_jobs.c.status.in_([PackStatus.PENDING, PackStatus.PACKING]))
                .order_by(pack_jobs.c.id)
            )
        ).scalars().all()
        return [int(pack_id) for pack_id in rows]

    async def cleanup_user(self, *, user_id: int, now_ms: int) -> None:
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
        await self.connection.execute(delete(pack_jobs).where(pack_jobs.c.owner_id == user_id))
        await QuotaRepository(self.connection).rebuild(user_id)
