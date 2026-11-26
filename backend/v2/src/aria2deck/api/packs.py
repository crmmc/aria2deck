from __future__ import annotations

from fastapi import APIRouter, Depends, Path, Query

from ..application.accounts.models import Principal
from ..application.packs.admission import PackAdmissionService
from ..application.packs.control import PackControlService
from ..application.packs.models import PackView, PrecomputeResultView
from ..application.packs.progress import PackProgressService
from ..application.settings.rate_limit import RateLimiter
from ..domain.packs import pack_failure_message
from .contracts import (
    ErrorBody,
    OkResponse,
    PackClearRecordsResponse,
    PackCreateRequest,
    PackListResponse,
    PackOutputResponse,
    PackPrecomputeItemResponse,
    PackPrecomputeRequest,
    PackPrecomputeResponse,
    PackViewResponse,
)
from .dependencies import (
    pack_admission,
    pack_control,
    pack_progress,
    rate_limiter,
    required_principal,
)
from .errors import error_responses

router = APIRouter(prefix="/api/v2", tags=["packs"])


def _pack(view: PackView) -> PackViewResponse:
    return PackViewResponse(
        id=view.id,
        status=view.status.value,
        step=None if view.step is None else view.step.value,
        step_progress=view.step_progress,
        step_started_at_ms=view.step_started_at_ms,
        created_at_ms=view.created_at_ms,
        updated_at_ms=view.updated_at_ms,
        finished_at_ms=view.finished_at_ms,
        error_code=view.error_code,
        error_message=pack_failure_message(view.error_code),
        delete_source=view.delete_source,
        output_name=view.output_name,
        output=(
            None
            if view.output is None
            else PackOutputResponse(
                user_file_id=view.output.user_file_id,
                display_name=view.output.display_name,
                size=view.output.size,
            )
        ),
    )


def _precompute(result: PrecomputeResultView) -> PackPrecomputeResponse:
    return PackPrecomputeResponse(
        items=[
            PackPrecomputeItemResponse(
                user_file_id=item.user_file_id,
                accepted=item.accepted,
                display_name=item.display_name,
                size=item.size,
                error=(
                    None
                    if item.error_code is None
                    else ErrorBody(code=item.error_code, message=item.message or "")
                ),
            )
            for item in result.items
        ],
        total_size=result.total_size,
        available_bytes=result.available_bytes,
    )


@router.post(
    "/packs/precompute",
    response_model=PackPrecomputeResponse,
    responses=error_responses(401, 422, 429, 500),
)
async def precompute_pack(
    payload: PackPrecomputeRequest,
    principal: Principal = Depends(required_principal),
    service: PackAdmissionService = Depends(pack_admission),
    limiter: RateLimiter = Depends(rate_limiter),
) -> PackPrecomputeResponse:
    limiter.check_create_pack(user_id=principal.user.id)
    return _precompute(await service.precompute(principal.user.id, payload.items))


@router.post(
    "/packs",
    response_model=PackViewResponse,
    status_code=201,
    responses=error_responses(401, 404, 409, 422, 429, 500),
)
async def create_pack(
    payload: PackCreateRequest,
    principal: Principal = Depends(required_principal),
    service: PackAdmissionService = Depends(pack_admission),
    progress: PackProgressService = Depends(pack_progress),
    limiter: RateLimiter = Depends(rate_limiter),
) -> PackViewResponse:
    limiter.check_create_pack(user_id=principal.user.id)
    pack_id = await service.create(
        principal.user.id,
        user_file_ids=payload.items,
        output_name=payload.output_name,
        delete_source=payload.delete_source,
    )
    return _pack(await progress.get_pack(principal.user.id, pack_id))


@router.get(
    "/packs",
    response_model=PackListResponse,
    responses=error_responses(401, 422, 429, 500),
)
async def list_packs(
    limit: int = Query(default=10, ge=1, le=50),
    principal: Principal = Depends(required_principal),
    service: PackProgressService = Depends(pack_progress),
) -> PackListResponse:
    views = await service.list_packs(principal.user.id, limit)
    return PackListResponse(items=[_pack(view) for view in views])


@router.delete(
    "/packs/records",
    response_model=PackClearRecordsResponse,
    responses=error_responses(401, 422, 429, 500),
)
async def clear_pack_records(
    principal: Principal = Depends(required_principal),
    service: PackControlService = Depends(pack_control),
) -> PackClearRecordsResponse:
    """清空本人全部终态打包记录（保留输出文件）（A3）。"""
    cleared = await service.clear_records(principal.user.id)
    return PackClearRecordsResponse(cleared_count=cleared)


@router.post(
    "/packs/{pack_id}/cancel",
    response_model=OkResponse,
    responses=error_responses(401, 404, 409, 422, 429, 500),
)
async def cancel_pack(
    pack_id: int = Path(ge=1),
    principal: Principal = Depends(required_principal),
    service: PackControlService = Depends(pack_control),
) -> OkResponse:
    await service.cancel(principal.user.id, pack_id)
    return OkResponse()


@router.delete(
    "/packs/{pack_id}/record",
    response_model=OkResponse,
    responses=error_responses(401, 404, 409, 422, 429, 500),
)
async def clear_pack_record(
    pack_id: int = Path(ge=1),
    principal: Principal = Depends(required_principal),
    service: PackControlService = Depends(pack_control),
) -> OkResponse:
    await service.clear_record(principal.user.id, pack_id)
    return OkResponse()
