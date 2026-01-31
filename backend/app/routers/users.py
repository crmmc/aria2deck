"""用户管理接口模块"""
import secrets
import shutil
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from app.auth import clear_user_sessions, require_admin, require_user
from app.core.config import settings
from app.core.rate_limit import login_limiter
from app.core.security import hash_password
from app.database import get_session
from app.models import User, Session as SessionModel, Task, PackTask, UserFile
from app.schemas import RpcAccessStatus, RpcAccessToggle, UserCreate, UserOut, UserUpdate


router = APIRouter(prefix="/api/users", tags=["users"])


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    client_ip = request.client.host if request.client else "unknown"

    # 首次创建用户时的 IP 限流（防止滥用）
    if not await _has_any_user():
        if await login_limiter.is_blocked(client_ip):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="请求过于频繁，请稍后再试"
            )

    if await _has_any_user():
        await require_admin(await require_user(request))

        async with get_session() as db:
            # 检查用户名是否已存在
            result = await db.exec(select(User).where(User.username == payload.username))
            if result.first():
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
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="用户名已存在"
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

        if result.rowcount == 0:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="需要管理员权限"
            )

        result = await db.exec(select(User).where(User.username == payload.username))
        user = result.first()
        if not user:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="创建用户失败"
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
        return [{
            "id": u.id,
            "username": u.username,
            "is_admin": u.is_admin,
            "quota": u.quota
        } for u in users]


@router.delete("/{user_id}")
async def delete_user(
    user_id: int,
    delete_files: bool = Query(False, description="是否删除用户下载目录"),
    admin: User = Depends(require_admin)
) -> dict:
    """删除用户（管理员）

    删除用户时会同时删除：
    - 用户的所有会话
    - 用户的所有下载任务记录
    - 用户的所有打包任务记录
    - 可选：用户的下载目录

    注意: 不能删除自己
    """
    if user_id == admin.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="不能删除自己"
        )

    async with get_session() as db:
        result = await db.exec(select(User).where(User.id == user_id))
        user = result.first()
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="用户不存在"
            )

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

        # 获取用户的所有文件引用 ID（在事务外删除以正确处理 ref_count）
        user_files_result = await db.exec(select(UserFile).where(UserFile.owner_id == user_id))
        user_file_ids = [uf.id for uf in user_files_result.all()]

        # 删除用户
        await db.delete(user)

    # 删除用户文件引用（正确递减 ref_count 并清理物理文件）
    from app.services.storage import delete_user_file_reference
    for user_file_id in user_file_ids:
        await delete_user_file_reference(user_file_id)

    # 可选：删除用户下载目录
    if delete_files:
        user_download_dir = Path(settings.download_dir) / str(user_id)
        if user_download_dir.exists():
            shutil.rmtree(user_download_dir, ignore_errors=True)

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
        return {
            "id": user.id,
            "username": user.username,
            "is_admin": user.is_admin,
            "quota": user.quota
        }


@router.put("/{user_id}", response_model=UserOut)
async def update_user(user_id: int, payload: UserUpdate, admin: User = Depends(require_admin)) -> dict:
    """更新用户信息（管理员）"""
    async with get_session() as db:
        result = await db.exec(select(User).where(User.id == user_id))
        user = result.first()
        if not user:
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
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="用户名已被占用"
                )
            user.username = payload.username

        if payload.password is not None:
            user.password_hash = hash_password(payload.password)
            user.is_initial_password = True  # 管理员重置密码后，用户需要自行修改
            # 密码修改后使该用户的所有 session 失效
            await clear_user_sessions(user_id)

        if payload.is_admin is not None:
            # 不能取消自己的管理员权限
            if user_id == admin.id and not payload.is_admin:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="不能取消自己的管理员权限"
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
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="用户名已被占用"
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

        return RpcAccessStatus(
            enabled=db_user.rpc_secret is not None,
            secret=db_user.rpc_secret,
            created_at=db_user.rpc_secret_created_at
        )


@router.put("/me/rpc-access", response_model=RpcAccessStatus)
async def set_rpc_access(
    payload: RpcAccessToggle,
    user: User = Depends(require_user)
) -> RpcAccessStatus:
    """开启或关闭 RPC 访问"""
    async with get_session() as db:
        result = await db.exec(select(User).where(User.id == user.id))
        db_user = result.first()
        if not db_user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="用户不存在"
            )

        if payload.enabled:
            # 开启：生成新 secret
            new_secret = "aria2_" + secrets.token_urlsafe(32)
            created_at = utc_now()
            db_user.rpc_secret = new_secret
            db_user.rpc_secret_created_at = created_at
            db.add(db_user)
            await db.commit()
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
            return RpcAccessStatus(
                enabled=False,
                secret=None,
                created_at=None
            )


@router.post("/me/rpc-access/refresh", response_model=RpcAccessStatus)
async def refresh_rpc_secret(user: User = Depends(require_user)) -> RpcAccessStatus:
    """刷新 RPC Secret（旧的立即失效）"""
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

        # 生成新 secret
        new_secret = "aria2_" + secrets.token_urlsafe(32)
        created_at = utc_now()
        db_user.rpc_secret = new_secret
        db_user.rpc_secret_created_at = created_at
        db.add(db_user)
        await db.commit()

        return RpcAccessStatus(
            enabled=True,
            secret=new_secret,
            created_at=created_at
        )
