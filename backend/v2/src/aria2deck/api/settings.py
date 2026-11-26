from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, Request

from ..application.accounts.models import Principal
from ..application.settings.rate_limit import RateLimiter
from ..application.settings.service import SettingsService
from ..application.settings.trackers import TrackerCache, TrackerService
from ..domain.settings import SECRET_MASK, SystemSettings
from .contracts import (
    Aria2TestRequest,
    Aria2TestResponse,
    PublicSiteInfoResponse,
    SettingsResponse,
    SettingsUpdateRequest,
    TrackerStatusResponse,
)
from .dependencies import (
    client_ip,
    rate_limiter,
    required_session_principal,
    settings_service,
    tracker_service,
)
from .errors import error_responses

router = APIRouter(prefix="/api/v2", tags=["settings"])


def _settings_response(settings: SystemSettings) -> SettingsResponse:
    values = asdict(settings)
    values["aria2_rpc_secret"] = SECRET_MASK if settings.aria2_rpc_secret is not None else None
    return SettingsResponse(**values)


@router.get(
    "/settings",
    response_model=SettingsResponse,
    responses=error_responses(401, 403, 429, 500),
)
async def read_settings(
    principal: Principal = Depends(required_session_principal),
    service: SettingsService = Depends(settings_service),
) -> SettingsResponse:
    return _settings_response(service.read(principal))


@router.patch(
    "/settings",
    response_model=SettingsResponse,
    responses=error_responses(401, 403, 409, 422, 429, 500),
)
async def update_settings(
    payload: SettingsUpdateRequest,
    principal: Principal = Depends(required_session_principal),
    service: SettingsService = Depends(settings_service),
    trackers: TrackerService = Depends(tracker_service),
) -> SettingsResponse:
    """保存配置；远程源集合变化时立即重建 Tracker 缓存，保证生效列表与页面一致。"""
    previous_remote_urls = service.read(principal).tracker_remote_urls
    updated = await service.update(
        principal, payload.model_dump(exclude_unset=True, exclude_none=True)
    )
    if updated.tracker_remote_urls != previous_remote_urls:
        await trackers.rebuild()
    return _settings_response(updated)


@router.post(
    "/settings/aria2/test",
    response_model=Aria2TestResponse,
    responses=error_responses(401, 403, 422, 429, 500),
)
async def test_aria2_connection(
    payload: Aria2TestRequest | None = None,
    principal: Principal = Depends(required_session_principal),
    service: SettingsService = Depends(settings_service),
    limiter: RateLimiter = Depends(rate_limiter),
) -> Aria2TestResponse:
    limiter.check_aria2_test(user_id=principal.user.id)
    result = await service.test_aria2(
        principal,
        url=None if payload is None else payload.aria2_rpc_url,
        secret=None if payload is None else payload.aria2_rpc_secret,
    )
    return Aria2TestResponse(
        connected=result.connected,
        version=result.version,
        features=list(result.features),
    )


@router.get(
    "/settings/aria2/version",
    response_model=Aria2TestResponse,
    responses=error_responses(401, 403, 429, 500),
)
async def aria2_version(
    principal: Principal = Depends(required_session_principal),
    service: SettingsService = Depends(settings_service),
    limiter: RateLimiter = Depends(rate_limiter),
) -> Aria2TestResponse:
    """查询当前配置连接的 aria2 版本（A4，只读探测）。"""
    limiter.check_aria2_test(user_id=principal.user.id)
    result = await service.test_aria2(principal)
    return Aria2TestResponse(
        connected=result.connected,
        version=result.version,
        features=list(result.features),
    )


@router.get(
    "/public/site-info", response_model=PublicSiteInfoResponse, responses=error_responses(429, 500)
)
async def public_site_info(
    request: Request,
    service: SettingsService = Depends(settings_service),
    limiter: RateLimiter = Depends(rate_limiter),
) -> PublicSiteInfoResponse:
    limiter.check_public(client_ip(request))
    settings = service.current()
    return PublicSiteInfoResponse(
        site_title=settings.site_title,
        ws_reconnect_max_delay_seconds=settings.ws_reconnect_max_delay_seconds,
        ws_reconnect_jitter_seconds=settings.ws_reconnect_jitter_seconds,
        ws_reconnect_factor=settings.ws_reconnect_factor,
    )


def _tracker_status(cache: TrackerCache) -> TrackerStatusResponse:
    return TrackerStatusResponse(
        items=list(cache.merged),
        entry_count=len(cache.merged),
        refresh_status=cache.refresh_status,
        failed_urls=list(cache.failed_urls),
        updated_at_ms=cache.updated_at_ms,
        last_refresh_at_ms=cache.last_refresh_at_ms,
    )


@router.get(
    "/trackers",
    response_model=TrackerStatusResponse,
    responses=error_responses(401, 403, 429, 500),
)
async def tracker_status(
    principal: Principal = Depends(required_session_principal),
    service: TrackerService = Depends(tracker_service),
) -> TrackerStatusResponse:
    return _tracker_status(await service.status(principal))


@router.post(
    "/trackers/refresh",
    response_model=TrackerStatusResponse,
    responses=error_responses(401, 403, 429, 500),
)
async def refresh_trackers(
    principal: Principal = Depends(required_session_principal),
    service: TrackerService = Depends(tracker_service),
) -> TrackerStatusResponse:
    return _tracker_status(await service.refresh_for(principal))
