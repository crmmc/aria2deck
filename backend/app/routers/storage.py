"""存储文件管理路由（管理员专用）"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field

from app.auth import AuthUser, require_limited_admin
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
    cleanup_state: str
    cleanup_attempts: int
    cleanup_next_retry_at: str | None = None
    cleanup_error: str | None = None


class StoredFileListResponse(BaseModel):
    files: list[StoredFileInfo]
    total: int
    page: int
    page_size: int


class FileUserInfo(BaseModel):
    user_id: int
    username: str
    display_name: str


class FileUsersResponse(BaseModel):
    file_id: int
    users: list[FileUserInfo]


class BulkDeleteRequest(BaseModel):
    file_ids: list[int] = Field(..., min_length=1, max_length=1000)


class BulkDeleteItem(BaseModel):
    file_id: int
    ok: bool
    state: str
    accepted: bool
    error: str | None = None


class BulkDeleteResponse(BaseModel):
    deleted_count: int
    accepted_count: int
    failed_ids: list[int]
    errors: list[str]
    results: list[BulkDeleteItem]


@router.get("/files", response_model=StoredFileListResponse)
async def list_stored_files(
    admin: AuthUser = Depends(require_limited_admin),
    search: str = Query(default="", description="搜索文件名"),
    orphan_only: bool = Query(default=False, description="仅显示无引用的孤立文件"),
    page: int = Query(default=1, ge=1, description="页码，从 1 开始"),
    page_size: int = Query(default=20, ge=1, le=100, description="每页条数"),
) -> StoredFileListResponse:
    del admin
    return StoredFileListResponse(
        **await storage_admin_service.list_stored_files(
            search,
            orphan_only,
            page=page,
            page_size=page_size,
        )
    )


@router.get("/files/{file_id}/users", response_model=FileUsersResponse)
async def get_file_users(
    file_id: int,
    admin: AuthUser = Depends(require_limited_admin),
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
    response: Response,
    admin: AuthUser = Depends(require_limited_admin),
) -> BulkDeleteResponse:
    del admin
    result = await storage_admin_service.bulk_delete_files(payload.file_ids)
    if result["accepted_count"]:
        response.status_code = status.HTTP_202_ACCEPTED
    return BulkDeleteResponse(**result)


@router.post("/scan", status_code=status.HTTP_501_NOT_IMPLEMENTED)
async def scan_store(admin: AuthUser = Depends(require_limited_admin)) -> None:
    del admin
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="存储扫描功能暂未实现",
    )


@router.post("/repair", status_code=status.HTTP_501_NOT_IMPLEMENTED)
async def repair_storage(admin: AuthUser = Depends(require_limited_admin)) -> None:
    del admin
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="存储修复功能暂未实现",
    )
