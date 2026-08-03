import logging

from fastapi import APIRouter, Depends, Request, Response

from app.auth import (
    AuthUser,
    require_limited_api_user,
    require_limited_session_user,
    set_session_cookie,
)
from app.core.config import settings
from app.core.request_rate_guard import client_ip_from_request
from app.domain.errors import DomainError
from app.http.errors import raise_http
from app.schemas import ChangePasswordRequest, LoginRequest, UserOut
from app.services import auth_service

router = APIRouter(prefix="/api/auth", tags=["auth"])
logger = logging.getLogger(__name__)


@router.post("/login", response_model=UserOut)
async def login(payload: LoginRequest, request: Request, response: Response) -> dict:
    try:
        user_payload, session_id, user_id = await auth_service.login(
            username=payload.username,
            password=payload.password,
            client_ip=client_ip_from_request(request),
            old_session_id=request.cookies.get(settings.session_cookie_name),
            request_id=getattr(request.state, "request_id", "-"),
        )
    except DomainError as exc:
        raise_http(exc)
    set_session_cookie(response, session_id)
    request.state.auth_user_id = user_id
    return user_payload


@router.post("/logout")
async def logout(
    request: Request, response: Response, user: AuthUser = Depends(require_limited_session_user)
) -> dict:
    result = await auth_service.logout(
        session_id=request.cookies.get(settings.session_cookie_name),
        user_id=user.id,
        request_id=getattr(request.state, "request_id", "-"),
    )
    response.delete_cookie(settings.session_cookie_name)
    return result


@router.get("/me", response_model=UserOut)
async def me(user: AuthUser = Depends(require_limited_api_user)) -> dict:
    logger.debug("获取当前用户信息 user_id=%s", user.id)
    return auth_service.user_response(user)


@router.post("/change-password")
async def change_password(
    payload: ChangePasswordRequest,
    request: Request,
    response: Response,
    user: AuthUser = Depends(require_limited_session_user),
) -> dict:
    try:
        result, session_id = await auth_service.change_password(
            user=user,
            old_password=payload.old_password,
            new_password=payload.new_password,
            request_id=getattr(request.state, "request_id", "-"),
        )
    except DomainError as exc:
        raise_http(exc)
    set_session_cookie(response, session_id)
    return result
