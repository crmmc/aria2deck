"""用户管理接口模块"""
from __future__ import annotations

import logging
import secrets
import time

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.exc import IntegrityError

from app.auth import AuthUser, require_admin, require_user
from app.core.request_rate_guard import client_ip_from_request, ensure_account_security_allowed
from app.core.security import hash_password
from app.repositories import auth as auth_repo
from app.schemas import RpcAccessStatus, RpcAccessToggle, UserCreate, UserOut, UserUpdate


router = APIRouter(prefix="/api/users", tags=["users"])
logger = logging.getLogger(__name__)

DEFAULT_QUOTA_BYTES = 100 * 1024 * 1024 * 1024


def now_ms() -> int:
    return int(time.time() * 1000)


def ms_to_iso(timestamp_ms: int | None) -> str | None:
    if timestamp_ms is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.gmtime(timestamp_ms / 1000))


def user_out(row: dict) -> dict:
    return {
        "id": row["id"],
        "username": row["username"],
        "is_admin": bool(row["is_admin"]),
        "quota": row["quota_bytes"],
        "is_initial_password": bool(row.get("is_initial_password", 0)),
    }


async def _generate_unique_rpc_secret(max_attempts: int = 20) -> str:
    for _ in range(max_attempts):
        # The database uniqueness constraint is the final guard; collisions are practically irrelevant here.
        return "aria2_" + secrets.token_urlsafe(32)

    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="生成 RPC 密钥失败，请稍后重试",
    )


async def _has_any_user() -> bool:
    return await auth_repo.has_any_user()


@router.post("", response_model=UserOut)
async def create_user(payload: UserCreate, request: Request) -> dict:
    """创建用户

    首次调用（无用户时）无需认证，之后需要管理员权限。
    """
    client_ip = client_ip_from_request(request)
    request_id = getattr(request.state, "request_id", "-")
    has_users = await _has_any_user()

    if not has_users:
        try:
            await ensure_account_security_allowed(
                client_ip,
                detail="请求过于频繁，请稍后再试",
            )
        except HTTPException:
            logger.warning(
                "创建首个用户被限流 username=%s ip=%s request_id=%s",
                payload.username,
                client_ip,
                request_id,
            )
            raise

    actor_id = None
    if has_users:
        admin = await require_admin(await require_user(request))
        actor_id = admin.id
        if await auth_repo.get_user_by_username(payload.username):
            logger.warning(
                "创建用户失败 username=%s reason=duplicate request_id=%s",
                payload.username,
                request_id,
            )
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="用户名已存在")

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
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限")
    except IntegrityError:
        logger.warning("创建用户冲突 username=%s request_id=%s", payload.username, request_id)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="用户名已存在") from None

    if has_users:
        logger.info(
            "创建用户成功 actor_id=%s user_id=%s username=%s is_admin=%s request_id=%s",
            actor_id,
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


@router.get("", response_model=list[UserOut])
async def list_users(admin: AuthUser = Depends(require_admin)) -> list[dict]:
    """获取用户列表（管理员）"""
    rows = await auth_repo.list_users()
    logger.debug("查询用户列表 admin_id=%s count=%s", admin.id, len(rows))
    return [user_out(row) for row in rows]


@router.delete("/{user_id}")
async def delete_user(
    request: Request,
    user_id: int,
    admin: AuthUser = Depends(require_admin),
) -> dict:
    """删除用户（管理员）"""
    request_id = getattr(request.state, "request_id", "-")

    if user_id == admin.id:
        logger.warning(
            "删除用户失败 actor_id=%s target_user_id=%s reason=self_delete request_id=%s",
            admin.id,
            user_id,
            request_id,
        )
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="不能删除自己")

    user = await auth_repo.get_user_by_id(user_id)
    if not user:
        logger.warning(
            "删除用户失败 actor_id=%s target_user_id=%s reason=not_found request_id=%s",
            admin.id,
            user_id,
            request_id,
        )
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")

    await auth_repo.delete_user_owned_rows(user_id)
    await auth_repo.delete_user(user_id)

    logger.info("删除用户成功 actor_id=%s target_user_id=%s request_id=%s", admin.id, user_id, request_id)
    return {"ok": True}


@router.get("/{user_id}", response_model=UserOut)
async def get_user(user_id: int, admin: AuthUser = Depends(require_admin)) -> dict:
    """获取单个用户详情（管理员）"""
    user = await auth_repo.get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")
    logger.debug("查询用户详情 admin_id=%s target_user_id=%s", admin.id, user_id)
    return user_out(user)


