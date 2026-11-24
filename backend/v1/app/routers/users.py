"""用户管理接口模块"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response, status

from app.auth import AuthUser, require_limited_admin, require_limited_session_user
from app.core.request_rate_guard import client_ip_from_request
from app.domain.errors import DomainError
from app.http.errors import raise_http
from app.schemas import (
    RpcAccessIssued,
    RpcAccessStatus,
    RpcAccessToggle,
    UserCreate,
    UserOut,
    UserUpdate,
)
from app.services import user_service

router = APIRouter(prefix="/api/users", tags=["users"])


@router.post("", response_model=UserOut)
async def create_user(payload: UserCreate, request: Request) -> dict:
    has_users = await user_service.has_any_user()
    admin = await require_limited_admin(request) if has_users else None
    try:
        return await user_service.create_user(
            payload=payload,
            client_ip=client_ip_from_request(request),
            request_id=getattr(request.state, "request_id", "-"),
            admin=admin,
        )
    except DomainError as exc:
        raise_http(exc)


@router.get("", response_model=list[UserOut])
async def list_users(admin: AuthUser = Depends(require_limited_admin)) -> list[dict]:
    return await user_service.list_users(admin.id)


@router.delete("/{user_id}")
async def delete_user(
    request: Request,
    user_id: int,
    response: Response,
    admin: AuthUser = Depends(require_limited_admin),
) -> dict:
    try:
        result = await user_service.delete_user(
            actor=admin,
            user_id=user_id,
            request_id=getattr(request.state, "request_id", "-"),
        )
    except DomainError as exc:
        raise_http(exc)
    response.status_code = status.HTTP_202_ACCEPTED
    return result


@router.get("/{user_id}", response_model=UserOut)
async def get_user(user_id: int, admin: AuthUser = Depends(require_limited_admin)) -> dict:
    try:
        return await user_service.get_user(actor_id=admin.id, user_id=user_id)
    except DomainError as exc:
        raise_http(exc)


@router.put("/{user_id}", response_model=UserOut)
async def update_user(
    user_id: int,
    payload: UserUpdate,
    request: Request,
    admin: AuthUser = Depends(require_limited_admin),
) -> dict:
    try:
        return await user_service.update_user(
            actor=admin,
            user_id=user_id,
            payload=payload,
            request_id=getattr(request.state, "request_id", "-"),
        )
    except DomainError as exc:
        raise_http(exc)


@router.get("/me/rpc-access", response_model=RpcAccessStatus)
async def get_rpc_access(user: AuthUser = Depends(require_limited_session_user)) -> RpcAccessStatus:
    try:
        return await user_service.get_rpc_access(user.id)
    except DomainError as exc:
        raise_http(exc)


@router.put("/me/rpc-access", response_model=RpcAccessStatus | RpcAccessIssued)
async def set_rpc_access(
    payload: RpcAccessToggle,
    request: Request,
    user: AuthUser = Depends(require_limited_session_user),
) -> RpcAccessStatus | RpcAccessIssued:
    try:
        return await user_service.set_rpc_access(
            user_id=user.id,
            enabled=payload.enabled,
            request_id=getattr(request.state, "request_id", "-"),
        )
    except DomainError as exc:
        raise_http(exc)


@router.post("/me/rpc-access/refresh", response_model=RpcAccessIssued)
async def refresh_rpc_secret(
    request: Request, user: AuthUser = Depends(require_limited_session_user)
) -> RpcAccessIssued:
    try:
        return await user_service.refresh_rpc_secret(
            user_id=user.id,
            request_id=getattr(request.state, "request_id", "-"),
        )
    except DomainError as exc:
        raise_http(exc)
