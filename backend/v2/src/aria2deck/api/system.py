from __future__ import annotations

from fastapi import APIRouter, Depends

from ..application.accounts.models import Principal
from ..application.system.connectivity import BackendConnectivity
from ..application.system.machine import MachineStatsService
from .contracts import BackendStatusResponse, MachineStatsResponse, SystemStatusResponse
from .dependencies import connectivity, machine_stats, required_admin_principal, required_principal
from .errors import error_responses

router = APIRouter(prefix="/api/v2", tags=["system"])


@router.get(
    "/system/status", response_model=SystemStatusResponse, responses=error_responses(401, 429, 500)
)
def system_status(
    principal: Principal = Depends(required_principal),
    service: BackendConnectivity = Depends(connectivity),
) -> SystemStatusResponse:
    status = service.status(is_admin=principal.user.is_admin)
    return SystemStatusResponse(
        download_backend=BackendStatusResponse(status=status.status, message=status.message)
    )


@router.get(
    "/stats/machine",
    response_model=MachineStatsResponse,
    responses=error_responses(401, 403, 429, 500),
)
async def machine_disk_stats(
    principal: Principal = Depends(required_admin_principal),
    service: MachineStatsService = Depends(machine_stats),
) -> MachineStatsResponse:
    """E1 机器磁盘统计：仅管理员可见的只读快照。"""
    view = await service.stats()
    return MachineStatsResponse(
        total_bytes=view.total_bytes,
        used_bytes=view.used_bytes,
        free_bytes=view.free_bytes,
        download_used_bytes=view.download_used_bytes,
        system_used_bytes=view.system_used_bytes,
    )
