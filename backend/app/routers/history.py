"""任务历史记录接口"""

from fastapi import APIRouter, Depends, Query

from app.auth import AuthUser, require_limited_api_user
from app.domain.errors import DomainError
from app.http.errors import raise_http
from app.services import history_service

router = APIRouter(prefix="/api/history", tags=["history"])
v2_router = APIRouter(prefix="/api/v2", tags=["history"])


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


@router.delete("/{history_id}")
async def delete_history(
    history_id: int,
    user: AuthUser = Depends(require_limited_api_user),
) -> dict:
    try:
        return await history_service.delete_history(user.id, history_id)
    except DomainError as exc:
        raise_http(exc)


@router.delete("")
async def clear_history(user: AuthUser = Depends(require_limited_api_user)) -> dict:
    return await history_service.clear_history(user.id)
