from __future__ import annotations

from dataclasses import dataclass
import logging
import secrets
import time
from uuid import uuid4

from fastapi import Depends, HTTPException, Request, Response, status

from app.core.config import settings
from app.repositories import auth as auth_repo


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class AuthUser:
    id: int
    username: str
    password_hash: str
    is_admin: bool
    quota: int
    quota_bytes: int
    rpc_secret: str | None
    rpc_secret_created_at: str | None
    rpc_secret_created_at_ms: int | None
    is_initial_password: bool


def now_ms() -> int:
    return int(time.time() * 1000)


def ms_to_iso(timestamp_ms: int | None) -> str | None:
    if timestamp_ms is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.gmtime(timestamp_ms / 1000))


def user_from_row(row: dict) -> AuthUser:
    quota_bytes = int(row["quota_bytes"])
    return AuthUser(
        id=int(row["id"]),
        username=str(row["username"]),
        password_hash=str(row["password_hash"]),
        is_admin=bool(row["is_admin"]),
        quota=quota_bytes,
        quota_bytes=quota_bytes,
        rpc_secret=row.get("rpc_secret"),
        rpc_secret_created_at=ms_to_iso(row.get("rpc_secret_created_at_ms")),
        rpc_secret_created_at_ms=row.get("rpc_secret_created_at_ms"),
        is_initial_password=bool(row["is_initial_password"]),
    )


async def get_user_by_rpc_secret(secret: str) -> dict | None:
    """Return an RPC-authenticated user shape for a valid RPC secret."""
    rows = await auth_repo.list_users_by_rpc_secret(secret, limit=2)

    if len(rows) != 1:
        secrets.compare_digest(secret, "dummy_secret_placeholder_value")
        if len(rows) > 1:
            logger.error("RPC secret 冲突，拒绝鉴权 secret_prefix=%s***", secret[:8])
        return None

    user = rows[0]
    if not secrets.compare_digest(secret, str(user["rpc_secret"] or "")):
        return None

    quota_bytes = int(user["quota_bytes"])
    return {
        "id": int(user["id"]),
        "username": str(user["username"]),
        "is_admin": bool(user["is_admin"]),
        "quota": quota_bytes,
        "quota_bytes": quota_bytes,
    }


async def create_session(user_id: int) -> str:
    session_id = uuid4().hex
    expires_at_ms = now_ms() + settings.session_ttl_seconds * 1000
    await auth_repo.create_session(session_id, user_id, expires_at_ms)
    return session_id


async def clear_session(session_id: str) -> None:
    await auth_repo.delete_session(session_id)


async def clear_user_sessions(user_id: int) -> int:
    """清除指定用户的所有 session

    Args:
        user_id: 用户 ID

    Returns:
        被删除的 session 数量
    """
    return await auth_repo.delete_user_sessions(user_id)


async def get_user_by_session(session_id: str | None) -> AuthUser | None:
    if not session_id:
        return None
    row = await auth_repo.get_session_user(session_id)
    if not row:
        return None
    if int(row["session_expires_at_ms"]) < now_ms():
        await auth_repo.delete_session(session_id)
        logger.info("会话已过期并自动清理")
        return None
    return user_from_row(row)


async def require_user(request: Request) -> AuthUser:
    session_id = request.cookies.get(settings.session_cookie_name)
    user = await get_user_by_session(session_id)
    if not user:
        request.state.auth_user_id = None
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未登录")
    request.state.auth_user_id = user.id
    return user


async def require_admin(user: AuthUser = Depends(require_user)) -> AuthUser:
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限")
    return user


def set_session_cookie(response: Response, session_id: str) -> None:
    response.set_cookie(
        settings.session_cookie_name,
        session_id,
        httponly=True,
        secure=not settings.debug,
        samesite="lax",
        max_age=settings.session_ttl_seconds,
    )
