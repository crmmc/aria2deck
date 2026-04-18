"""用户管理接口模块"""
import logging
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import text, func
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from app.auth import clear_user_sessions, require_admin, require_user
from app.core.request_rate_guard import client_ip_from_request, ensure_account_security_allowed
from app.core.security import hash_password
from app.database import get_session
from app.models import User, Session as SessionModel, Task, PackTask, UserFile, UserTaskSubscription, TaskHistory, ShareLink
from app.schemas import RpcAccessStatus, RpcAccessToggle, UserCreate, UserOut, UserUpdate


router = APIRouter(prefix="/api/users", tags=["users"])
logger = logging.getLogger(__name__)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _generate_unique_rpc_secret(db, max_attempts: int = 20) -> str:
    for _ in range(max_attempts):
        candidate = "aria2_" + secrets.token_urlsafe(32)
        result = await db.exec(select(User.id).where(User.rpc_secret == candidate))
        if result.first() is None:
            return candidate

    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="生成 RPC 密钥失败，请稍后重试",
    )


async def _has_any_user() -> bool:
    async with get_session() as db:
        result = await db.exec(select(User).limit(1))
        return result.first() is not None


@router.post("", response_model=UserOut)
async def create_user(payload: UserCreate, request: Request) -> dict:
    """创建用户

    首次调用（无用户时）无需认证，之后需要管理员权限。
    """
    # 获取客户端 IP 用于限流
    client_ip = client_ip_from_request(request)
    request_id = getattr(request.state, "request_id", "-")

    # 单次读取状态，避免 TOCTOU
    has_users = await _has_any_user()

    # 首次创建用户时的 IP 限流（防止滥用）
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

    if has_users:
        await require_admin(await require_user(request))

        async with get_session() as db:
            # 检查用户名是否已存在
            result = await db.exec(select(User).where(User.username == payload.username))
            if result.first():
                logger.warning(
                    "创建用户失败 username=%s reason=duplicate request_id=%s",
                    payload.username,
                    request_id,
                )
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="用户名已存在"
                )

            # 默认配额 100GB
            quota = payload.quota if payload.quota is not None else 107374182400

            user = User(
                username=payload.username,
                password_hash=hash_password(payload.password),
                is_admin=payload.is_admin,
                is_initial_password=True,  # 新用户需要自行修改密码
                quota=quota,
                created_at=utc_now()
            )
            db.add(user)
            try:
                await db.commit()
                await db.refresh(user)
            except IntegrityError:
                await db.rollback()
                logger.warning(
                    "创建用户冲突 username=%s request_id=%s",
                    payload.username,
                    request_id,
                )
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="用户名已存在"
                )

            logger.info(
                "创建用户成功 actor_id=%s user_id=%s username=%s is_admin=%s request_id=%s",
                request.state.auth_user_id,
                user.id,
                user.username,
                user.is_admin,
                request_id,
            )

            return {
                "id": user.id,
                "username": user.username,
                "is_admin": user.is_admin,
                "quota": user.quota
            }

    # 首次创建用户：仅允许第一个请求插入
    async with get_session() as db:
        quota = payload.quota if payload.quota is not None else 107374182400
        now = utc_now()
        result = await db.execute(
            text(
                """
                INSERT INTO users (
                    username, password_hash, is_admin,
                    quota, created_at, is_initial_password
                )
                SELECT
                    :username, :password_hash, :is_admin,
                    :quota, :created_at, 1
                WHERE NOT EXISTS (SELECT 1 FROM users)
                """
            ),
            {
                "username": payload.username,
                "password_hash": hash_password(payload.password),
                "is_admin": payload.is_admin,
                "quota": quota,
                "created_at": now,
            },
        )

        result = await db.exec(select(User).where(User.username == payload.username))
        user = result.first()
        if not user:
            logger.warning(
                "创建首个用户失败 username=%s reason=race_or_permission request_id=%s",
                payload.username,
                request_id,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="需要管理员权限"
            )

        logger.info(
            "创建首个用户成功 user_id=%s username=%s is_admin=%s ip=%s request_id=%s",
            user.id,
            user.username,
            user.is_admin,
            client_ip,
            request_id,
        )

        return {
            "id": user.id,
            "username": user.username,
            "is_admin": user.is_admin,
            "quota": user.quota
        }


@router.get("", response_model=list[UserOut])
async def list_users(admin: User = Depends(require_admin)) -> list[dict]:
    """获取用户列表（管理员）"""
    async with get_session() as db:
        result = await db.exec(select(User))
        users = result.all()
        logger.debug("查询用户列表 admin_id=%s count=%s", admin.id, len(users))
        return [{
            "id": u.id,
            "username": u.username,
            "is_admin": u.is_admin,
            "quota": u.quota
        } for u in users]