@router.put("/{user_id}", response_model=UserOut)
async def update_user(
    user_id: int,
    payload: UserUpdate,
    request: Request,
    admin: AuthUser = Depends(require_admin),
) -> dict:
    """更新用户信息（管理员）"""
    request_id = getattr(request.state, "request_id", "-")
    user = await auth_repo.get_user_by_id(user_id)
    if not user:
        logger.warning(
            "更新用户失败 actor_id=%s target_user_id=%s reason=not_found request_id=%s",
            admin.id,
            user_id,
            request_id,
        )
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")

    changes: dict = {}
    if payload.username is not None:
        existing = await auth_repo.get_user_by_username(payload.username)
        if existing and existing["id"] != user_id:
            logger.warning(
                "更新用户失败 actor_id=%s target_user_id=%s reason=username_taken request_id=%s",
                admin.id,
                user_id,
                request_id,
            )
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="用户名已被占用")
        changes["username"] = payload.username

    if payload.password is not None:
        changes["password_hash"] = hash_password(payload.password)
        changes["is_initial_password"] = user_id != admin.id

    if payload.is_admin is not None:
        if user_id == admin.id and not payload.is_admin:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="不能取消自己的管理员权限")
        if bool(user["is_admin"]) and not payload.is_admin:
            admin_count = await auth_repo.count_admins()
            if admin_count <= 1:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="不能降级最后一个管理员")
        changes["is_admin"] = payload.is_admin

    if payload.quota is not None:
        changes["quota_bytes"] = payload.quota

    try:
        updated = await auth_repo.update_user(user_id, **changes)
    except IntegrityError:
        logger.warning(
            "更新用户冲突 actor_id=%s target_user_id=%s request_id=%s",
            admin.id,
            user_id,
            request_id,
        )
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="用户名已被占用") from None

    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")

    if payload.password is not None:
        await auth_repo.delete_user_sessions(user_id)

    logger.info(
        "更新用户成功 actor_id=%s target_user_id=%s set_username=%s set_password=%s set_is_admin=%s set_quota=%s request_id=%s",
        admin.id,
        user_id,
        payload.username is not None,
        payload.password is not None,
        payload.is_admin is not None,
        payload.quota is not None,
        request_id,
    )
    return user_out(updated)


# ============ RPC 访问管理接口 ============


@router.get("/me/rpc-access", response_model=RpcAccessStatus)
async def get_rpc_access(user: AuthUser = Depends(require_user)) -> RpcAccessStatus:
    """获取当前用户的 RPC 访问状态"""
    db_user = await auth_repo.get_user_by_id(user.id)
    if not db_user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")

    logger.debug("查询RPC访问状态 user_id=%s enabled=%s", user.id, db_user["rpc_secret"] is not None)
    return RpcAccessStatus(
        enabled=db_user["rpc_secret"] is not None,
        secret=db_user["rpc_secret"],
        created_at=ms_to_iso(db_user["rpc_secret_created_at_ms"]),
    )


@router.put("/me/rpc-access", response_model=RpcAccessStatus)
async def set_rpc_access(
    payload: RpcAccessToggle,
    request: Request,
    user: AuthUser = Depends(require_user),
) -> RpcAccessStatus:
    """开启或关闭 RPC 访问"""
    request_id = getattr(request.state, "request_id", "-")
    db_user = await auth_repo.get_user_by_id(user.id)
    if not db_user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")

    if payload.enabled:
        new_secret = await _generate_unique_rpc_secret()
        created_at_ms = now_ms()
        await auth_repo.update_user(user.id, rpc_secret=new_secret, rpc_secret_created_at_ms=created_at_ms)
        logger.info("开启RPC访问 user_id=%s request_id=%s", user.id, request_id)
        return RpcAccessStatus(enabled=True, secret=new_secret, created_at=ms_to_iso(created_at_ms))

    await auth_repo.update_user(user.id, rpc_secret=None, rpc_secret_created_at_ms=None)
    logger.info("关闭RPC访问 user_id=%s request_id=%s", user.id, request_id)
    return RpcAccessStatus(enabled=False, secret=None, created_at=None)


@router.post("/me/rpc-access/refresh", response_model=RpcAccessStatus)
async def refresh_rpc_secret(request: Request, user: AuthUser = Depends(require_user)) -> RpcAccessStatus:
    """刷新 RPC Secret（旧的立即失效）"""
    request_id = getattr(request.state, "request_id", "-")
    db_user = await auth_repo.get_user_by_id(user.id)
    if not db_user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")
    if db_user["rpc_secret"] is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="RPC 访问未开启，请先开启后再刷新")

    new_secret = await _generate_unique_rpc_secret()
    created_at_ms = now_ms()
    await auth_repo.update_user(user.id, rpc_secret=new_secret, rpc_secret_created_at_ms=created_at_ms)
    logger.info("刷新RPC密钥 user_id=%s request_id=%s", user.id, request_id)
    return RpcAccessStatus(enabled=True, secret=new_secret, created_at=ms_to_iso(created_at_ms))
