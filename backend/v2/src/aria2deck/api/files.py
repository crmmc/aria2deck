from __future__ import annotations

from functools import partial
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, Header, Path, Query

from ..application.accounts.models import Principal
from ..application.downloads.queries import DownloadQueryService
from ..application.library.commands import LibraryCommandService
from ..application.library.download import LibraryDownloadService
from ..application.library.models import UserFileView
from ..application.library.queries import LibraryQueryService
from ..application.settings.download_limit import DownloadConnectionLimiter
from ..application.settings.rate_limit import RateLimiter
from ..domain.accounts import require_session
from ..domain.content import ContentKind
from ..domain.quota import VisibleSpace
from .contracts import (
    ErrorBody,
    FileBatchDeleteRequest,
    FileBatchDeleteResponse,
    FileBatchDeleteResultResponse,
    FileEntryItemResponse,
    FileEntryListResponse,
    FileItemResponse,
    FileListResponse,
    FileRenameRequest,
    FileSearchHitResponse,
    FileSearchResponse,
    StorageDeleteRequest,
    StorageDeleteResponse,
    StorageDeleteResultResponse,
    StorageFileListResponse,
    StorageFileResponse,
    StorageReferencingUserListResponse,
    StorageReferencingUserResponse,
    TaskQuotaResponse,
)
from .dependencies import (
    download_connections,
    download_queries,
    library_commands,
    library_download,
    library_queries,
    rate_limiter,
    required_admin_principal,
    required_principal,
    required_session_principal,
)
from .errors import error_responses
from .streaming import LeaseStreamingResponse

router = APIRouter(prefix="/api/v2", tags=["files"])


def _kind(kind: ContentKind) -> Literal["file", "directory"]:
    return "directory" if kind is ContentKind.DIRECTORY else "file"


def _file(item: UserFileView) -> FileItemResponse:
    return FileItemResponse(
        id=item.id,
        display_name=item.display_name,
        kind=_kind(item.kind),
        size=item.size,
        created_at_ms=item.created_at_ms,
    )


def _space(visible: VisibleSpace) -> TaskQuotaResponse:
    # 与 /tasks/stats 的 disk 块同一 user_visible_space 结果（S1：唯一口径）。
    return TaskQuotaResponse(
        quota_bytes=visible.total_bytes,
        used_bytes=visible.used_bytes,
        reserved_bytes=visible.reserved_bytes,
        available_bytes=visible.available_bytes,
        limited=visible.limited,
    )


def _content_disposition(name: str) -> str:
    fallback = name if name.isascii() and '"' not in name and "\\" not in name else "download"
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(name, safe='')}"


@router.get(
    "/files",
    response_model=FileListResponse | FileSearchResponse,
    responses=error_responses(400, 401, 404, 422, 429, 500),
)
async def list_files(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    search: str | None = Query(default=None, max_length=200),
    scope: int | None = Query(default=None, ge=1),
    principal: Principal = Depends(required_principal),
    service: LibraryQueryService = Depends(library_queries),
    queries: DownloadQueryService = Depends(download_queries),
    limiter: RateLimiter = Depends(rate_limiter),
) -> FileListResponse | FileSearchResponse:
    if search is not None:
        limiter.check_file_search(user_id=principal.user.id)
        hits, truncated = await service.search(
            user_id=principal.user.id, term=search, scope_file_id=scope
        )
        return FileSearchResponse(
            hits=[
                FileSearchHitResponse(
                    user_file_id=hit.user_file_id,
                    display_name=hit.display_name,
                    path=hit.path,
                    kind=_kind(hit.kind),
                    size=hit.size,
                )
                for hit in hits
            ],
            truncated=truncated,
        )
    items, total, _quota = await service.list_root_files(
        user_id=principal.user.id, page=page, page_size=page_size
    )
    return FileListResponse(
        items=[_file(item) for item in items],
        total=total,
        page=page,
        page_size=page_size,
        space=_space(await queries.user_visible_space(user_id=principal.user.id)),
    )


@router.get(
    "/files/{file_id}/entries",
    response_model=FileEntryListResponse,
    responses=error_responses(400, 401, 404, 422, 429, 500),
)
async def list_file_entries(
    file_id: int = Path(ge=1),
    path: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    principal: Principal = Depends(required_principal),
    service: LibraryQueryService = Depends(library_queries),
) -> FileEntryListResponse:
    entries, breadcrumbs, total = await service.browse_directory(
        user_id=principal.user.id, file_id=file_id, path=path, page=page, page_size=page_size
    )
    return FileEntryListResponse(
        items=[
            FileEntryItemResponse(
                name=entry.name,
                path=entry.path,
                kind=_kind(entry.kind),
                size=entry.size,
            )
            for entry in entries
        ],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/files/{file_id}/download",
    responses=error_responses(400, 401, 404, 409, 416, 422, 429, 500),
)
async def download_file(
    file_id: int = Path(ge=1),
    path: str | None = Query(default=None),
    range_header: str | None = Header(default=None, alias="Range"),
    principal: Principal = Depends(required_session_principal),
    service: LibraryDownloadService = Depends(library_download),
    connections: DownloadConnectionLimiter = Depends(download_connections),
) -> LeaseStreamingResponse:
    require_session(principal.kind)  # API Token 不能下载文件，仅浏览器会话可用。
    lease = await connections.acquire_authenticated(
        user_id=principal.user.id, file_key=str(file_id)
    )
    try:
        target = await service.open_download(
            user_id=principal.user.id,
            file_id=file_id,
            path=path,
            range_header=range_header,
        )
    except BaseException:
        await lease.release()
        raise
    headers = {
        "Content-Disposition": _content_disposition(target.display_name),
        "Accept-Ranges": "bytes",
    }
    if target.range_spec is not None:
        headers["Content-Length"] = str(target.range_spec.length)
        headers["Content-Range"] = (
            f"bytes {target.range_spec.start}-{target.range_spec.end}/{target.total_size}"
        )
        status_code = 206
    else:
        headers["Content-Length"] = str(target.total_size)
        status_code = 200
    # 连接租约与内容读租约都挂进 releases=：ASGI finally 覆盖正常结束、
    # 中途异常与客户端断开；Starlette 的 BackgroundTask 在断开时不会执行，
    # 曾导致读租约只增不减、删除后的内容永久 pending_delete（实测回归）。
    return LeaseStreamingResponse(
        service.iter_chunks(target),
        releases=(lease.release, partial(service.release, target)),
        status_code=status_code,
        headers=headers,
    )


