from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from app.auth import clear_session, create_session, require_user, set_session_cookie
from app.core.config import settings
from app.core.rate_limit import login_limiter
from app.core.security import hash_password, verify_password
from app.database import get_session
from app.db import fetch_one
from app.models import User
from app.schemas import ChangePasswordRequest, LoginRequest, UserOut

DEFAULT_PASSWORD = "123456"

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login", response_model=UserOut)
async def login(payload: LoginRequest, request: Request, response: Response) -> dict:
    # 获取客户端 IP
    client_ip = request.client.host if request.client else "unknown"

    # 检查是否被限制
    if login_limiter.is_blocked(client_ip):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="登录尝试次数过多，请稍后再试"
        )

    user = fetch_one("SELECT * FROM users WHERE username = ?", [payload.username])
    if not user or not verify_password(payload.password, user["password_hash"]):
        # 记录失败尝试
        login_limiter.record_failure(client_ip)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    # 登录成功，清除失败记录
    login_limiter.clear(client_ip)

    # 会话固定防护：清除请求中可能存在的旧 session
    old_session_id = request.cookies.get(settings.session_cookie_name)
    if old_session_id:
        await clear_session(old_session_id)

    session_id = await create_session(user["id"])
    set_session_cookie(response, session_id)

    # 检测是否使用默认密码
    is_default_password = payload.password == DEFAULT_PASSWORD
    password_warning = None
    if is_default_password:
        password_warning = "您正在使用默认密码，请尽快修改密码以确保账户安全"

    return {
        "id": user["id"],
        "username": user["username"],
        "is_admin": bool(user["is_admin"]),
        "quota": user["quota"],
        "password_warning": password_warning,
        "is_default_password": is_default_password
    }


@router.post("/logout")
async def logout(request: Request, response: Response, user: User = Depends(require_user)) -> dict:
    session_id = request.cookies.get(settings.session_cookie_name)
    if session_id:
        await clear_session(session_id)
    response.delete_cookie(settings.session_cookie_name)
    return {"ok": True}


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(require_user)) -> dict:
    # 检测是否使用默认密码
    is_default_password = verify_password(DEFAULT_PASSWORD, user.password_hash)

    return {
        "id": user.id,
        "username": user.username,
        "is_admin": bool(user.is_admin),
        "quota": user.quota,
        "is_default_password": is_default_password
    }


@router.post("/change-password")
async def change_password(
    payload: ChangePasswordRequest,
    request: Request,
    response: Response,
    user: User = Depends(require_user)
) -> dict:
    # 验证旧密码
    if not verify_password(payload.old_password, user.password_hash):
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
        user.password_hash = hash_password(payload.new_password)
        db.add(user)
        await db.commit()

        # 使该用户的所有 session 失效
        from sqlmodel import delete
        from app.models import Session
        await db.exec(delete(Session).where(Session.user_id == user.id))
        await db.commit()

    # 创建新 session
    session_id = await create_session(user.id)
    set_session_cookie(response, session_id)

    return {"ok": True, "message": "密码修改成功"}
