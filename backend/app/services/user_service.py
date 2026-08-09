from __future__ import annotations

import logging
import secrets

from app.auth import AuthUser
from app.core.security import credential_digest, credential_prefix, decrypt_credential, encrypt_credential, hash_password
from app.core.time_utils import ms_to_iso, now_ms
from app.domain.errors import BadRequestError, ForbiddenError, NotFoundError
from app.domain.quota import machine_share_percent, usage_percent
from app.repositories import auth as auth_repo
from app.repositories.usage import list_usage_rows
from app.schemas import RpcAccessIssued, RpcAccessStatus, UserCreate, UserUpdate
from app.services.rate_limit_service import ensure_account_security_allowed
from app.services.task_broadcast import remove_connections_for_user
from app.services.usage_service import get_machine_headroom, visible_space_from_usage

logger = logging.getLogger(__name__)

DEFAULT_QUOTA_BYTES = 100 * 1024 * 1024 * 1024


def user_out(row: dict) -> dict:
    payload = {
        "id": row["id"],
        "username": row["username"],
        "is_admin": bool(row["is_admin"]),
        "quota": row["quota_bytes"],
        "is_initial_password": bool(row.get("is_initial_password", 0)),
    }
    for key in (
        "used_bytes",
        "reserved_bytes",
        "available_bytes",
        "usage_percent",
        "machine_share_percent",
    ):
        if key in row and row[key] is not None:
            payload[key] = row[key]
    return payload


def _usage_map(rows: list[dict]) -> dict[int, dict]:
    return {int(row["user_id"]): row for row in rows}


def _enrich_user_with_storage(
    row: dict,
    *,
    usage_row: dict | None,
    machine_headroom: int,
    total_used_bytes: int,
) -> dict:
    used = int((usage_row or {}).get("used_bytes") or 0)
    reserved = int((usage_row or {}).get("reserved_bytes") or 0)
    visible = visible_space_from_usage(
        {
            "quota_bytes": int(row["quota_bytes"]),
            "used_bytes": used,
            "reserved_bytes": reserved,
        },
        machine_headroom=machine_headroom,
    )
    enriched = dict(row)
    enriched.update(
        {
            "used_bytes": used,
            "reserved_bytes": reserved,
            "available_bytes": int(visible["available"]),
            "usage_percent": usage_percent(
                used_bytes=used,
                quota_bytes=int(row["quota_bytes"]),
            ),
            "machine_share_percent": machine_share_percent(
                used_bytes=used,
                total_used_bytes=total_used_bytes,
            ),
        }
    )
    return user_out(enriched)


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
        if not payload.is_admin:
            raise BadRequestError("首个用户必须是管理员")
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
    user: dict | None
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
                is_admin=True,
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

    return _enrich_user_with_storage(
        user,
        usage_row={"used_bytes": 0, "reserved_bytes": 0},
        machine_headroom=await get_machine_headroom(),
        total_used_bytes=0,
    )


async def list_users(admin_id: int | None) -> list[dict]:
    rows = await auth_repo.list_users()
    usage_by_user = _usage_map(await list_usage_rows())
    machine_headroom = await get_machine_headroom()
    total_used = sum(int(row.get("used_bytes") or 0) for row in usage_by_user.values())
    logger.debug("查询用户列表 admin_id=%s count=%s", admin_id, len(rows))
    return [
        _enrich_user_with_storage(
            row,
            usage_row=usage_by_user.get(int(row["id"])),
            machine_headroom=machine_headroom,
            total_used_bytes=total_used,
        )
        for row in rows
    ]


async def delete_user(*, actor: AuthUser, user_id: int, request_id: str) -> dict:
    if actor.id == user_id:
        raise BadRequestError("不能删除自己")
    target = await auth_repo.get_user_by_id_any(user_id)
    if target is None:
        raise NotFoundError("用户不存在")
    if bool(target["pending_delete"]):
        from app.services.deletion_cleanup import DeletionCleanupManager

        DeletionCleanupManager.wake()
        return {"ok": True, "state": "pending", "accepted": True}
    if bool(target["is_admin"]):
        users = await auth_repo.list_users()
        if sum(bool(user["is_admin"]) for user in users) <= 1:
            raise BadRequestError("不能删除最后一个管理员")

    try:
        deleted = await auth_repo.delete_user_as_admin(
            actor_id=actor.id,
            user_id=user_id,
        )
    except auth_repo.AdminActorInvalidError:
        raise ForbiddenError("需要管理员权限") from None
    except auth_repo.CannotMutateSelfError:
        logger.warning(
            "删除用户失败 actor_id=%s target_user_id=%s reason=self_delete request_id=%s",
            actor.id,
            user_id,
            request_id,
        )
        raise BadRequestError("不能删除自己") from None
    except auth_repo.LastAdminError:
        raise BadRequestError("不能删除最后一个管理员") from None
    except auth_repo.AdminMutationConflictError:
        raise BadRequestError("用户状态已变化，请重试") from None

    if deleted is None:
        logger.warning(
            "删除用户失败 actor_id=%s target_user_id=%s reason=not_found request_id=%s",
            actor.id,
            user_id,
            request_id,
        )
        raise NotFoundError("用户不存在")

    await remove_connections_for_user(user_id)
    from app.services.deletion_cleanup import DeletionCleanupManager

    DeletionCleanupManager.wake()
    logger.info(
        "删除用户已受理 actor_id=%s target_user_id=%s request_id=%s",
        actor.id,
        user_id,
        request_id,
    )
    return {"ok": True, "state": "pending", "accepted": True}


