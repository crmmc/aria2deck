from __future__ import annotations

from functools import partial
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, Header, Path, Query, Request

from ..application.settings.download_limit import DownloadConnectionLease, DownloadConnectionLimiter
from ..application.settings.rate_limit import RateLimiter
from ..application.shares.access import PublicShareService
from ..application.shares.models import PublicShareDownload
from .contracts import (
    PublicEntryItemResponse,
    PublicEntryListResponse,
    PublicShareInfoResponse,
    ShareVerifyRequest,
    ShareVerifyResponse,
)
from .dependencies import client_ip, download_connections, public_shares, rate_limiter
from .errors import error_responses
from .streaming import LeaseStreamingResponse, Releaser

router = APIRouter(prefix="/api/s", tags=["public-shares"])


def _content_disposition(name: str) -> str:
    fallback = name if name.isascii() and '"' not in name and "\\" not in name else "download"
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(name, safe='')}"


def _bearer_token(authorization: str | None) -> str:
    """Access tokens travel ONLY in the Bearer header or POST form; query is never read."""
    if authorization is None:
        return ""
    scheme, separator, value = authorization.partition(" ")
    if scheme.casefold() != "bearer" or not separator:
        return ""
    return value.strip()


def _streaming(
    result: PublicShareDownload,
    service: PublicShareService,
    connection: DownloadConnectionLease | None = None,
) -> LeaseStreamingResponse:
    headers = {
        "Content-Disposition": _content_disposition(result.display_name),
        "Accept-Ranges": "bytes",
    }
    if result.decision.status_code == 206:
        headers["Content-Length"] = str(result.decision.content_length)
        headers["Content-Range"] = (
            f"bytes {result.decision.start}-{result.decision.end}/{result.total_size}"
        )
    else:
        headers["Content-Length"] = str(result.total_size)
    # 连接租约与内容读租约都挂进 releases=：ASGI finally 覆盖正常结束、
    # 中途异常与客户端断开；BackgroundTask 在断开时不会执行，曾导致读租约泄漏。
    releases: tuple[Releaser, ...] = (
        partial(service.release, result),
    )
    if connection is not None:
        releases = (connection.release, partial(service.release, result))
    return LeaseStreamingResponse(
        service.iter_chunks(result),
        releases=releases,
        status_code=result.decision.status_code,
        headers=headers,
    )


@router.get(
    "/{code}/info",
    response_model=PublicShareInfoResponse,
    responses=error_responses(404, 410, 422, 429, 500),
)
async def share_info(
    request: Request,
    code: str = Path(min_length=1, max_length=64),
    limiter: RateLimiter = Depends(rate_limiter),
    service: PublicShareService = Depends(public_shares),
) -> PublicShareInfoResponse:
    ip = client_ip(request)
    limiter.check_public(ip)
    limiter.check_share_access(ip)
    info = await service.info(code)
    return PublicShareInfoResponse(
        file_name=info.file_name,
        file_size=info.file_size,
        file_kind=info.file_kind,
        has_password=info.has_password,
        expires_at_ms=info.expires_at_ms,
        max_downloads=info.max_downloads,
        download_count=info.download_count,
    )


@router.post(
    "/{code}/verify",
    response_model=ShareVerifyResponse,
    responses=error_responses(400, 403, 404, 410, 422, 429, 500),
)
async def verify_share(
    payload: ShareVerifyRequest,
    request: Request,
    code: str = Path(min_length=1, max_length=64),
    limiter: RateLimiter = Depends(rate_limiter),
    service: PublicShareService = Depends(public_shares),
) -> ShareVerifyResponse:
    ip = client_ip(request)
    limiter.check_public(ip)
    limiter.check_share_access(ip)
    token, expires_in = await service.verify(code, payload.password)
    return ShareVerifyResponse(access_token=token)


@router.get(
    "/{code}/entries",
    response_model=PublicEntryListResponse,
    responses=error_responses(400, 401, 404, 410, 422, 429, 500),
)
async def browse_share(
    request: Request,
    code: str = Path(min_length=1, max_length=64),
    path: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    authorization: Annotated[str | None, Header()] = None,
    limiter: RateLimiter = Depends(rate_limiter),
    service: PublicShareService = Depends(public_shares),
) -> PublicEntryListResponse:
    ip = client_ip(request)
    limiter.check_public(ip)
    limiter.check_share_access(ip)
    entries, breadcrumbs, total = await service.browse(
        code, _bearer_token(authorization), path, page, page_size
    )
    return PublicEntryListResponse(
        items=[
            PublicEntryItemResponse(
                name=entry.name, path=entry.path, kind=entry.kind.value, size=entry.size
            )
            for entry in entries
        ],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/{code}/download",
    responses=error_responses(400, 401, 403, 404, 410, 416, 422, 429, 500),
)
async def download_share(
    request: Request,
    code: str = Path(min_length=1, max_length=64),
    path: str | None = Query(default=None),
    range_header: str | None = Header(default=None, alias="Range"),
    authorization: Annotated[str | None, Header()] = None,
    limiter: RateLimiter = Depends(rate_limiter),
    service: PublicShareService = Depends(public_shares),
    connections: DownloadConnectionLimiter = Depends(download_connections),
) -> LeaseStreamingResponse:
    ip = client_ip(request)
    limiter.check_public(ip)
    limiter.check_share_access(ip)
    # 校验先行（与旧版同序）：无效分享码/令牌不消耗匿名连接档；
    # per-file 档按内容身份计（旧版用 content_hash 口径，此处对应 content_key）。
    record = await service.check_access(code, _bearer_token(authorization))
    connection = await connections.acquire_anonymous(
        client_ip=client_ip(request), file_key=record.content_key
    )
    try:
        result = await service.download(code, _bearer_token(authorization), path, range_header)
    except BaseException:
        await connection.release()
        raise
    return _streaming(result, service, connection)

