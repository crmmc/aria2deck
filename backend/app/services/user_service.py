from __future__ import annotations

import logging
import secrets

from app.auth import AuthUser
from app.core.security import hash_password
from app.core.time_utils import ms_to_iso, now_ms
from app.domain.errors import BadRequestError, ForbiddenError, NotFoundError
from app.repositories import auth as auth_repo
from app.schemas import RpcAccessStatus, UserCreate, UserUpdate
from app.services.rate_limit_service import ensure_account_security_allowed

logger = logging.getLogger(__name__)

DEFAULT_QUOTA_BYTES = 100 * 1024 * 1024 * 1024


def user_out(row: dict) -> dict:
    return {
        "id": row["id"],
        "username": row["username"],
        "is_admin": bool(row["is_admin"]),
        "quota": row["quota_bytes"],
        "is_initial_password": bool(row.get("is_initial_password", 0)),
    }


async def has_any_user() -> bool:
    return await auth_repo.has_any_user()


async def create_user(
    *,
    payload: UserCreate,
    client_ip: str,
    request_id: str,
    admin: AuthUser | None,
) -> dict:
    has_users = await has_any_user()

    if not has_users:
        try:
            await ensure_account_security_allowed(
                client_ip,
                detail="请求过于频繁，请稍后再试",
            )
        except Exception:
            logger.warning(
                "创建首个用户被限流 username=%s ip=%s request_id=%s",
                payload.username,
                client_ip,
                request_id,
            )
            raise

    if has_users:
        if admin is None:
            raise ForbiddenError("需要管理员权限")
        if await auth_repo.get_user_by_username(payload.username):
            logger.warning(
                "创建用户失败 username=%s reason=duplicate request_id=%s",
                payload.username,
                request_id,
            )
            raise BadRequestError("用户名已存在")

    quota = payload.quota if payload.quota is not None else DEFAULT_QUOTA_BYTES
    try:
        if has_users:
            user = await auth_repo.create_user(
                username=payload.username,
                password_hash=hash_password(payload.password),
                is_admin=payload.is_admin,
                is_initial_password=True,
                quota_bytes=quota,
            )
        else:
            user = await auth_repo.create_first_user_if_none(
                username=payload.username,
                password_hash=hash_password(payload.password),
                is_admin=payload.is_admin,
                is_initial_password=True,
                quota_bytes=quota,
            )
            if user is None:
                logger.warning(
                    "创建首个用户失败 username=%s reason=race_or_permission request_id=%s",
                    payload.username,
                    request_id,
                )
                raise ForbiddenError("需要管理员权限")
    except auth_repo.DuplicateUserError:
        logger.warning("创建用户冲突 username=%s request_id=%s", payload.username, request_id)
        raise BadRequestError("用户名已存在") from None

    if has_users:
        logger.info(
            "创建用户成功 actor_id=%s user_id=%s username=%s is_admin=%s request_id=%s",
            admin.id if admin else None,
            user["id"],
            user["username"],
            bool(user["is_admin"]),
            request_id,
        )
    else:
        logger.info(
            "创建首个用户成功 user_id=%s username=%s is_admin=%s ip=%s request_id=%s",
            user["id"],
            user["username"],
            bool(user["is_admin"]),
            client_ip,
            request_id,
        )

    return user_out(user)


async def list_users(admin_id: int | None) -> list[dict]:
    rows = await auth_repo.list_users()
    logger.debug("查询用户列表 admin_id=%s count=%s", admin_id, len(rows))
    return [user_out(row) for row in rows]


async def delete_user(*, actor: AuthUser, user_id: int, request_id: str) -> dict:
    if user_id == actor.id:
        logger.warning(
            "删除用户失败 actor_id=%s target_user_id=%s reason=self_delete request_id=%s",
            actor.id,
            user_id,
            request_id,
        )
        raise BadRequestError("不能删除自己")

    user = await auth_repo.get_user_by_id(user_id)
    if not user:
        logger.warning(
            "删除用户失败 actor_id=%s target_user_id=%s reason=not_found request_id=%s",
            actor.id,
            user_id,
            request_id,
        )
        raise NotFoundError("用户不存在")

    await auth_repo.delete_user_owned_rows(user_id)
    await auth_repo.delete_user(user_id)

    logger.info("删除用户成功 actor_id=%s target_user_id=%s request_id=%s", actor.id, user_id, request_id)
    return {"ok": True}


async def get_user(*, actor_id: int | None, user_id: int) -> dict:
    user = await auth_repo.get_user_by_id(user_id)
    if not user:
        raise NotFoundError("用户不存在")
    logger.debug("查询用户详情 admin_id=%s target_user_id=%s", actor_id, user_id)
    return user_out(user)


