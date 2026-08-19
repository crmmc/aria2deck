"""任务历史记录接口"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from app.auth import AuthUser, require_limited_api_user
from app.services import history_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/history", tags=["history"])
v2_router = APIRouter(prefix="/api/v2", tags=["history"])


class BatchDeleteHistoryRequest(BaseModel):
    history_ids: list[int]


@router.get("")
async def list_history(user: AuthUser = Depends(require_limited_api_user)) -> list[dict]:
    return await history_service.list_history(user.id)


@v2_router.get("/history")
async def list_history_v2(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    user: AuthUser = Depends(require_limited_api_user),
) -> dict:
    return await history_service.list_history_page(
        user_id=user.id,
        page=page,
        page_size=page_size,
    )


@router.delete("")
async def delete_history(
    payload: BatchDeleteHistoryRequest | None = None,
    user: AuthUser = Depends(require_limited_api_user),
) -> dict:
    if payload is None:
        return await history_service.clear_history(user.id)
    if not payload.history_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="至少选择一个条目",
        )
    if len(payload.history_ids) > 1000:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="一次最多操作 1000 个条目",
        )
    result = await history_service.bulk_delete_history(user.id, payload.history_ids)
    logger.info(
        "批量删除历史记录已受理 user_id=%s requested=%s accepted=%s failed=%s",
        user.id,
        len(payload.history_ids),
        result["accepted_count"],
        result["failed_count"],
    )
    return result