@router.delete("/{user_id}")
async def delete_user(
    request: Request,
    user_id: int,
    admin: User = Depends(require_admin)
) -> dict:
    """删除用户（管理员）

    删除用户时会同时删除：
    - 用户的所有会话
    - 用户的所有下载任务记录
    - 用户的所有打包任务记录
    - 用户的所有文件引用（正确递减 ref_count）

    注意: 不能删除自己
    """
    request_id = getattr(request.state, "request_id", "-")

    if user_id == admin.id:
        logger.warning(
            "删除用户失败 actor_id=%s target_user_id=%s reason=self_delete request_id=%s",
            admin.id,
            user_id,
            request_id,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="不能删除自己"
        )

    async with get_session() as db:
        result = await db.exec(select(User).where(User.id == user_id))
        user = result.first()
        if not user:
            logger.warning(
                "删除用户失败 actor_id=%s target_user_id=%s reason=not_found request_id=%s",
                admin.id,
                user_id,
                request_id,
            )
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="用户不存在"
            )

        # 获取用户的所有文件引用 ID（需要在删除用户前处理）
        user_files_result = await db.exec(select(UserFile).where(UserFile.owner_id == user_id))
        user_file_ids = [uf.id for uf in user_files_result.all() if uf.id is not None]

        # 删除用户文件引用（正确递减 ref_count 并清理物理文件）
        # 必须在删除 User 之前处理，否则级联删除会跳过 ref_count 递减
        from app.services.storage import delete_user_file_reference
        failed_file_ids: list[int] = []
        for user_file_id in user_file_ids:
            try:
                await delete_user_file_reference(user_file_id)
            except Exception as e:
                logger.error(
                    "删除用户文件引用失败 user_file_id=%s error=%s",
                    user_file_id, e
                )
                failed_file_ids.append(user_file_id)

        if failed_file_ids:
            logger.warning(
                "部分用户文件引用删除失败 user_id=%s failed_ids=%s",
                user_id, failed_file_ids
            )

    # 在同一事务中删除用户及其关联数据
    async with get_session() as db:
        # 删除用户的所有会话
        sessions_result = await db.exec(select(SessionModel).where(SessionModel.user_id == user_id))
        for session in sessions_result.all():
            await db.delete(session)

        # 删除用户的所有下载任务记录
        tasks_result = await db.exec(select(Task).where(Task.owner_id == user_id))
        for task in tasks_result.all():
            await db.delete(task)

        # 删除用户的所有打包任务记录
        pack_tasks_result = await db.exec(select(PackTask).where(PackTask.owner_id == user_id))
        for pack_task in pack_tasks_result.all():
            await db.delete(pack_task)

        # 删除用户的所有任务订阅
        subs_result = await db.exec(select(UserTaskSubscription).where(UserTaskSubscription.owner_id == user_id))
        for sub in subs_result.all():
            await db.delete(sub)

        # 删除用户的所有任务历史
        history_result = await db.exec(select(TaskHistory).where(TaskHistory.owner_id == user_id))
        for history in history_result.all():
            await db.delete(history)

        # 删除用户的所有分享链接
        shares_result = await db.exec(select(ShareLink).where(ShareLink.owner_id == user_id))
        for share in shares_result.all():
            await db.delete(share)

        # 删除用户
        result = await db.exec(select(User).where(User.id == user_id))
        user = result.first()
        if user:
            await db.delete(user)

        await db.commit()

    logger.info(
        "删除用户成功 actor_id=%s target_user_id=%s request_id=%s",
        admin.id,
        user_id,
        request_id,
    )

    return {"ok": True}


@router.get("/{user_id}", response_model=UserOut)
async def get_user(user_id: int, admin: User = Depends(require_admin)) -> dict:
    """获取单个用户详情（管理员）"""
    async with get_session() as db:
        result = await db.exec(select(User).where(User.id == user_id))
        user = result.first()
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="用户不存在"
            )
        logger.debug("查询用户详情 admin_id=%s target_user_id=%s", admin.id, user_id)
        return {
            "id": user.id,
            "username": user.username,
            "is_admin": user.is_admin,
            "quota": user.quota
        }


