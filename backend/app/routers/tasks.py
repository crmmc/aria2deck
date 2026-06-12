"""任务管理接口模块（共享下载架构）"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel

from app.auth import AuthUser, require_user
from app.domain.errors import DomainError
from app.http.errors import raise_http
from app.services import task_service

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


class TaskCreate(BaseModel):
    uri: str
    options: dict | None = None


class TorrentCreate(BaseModel):
    torrent: str
    selected_file_indexes: list[object] | None = None
    options: dict | None = None


class TorrentPreviewCreate(BaseModel):
    torrent: str


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_task(
    payload: TaskCreate,
    request: Request,
    user: AuthUser = Depends(require_user),
) -> dict:
    try:
        return await task_service.create_task(
            user_id=user.id,
            quota_bytes=user.quota,
            uri=payload.uri,
            options=payload.options,
            app_state=getattr(request.app.state, "app_state", None),
        )
    except DomainError as exc:
        raise_http(exc)


@router.post("/torrent/preview")
async def preview_torrent_task(
    payload: TorrentPreviewCreate,
    user: AuthUser = Depends(require_user),
) -> dict:
    try:
        return await task_service.preview_torrent_task(
            user_id=user.id,
            torrent=payload.torrent,
        )
    except DomainError as exc:
        raise_http(exc)


@router.post("/torrent", status_code=status.HTTP_201_CREATED)
async def create_torrent_task(
    payload: TorrentCreate,
    request: Request,
    user: AuthUser = Depends(require_user),
) -> dict:
    try:
        return await task_service.create_torrent_task(
            user_id=user.id,
            quota_bytes=int(user.quota_bytes),
            torrent=payload.torrent,
            selected_file_indexes=payload.selected_file_indexes,
            options=payload.options,
            app_state=getattr(request.app.state, "app_state", None),
        )
    except DomainError as exc:
        raise_http(exc)


@router.get("")
async def list_tasks(
    request: Request,
    status_filter: str | None = None,
    user: AuthUser = Depends(require_user),
) -> list[dict]:
    try:
        return await task_service.list_tasks(
            user_id=user.id,
            status_filter=status_filter,
            app_state=getattr(request.app.state, "app_state", None),
        )
    except DomainError as exc:
        raise_http(exc)


@router.delete("/{subscription_id}")
async def cancel_task(
    subscription_id: int,
    request: Request,
    user: AuthUser = Depends(require_user),
) -> dict:
    try:
        return await task_service.cancel_task(
            user_id=user.id,
            user_task_id=subscription_id,
            quota_bytes=int(user.quota_bytes),
            app_state=getattr(request.app.state, "app_state", None),
        )
    except DomainError as exc:
        raise_http(exc)


@router.delete("")
async def clear_history(user: AuthUser = Depends(require_user)) -> dict:
    return await task_service.clear_history(user.id)
