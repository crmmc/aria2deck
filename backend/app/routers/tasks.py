"""任务管理接口模块（共享下载架构）"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from app.auth import AuthUser, require_limited_api_user
from app.core.request_rate_guard import RateLimitScope, ensure_authenticated_allowed
from app.domain.errors import DomainError
from app.http.errors import raise_http
from app.services import task_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/tasks", tags=["tasks"])
v2_router = APIRouter(prefix="/api/v2", tags=["tasks"])


class TaskCreate(BaseModel):
    uri: str
    options: dict | None = None


class BatchCancelTasksRequest(BaseModel):
    task_ids: list[int]


class TorrentCreate(BaseModel):
    torrent: str
    selected_file_indexes: list[object] | None = None
    options: dict | None = None


class TorrentPreviewCreate(BaseModel):
    torrent: str


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_task(
    payload: TaskCreate,
    user: AuthUser = Depends(require_limited_api_user),
) -> dict:
    await ensure_authenticated_allowed(
        user.id,
        RateLimitScope.CREATE_TASK,
        detail="操作过于频繁，请稍后再试",
    )
    try:
        return await task_service.create_task(
            user_id=user.id,
            quota_bytes=user.quota,
            uri=payload.uri,
            options=payload.options,
        )
    except DomainError as exc:
        raise_http(exc)


@router.post("/torrent/preview")
async def preview_torrent_task(
    payload: TorrentPreviewCreate,
    user: AuthUser = Depends(require_limited_api_user),
) -> dict:
    await ensure_authenticated_allowed(
        user.id,
        RateLimitScope.CREATE_TORRENT,
        detail="操作过于频繁，请稍后再试",
    )
    try:
        return await task_service.preview_torrent_task(
            user_id=user.id,
            torrent=payload.torrent,
        )
    except DomainError as exc:
        raise_http(exc)


@router.post("/torrent", status_code=status.HTTP_201_CREATED)
async def create_torrent_task(
    payload: TorrentCreate,
    user: AuthUser = Depends(require_limited_api_user),
) -> dict:
    await ensure_authenticated_allowed(
        user.id,
        RateLimitScope.CREATE_TORRENT,
        detail="操作过于频繁，请稍后再试",
    )
    try:
        return await task_service.create_torrent_task(
            user_id=user.id,
            quota_bytes=int(user.quota_bytes),
            torrent=payload.torrent,
            selected_file_indexes=payload.selected_file_indexes,
            options=payload.options,
        )
    except DomainError as exc:
        raise_http(exc)


@router.get("")
async def list_tasks(
    status_filter: str | None = None,
    user: AuthUser = Depends(require_limited_api_user),
) -> list[dict]:
    try:
        return await task_service.list_tasks(
            user_id=user.id,
            status_filter=status_filter,
        )
    except DomainError as exc:
        raise_http(exc)


@v2_router.get("/tasks")
async def list_tasks_v2(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    status_filter: str | None = None,
    user: AuthUser = Depends(require_limited_api_user),
) -> dict:
    try:
        return await task_service.list_tasks_page(
            user_id=user.id,
            status_filter=status_filter,
            page=page,
            page_size=page_size,
        )
    except DomainError as exc:
        raise_http(exc)


@router.post("/{user_task_id}/retry", status_code=status.HTTP_201_CREATED)
async def retry_task(
    user_task_id: int,
    user: AuthUser = Depends(require_limited_api_user),
) -> dict:
    await ensure_authenticated_allowed(
        user.id,
        RateLimitScope.CREATE_TASK,
        detail="操作过于频繁，请稍后再试",
    )
    try:
        from app.services import task_retry

        return await task_retry.retry_task(
            user_id=user.id,
            user_task_id=user_task_id,
            quota_bytes=int(user.quota_bytes),
        )
    except DomainError as exc:
        raise_http(exc)


@router.post("/cancel")
async def cancel_tasks(
    payload: BatchCancelTasksRequest,
    user: AuthUser = Depends(require_limited_api_user),
) -> dict:
    if not payload.task_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="至少选择一个条目",
        )
    if len(payload.task_ids) > 1000:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="一次最多操作 1000 个条目",
        )
    result = await task_service.bulk_cancel_tasks(
        user_id=user.id,
        task_ids=payload.task_ids,
        quota_bytes=int(user.quota_bytes),
    )
    logger.info(
        "批量取消任务已受理 user_id=%s requested=%s accepted=%s failed=%s",
        user.id,
        len(payload.task_ids),
        result["accepted_count"],
        result["failed_count"],
    )
    return result


@router.delete("")
async def clear_history(user: AuthUser = Depends(require_limited_api_user)) -> dict:
    return await task_service.clear_history(user.id)