async def update_user(
    *,
    actor: AuthUser,
    user_id: int,
    payload: UserUpdate,
    request_id: str,
) -> dict:
    user = await auth_repo.get_user_by_id(user_id)
    if not user:
        logger.warning(
            "更新用户失败 actor_id=%s target_user_id=%s reason=not_found request_id=%s",
            actor.id,
            user_id,
            request_id,
        )
        raise NotFoundError("用户不存在")

    changes: dict = {}
    if payload.username is not None:
        existing = await auth_repo.get_user_by_username(payload.username)
        if existing and existing["id"] != user_id:
            logger.warning(
                "更新用户失败 actor_id=%s target_user_id=%s reason=username_taken request_id=%s",
                actor.id,
                user_id,
                request_id,
            )
            raise BadRequestError("用户名已被占用")
        changes["username"] = payload.username

    if payload.password is not None:
        changes["password_hash"] = hash_password(payload.password)
        changes["is_initial_password"] = user_id != actor.id

    if payload.is_admin is not None:
        if user_id == actor.id and not payload.is_admin:
            raise BadRequestError("不能取消自己的管理员权限")
        if bool(user["is_admin"]) and not payload.is_admin:
            admin_count = await auth_repo.count_admins()
            if admin_count <= 1:
                raise BadRequestError("不能降级最后一个管理员")
        changes["is_admin"] = payload.is_admin

    if payload.quota is not None:
        changes["quota_bytes"] = payload.quota

    try:
        updated = await auth_repo.update_user(user_id, **changes)
    except auth_repo.DuplicateUserError:
        logger.warning(
            "更新用户冲突 actor_id=%s target_user_id=%s request_id=%s",
            actor.id,
            user_id,
            request_id,
        )
        raise BadRequestError("用户名已被占用") from None

    if updated is None:
        raise NotFoundError("用户不存在")

    if payload.password is not None:
        await auth_repo.delete_user_sessions(user_id)

    logger.info(
        "更新用户成功 actor_id=%s target_user_id=%s set_username=%s set_password=%s set_is_admin=%s set_quota=%s request_id=%s",
        actor.id,
        user_id,
        payload.username is not None,
        payload.password is not None,
        payload.is_admin is not None,
        payload.quota is not None,
        request_id,
    )
    return user_out(updated)


async def generate_unique_rpc_secret(max_attempts: int = 20) -> str:
    for _ in range(max_attempts):
        return "aria2_" + secrets.token_urlsafe(32)
    raise BadRequestError("生成 RPC 密钥失败，请稍后重试")


async def get_rpc_access(user_id: int) -> RpcAccessStatus:
    db_user = await auth_repo.get_user_by_id(user_id)
    if not db_user:
        raise NotFoundError("用户不存在")

    logger.debug("查询RPC访问状态 user_id=%s enabled=%s", user_id, db_user["rpc_secret"] is not None)
    return RpcAccessStatus(
        enabled=db_user["rpc_secret"] is not None,
        secret=db_user["rpc_secret"],
        created_at=ms_to_iso(db_user["rpc_secret_created_at_ms"]),
    )


async def set_rpc_access(
    *,
    user_id: int,
    enabled: bool,
    request_id: str,
) -> RpcAccessStatus:
    db_user = await auth_repo.get_user_by_id(user_id)
    if not db_user:
        raise NotFoundError("用户不存在")

    if enabled:
        new_secret = await generate_unique_rpc_secret()
        created_at_ms = now_ms()
        await auth_repo.update_user(
            user_id, rpc_secret=new_secret, rpc_secret_created_at_ms=created_at_ms
        )
        logger.info("开启RPC访问 user_id=%s request_id=%s", user_id, request_id)
        return RpcAccessStatus(enabled=True, secret=new_secret, created_at=ms_to_iso(created_at_ms))

    await auth_repo.update_user(user_id, rpc_secret=None, rpc_secret_created_at_ms=None)
    logger.info("关闭RPC访问 user_id=%s request_id=%s", user_id, request_id)
    return RpcAccessStatus(enabled=False, secret=None, created_at=None)


async def refresh_rpc_secret(*, user_id: int, request_id: str) -> RpcAccessStatus:
    db_user = await auth_repo.get_user_by_id(user_id)
    if not db_user:
        raise NotFoundError("用户不存在")
    if db_user["rpc_secret"] is None:
        raise BadRequestError("RPC 访问未开启，请先开启后再刷新")

    new_secret = await generate_unique_rpc_secret()
    created_at_ms = now_ms()
    await auth_repo.update_user(
        user_id, rpc_secret=new_secret, rpc_secret_created_at_ms=created_at_ms
    )
    logger.info("刷新RPC密钥 user_id=%s request_id=%s", user_id, request_id)
    return RpcAccessStatus(enabled=True, secret=new_secret, created_at=ms_to_iso(created_at_ms))
