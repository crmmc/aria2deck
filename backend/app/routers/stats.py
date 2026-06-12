"""系统状态接口模块"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.auth import AuthUser, require_admin, require_user
from app.services import stats_service

router = APIRouter(prefix="/api/stats", tags=["stats"])


@router.get("")
async def get_stats(
    user: AuthUser = Depends(require_user),
) -> dict:
    return await stats_service.get_user_stats(
        user_id=user.id,
        quota_bytes=user.quota_bytes,
    )


@router.get("/machine")
async def get_machine_stats(user: AuthUser = Depends(require_admin)) -> dict:
    return await stats_service.get_machine_stats(user.id)
