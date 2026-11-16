from __future__ import annotations

from functools import partial
from typing import Annotated
from urllib.parse import parse_qs, quote

from fastapi import APIRouter, Depends, Header, Path, Query, Request

from ..application.settings.download_limit import DownloadConnectionLease, DownloadConnectionLimiter
from ..application.settings.rate_limit import RateLimiter
from ..application.shares.access import PublicShareService
from ..application.shares.models import PublicShareDownload
from ..domain.errors import DomainError, ErrorCode
from .contracts import (
    PublicBreadcrumbResponse,
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


MAX_FORM_BYTES = 64 * 1024


def _require_urlencoded_form(request: Request) -> None:
    content_type = request.headers.get("content-type", "")
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type != "application/x-www-form-urlencoded":
        raise DomainError(ErrorCode.INVALID_INPUT, "表单下载仅支持 application/x-www-form-urlencoded")


def _require_form_size(request: Request) -> None:
    declared = request.headers.get("content-length", "")
    if declared.isdigit():
        _require_form_body_size(int(declared))


def _require_form_body_size(size: int) -> None:
    if size > MAX_FORM_BYTES:
        raise DomainError(ErrorCode.TASK_TOO_LARGE, "表单请求体过大")


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
    return ShareVerifyResponse(access_token=token, expires_in=expires_in)


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
        breadcrumbs=[PublicBreadcrumbResponse(name=item.name, path=item.path) for item in breadcrumbs],
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


@router.post(
    "/{code}/download",
    responses=error_responses(400, 401, 403, 404, 410, 413, 416, 422, 429, 500),
)
async def download_share_by_form(
    request: Request,
    code: str = Path(min_length=1, max_length=64),
    range_header: str | None = Header(default=None, alias="Range"),
    limiter: RateLimiter = Depends(rate_limiter),
    service: PublicShareService = Depends(public_shares),
    connections: DownloadConnectionLimiter = Depends(download_connections),
) -> LeaseStreamingResponse:
    # 限流先行：无 token 的请求同样消耗额度。
    ip = client_ip(request)
    limiter.check_public(ip)
    limiter.check_share_access(ip)
    _require_urlencoded_form(request)
    _require_form_size(request)
    # 表单通道：手工解析 x-www-form-urlencoded（不引 python-multipart），token 只从表单或 Bearer 头进入。
    # token 是否必需由 service 按分享是否设密码裁决（无密码分享凭分享码直接访问）。
    body = (await request.body()).decode("utf-8", errors="replace")
    _require_form_body_size(len(body.encode("utf-8")))
    form = parse_qs(body, keep_blank_values=True)
    raw_token = form.get("token", [""])[0]
    raw_path = form.get("path", [""])[0]
    path = raw_path or None
    # 校验先行（与旧版同序）：无效分享码/令牌不消耗匿名连接档；per-file 档按内容身份计。
    record = await service.check_access(code, raw_token)
    connection = await connections.acquire_anonymous(
        client_ip=client_ip(request), file_key=record.content_key
    )
    try:
        result = await service.download(code, raw_token, path, range_header)
    except BaseException:
        await connection.release()
        raise
    return _streaming(result, service, connection)
