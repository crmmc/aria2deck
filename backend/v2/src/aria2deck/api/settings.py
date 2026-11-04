from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends

from ..application.accounts.models import Principal
from ..application.settings.service import SettingsService
from ..application.settings.trackers import TrackerCache, TrackerService
from ..domain.settings import SECRET_MASK, SystemSettings
from .contracts import (
    PublicSiteInfoResponse,
    SettingsResponse,
    SettingsUpdateRequest,
    TrackerStatusResponse,
)
from .dependencies import required_session_principal, settings_service, tracker_service
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
) -> SettingsResponse:
    updated = await service.update(
        principal, payload.model_dump(exclude_unset=True, exclude_none=True)
    )
    return _settings_response(updated)


@router.get(
    "/public/site-info", response_model=PublicSiteInfoResponse, responses=error_responses(500)
)
async def public_site_info(
    service: SettingsService = Depends(settings_service),
) -> PublicSiteInfoResponse:
    return PublicSiteInfoResponse(site_title=service.public_site_title())


def _tracker_status(cache: TrackerCache) -> TrackerStatusResponse:
    return TrackerStatusResponse(
        items=list(cache.merged),
        entry_count=len(cache.merged),
        remote_items=list(cache.remote),
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
