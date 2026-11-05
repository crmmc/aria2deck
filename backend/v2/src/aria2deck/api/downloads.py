from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Header, Path, Query
from fastapi.responses import StreamingResponse

from ..application.accounts.models import Principal
from ..application.downloads.broadcast import task_message
from ..application.downloads.commands import DownloadCommandService
from ..application.downloads.gateway import DownloadGatewayService
from ..application.downloads.history import HistoryService
from ..application.downloads.models import (
    DownloadWriteResult,
    TaskProjection,
    TaskStats,
    UriDownloadInput,
)
from ..application.downloads.queries import DownloadQueryService
from ..domain.downloads import SubscriptionStatus
from ..domain.quota import available_user_bytes
from .contracts import (
    ErrorBody,
    HistoryPurgeRequest,
    HistoryPurgeResponse,
    TaskActionRequest,
    TaskBatchCreateRequest,
    TaskBatchResponse,
    TaskListResponse,
    TaskQuotaResponse,
    TaskResponse,
    TaskStatsResponse,
    TaskStatusCountsResponse,
    TaskWriteResultResponse,
    TorrentCreateRequest,
    TorrentFilePreviewResponse,
    TorrentPreviewRequest,
    TorrentPreviewResponse,
)
from .dependencies import (
    download_commands,
    download_gateway,
    download_history,
    download_queries,
    required_principal,
    required_session_principal,
)
from .errors import error_responses

router = APIRouter(prefix="/api/v2", tags=["downloads"])


def _task(task: TaskProjection) -> TaskResponse:
    """REST 与 WS 共用唯一序列化点：broadcast.task_message 是 TaskResponse 的唯一来源。"""
    return TaskResponse.model_validate(task_message(task).payload)


def _stats(stats: TaskStats) -> TaskStatsResponse:
    return TaskStatsResponse(
        counts=TaskStatusCountsResponse(
            **{status.value: count for status, count in stats.counts.items()}
        ),
        total=stats.total,
        download_speed=stats.download_speed,
        upload_speed=stats.upload_speed,
        quota=TaskQuotaResponse(
            quota_bytes=stats.quota.quota_bytes,
            used_bytes=stats.quota.used_bytes,
            reserved_bytes=stats.quota.reserved_bytes,
            available_bytes=available_user_bytes(stats.quota),
        ),
    )


@router.get(
    "/tasks/stats",
    response_model=TaskStatsResponse,
    responses=error_responses(401, 429, 500),
)
async def task_stats(
    principal: Principal = Depends(required_principal),
    service: DownloadQueryService = Depends(download_queries),
) -> TaskStatsResponse:
    return _stats(await service.stats(user_id=principal.user.id))


@router.get(
    "/tasks",
    response_model=TaskListResponse,
    responses=error_responses(401, 422, 429, 500),
)
async def list_tasks(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    status: SubscriptionStatus | None = None,
    q: str = Query(default="", max_length=200),
    sort: Literal["created_at", "name", "size"] = "created_at",
    order: Literal["asc", "desc"] = "desc",
    principal: Principal = Depends(required_principal),
    service: DownloadQueryService = Depends(download_queries),
) -> TaskListResponse:
    result = await service.list_tasks(
        user_id=principal.user.id,
        status=status,
        search=q,
        page=page,
        page_size=page_size,
        sort=sort,
        order=order,
    )
    return TaskListResponse(
        items=[_task(task) for task in result.items],
        total=result.total,
        page=result.page,
        page_size=result.page_size,
    )


@router.get(
    "/tasks/{task_id}",
    response_model=TaskResponse,
    responses=error_responses(401, 404, 422, 429, 500),
)
async def get_task(
    task_id: int = Path(ge=1),
    principal: Principal = Depends(required_principal),
    service: DownloadQueryService = Depends(download_queries),
) -> TaskResponse:
    return _task(await service.get_task(user_id=principal.user.id, task_id=task_id))


async def _write_result(
    result: DownloadWriteResult,
    queries: DownloadQueryService,
    *,
    user_id: int,
    include_task: bool = True,
) -> TaskWriteResultResponse:
    task = None
    if include_task and result.task_id is not None and (
        result.accepted or result.outcome is not None
    ):
        task = _task(await queries.get_task(user_id=user_id, task_id=result.task_id))
    return TaskWriteResultResponse(
        accepted=result.accepted,
        task=task,
        outcome=result.outcome.value if result.outcome is not None else None,
        error=(
            ErrorBody(code=result.error_code, message=result.message)
            if result.error_code is not None and result.message is not None
            else None
        ),
        stop_confirmed=result.stop_confirmed,
    )


async def _batch_response(
    results: list[DownloadWriteResult],
    queries: DownloadQueryService,
    *,
    user_id: int,
    include_task: bool = True,
) -> TaskBatchResponse:
    items = [
        await _write_result(result, queries, user_id=user_id, include_task=include_task)
        for result in results
    ]
    accepted = sum(item.accepted for item in items)
    return TaskBatchResponse(
        items=items, accepted_count=accepted, rejected_count=len(items) - accepted
    )


