"""存储文件管理路由（管理员专用）"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select, update

from app.auth import AuthUser, require_admin
from app.db.engine import transaction
from app.db.schema import (
    global_downloads,
    pack_tasks,
    stored_file_entries,
    stored_files,
    user_tasks,
    user_files,
    users,
)
from app.routers.files import ms_to_iso, now_ms
from app.services.storage import get_store_dir, safe_delete_path

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/storage", tags=["admin-storage"])


class StoredFileInfo(BaseModel):
    id: int
    content_hash: str
    original_name: str
    size: int
    is_directory: bool
    ref_count: int
    created_at: str
    real_path: str
    exists_on_disk: bool


class StoredFileListResponse(BaseModel):
    files: list[StoredFileInfo]
    total: int


class FileUserInfo(BaseModel):
    user_id: int
    username: str
    display_name: str


class FileUsersResponse(BaseModel):
    file_id: int
    users: list[FileUserInfo]


class BulkDeleteRequest(BaseModel):
    file_ids: list[int] = Field(..., min_length=1, max_length=1000)


class BulkDeleteResponse(BaseModel):
    deleted_count: int
    failed_ids: list[int]
    errors: list[str]


class ScanResult(BaseModel):
    scanned_dirs: int
    new_records: int
    already_exists: int
    errors: list[str]


class RepairResult(BaseModel):
    tasks_checked: int
    tasks_repaired: int
    errors: list[str]


@router.get("/files", response_model=StoredFileListResponse)
async def list_stored_files(
    admin: AuthUser = Depends(require_admin),
    search: str = Query(default="", description="搜索文件名"),
    orphan_only: bool = Query(default=False, description="仅显示无引用的孤立文件"),
) -> StoredFileListResponse:
    del admin
    ref_counts = (
        select(
            user_files.c.stored_file_id.label("stored_file_id"),
            func.count(user_files.c.id).label("ref_count"),
        )
        .group_by(user_files.c.stored_file_id)
        .subquery()
    )
    stmt = (
        select(
            stored_files, func.coalesce(ref_counts.c.ref_count, 0).label("ref_count")
        )
        .select_from(
            stored_files.outerjoin(
                ref_counts, stored_files.c.id == ref_counts.c.stored_file_id
            )
        )
        .order_by(stored_files.c.created_at_ms.desc())
    )
    if search:
        stmt = stmt.where(stored_files.c.original_name.contains(search))
    if orphan_only:
        stmt = stmt.where(func.coalesce(ref_counts.c.ref_count, 0) <= 0)

    async with transaction() as conn:
        rows = (await conn.execute(stmt)).mappings().all()

    files = [
        StoredFileInfo(
            id=int(row["id"]),
            content_hash=row["content_hash"],
            original_name=row["original_name"],
            size=int(row["size_bytes"]),
            is_directory=bool(row["is_directory"]),
            ref_count=int(row["ref_count"] or 0),
            created_at=ms_to_iso(row["created_at_ms"]) or "",
            real_path=row["real_path"],
            exists_on_disk=Path(row["real_path"]).exists(),
        )
        for row in rows
    ]
    return StoredFileListResponse(files=files, total=len(files))


@router.get("/files/{file_id}/users", response_model=FileUsersResponse)
async def get_file_users(
    file_id: int,
    admin: AuthUser = Depends(require_admin),
) -> FileUsersResponse:
    del admin
    async with transaction() as conn:
        exists = (
            await conn.execute(
                select(stored_files.c.id).where(stored_files.c.id == file_id)
            )
        ).first()
        if not exists:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"存储文件不存在: {file_id}",
            )
        rows = (
            (
                await conn.execute(
                    select(
                        user_files.c.user_id,
                        users.c.username,
                        user_files.c.display_name,
                    )
                    .select_from(
                        user_files.join(users, user_files.c.user_id == users.c.id)
                    )
                    .where(user_files.c.stored_file_id == file_id)
                )
            )
            .mappings()
            .all()
        )

    return FileUsersResponse(
        file_id=file_id,
        users=[
            FileUserInfo(
                user_id=int(row["user_id"]),
                username=row["username"],
                display_name=row["display_name"],
            )
            for row in rows
        ],
    )


@router.delete("/files", response_model=BulkDeleteResponse)
async def bulk_delete_files(
    payload: BulkDeleteRequest,
    http_request: Request,
    admin: AuthUser = Depends(require_admin),
) -> BulkDeleteResponse:
    del admin
    deleted_count = 0
    failed_ids: list[int] = []
    errors: list[str] = []

    for file_id in payload.file_ids:
        try:
            affected_download_ids: list[int] = []
            async with transaction() as conn:
                stored_file = (
                    (
                        await conn.execute(
                            select(stored_files).where(stored_files.c.id == file_id)
                        )
                    )
                    .mappings()
                    .first()
                )
                if not stored_file:
                    failed_ids.append(file_id)
                    errors.append(f"文件不存在: {file_id}")
                    continue
                ref_count = (
                    await conn.execute(
                        select(func.count())
                        .select_from(user_files)
                        .where(user_files.c.stored_file_id == file_id)
                    )
                ).scalar_one()
                if int(ref_count or 0) > 0:
                    failed_ids.append(file_id)
                    errors.append(f"文件 {file_id} 仍有 {ref_count} 个引用，无法删除")
                    continue
                timestamp = now_ms()
                affected_downloads = (
                    await conn.execute(
                        update(global_downloads)
                        .where(global_downloads.c.completed_file_id == file_id)
                        .values(
                            status="cancelled",
                            aria2_gid=None,
                            completed_file_id=None,
                            completed_bytes=0,
                            completed_at_ms=None,
                            error_code="stored_file_deleted",
                            error_message="Stored file was deleted",
                            updated_at_ms=timestamp,
                        )
                        .returning(global_downloads.c.id)
                    )
                ).all()
                affected_download_ids = [int(item[0]) for item in affected_downloads]
                if affected_download_ids:
                    await conn.execute(
                        update(user_tasks)
                        .where(
                            user_tasks.c.global_download_id.in_(affected_download_ids),
                            user_tasks.c.status == "completed",
                        )
                        .values(
                            status="cancelled",
                            error_message="Stored file was deleted",
                            updated_at_ms=timestamp,
                            finished_at_ms=timestamp,
                        )
                    )
                await conn.execute(
                    update(pack_tasks)
                    .where(pack_tasks.c.output_stored_file_id == file_id)
                    .values(output_stored_file_id=None, updated_at_ms=timestamp)
                )
                await conn.execute(
                    delete(stored_file_entries).where(
                        stored_file_entries.c.stored_file_id == file_id
                    )
                )
                await conn.execute(
                    delete(stored_files).where(stored_files.c.id == file_id)
                )
                real_path = stored_file["real_path"]

            path = Path(real_path)
            if path.exists():
                try:
                    await asyncio.to_thread(
                        safe_delete_path,
                        base_dir=get_store_dir(),
                        target=path,
                        recursive=path.is_dir(),
                        allow_missing=True,
                    )
                except Exception:
                    logger.warning(
                        "Failed to delete unreferenced stored path=%s",
                        path,
                        exc_info=True,
                    )
            if affected_download_ids:
                from app.services.task_broadcast import broadcast_task_update_to_subscribers

                state = http_request.app.state.app_state
                for download_id in affected_download_ids:
                    await broadcast_task_update_to_subscribers(state, download_id)
            deleted_count += 1
            logger.info("管理员删除存储文件: %s", stored_file["content_hash"])
        except Exception as exc:
            failed_ids.append(file_id)
            errors.append(f"删除失败 {file_id}: {exc!s}")
            logger.exception("删除存储文件失败: %s", file_id)

    return BulkDeleteResponse(
        deleted_count=deleted_count,
        failed_ids=failed_ids,
        errors=errors,
    )


@router.post("/scan", response_model=ScanResult)
async def scan_store(admin: AuthUser = Depends(require_admin)) -> ScanResult:
    del admin
    return ScanResult(scanned_dirs=0, new_records=0, already_exists=0, errors=[])


@router.post("/repair", response_model=RepairResult)
async def repair_storage(admin: AuthUser = Depends(require_admin)) -> RepairResult:
    del admin
    return RepairResult(tasks_checked=0, tasks_repaired=0, errors=[])
