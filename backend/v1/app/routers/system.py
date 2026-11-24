"""Public system status endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.auth import AuthUser, require_limited_api_user
from app.services import backend_connectivity

router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("/status")
async def get_system_status(
    user: AuthUser = Depends(require_limited_api_user),
) -> dict:
    """Return download-backend reachability for authenticated users.

    Response is role-scoped: both roles get only status + fixed message.
    Detailed diagnostics remain on admin settings endpoints.
    """
    return backend_connectivity.snapshot(is_admin=bool(user.is_admin))