async def get_user(*, actor_id: int | None, user_id: int) -> dict:
    user = await auth_repo.get_user_by_id(user_id)
    if not user:
        raise NotFoundError("用户不存在")
    usage_by_user = _usage_map(await list_usage_rows())
    machine_headroom = await get_machine_headroom()
    total_used = sum(int(row.get("used_bytes") or 0) for row in usage_by_user.values())
    logger.debug("查询用户详情 admin_id=%s target_user_id=%s", actor_id, user_id)
    return _enrich_user_with_storage(
        user,
        usage_row=usage_by_user.get(int(user["id"])),
        machine_headroom=machine_headroom,
        total_used_bytes=total_used,
    )


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

    if (
        payload.username is not None
        and payload.username.lower() != str(user["username"]).lower()
        and payload.password is None
    ):
        raise BadRequestError("修改用户名时必须同时提供按新用户名派生的密码")

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
        changes["is_admin"] = payload.is_admin

    if payload.quota is not None:
        changes["quota_bytes"] = payload.quota

    try:
        updated = await auth_repo.update_user_as_admin(
            actor_id=actor.id,
            user_id=user_id,
            expected_username=str(user["username"]),
            **changes,
        )
    except auth_repo.DuplicateUserError:
        logger.warning(
            "更新用户冲突 actor_id=%s target_user_id=%s request_id=%s",
            actor.id,
            user_id,
            request_id,
        )
        raise BadRequestError("用户名已被占用") from None
    except auth_repo.AdminActorInvalidError:
        raise ForbiddenError("需要管理员权限") from None
    except auth_repo.CannotMutateSelfError:
        raise BadRequestError("不能取消自己的管理员权限") from None
    except auth_repo.LastAdminError:
        raise BadRequestError("不能降级最后一个管理员") from None
    except auth_repo.UsernamePasswordRequiredError:
        raise BadRequestError(
            "修改用户名时必须同时提供按新用户名派生的密码"
        ) from None
    except auth_repo.QuotaBelowUsageError:
        raise BadRequestError("用户配额不能低于当前已用空间与冻结空间之和") from None
    except auth_repo.AdminMutationConflictError:
        raise BadRequestError("用户状态已变化，请重试") from None

    if updated is None:
        raise NotFoundError("用户不存在")

    if payload.password is not None:
        await remove_connections_for_user(user_id)

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
    usage_by_user = _usage_map(await list_usage_rows())
    machine_headroom = await get_machine_headroom()
    total_used = sum(int(row.get("used_bytes") or 0) for row in usage_by_user.values())
    return _enrich_user_with_storage(
        updated,
        usage_row=usage_by_user.get(int(updated["id"])),
        machine_headroom=machine_headroom,
        total_used_bytes=total_used,
    )


async def get_rpc_access(user_id: int) -> RpcAccessStatus:
    db_user = await auth_repo.get_user_by_id(user_id)
    if not db_user:
        raise NotFoundError("用户不存在")
    enabled = db_user["rpc_secret_digest"] is not None
    secret: str | None = None
    if enabled and db_user.get("rpc_secret_encrypted"):
        secret = decrypt_credential(db_user["rpc_secret_encrypted"])
    logger.debug("查询RPC访问状态 user_id=%s enabled=%s", user_id, enabled)
    return RpcAccessStatus(
        enabled=enabled,
        secret_prefix=db_user["rpc_secret_prefix"] if enabled else None,
        secret=secret,
        created_at=ms_to_iso(db_user["rpc_secret_created_at_ms"]),
    )


async def _issue_rpc_secret(
    *, user_id: int, request_id: str, require_enabled: bool
) -> RpcAccessIssued:
    for _ in range(20):
        secret = "aria2_" + secrets.token_urlsafe(32)
        created_at_ms = now_ms()
        try:
            updated = await auth_repo.set_rpc_secret(
                user_id,
                credential_digest("rpc-secret", secret),
                credential_prefix(secret),
                created_at_ms,
                encrypted=encrypt_credential(secret),
                require_enabled=require_enabled,
            )
        except auth_repo.DuplicateCredentialError:
            continue
        if not updated:
            db_user = await auth_repo.get_user_by_id(user_id)
            if db_user is None:
                raise NotFoundError("用户不存在")
            raise BadRequestError("RPC 访问未开启，请先开启后再刷新")
        logger.info("签发RPC密钥 user_id=%s request_id=%s", user_id, request_id)
        return RpcAccessIssued(
            enabled=True,
            secret_prefix=secret[:16],
            secret=secret,
            created_at=ms_to_iso(created_at_ms),
        )
    raise BadRequestError("生成 RPC 密钥失败，请稍后重试")


async def set_rpc_access(
    *,
    user_id: int,
    enabled: bool,
    request_id: str,
) -> RpcAccessStatus | RpcAccessIssued:
    if enabled:
        return await _issue_rpc_secret(
            user_id=user_id, request_id=request_id, require_enabled=False
        )
    updated = await auth_repo.set_rpc_secret(user_id, None, None, None)
    if not updated:
        raise NotFoundError("用户不存在")
    logger.info("关闭RPC访问 user_id=%s request_id=%s", user_id, request_id)
    return RpcAccessStatus(enabled=False)


async def refresh_rpc_secret(*, user_id: int, request_id: str) -> RpcAccessIssued:
    db_user = await auth_repo.get_user_by_id(user_id)
    if not db_user:
        raise NotFoundError("用户不存在")
    if db_user["rpc_secret_digest"] is None:
        raise BadRequestError("RPC 访问未开启，请先开启后再刷新")
    return await _issue_rpc_secret(
        user_id=user_id, request_id=request_id, require_enabled=True
    )
