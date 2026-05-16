import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from app.auth import AuthUser, clear_session, clear_user_sessions, create_session, require_user, set_session_cookie, user_from_row
from app.core.config import settings
from app.core.request_rate_guard import (
    RateLimitScope,
    clear_account_security_failures,
    client_ip_from_request,
    ensure_account_security_allowed,
    ensure_authenticated_allowed,
    record_account_security_failure,
)
from app.core.security import hash_password, verify_password, verify_password_constant_time
from app.repositories import auth as auth_repo
from app.schemas import ChangePasswordRequest, LoginRequest, UserOut

router = APIRouter(prefix="/api/auth", tags=["auth"])
logger = logging.getLogger(__name__)


@router.post("/login", response_model=UserOut)
async def login(payload: LoginRequest, request: Request, response: Response) -> dict:
    # 获取客户端 IP
    client_ip = client_ip_from_request(request)
    request_id = getattr(request.state, "request_id", "-")

    # 检查是否被限制
    try:
        await ensure_account_security_allowed(
            client_ip,
            detail="登录尝试次数过多，请稍后再试",
        )
    except HTTPException:
        logger.warning(
            "登录限流触发 username=%s ip=%s request_id=%s",
            payload.username,
            client_ip,
            request_id,
        )
        raise

    user_row = await auth_repo.get_user_by_username(payload.username)

    password_hash = user_row["password_hash"] if user_row else None

    if not verify_password_constant_time(payload.password, password_hash):
        # 记录失败尝试
        await record_account_security_failure(client_ip)
        logger.warning(
            "登录失败 username=%s ip=%s request_id=%s",
            payload.username,
            client_ip,
            request_id,
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")

    # 验证成功意味着 user 必定存在（verify_password_constant_time 在 user 为 None 时返回 False）
    assert user_row is not None
    user = user_from_row(user_row)

    # 登录成功，清除失败记录
    await clear_account_security_failures(client_ip)

    # 会话固定防护：清除请求中可能存在的旧 session
    old_session_id = request.cookies.get(settings.session_cookie_name)
    if old_session_id:
        await clear_session(old_session_id)

    assert user.id is not None
    session_id = await create_session(user.id)
    set_session_cookie(response, session_id)
    request.state.auth_user_id = user.id
    logger.info(
        "登录成功 user_id=%s username=%s ip=%s request_id=%s",
        user.id,
        user.username,
        client_ip,
        request_id,
    )

    return {
        "id": user.id,
        "username": user.username,
        "is_admin": bool(user.is_admin),
        "quota": user.quota,
        "is_initial_password": bool(user.is_initial_password)
    }


@router.post("/logout")
async def logout(request: Request, response: Response, user: AuthUser = Depends(require_user)) -> dict:
    session_id = request.cookies.get(settings.session_cookie_name)
    if session_id:
        await clear_session(session_id)
    response.delete_cookie(settings.session_cookie_name)
    request_id = getattr(request.state, "request_id", "-")
    logger.info(
        "用户登出 user_id=%s request_id=%s",
        user.id,
        request_id,
    )
    return {"ok": True}


@router.get("/me", response_model=UserOut)
async def me(user: AuthUser = Depends(require_user)) -> dict:
    logger.debug("获取当前用户信息 user_id=%s", user.id)
    return {
        "id": user.id,
        "username": user.username,
        "is_admin": bool(user.is_admin),
        "quota": user.quota,
        "is_initial_password": bool(user.is_initial_password)
    }


@router.post("/change-password")
async def change_password(
    payload: ChangePasswordRequest,
    request: Request,
    response: Response,
    user: AuthUser = Depends(require_user)
) -> dict:
    request_id = getattr(request.state, "request_id", "-")
    user_id = user.id
    if user_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未登录")

    try:
        await ensure_authenticated_allowed(
            int(user_id),
            RateLimitScope.ACCOUNT_SECURITY,
            detail="操作过于频繁，请稍后再试",
        )
    except HTTPException:
        logger.warning(
            "修改密码限流触发 user_id=%s request_id=%s",
            user.id,
            request_id,
        )
        raise

    if not user.is_initial_password:
        # 验证旧密码
        if not verify_password(payload.old_password, user.password_hash):
            logger.warning(
                "修改密码失败 user_id=%s reason=old_password_mismatch request_id=%s",
                user.id,
                request_id,
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="旧密码错误"
            )

        # 新密码不能与旧密码相同
        if payload.old_password == payload.new_password:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="新密码不能与旧密码相同"
            )

    db_user = await auth_repo.update_user(
        user.id,
        password_hash=hash_password(payload.new_password),
        is_initial_password=False,
    )
    if not db_user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="用户不存在"
        )

    # 使该用户的所有 session 失效
    await clear_user_sessions(user.id)

    # 创建新 session
    assert user.id is not None
    session_id = await create_session(user.id)
    set_session_cookie(response, session_id)
    logger.info(
        "修改密码成功 user_id=%s request_id=%s",
        user.id,
        request_id,
    )

    return {"ok": True, "message": "密码修改成功"}
