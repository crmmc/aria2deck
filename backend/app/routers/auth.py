from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from app.auth import clear_session, create_session, require_user, set_session_cookie
from app.core.config import settings
from app.core.rate_limit import login_limiter
from app.core.security import verify_password
from app.db import fetch_one
from app.schemas import LoginRequest, UserOut


router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login", response_model=UserOut)
def login(payload: LoginRequest, request: Request, response: Response) -> dict:
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
    session_id = create_session(user["id"])
    set_session_cookie(response, session_id)
    return {
        "id": user["id"],
        "username": user["username"],
        "is_admin": bool(user["is_admin"]),
        "quota": user["quota"]
    }


@router.post("/logout")
def logout(request: Request, response: Response, user: dict = Depends(require_user)) -> dict:
    session_id = request.cookies.get(settings.session_cookie_name)
    if session_id:
        clear_session(session_id)
    response.delete_cookie(settings.session_cookie_name)
    return {"ok": True}


@router.get("/me", response_model=UserOut)
def me(user: dict = Depends(require_user)) -> dict:
    return {
        "id": user["id"],
        "username": user["username"],
        "is_admin": bool(user["is_admin"]),
        "quota": user["quota"]
    }
