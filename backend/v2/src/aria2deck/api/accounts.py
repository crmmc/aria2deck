from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Depends, Query, Request, Response

from ..application.accounts.models import Principal, RpcAccess, UserView
from ..application.accounts.service import AccountService
from ..application.settings.rate_limit import RateLimiter
from ..domain.quota import available_user_bytes
from .contracts import (
    CredentialInvalidateRequest,
    CredentialInvalidateResponse,
    DeleteUserResponse,
    LoginRequest,
    OkResponse,
    PasswordChangeRequest,
    RpcAccessResponse,
    RpcAccessUpdateRequest,
    UserCreateRequest,
    UserListResponse,
    UserResponse,
    UserUpdateRequest,
)
from .dependencies import (
    OPTIONAL_SESSION_OPENAPI,
    SESSION_COOKIE,
    account_service,
    client_ip,
    optional_principal,
    rate_limiter,
    required_principal,
    required_session_principal,
)
from .errors import error_responses

router = APIRouter(prefix="/api/v2", tags=["accounts"])


def _user(view: UserView) -> UserResponse:
    return UserResponse(
        id=view.user.id,
        username=view.user.username,
        is_admin=view.user.is_admin,
        quota_bytes=view.user.quota_bytes,
        is_initial_password=view.user.is_initial_password,
        pending_delete=view.user.pending_delete,
        used_bytes=view.quota.used_bytes,
        reserved_bytes=view.quota.reserved_bytes,
        available_bytes=available_user_bytes(view.quota),
    )


def _rpc(access: RpcAccess) -> RpcAccessResponse:
    return RpcAccessResponse(
        enabled=access.enabled,
        secret=access.secret,
    )


def _set_session_cookie(
    request: Request, response: Response, service: AccountService, session_id: str
) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        session_id,
        max_age=service.session_ttl_seconds,
        httponly=True,
        secure=cast(bool, request.app.state.cookie_secure),
        samesite="lax",
        path="/",
    )


@router.post("/auth/login", response_model=UserResponse, responses=error_responses(401, 422, 429, 500))
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    service: AccountService = Depends(account_service),
    limiter: RateLimiter = Depends(rate_limiter),
) -> UserResponse:
    limiter.check_login(client_ip(request), payload.username)
    result = await service.login(username=payload.username, password_hash=payload.password)
    _set_session_cookie(request, response, service, result.session_id)
    return _user(result.view)


@router.post(
    "/auth/logout",
    response_model=OkResponse,
    responses=error_responses(401, 403, 429, 500),
)
async def logout(
    response: Response,
    principal: Principal = Depends(required_session_principal),
    service: AccountService = Depends(account_service),
) -> OkResponse:
    await service.logout(principal)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return OkResponse()


@router.get("/auth/me", response_model=UserResponse, responses=error_responses(401, 404, 429, 500))
async def me(
    principal: Principal = Depends(required_principal),
    service: AccountService = Depends(account_service),
) -> UserResponse:
    return _user(await service.current_user(principal))


@router.post(
    "/auth/password",
    response_model=OkResponse,
    responses=error_responses(401, 403, 409, 422, 429, 500),
)
async def change_password(
    payload: PasswordChangeRequest,
    request: Request,
    response: Response,
    principal: Principal = Depends(required_session_principal),
    service: AccountService = Depends(account_service),
) -> OkResponse:
    session_id = await service.change_password(
        principal,
        old_password_hash=payload.old_password,
        new_password_hash=payload.new_password,
    )
    _set_session_cookie(request, response, service, session_id)
    return OkResponse()


@router.post(
    "/users",
    response_model=UserResponse,
    status_code=201,
    responses=error_responses(401, 403, 409, 422, 429, 500),
    openapi_extra=OPTIONAL_SESSION_OPENAPI,
)
async def create_user(
    payload: UserCreateRequest,
    actor: Principal | None = Depends(optional_principal),
    service: AccountService = Depends(account_service),
) -> UserResponse:
    view = await service.create_user(
        actor,
        username=payload.username,
        password_hash=payload.password,
        is_admin=payload.is_admin,
        is_initial_password=payload.is_initial_password,
        quota_bytes=payload.quota_bytes,
    )
    return _user(view)


@router.get(
    "/users",
    response_model=UserListResponse,
    responses=error_responses(401, 403, 422, 429, 500),
)
async def list_users(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    principal: Principal = Depends(required_session_principal),
    service: AccountService = Depends(account_service),
) -> UserListResponse:
    users, total = await service.list_users(principal, page=page, page_size=page_size)
    return UserListResponse(
        items=[_user(user) for user in users], total=total, page=page, page_size=page_size
    )


@router.patch(
    "/users/{user_id}",
    response_model=UserResponse,
    responses=error_responses(401, 403, 404, 409, 422, 429, 500),
)
async def update_user(
    user_id: int,
    payload: UserUpdateRequest,
    principal: Principal = Depends(required_session_principal),
    service: AccountService = Depends(account_service),
) -> UserResponse:
    changes = payload.model_dump(exclude_unset=True, exclude_none=True)
    changes["password_hash"] = changes.pop("password", None)
    changes = {key: value for key, value in changes.items() if value is not None}
    return _user(await service.update_user(principal, user_id=user_id, changes=changes))


@router.delete(
    "/users/{user_id}",
    response_model=DeleteUserResponse,
    status_code=202,
    responses=error_responses(401, 403, 404, 409, 422, 429, 500),
)
async def delete_user(
    user_id: int,
    principal: Principal = Depends(required_session_principal),
    service: AccountService = Depends(account_service),
) -> DeleteUserResponse:
    await service.delete_user(principal, user_id=user_id)
    return DeleteUserResponse()



@router.get(
    "/credentials/rpc",
    response_model=RpcAccessResponse,
    responses=error_responses(401, 403, 409, 429, 500),
)
async def get_rpc_access(
    principal: Principal = Depends(required_session_principal),
    service: AccountService = Depends(account_service),
) -> RpcAccessResponse:
    return _rpc(await service.rpc_access(principal))


@router.put(
    "/credentials/rpc",
    response_model=RpcAccessResponse,
    responses=error_responses(401, 403, 404, 422, 429, 500),
)
async def set_rpc_access(
    payload: RpcAccessUpdateRequest,
    principal: Principal = Depends(required_session_principal),
    service: AccountService = Depends(account_service),
) -> RpcAccessResponse:
    return _rpc(await service.set_rpc_access(principal, enabled=payload.enabled))


@router.post(
    "/credentials/rpc/refresh",
    response_model=RpcAccessResponse,
    responses=error_responses(401, 403, 404, 409, 429, 500),
)
async def refresh_rpc_access(
    principal: Principal = Depends(required_session_principal),
    service: AccountService = Depends(account_service),
) -> RpcAccessResponse:
    return _rpc(await service.refresh_rpc_access(principal))


@router.post(
    "/credentials/invalidate",
    response_model=CredentialInvalidateResponse,
    responses=error_responses(401, 403, 422, 429, 500),
)
async def invalidate_credentials(
    payload: CredentialInvalidateRequest,
    principal: Principal = Depends(required_session_principal),
    service: AccountService = Depends(account_service),
) -> CredentialInvalidateResponse:
    api_tokens, rpc_secrets = await service.invalidate_credentials(
        principal, confirmation=payload.confirmation
    )
    return CredentialInvalidateResponse(api_token_count=api_tokens, rpc_secret_count=rpc_secrets)
