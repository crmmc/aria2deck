"""文件分享接口"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from pydantic import BaseModel

from app.auth import AuthUser, require_limited_api_user
from app.core.download_limiter import download_limiter
from app.core.request_rate_guard import (
    RateLimitScope,
    client_ip_from_request,
    ensure_authenticated_allowed,
    ensure_public_allowed,
    ensure_share_access_allowed,
)
from app.domain.errors import DomainError
from app.http.errors import raise_http
from app.http.file_response import (
    prepare_range_file_response,
    release_response_leases,
    tracked_response,
)
from app.schemas import (
    BrowseEntryOut,
    BrowsePageResponse,
    CreateShareRequest,
    ShareAccessRequest,
    ShareAccessResponse,
    ShareInfoOut,
    ShareLinkOut,
)
from app.services import file_service, share_service
from app.services.storage_locks import (
    acquire_content_read_lease_locked,
    get_content_hash_lock,
)

router = APIRouter(tags=["shares"])
logger = logging.getLogger(__name__)


class BatchDeleteSharesRequest(BaseModel):
    share_ids: list[int]


class ShareBatchItem(BaseModel):
    share_id: int
    ok: bool
    state: str
    accepted: bool
    error: str | None = None


class SharesBatchOperationResponse(BaseModel):
    accepted_count: int
    failed_count: int
    results: list[ShareBatchItem]


def _bearer_token(request: Request) -> str | None:
    authorization = request.headers.get("authorization")
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


async def _serve_shared_download(
    code: str,
    request: Request,
    *,
    token: str | None,
    subpath: str | None,
):
    client_ip = client_ip_from_request(request)
    try:
        share = await share_service.check_share_access(code, token)
    except DomainError as exc:
        raise_http(exc)
    acquire_result = await download_limiter.acquire_anonymous(
        client_ip, share["content_hash"]
    )
    if not acquire_result.allowed:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, acquire_result.detail())

    lease = acquire_result.lease
    read_lease = None
    response_transferred = False
    try:
        content_lock = await get_content_hash_lock(str(share["content_hash"]))
        async with content_lock:
            share = await share_service.check_share_access(code, token)
            target, filename = await share_service.resolve_shared_download_target(
                share,
                subpath=subpath,
            )
            response, covers_full_entity = prepare_range_file_response(
                request, target, filename
            )
            read_lease = acquire_content_read_lease_locked(str(share["content_hash"]))
            await share_service.record_shared_download(
                share,
                should_count_download=covers_full_entity,
            )
        response = tracked_response(response, lease, read_lease)
        response_transferred = True
        return response
    except DomainError as exc:
        raise_http(exc)
    finally:
        if not response_transferred:
            await release_response_leases(lease, read_lease)

@router.post("/api/shares", status_code=status.HTTP_201_CREATED)
async def create_share(
    req: CreateShareRequest,
    user: AuthUser = Depends(require_limited_api_user),
) -> ShareLinkOut:
    await ensure_authenticated_allowed(
        user.id,
        RateLimitScope.CREATE_SHARE,
        detail="创建分享过于频繁，请稍后再试",
    )
    try:
        result = await share_service.create_share(
            user_id=user.id,
            user_file_id=req.user_file_id,
            password=req.password,
            expires_in=req.expires_in,
            max_downloads=req.max_downloads,
        )
    except DomainError as exc:
        raise_http(exc)
    logger.info("创建分享 user_id=%s file_id=%s code=%s", user.id, req.user_file_id, result["share_code"])
    return ShareLinkOut(**result)


@router.get("/api/shares")
async def list_shares(user: AuthUser = Depends(require_limited_api_user)) -> list[ShareLinkOut]:
    return [ShareLinkOut(**item) for item in await share_service.list_shares(user.id)]


@router.put("/api/shares/{share_id}/revoke")
async def revoke_share(
    share_id: int,
    user: AuthUser = Depends(require_limited_api_user),
) -> dict:
    try:
        result = await share_service.revoke_share(share_id, user.id)
    except DomainError as exc:
        raise_http(exc)
    logger.info("失效分享 user_id=%s share_id=%s", user.id, share_id)
    return result


@router.delete("/api/shares", response_model=SharesBatchOperationResponse)
async def delete_shares(
    payload: BatchDeleteSharesRequest,
    user: AuthUser = Depends(require_limited_api_user),
) -> SharesBatchOperationResponse:
    if not payload.share_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="至少选择一个条目",
        )
    if len(payload.share_ids) > 1000:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="一次最多操作 1000 个条目",
        )
    result = await share_service.bulk_delete_shares(user.id, payload.share_ids)
    logger.info(
        "批量删除分享 user_id=%s requested=%s accepted=%s failed=%s",
        user.id,
        len(payload.share_ids),
        result["accepted_count"],
        result["failed_count"],
    )
    return SharesBatchOperationResponse(**result)


@router.put("/api/shares/revoke-all")
async def revoke_all_shares(user: AuthUser = Depends(require_limited_api_user)) -> dict:
    result = await share_service.revoke_all_shares(user.id)
    logger.info("批量失效分享 user_id=%s count=%s", user.id, result["count"])
    return result


@router.get("/api/s/{code}")
async def get_share_info(code: str, request: Request) -> ShareInfoOut:
    await ensure_public_allowed(
        client_ip_from_request(request),
        RateLimitScope.PUBLIC_API,
        detail="请求过于频繁",
    )
    try:
        result = await share_service.get_share_info(code)
    except DomainError as exc:
        raise_http(exc)
    return ShareInfoOut(**result)


@router.post("/api/s/{code}/access")
async def access_share(
    code: str,
    req: ShareAccessRequest,
    request: Request,
) -> ShareAccessResponse:
    await ensure_share_access_allowed(
        client_ip_from_request(request),
        code,
        detail="请求过于频繁",
    )
    try:
        result = await share_service.access_share(code, req.password)
    except DomainError as exc:
        raise_http(exc)
    return ShareAccessResponse(**result)


@router.get("/api/s/{code}/download")
async def download_shared_file(
    code: str,
    request: Request,
    subpath: str | None = Query(default=None),
):
    return await _serve_shared_download(
        code,
        request,
        token=_bearer_token(request),
        subpath=subpath,
    )

@router.post("/api/s/{code}/download")
async def submit_shared_file_download(
    code: str,
    request: Request,
    token: str | None = Form(default=None),
    subpath: str | None = Form(default=None),
):
    return await _serve_shared_download(
        code,
        request,
        token=token or _bearer_token(request),
        subpath=subpath,
    )


@router.get("/api/s/{code}/browse", response_model=BrowsePageResponse)
async def browse_shared_directory(
    code: str,
    request: Request,
    subpath: str = Query(default=""),
    page: int = 1,
    page_size: int = file_service.BROWSE_DEFAULT_PAGE_SIZE,
) -> BrowsePageResponse:
    await ensure_public_allowed(
        client_ip_from_request(request),
        RateLimitScope.PUBLIC_API,
        detail="请求过于频繁",
    )
    page, page_size = file_service.clamp_browse_page(page, page_size)
    try:
        entries, total = await share_service.browse_shared_directory(
            code,
            _bearer_token(request),
            subpath,
            page=page,
            page_size=page_size,
        )
    except DomainError as exc:
        raise_http(exc)
        raise AssertionError("unreachable")
    return BrowsePageResponse(
        items=[BrowseEntryOut(**entry) for entry in entries],
        total=total,
        page=page,
        page_size=page_size,
    )
