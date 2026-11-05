from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from ..application.accounts.models import Principal
from ..application.settings.rate_limit import RateLimiter
from ..application.shares.manage import ShareBatchItem, ShareManageService
from ..application.shares.models import ShareView
from ..domain.shares import ShareStatus
from .contracts import (
    ErrorBody,
    ShareBatchItemResponse,
    ShareBatchRequest,
    ShareBatchResponse,
    ShareCreatedResponse,
    ShareCreateRequest,
    ShareItemResponse,
    ShareListResponse,
    ShareRevokeAllResponse,
)
from .dependencies import rate_limiter, required_principal, share_manage
from .errors import error_responses

router = APIRouter(prefix="/api/v2", tags=["shares"])


def _share(view: ShareView) -> ShareItemResponse:
    return ShareItemResponse(
        id=view.id,
        code=view.code,
        file_name=view.file_name,
        file_size=view.file_size,
        file_kind=view.file_kind,
        status=view.status.value,
        expires_at_ms=view.expires_at_ms,
        max_downloads=view.max_downloads,
        download_count=view.download_count,
        created_at_ms=view.created_at_ms,
        has_password=view.has_password,
    )


def _batch(items: list[ShareBatchItem]) -> ShareBatchResponse:
    results = [
        ShareBatchItemResponse(
            share_id=item.share_id,
            ok=item.ok,
            already_revoked=item.already_revoked,
            error=(
                None
                if item.error_code is None
                else ErrorBody(code=item.error_code, message=item.message or "")
            ),
        )
        for item in items
    ]
    succeeded = sum(result.ok for result in results)
    return ShareBatchResponse(
        results=results, accepted_count=succeeded, rejected_count=len(results) - succeeded
    )


@router.post(
    "/shares",
    response_model=ShareCreatedResponse,
    status_code=201,
    responses=error_responses(401, 404, 409, 422, 429, 500),
)
async def create_share(
    payload: ShareCreateRequest,
    principal: Principal = Depends(required_principal),
    service: ShareManageService = Depends(share_manage),
    limiter: RateLimiter = Depends(rate_limiter),
) -> ShareCreatedResponse:
    limiter.check_create_share(user_id=principal.user.id)
    created = await service.create(
        principal.user.id,
        user_file_id=payload.user_file_id,
        expires_in_seconds=payload.expires_in_seconds,
        max_downloads=payload.max_downloads,
        password=payload.password,
    )
    return ShareCreatedResponse(**_share(created.view).model_dump())


@router.get(
    "/shares",
    response_model=ShareListResponse,
    responses=error_responses(401, 422, 429, 500),
)
async def list_shares(
    status: ShareStatus | None = None,
    q: str | None = Query(default=None, max_length=200),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    principal: Principal = Depends(required_principal),
    service: ShareManageService = Depends(share_manage),
) -> ShareListResponse:
    items, total = await service.list_shares(
        principal.user.id, status=status, term=q, page=page, page_size=page_size
    )
    return ShareListResponse(
        items=[_share(item) for item in items],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post(
    "/shares/revoke",
    response_model=ShareBatchResponse,
    responses=error_responses(401, 422, 429, 500),
)
async def revoke_shares(
    payload: ShareBatchRequest,
    principal: Principal = Depends(required_principal),
    service: ShareManageService = Depends(share_manage),
) -> ShareBatchResponse:
    return _batch(await service.revoke(principal.user.id, payload.share_ids))


@router.delete(
    "/shares/all",
    response_model=ShareRevokeAllResponse,
    responses=error_responses(401, 422, 429, 500),
)
async def revoke_all_shares(
    principal: Principal = Depends(required_principal),
    service: ShareManageService = Depends(share_manage),
) -> ShareRevokeAllResponse:
    return ShareRevokeAllResponse(revoked_count=await service.revoke_all(principal.user.id))


@router.delete(
    "/shares",
    response_model=ShareBatchResponse,
    responses=error_responses(401, 422, 429, 500),
)
async def delete_shares(
    payload: ShareBatchRequest,
    principal: Principal = Depends(required_principal),
    service: ShareManageService = Depends(share_manage),
) -> ShareBatchResponse:
    return _batch(await service.delete(principal.user.id, payload.share_ids))
