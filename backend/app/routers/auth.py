import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlmodel import select

from app.auth import clear_session, create_session, require_user, set_session_cookie
from app.core.config import settings
from app.core.rate_limit import api_limiter, login_limiter
from app.core.security import hash_password, verify_password, verify_password_constant_time
from app.database import get_session
from app.models import User
from app.schemas import ChangePasswordRequest, LoginRequest, UserOut

router = APIRouter(prefix="/api/auth", tags=["auth"])
logger = logging.getLogger(__name__)


@router.post("/login", response_model=UserOut)
async def login(payload: LoginRequest, request: Request, response: Response) -> dict:
    # 获取客户端 IP
    client_ip = (request.client.host if request.client and request.client.host else "unknown")
    request_id = getattr(request.state, "request_id", "-")

    # 检查是否被限制
    if await login_limiter.is_blocked(client_ip):
        logger.warning(
            "登录限流触发 username=%s ip=%s request_id=%s",
            payload.username,
            client_ip,
            request_id,
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="登录尝试次数过多，请稍后再试"
        )

    # 异步查询用户，避免阻塞事件循环
    async with get_session() as db:
        result = await db.exec(select(User).where(User.username == payload.username))
        user = result.first()

    password_hash = user.password_hash if user else None

    if not verify_password_constant_time(payload.password, password_hash):
        # 记录失败尝试
        await login_limiter.record_failure(client_ip)
        logger.warning(
            "登录失败 username=%s ip=%s request_id=%s",
            payload.username,
            client_ip,
            request_id,
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")

    # 验证成功意味着 user 必定存在（verify_password_constant_time 在 user 为 None 时返回 False）
    assert user is not None

    # 登录成功，清除失败记录
    await login_limiter.clear(client_ip)

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
async def logout(request: Request, response: Response, user: User = Depends(require_user)) -> dict:
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
async def me(user: User = Depends(require_user)) -> dict:
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
    user: User = Depends(require_user)
) -> dict:
    request_id = getattr(request.state, "request_id", "-")
    if not await api_limiter.is_allowed(user.id, "change_password", limit=5, window_seconds=300):
        logger.warning(
            "修改密码限流触发 user_id=%s request_id=%s",
            user.id,
            request_id,
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="操作过于频繁，请稍后再试"
        )

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

    # 更新密码
    async with get_session() as db:
        # 重新查询 user，避免使用 detached ORM 对象
        from sqlmodel import select
        result = await db.exec(select(User).where(User.id == user.id))
        db_user = result.first()
        if not db_user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="用户不存在"
            )
        db_user.password_hash = hash_password(payload.new_password)
        db_user.is_initial_password = False  # 清除初始密码标记
        db.add(db_user)
        await db.commit()

        # 使该用户的所有 session 失效
        from sqlmodel import delete
        from app.models import Session
        # user.id 来自 require_user 依赖，保证非空
        user_id = user.id
        assert user_id is not None
        await db.exec(delete(Session).where(Session.user_id == user_id))
        await db.commit()

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
