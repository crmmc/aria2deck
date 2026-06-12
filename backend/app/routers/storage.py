"""存储文件管理路由（管理员专用）"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from app.auth import AuthUser, require_admin
from app.domain.errors import DomainError
from app.http.errors import raise_http
from app.services import storage_admin_service

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
    return StoredFileListResponse(
        **await storage_admin_service.list_stored_files(search, orphan_only)
    )


@router.get("/files/{file_id}/users", response_model=FileUsersResponse)
async def get_file_users(
    file_id: int,
    admin: AuthUser = Depends(require_admin),
) -> FileUsersResponse:
    del admin
    try:
        result = await storage_admin_service.get_file_users(file_id)
    except DomainError as exc:
        raise_http(exc)
    return FileUsersResponse(**result)


@router.delete("/files", response_model=BulkDeleteResponse)
async def bulk_delete_files(
    payload: BulkDeleteRequest,
    admin: AuthUser = Depends(require_admin),
) -> BulkDeleteResponse:
    del admin
    return BulkDeleteResponse(
        **await storage_admin_service.bulk_delete_files(payload.file_ids)
    )


@router.post("/scan", response_model=ScanResult)
async def scan_store(admin: AuthUser = Depends(require_admin)) -> ScanResult:
    del admin
    return ScanResult(scanned_dirs=0, new_records=0, already_exists=0, errors=[])


@router.post("/repair", response_model=RepairResult)
async def repair_storage(admin: AuthUser = Depends(require_admin)) -> RepairResult:
    del admin
    return RepairResult(tasks_checked=0, tasks_repaired=0, errors=[])