@router.post(
    "/tasks",
    response_model=TaskBatchResponse,
    responses=error_responses(401, 422, 429, 500, 502),
)
async def create_tasks(
    payload: TaskBatchCreateRequest,
    principal: Principal = Depends(required_principal),
    service: DownloadCommandService = Depends(download_commands),
    queries: DownloadQueryService = Depends(download_queries),
) -> TaskBatchResponse:
    results = await service.create_uris(
        user_id=principal.user.id,
        items=[
            UriDownloadInput(uri=item.uri, options=item.options.model_dump())
            for item in payload.tasks
        ],
    )
    return await _batch_response(results, queries, user_id=principal.user.id)


@router.post(
    "/tasks/torrent/preview",
    response_model=TorrentPreviewResponse,
    responses=error_responses(401, 422, 429, 500),
)
async def preview_torrent(
    payload: TorrentPreviewRequest,
    principal: Principal = Depends(required_principal),
    service: DownloadCommandService = Depends(download_commands),
) -> TorrentPreviewResponse:
    metadata = service.preview_torrent(
        user_id=principal.user.id, payload_base64=payload.torrent
    )
    return TorrentPreviewResponse(
        info_hash=metadata.info_hash,
        name=metadata.name,
        files=[
            TorrentFilePreviewResponse(index=file.index, path=file.path, size=file.size)
            for file in metadata.files
        ],
        total_size=metadata.total_size,
        default_selection=[file.index for file in metadata.files],
    )


@router.post(
    "/tasks/torrent",
    response_model=TaskWriteResultResponse,
    responses=error_responses(401, 409, 422, 429, 500, 502),
)
async def create_torrent(
    payload: TorrentCreateRequest,
    principal: Principal = Depends(required_principal),
    service: DownloadCommandService = Depends(download_commands),
    queries: DownloadQueryService = Depends(download_queries),
) -> TaskWriteResultResponse:
    result = await service.create_torrent(
        user_id=principal.user.id,
        payload_base64=payload.torrent,
        selected_file_indexes=payload.selected_file_indexes,
    )
    return await _write_result(result, queries, user_id=principal.user.id)


@router.post(
    "/tasks/cancel",
    response_model=TaskBatchResponse,
    responses=error_responses(401, 422, 429, 500),
)
async def cancel_tasks(
    payload: TaskActionRequest,
    principal: Principal = Depends(required_principal),
    service: DownloadCommandService = Depends(download_commands),
    queries: DownloadQueryService = Depends(download_queries),
) -> TaskBatchResponse:
    results = await service.cancel(user_id=principal.user.id, task_ids=payload.task_ids)
    return await _batch_response(results, queries, user_id=principal.user.id)


@router.post(
    "/tasks/{task_id}/retry",
    response_model=TaskWriteResultResponse,
    responses=error_responses(401, 404, 409, 422, 429, 500, 502),
)
async def retry_task(
    task_id: int = Path(ge=1),
    principal: Principal = Depends(required_principal),
    service: DownloadCommandService = Depends(download_commands),
    queries: DownloadQueryService = Depends(download_queries),
) -> TaskWriteResultResponse:
    result = await service.retry(user_id=principal.user.id, task_id=task_id)
    return await _write_result(result, queries, user_id=principal.user.id)


@router.get(
    "/history",
    response_model=TaskListResponse,
    responses=error_responses(401, 422, 429, 500),
)
async def list_history(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    status: SubscriptionStatus | None = None,
    q: str = Query(default="", max_length=200),
    principal: Principal = Depends(required_principal),
    service: HistoryService = Depends(download_history),
) -> TaskListResponse:
    result = await service.list_history(
        user_id=principal.user.id,
        status=status,
        search=q,
        page=page,
        page_size=page_size,
    )
    return TaskListResponse(
        items=[_task(task) for task in result.items],
        total=result.total,
        page=result.page,
        page_size=result.page_size,
    )


@router.delete(
    "/history",
    response_model=TaskBatchResponse,
    responses=error_responses(401, 422, 429, 500),
)
async def delete_history(
    payload: TaskActionRequest,
    principal: Principal = Depends(required_principal),
    service: HistoryService = Depends(download_history),
    queries: DownloadQueryService = Depends(download_queries),
) -> TaskBatchResponse:
    results = await service.delete(user_id=principal.user.id, task_ids=payload.task_ids)
    return await _batch_response(results, queries, user_id=principal.user.id, include_task=False)


@router.post(
    "/history/purge",
    response_model=HistoryPurgeResponse,
    responses=error_responses(401, 403, 422, 429, 500),
)
async def purge_history(
    payload: HistoryPurgeRequest,
    principal: Principal = Depends(required_session_principal),
    service: HistoryService = Depends(download_history),
) -> HistoryPurgeResponse:
    result = await service.purge(
        principal,
        before_ms=payload.before_ms,
        older_than_days=payload.older_than_days,
    )
    return HistoryPurgeResponse(
        expired_tasks=result.expired_tasks, detached_sources=result.detached_sources
    )


@router.get("/internal/fetch/{attempt_id}/{source_index}", include_in_schema=False)
async def internal_fetch(
    attempt_id: int = Path(ge=1),
    source_index: int = Path(ge=0),
    capability: str | None = Header(default=None, alias="X-Mjiz-Fetch-Capability"),
    range_header: str | None = Header(default=None, alias="Range"),
    service: DownloadGatewayService = Depends(download_gateway),
) -> StreamingResponse:
    result = await service.open(
        attempt_id=attempt_id,
        source_index=source_index,
        capability=capability,
        range_header=range_header,
    )
    return StreamingResponse(
        result.body, status_code=result.status, headers=dict(result.headers)
    )