@router.patch(
    "/files/{file_id}",
    response_model=FileItemResponse,
    responses=error_responses(401, 404, 409, 422, 429, 500),
)
async def rename_file(
    file_id: int,
    payload: FileRenameRequest,
    principal: Principal = Depends(required_principal),
    service: LibraryCommandService = Depends(library_commands),
) -> FileItemResponse:
    view = await service.rename(
        user_id=principal.user.id, file_id=file_id, new_name=payload.name
    )
    return _file(view)


@router.delete(
    "/files",
    response_model=FileBatchDeleteResponse,
    responses=error_responses(401, 404, 409, 422, 429, 500),
)
async def delete_files(
    payload: FileBatchDeleteRequest,
    principal: Principal = Depends(required_principal),
    service: LibraryCommandService = Depends(library_commands),
) -> FileBatchDeleteResponse:
    """批量删除本人文件引用（A1）：逐项受理，单项失败不阻断其余。"""
    outcomes = await service.delete_user_files(
        user_id=principal.user.id, file_ids=payload.file_ids
    )
    results = [
        FileBatchDeleteResultResponse(
            file_id=outcome.content_id,
            accepted=outcome.accepted,
            error=(
                ErrorBody(code=outcome.error_code, message=outcome.message)
                if outcome.error_code is not None and outcome.message is not None
                else None
            ),
        )
        for outcome in outcomes
    ]
    accepted = sum(result.accepted for result in results)
    return FileBatchDeleteResponse(
        accepted_count=accepted,
        rejected_count=len(results) - accepted,
        results=results,
    )


@router.get(
    "/storage/files",
    response_model=StorageFileListResponse,
    responses=error_responses(401, 403, 422, 429, 500),
)
async def list_storage_files(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    search: str | None = Query(default=None, max_length=200),
    orphans_only: bool = Query(default=False),
    principal: Principal = Depends(required_admin_principal),
    service: LibraryQueryService = Depends(library_queries),
) -> StorageFileListResponse:
    items, total = await service.list_storage_files(
        page=page,
        page_size=page_size,
        search=search,
        orphans_only=orphans_only,
    )
    return StorageFileListResponse(
        items=[
            StorageFileResponse(
                content_id=item.content_id,
                content_key=item.content_key,
                kind=_kind(item.kind),
                size=item.size,
                on_disk=item.on_disk,
                reference_count=item.reference_count,
                deletion_state=item.deletion_state.value,
                created_at_ms=item.created_at_ms,
                file_names=list(item.file_names),
            )
            for item in items
        ],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/storage/files/{content_id}/users",
    response_model=StorageReferencingUserListResponse,
    responses=error_responses(401, 403, 404, 422, 429, 500),
)
async def list_storage_referencing_users(
    content_id: int = Path(ge=1),
    principal: Principal = Depends(required_admin_principal),
    service: LibraryQueryService = Depends(library_queries),
) -> StorageReferencingUserListResponse:
    users = await service.list_referencing_users(content_id)
    return StorageReferencingUserListResponse(
        items=[
            StorageReferencingUserResponse(
                user_id=row.user_id,
                username=row.username,
                display_name=row.display_name,
                referenced_at_ms=row.referenced_at_ms,
            )
            for row in users
        ]
    )


@router.post(
    "/storage/files/delete",
    response_model=StorageDeleteResponse,
    status_code=202,
    responses=error_responses(401, 403, 422, 429, 500),
)
async def delete_storage_files(
    payload: StorageDeleteRequest,
    principal: Principal = Depends(required_admin_principal),
    service: LibraryCommandService = Depends(library_commands),
) -> StorageDeleteResponse:
    unique_ids = list(dict.fromkeys(payload.content_ids))
    outcomes = await service.storage_delete_orphans(unique_ids)
    results = [
        StorageDeleteResultResponse(
            content_id=outcome.content_id,
            accepted=outcome.accepted,
            error=(
                ErrorBody(code=outcome.error_code, message=outcome.message)
                if outcome.error_code is not None and outcome.message is not None
                else None
            ),
        )
        for outcome in outcomes
    ]
    accepted = sum(result.accepted for result in results)
    return StorageDeleteResponse(
        accepted_count=accepted,
        rejected_count=len(results) - accepted,
        results=results,
    )
