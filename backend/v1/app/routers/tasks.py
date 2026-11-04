"""任务管理接口模块（共享下载架构）"""

from __future__ import annotations

import logging
import time
from typing import Any, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from app.auth import AuthUser, require_limited_api_user
from app.core.request_rate_guard import RateLimitScope, ensure_authenticated_allowed
from app.domain.errors import DomainError
from app.domain.task_policy import legacy_rest_status
from app.http.errors import raise_http
from app.services import task_service
from app.services.task_batch_submission import (
    BatchAllowanceDeniedError,
    BatchSubmissionUndeterminedError,
)
from app.services.task_batch_submission import BatchTaskItem as _BatchTaskItem

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/tasks", tags=["tasks"])
v2_router = APIRouter(prefix="/api/v2", tags=["tasks"])


class BatchTaskCreateItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    uri: str
    options: dict[str, Any] = Field(default_factory=dict)


class BatchTaskCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tasks: list[BatchTaskCreateItem]


PublicTaskStatus = Literal[
    "queued", "active", "waiting", "paused", "complete", "error"
]


class BatchTaskCreateResultItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_index: int
    accepted: bool
    task_id: int | None
    status: PublicTaskStatus | None
    error: str | None


class BatchTaskCreateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accepted_count: int
    failed_count: int
    results: list[BatchTaskCreateResultItem]


class BatchCancelTasksRequest(BaseModel):
    task_ids: list[int]


class TorrentCreate(BaseModel):
    torrent: str
    selected_file_indexes: list[object] | None = None
    options: dict | None = None


class TorrentPreviewCreate(BaseModel):
    torrent: str


BATCH_TASK_LIMIT = 30


_HTTP_ERROR_SCHEMA = {
    "type": "object",
    "required": ["detail"],
    "properties": {"detail": {"type": "string"}},
    "additionalProperties": False,
}

_VALIDATION_ERROR_SCHEMA = {
    "type": "object",
    "required": ["detail"],
    "additionalProperties": False,
    "properties": {
        "detail": {
            "oneOf": [
                {"type": "string"},
                {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["loc", "msg", "type"],
                        "properties": {
                            "loc": {"type": "array", "items": {}},
                            "msg": {"type": "string"},
                            "type": {"type": "string"},
                        },
                        "additionalProperties": False,
                    },
                },
            ]
        }
    },
}


@router.post(
    "",
    operation_id="createTasks",
    openapi_extra={"security": [{"sessionCookie": []}, {"apiToken": []}]},
    response_model=BatchTaskCreateResponse,
    responses={
        "401": {
            "description": "未认证",
            "content": {"application/json": {"schema": _HTTP_ERROR_SCHEMA}},
        },
        "422": {
            "description": "请求结构错误、任务数组为空或去重后超过30条",
            "content": {
                "application/json": {"schema": _VALIDATION_ERROR_SCHEMA}
            },
        },
        "429": {
            "description": "authenticated_api请求级限流；逐条create_task限流在200结果中表达",
            "headers": {
                "Retry-After": {
                    "required": True,
                    "schema": {"type": "string"},
                }
            },
            "content": {"application/json": {"schema": _HTTP_ERROR_SCHEMA}},
        },
        "502": {
            "description": "aria2批量提交和再次核对均无法确定逐条结果",
            "content": {"application/json": {"schema": _HTTP_ERROR_SCHEMA}},
        },
    },
)
async def create_tasks(
    payload: BatchTaskCreateRequest,
    user: AuthUser = Depends(require_limited_api_user),
) -> BatchTaskCreateResponse:
    if not payload.tasks:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="任务列表不能为空",
        )
    if len({item.uri.strip() for item in payload.tasks}) > BATCH_TASK_LIMIT:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"一次最多创建 {BATCH_TASK_LIMIT} 个任务",
        )

    async def allow_create_task() -> None:
        try:
            await ensure_authenticated_allowed(
                user.id,
                RateLimitScope.CREATE_TASK,
                detail="操作过于频繁，请稍后再试",
            )
        except HTTPException as exc:
            if exc.status_code == 429:
                raise BatchAllowanceDeniedError() from exc
            raise

    started = time.monotonic()
    try:
        result = await task_service.create_tasks_batch(
            user_id=user.id,
            quota_bytes=int(user.quota_bytes),
            items=[
                _BatchTaskItem(uri=item.uri, options=item.options)
                for item in payload.tasks
            ],
            allow_create_task=allow_create_task,
        )
    except BatchSubmissionUndeterminedError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc) or "aria2 批量提交结果暂无法确认",
        ) from exc
    logger.info(
        "批量创建任务完成 user_id=%s requested=%s accepted=%s failed=%s duration_ms=%s",
        user.id,
        len(payload.tasks),
        result.accepted_count,
        result.failed_count,
        int((time.monotonic() - started) * 1000),
    )
    results = [
        BatchTaskCreateResultItem(
            input_index=item.input_index,
            accepted=item.accepted,
            task_id=item.task_id,
            status=cast(
                "PublicTaskStatus | None",
                legacy_rest_status(item.status) if item.status else None,
            ),
            error=item.error_message if not item.accepted else None,
        )
        for item in result.results
    ]
    return BatchTaskCreateResponse(
        accepted_count=result.accepted_count,
        failed_count=result.failed_count,
        results=results,
    )



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
