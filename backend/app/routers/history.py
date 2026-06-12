"""任务历史记录接口"""

from fastapi import APIRouter, Depends

from app.auth import AuthUser, require_user
from app.domain.errors import DomainError
from app.http.errors import raise_http
from app.services import history_service

router = APIRouter(prefix="/api/history", tags=["history"])


@router.get("")
async def list_history(user: AuthUser = Depends(require_user)) -> list[dict]:
    return await history_service.list_history(user.id)


@router.delete("/{history_id}")
async def delete_history(
    history_id: int,
    user: AuthUser = Depends(require_user),
) -> dict:
    try:
        return await history_service.delete_history(user.id, history_id)
    except DomainError as exc:
        raise_http(exc)


@router.delete("")
async def clear_history(user: AuthUser = Depends(require_user)) -> dict:
    return await history_service.clear_history(user.id)
