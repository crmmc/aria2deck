from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import Depends, HTTPException, Request, Response, status

from app.core.config import settings
from app.db import execute, fetch_one


def create_session(user_id: int) -> str:
    session_id = uuid4().hex
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=settings.session_ttl_seconds)).isoformat()
    execute(
        "INSERT INTO sessions (id, user_id, expires_at) VALUES (?, ?, ?)",
        [session_id, user_id, expires_at],
    )
    return session_id


def clear_session(session_id: str) -> None:
    execute("DELETE FROM sessions WHERE id = ?", [session_id])


def get_user_by_session(session_id: str | None) -> dict | None:
    if not session_id:
        return None
    session = fetch_one("SELECT * FROM sessions WHERE id = ?", [session_id])
    if not session:
        return None
    expires_at = datetime.fromisoformat(session["expires_at"])
    # 确保有时区信息，避免 naive datetime 与 aware datetime 比较
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < datetime.now(timezone.utc):
        clear_session(session_id)
        return None
    user = fetch_one("SELECT * FROM users WHERE id = ?", [session["user_id"]])
    return user


def require_user(request: Request) -> dict:
    session_id = request.cookies.get(settings.session_cookie_name)
    user = get_user_by_session(session_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    return user


def require_admin(user: dict = Depends(require_user)) -> dict:
    if not user.get("is_admin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin required")
    return user


def set_session_cookie(response: Response, session_id: str) -> None:
    response.set_cookie(
        settings.session_cookie_name,
        session_id,
        httponly=True,
        samesite="lax",
        max_age=settings.session_ttl_seconds,
    )
