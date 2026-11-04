from __future__ import annotations

from fastapi import APIRouter, Depends

from ..application.accounts.models import Principal
from ..application.system.connectivity import BackendConnectivity
from .contracts import BackendStatusResponse, SystemStatusResponse
from .dependencies import connectivity, required_principal
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
