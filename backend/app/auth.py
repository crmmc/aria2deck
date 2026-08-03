from __future__ import annotations

from dataclasses import dataclass
import logging
import secrets
import time
from uuid import uuid4

from fastapi import Depends, HTTPException, Request, Response, status

from app.core.config import settings
from app.core.request_rate_guard import RateLimitScope, ensure_authenticated_allowed
from app.core.security import credential_digest_candidates
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
        is_initial_password=bool(row["is_initial_password"]),
    )


async def get_user_by_rpc_secret(secret: str) -> dict | None:
    """Return an RPC-authenticated user shape for a valid ``token:<secret>`` value."""
    current_digest, previous_digest = credential_digest_candidates("rpc-secret", secret)
    rows = await auth_repo.list_users_by_rpc_secret_digests(
        current_digest, previous_digest, limit=2
    )
    if len(rows) != 1:
        secrets.compare_digest(current_digest, "0" * len(current_digest))
        if len(rows) > 1:
            logger.error("RPC secret 摘要冲突，拒绝鉴权")
        return None

    user = rows[0]
    stored_digest = str(user["rpc_secret_digest"] or "")
    if secrets.compare_digest(stored_digest, current_digest):
        pass
    elif previous_digest and secrets.compare_digest(stored_digest, previous_digest):
        try:
            promoted = await auth_repo.promote_rpc_secret_digest(
                int(user["id"]), previous_digest, current_digest
            )
        except auth_repo.DuplicateCredentialError:
            return None
        if not promoted:
            current_rows = await auth_repo.list_users_by_rpc_secret_digests(
                current_digest, None, limit=2
            )
            if len(current_rows) != 1 or current_rows[0]["id"] != user["id"]:
                return None
            user = current_rows[0]
    else:
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


def _set_request_auth_state(request: Request, user: AuthUser | None, method: str | None) -> None:
    request.state.auth_user_id = user.id if user else None
    request.state.auth_method = method
    request.state.auth_token_id = None


async def require_session_user(request: Request) -> AuthUser:
    session_id = request.cookies.get(settings.session_cookie_name)
    user = await get_user_by_session(session_id)
    if not user:
        _set_request_auth_state(request, None, None)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未登录")
    _set_request_auth_state(request, user, "session")
    return user


async def require_api_user(request: Request) -> AuthUser:
    authorization = request.headers.get("authorization")
    if authorization is None:
        return await require_session_user(request)
    scheme, separator, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not separator or not token or any(char.isspace() for char in token):
        _set_request_auth_state(request, None, None)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="API Token 无效")
    current_digest, previous_digest = credential_digest_candidates("api-token", token)
    row = await auth_repo.use_api_token_digests(current_digest, previous_digest)
    if not row or bool(row["is_admin"]):
        _set_request_auth_state(request, None, None)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="API Token 无效")
    user = user_from_row(row)
    _set_request_auth_state(request, user, "bearer")
    request.state.auth_token_id = int(row["api_token_id"])
    return user


async def require_limited_api_user(request: Request) -> AuthUser:
    """Authenticate a JSON API caller and apply the shared user request bucket."""
    user = await require_api_user(request)
    await ensure_authenticated_allowed(user.id, RateLimitScope.AUTHENTICATED_API)
    return user


async def require_limited_session_user(request: Request) -> AuthUser:
    """Authenticate a session-only JSON caller and apply the shared user bucket."""
    user = await require_session_user(request)
    await ensure_authenticated_allowed(user.id, RateLimitScope.AUTHENTICATED_API)
    return user


async def require_limited_admin(request: Request) -> AuthUser:
    """Require an administrator session after applying the shared user bucket."""
    user = await require_limited_session_user(request)
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限")
    return user


async def require_user(request: Request) -> AuthUser:
    """Backward-compatible user dependency: Cookie session or restricted Bearer API Token."""
    return await require_api_user(request)


async def require_admin(user: AuthUser = Depends(require_session_user)) -> AuthUser:
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