@router.put("/{user_id}", response_model=UserOut)
async def update_user(
    user_id: int,
    payload: UserUpdate,
    request: Request,
    admin: User = Depends(require_admin),
) -> dict:
    """更新用户信息（管理员）"""
    request_id = getattr(request.state, "request_id", "-")
    async with get_session() as db:
        result = await db.exec(select(User).where(User.id == user_id))
        user = result.first()
        if not user:
            logger.warning(
                "更新用户失败 actor_id=%s target_user_id=%s reason=not_found request_id=%s",
                admin.id,
                user_id,
                request_id,
            )
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="用户不存在"
            )

        if payload.username is not None:
            # 检查用户名是否被其他用户占用
            existing_result = await db.exec(
                select(User).where(User.username == payload.username, User.id != user_id)
            )
            if existing_result.first():
                logger.warning(
                    "更新用户失败 actor_id=%s target_user_id=%s reason=username_taken request_id=%s",
                    admin.id,
                    user_id,
                    request_id,
                )
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="用户名已被占用"
                )
            user.username = payload.username

        if payload.password is not None:
            user.password_hash = hash_password(payload.password)
            user.is_initial_password = user_id != admin.id
            # 密码修改后使该用户的所有 session 失效
            await clear_user_sessions(user_id)

        if payload.is_admin is not None:
            # 不能取消自己的管理员权限
            if user_id == admin.id and not payload.is_admin:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="不能取消自己的管理员权限"
                )
            # 不能降级最后一个管理员
            if user.is_admin and not payload.is_admin:
                admin_count_result = await db.exec(
                    select(func.count()).select_from(User).where(User.is_admin == True)  # noqa: E712
                )
                admin_count = admin_count_result.one()
                if admin_count <= 1:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="不能降级最后一个管理员"
                    )
            user.is_admin = payload.is_admin

        if payload.quota is not None:
            user.quota = payload.quota

        db.add(user)
        try:
            await db.commit()
            await db.refresh(user)
        except IntegrityError:
            await db.rollback()
            logger.warning(
                "更新用户冲突 actor_id=%s target_user_id=%s request_id=%s",
                admin.id,
                user_id,
                request_id,
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="用户名已被占用"
            )

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

        return {
            "id": user.id,
            "username": user.username,
            "is_admin": user.is_admin,
            "quota": user.quota
        }


# ============ RPC 访问管理接口 ============


@router.get("/me/rpc-access", response_model=RpcAccessStatus)
async def get_rpc_access(user: User = Depends(require_user)) -> RpcAccessStatus:
    """获取当前用户的 RPC 访问状态"""
    async with get_session() as db:
        result = await db.exec(select(User).where(User.id == user.id))
        db_user = result.first()
        if not db_user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="用户不存在"
            )

        logger.debug("查询RPC访问状态 user_id=%s enabled=%s", user.id, db_user.rpc_secret is not None)

        return RpcAccessStatus(
            enabled=db_user.rpc_secret is not None,
            secret=db_user.rpc_secret,
            created_at=db_user.rpc_secret_created_at
        )


@router.put("/me/rpc-access", response_model=RpcAccessStatus)
async def set_rpc_access(
    payload: RpcAccessToggle,
    request: Request,
    user: User = Depends(require_user)
) -> RpcAccessStatus:
    """开启或关闭 RPC 访问"""
    request_id = getattr(request.state, "request_id", "-")
    async with get_session() as db:
        result = await db.exec(select(User).where(User.id == user.id))
        db_user = result.first()
        if not db_user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="用户不存在"
            )

        if payload.enabled:
            new_secret = await _generate_unique_rpc_secret(db)
            created_at = utc_now()
            db_user.rpc_secret = new_secret
            db_user.rpc_secret_created_at = created_at
            db.add(db_user)
            await db.commit()
            logger.info("开启RPC访问 user_id=%s request_id=%s", user.id, request_id)
            return RpcAccessStatus(
                enabled=True,
                secret=new_secret,
                created_at=created_at
            )
        else:
            # 关闭：清除 secret
            db_user.rpc_secret = None
            db_user.rpc_secret_created_at = None
            db.add(db_user)
            await db.commit()
            logger.info("关闭RPC访问 user_id=%s request_id=%s", user.id, request_id)
            return RpcAccessStatus(
                enabled=False,
                secret=None,
                created_at=None
            )


@router.post("/me/rpc-access/refresh", response_model=RpcAccessStatus)
async def refresh_rpc_secret(request: Request, user: User = Depends(require_user)) -> RpcAccessStatus:
    """刷新 RPC Secret（旧的立即失效）"""
    request_id = getattr(request.state, "request_id", "-")
    async with get_session() as db:
        result = await db.exec(select(User).where(User.id == user.id))
        db_user = result.first()
        if not db_user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="用户不存在"
            )

        if db_user.rpc_secret is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="RPC 访问未开启，请先开启后再刷新"
            )

        new_secret = await _generate_unique_rpc_secret(db)
        created_at = utc_now()
        db_user.rpc_secret = new_secret
        db_user.rpc_secret_created_at = created_at
        db.add(db_user)
        await db.commit()

        logger.info("刷新RPC密钥 user_id=%s request_id=%s", user.id, request_id)

        return RpcAccessStatus(
            enabled=True,
            secret=new_secret,
            created_at=created_at
        )
