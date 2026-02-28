from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
from uuid import uuid4

from fastapi import Depends, HTTPException, Request, Response, status
from sqlmodel import select

from app.core.config import settings
from app.database import get_session
from app.models import Session, User


logger = logging.getLogger(__name__)


async def create_session(user_id: int) -> str:
    session_id = uuid4().hex
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=settings.session_ttl_seconds)).isoformat()
    async with get_session() as db:
        session = Session(id=session_id, user_id=user_id, expires_at=expires_at)
        db.add(session)
    return session_id


async def clear_session(session_id: str) -> None:
    async with get_session() as db:
        result = await db.exec(select(Session).where(Session.id == session_id))
        session = result.first()
        if session:
            await db.delete(session)


async def clear_user_sessions(user_id: int) -> int:
    """清除指定用户的所有 session

    Args:
        user_id: 用户 ID

    Returns:
        被删除的 session 数量
    """
    async with get_session() as db:
        result = await db.exec(select(Session).where(Session.user_id == user_id))
        sessions = result.all()
        count = len(sessions)
        for session in sessions:
            await db.delete(session)
        return count


async def get_user_by_session(session_id: str | None) -> User | None:
    if not session_id:
        return None
    async with get_session() as db:
        result = await db.exec(select(Session).where(Session.id == session_id))
        session = result.first()
        if not session:
            return None
        try:
            expires_at = datetime.fromisoformat(session.expires_at)
        except ValueError:
            logger.warning("会话过期时间格式无效 session_id=%s", session_id)
            await db.delete(session)
            return None
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at < datetime.now(timezone.utc):
            await db.delete(session)
            logger.info("会话已过期并自动清理")
            return None
        result = await db.exec(select(User).where(User.id == session.user_id))
        user = result.first()
        return user


async def require_user(request: Request) -> User:
    session_id = request.cookies.get(settings.session_cookie_name)
    user = await get_user_by_session(session_id)
    if not user:
        request.state.auth_user_id = None
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    request.state.auth_user_id = user.id
    return user


async def require_admin(user: User = Depends(require_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin required")
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
