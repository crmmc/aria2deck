"""用户文件管理接口模块（共享下载架构）"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.auth import (
    AuthUser,
    require_api_user,
    require_limited_api_user,
    require_session_user,
)
from app.core.download_limiter import download_limiter
from app.core.request_rate_guard import RateLimitScope, ensure_authenticated_allowed
from app.domain.errors import DomainError
from app.http.errors import raise_http
from app.http.file_response import (
    range_file_response,
    release_response_leases,
    tracked_response,
)
from app.modules import pack as pack_service
from app.services import file_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/files", tags=["files"])


class FileInfo(BaseModel):
    id: int
    content_hash: str
    name: str
    size: int
    is_directory: bool
    created_at: str


class FileListResponse(BaseModel):
    files: list[FileInfo]
    total: int
    space: dict


class FileSearchItem(BaseModel):
    user_file_id: int
    content_hash: str
    name: str
    size: int
    path: str
    is_directory: bool
    entry_path: str | None
    rank: int
    root_index: int


class FileSearchResponse(BaseModel):
    items: list[FileSearchItem]
    total: int
    truncated: bool


class RenameRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)


class PackRequest(BaseModel):
    file_ids: list[int] = Field(..., min_length=1, max_length=100)
    output_name: str | None = None
    delete_source: bool = False


class CalculateSizeRequest(BaseModel):
    file_ids: list[int] = Field(..., min_length=1, max_length=1000)


class BatchDeleteFilesRequest(BaseModel):
    file_hashes: list[str]


class FileBatchItem(BaseModel):
    content_hash: str
    ok: bool
    state: str
    accepted: bool
    error: str | None = None


class FilesBatchOperationResponse(BaseModel):
    accepted_count: int
    failed_count: int
    results: list[FileBatchItem]


def _require_user_id(user: AuthUser) -> int:
    if user.id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未登录")
    return int(user.id)


@router.get("", response_model=FileListResponse)
async def list_files(
    page: int = 1,
    page_size: int = 10,
    user: AuthUser = Depends(require_limited_api_user),
) -> FileListResponse:
    user_id = _require_user_id(user)
    result = await file_service.list_files(
        user_id,
        user.quota,
        page=page,
        page_size=page_size,
    )
    logger.debug(
        "查询文件列表 user_id=%s page=%s page_size=%s total=%s",
        user_id,
        page,
        page_size,
        result["total"],
    )
    return FileListResponse(**result)


@router.get("/search", response_model=FileSearchResponse)
async def search_files(
    q: str = "",
    scope_content_hash: str | None = None,
    scope_path: str = "",
    user: AuthUser = Depends(require_api_user),
) -> FileSearchResponse:
    user_id = _require_user_id(user)
    keyword = q.strip()
    if not keyword:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="请输入关键词"
        )
    if scope_content_hash and (scope_path.startswith("/") or ".." in scope_path):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="搜索范围路径不合法"
        )
    await ensure_authenticated_allowed(
        user_id,
        RateLimitScope.FILE_SEARCH,
        detail="操作过于频繁，请稍后再试",
    )
    result = await file_service.search_files(
        user_id,
        keyword,
        scope_content_hash=scope_content_hash,
        scope_path=scope_path,
    )
    logger.debug(
        "搜索文件 user_id=%s q=%s total=%s truncated=%s",
        user_id,
        keyword,
        result["total"],
        result["truncated"],
    )
    return FileSearchResponse(**result)


@router.get("/{file_hash}/browse")
async def browse_file(
    file_hash: str,
    path: str = "",
    user: AuthUser = Depends(require_limited_api_user),
) -> list[dict]:
    user_id = _require_user_id(user)
    try:
        files = await file_service.browse_file(user_id, file_hash, path)
    except DomainError as exc:
        logger.warning("浏览文件失败 user_id=%s file_hash=%s error=%s", user_id, file_hash, exc.detail)
        raise_http(exc)
    logger.debug("浏览文件成功 user_id=%s file_hash=%s count=%s", user_id, file_hash, len(files))
    return files


@router.get("/{file_hash}/download")
async def download_file(
    file_hash: str,
    request: Request,
    path: str = "",
    user: AuthUser = Depends(require_session_user),
):
    user_id = _require_user_id(user)
    acquire_result = await download_limiter.acquire_authenticated(user_id, file_hash)
    if not acquire_result.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=acquire_result.detail(),
        )

    lease = acquire_result.lease
    read_lease = None
    response_transferred = False
    try:
        target_path, download_name, read_lease = (
            await file_service.resolve_download_target_with_read_lease(
                user_id,
                file_hash,
                path,
            )
        )
        logger.info("下载文件成功 user_id=%s file_hash=%s file=%s", user_id, file_hash, download_name)
        response = tracked_response(
            range_file_response(request, target_path, download_name),
            lease,
            read_lease,
        )
        response_transferred = True
        return response
    except DomainError as exc:
        raise_http(exc)
    finally:
        if not response_transferred:
            await release_response_leases(lease, read_lease)


@router.delete("/pack")
async def clear_finished_pack_tasks(
    user: AuthUser = Depends(require_limited_api_user),
) -> dict:
    return await pack_service.clear_finished_pack_tasks(_require_user_id(user))


@router.delete("/pack/{task_id}")
async def cancel_or_delete_pack_task(
    task_id: int, user: AuthUser = Depends(require_limited_api_user)
) -> dict:
    try:
        return await pack_service.cancel_or_delete_pack_task(
            _require_user_id(user),
            task_id,
        )
    except DomainError as exc:
        raise_http(exc)
        raise AssertionError("unreachable")


@router.delete("", response_model=FilesBatchOperationResponse)
async def delete_files(
    payload: BatchDeleteFilesRequest,
    user: AuthUser = Depends(require_limited_api_user),
) -> FilesBatchOperationResponse:
    user_id = _require_user_id(user)
    if not payload.file_hashes:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="至少选择一个条目",
        )
    if len(payload.file_hashes) > 1000:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="一次最多操作 1000 个条目",
        )
    result = await file_service.bulk_delete_files_by_hashes(user_id, payload.file_hashes)
    logger.info(
        "批量删除文件已受理 user_id=%s requested=%s accepted=%s failed=%s",
        user_id,
        len(payload.file_hashes),
        result["accepted_count"],
        result["failed_count"],
    )
    return FilesBatchOperationResponse(**result)


@router.put("/{file_hash}/rename")
async def rename_file(
    file_hash: str,
    payload: RenameRequest,
    user: AuthUser = Depends(require_limited_api_user),
) -> dict:
    user_id = _require_user_id(user)
    try:
        await file_service.rename_file(user_id, file_hash, payload.name)
    except DomainError as exc:
        logger.warning("重命名文件失败 user_id=%s file_hash=%s error=%s", user_id, file_hash, exc.detail)
        raise_http(exc)
    logger.info("重命名文件成功 user_id=%s file_hash=%s", user_id, file_hash)
    return {"ok": True}


@router.get("/space")
async def get_space(user: AuthUser = Depends(require_limited_api_user)) -> dict:
    user_id = _require_user_id(user)
    space_info = await file_service.get_user_space_info(user_id, user.quota)
    logger.debug("查询空间信息 user_id=%s", user_id)
    return {
        "quota": space_info["quota"],
        "used": space_info["used"],
        "frozen": space_info["frozen"],
        "available": space_info["available"],
    }


@router.post("/pack/calculate-size")
async def calculate_paths_size(
    payload: CalculateSizeRequest, user: AuthUser = Depends(require_limited_api_user)
) -> dict:
    user_id = _require_user_id(user)
    try:
        total_size = await pack_service.calculate_user_files_size(user_id, payload.file_ids)
    except DomainError as exc:
        raise_http(exc)
    return {"total_size": total_size}


@router.get("/pack/available-space")
async def get_pack_available_space(user: AuthUser = Depends(require_limited_api_user)) -> dict:
    user_id = _require_user_id(user)
    info = await pack_service.get_pack_available_space_info(user_id)
    return info


@router.post("/pack", status_code=status.HTTP_201_CREATED)
async def create_pack_task(
    payload: PackRequest,
    user: AuthUser = Depends(require_limited_api_user),
) -> dict:
    user_id = _require_user_id(user)
    await ensure_authenticated_allowed(
        user_id,
        RateLimitScope.CREATE_PACK,
        detail="操作过于频繁，请稍后再试",
    )
    try:
        result = await pack_service.create_pack_task_from_user_files(
            user_id=user_id,
            quota_bytes=user.quota,
            file_ids=payload.file_ids,
            output_name=payload.output_name,
            delete_source=payload.delete_source,
        )
    except DomainError as exc:
        logger.warning("创建打包任务失败 user_id=%s error=%s", user_id, exc.detail)
        raise_http(exc)
    logger.info("创建打包任务成功 user_id=%s task_id=%s", user_id, result["id"])
    return result


@router.get("/pack")
async def list_pack_tasks(user: AuthUser = Depends(require_limited_api_user)) -> list[dict]:
    user_id = _require_user_id(user)
    tasks = await pack_service.list_pack_tasks(user_id)
    logger.debug("查询打包任务列表 user_id=%s count=%s", user_id, len(tasks))
    return tasks


@router.get("/pack/{task_id}")
async def get_pack_task(task_id: int, user: AuthUser = Depends(require_limited_api_user)) -> dict:
    user_id = _require_user_id(user)
    try:
        task = await pack_service.get_pack_task(user_id, task_id)
    except DomainError as exc:
        logger.warning("查询打包任务失败 user_id=%s task_id=%s error=%s", user_id, task_id, exc.detail)
        raise_http(exc)
    logger.debug("查询打包任务详情 user_id=%s task_id=%s", user_id, task_id)
    return task


@router.get("/quota")
async def get_quota(user: AuthUser = Depends(require_limited_api_user)) -> dict:
    user_id = _require_user_id(user)
    space_info = await file_service.get_user_space_info(user_id, user.quota)
    total = space_info["total"]
    percentage = (space_info["used"] / total * 100) if total > 0 else 0
    logger.debug("查询配额信息 user_id=%s", user_id)
    return {
        "used": space_info["used"],
        "total": total,
        "percentage": round(percentage, 2),
    }
