from __future__ import annotations

import logging

from app.auth import (
    AuthUser,
    clear_session,
    clear_user_sessions,
    create_session,
    user_from_row,
)
from app.core.config import settings
from app.services.rate_limit_service import (
    ACCOUNT_SECURITY_SCOPE,
    clear_account_security_failures,
    ensure_account_security_allowed,
    ensure_authenticated_allowed,
    record_account_security_failure,
)
from app.services.task_broadcast import (
    remove_connections_for_session,
    remove_connections_for_user,
)
from app.core.security import hash_password, verify_password, verify_password_constant_time
from app.domain.errors import BadRequestError, NotFoundError, UnauthorizedError
from app.repositories import auth as auth_repo

logger = logging.getLogger(__name__)


def user_response(user: AuthUser) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "is_admin": bool(user.is_admin),
        "quota": user.quota,
        "is_initial_password": bool(user.is_initial_password),
    }


async def login(
    *,
    username: str,
    password: str,
    client_ip: str,
    old_session_id: str | None,
    request_id: str,
) -> tuple[dict, str, int]:
    try:
        await ensure_account_security_allowed(
            client_ip,
            detail="登录尝试次数过多，请稍后再试",
        )
    except Exception:
        logger.warning(
            "登录限流触发 username=%s ip=%s request_id=%s",
            username,
            client_ip,
            request_id,
        )
        raise

    user_row = await auth_repo.get_user_by_username(username)
    password_hash = user_row["password_hash"] if user_row else None

    if not verify_password_constant_time(password, password_hash):
        await record_account_security_failure(client_ip)
        logger.warning(
            "登录失败 username=%s ip=%s request_id=%s",
            username,
            client_ip,
            request_id,
        )
        raise UnauthorizedError("用户名或密码错误")

    assert user_row is not None
    user = user_from_row(user_row)

    await clear_account_security_failures(client_ip)
    if old_session_id:
        await clear_session(old_session_id)
        await remove_connections_for_session(old_session_id)

    session_id = await create_session(user.id)
    logger.info(
        "登录成功 user_id=%s username=%s ip=%s request_id=%s",
        user.id,
        user.username,
        client_ip,
        request_id,
    )
    return user_response(user), session_id, user.id


async def logout(
    *,
    session_id: str | None,
    user_id: int,
    request_id: str,
) -> dict:
    if session_id:
        await clear_session(session_id)
        await remove_connections_for_session(session_id)
    logger.info("用户登出 user_id=%s request_id=%s", user_id, request_id)
    return {"ok": True}


async def change_password(
    *,
    user: AuthUser,
    old_password: str,
    new_password: str,
    request_id: str,
) -> tuple[dict, str]:
    user_id = user.id
    try:
        await ensure_authenticated_allowed(
            int(user_id),
            ACCOUNT_SECURITY_SCOPE,
            detail="操作过于频繁，请稍后再试",
        )
    except Exception:
        logger.warning("修改密码限流触发 user_id=%s request_id=%s", user.id, request_id)
        raise

    if not user.is_initial_password:
        if not verify_password(old_password, user.password_hash):
            logger.warning(
                "修改密码失败 user_id=%s reason=old_password_mismatch request_id=%s",
                user.id,
                request_id,
            )
            raise BadRequestError("旧密码错误")

        if old_password == new_password:
            raise BadRequestError("新密码不能与旧密码相同")

    db_user = await auth_repo.update_user(
        user.id,
        password_hash=hash_password(new_password),
        is_initial_password=False,
    )
    if not db_user:
        raise NotFoundError("用户不存在")

    await clear_user_sessions(user.id)
    await remove_connections_for_user(user.id)
    session_id = await create_session(user.id)
    logger.info("修改密码成功 user_id=%s request_id=%s", user.id, request_id)
    return {"ok": True, "message": "密码修改成功"}, session_id
