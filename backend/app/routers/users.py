"""用户管理接口模块（管理员专用）"""
from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.auth import require_admin, require_user
from app.core.security import hash_password
from app.db import execute, fetch_all, fetch_one, utc_now
from app.schemas import UserCreate, UserOut, UserUpdate


router = APIRouter(prefix="/api/users", tags=["users"])


def _has_any_user() -> bool:
    return bool(fetch_one("SELECT id FROM users LIMIT 1"))


@router.post("", response_model=UserOut)
def create_user(payload: UserCreate, request: Request) -> dict:
    """创建用户
    
    首次调用（无用户时）无需认证，之后需要管理员权限。
    """
    if _has_any_user():
        require_admin(require_user(request))
    
    # 检查用户名是否已存在
    existing = fetch_one("SELECT id FROM users WHERE username = ?", [payload.username])
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="用户名已存在"
        )
    
    # 默认配额 100GB
    quota = payload.quota if payload.quota is not None else 107374182400
    
    user_id = execute(
        """
        INSERT INTO users (username, password_hash, is_admin, quota, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        [payload.username, hash_password(payload.password), int(payload.is_admin), quota, utc_now()],
    )
    user = fetch_one("SELECT id, username, is_admin, quota FROM users WHERE id = ?", [user_id])
    return {
        "id": user["id"],
        "username": user["username"],
        "is_admin": bool(user["is_admin"]),
        "quota": user["quota"]
    }


@router.get("", response_model=list[UserOut])
def list_users(admin: dict = Depends(require_admin)) -> list[dict]:
    """获取用户列表（管理员）"""
    users = fetch_all("SELECT id, username, is_admin, quota FROM users")
    return [{
        "id": u["id"],
        "username": u["username"],
        "is_admin": bool(u["is_admin"]),
        "quota": u["quota"]
    } for u in users]


@router.delete("/{user_id}")
def delete_user(user_id: int, admin: dict = Depends(require_admin)) -> dict:
    """删除用户（管理员）
    
    注意: 不能删除自己
    """
    if user_id == admin["id"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="不能删除自己"
        )
    
    user = fetch_one("SELECT id FROM users WHERE id = ?", [user_id])
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="用户不存在"
        )
    
    # 删除用户的所有会话
    execute("DELETE FROM sessions WHERE user_id = ?", [user_id])
    # 删除用户
    execute("DELETE FROM users WHERE id = ?", [user_id])
    
    return {"ok": True}


@router.get("/{user_id}", response_model=UserOut)
def get_user(user_id: int, admin: dict = Depends(require_admin)) -> dict:
    """获取单个用户详情（管理员）"""
    user = fetch_one("SELECT id, username, is_admin, quota FROM users WHERE id = ?", [user_id])
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="用户不存在"
        )
    return {
        "id": user["id"],
        "username": user["username"],
        "is_admin": bool(user["is_admin"]),
        "quota": user["quota"]
    }


@router.put("/{user_id}", response_model=UserOut)
def update_user(user_id: int, payload: UserUpdate, admin: dict = Depends(require_admin)) -> dict:
    """更新用户信息（管理员）"""
    user = fetch_one("SELECT id, username, is_admin, quota FROM users WHERE id = ?", [user_id])
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="用户不存在"
        )
    
    # 构建更新字段
    updates = []
    params = []
    
    if payload.username is not None:
        # 检查用户名是否被其他用户占用
        existing = fetch_one(
            "SELECT id FROM users WHERE username = ? AND id != ?",
            [payload.username, user_id]
        )
        if existing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="用户名已被占用"
            )
        updates.append("username = ?")
        params.append(payload.username)
    
    if payload.password is not None:
        updates.append("password_hash = ?")
        params.append(hash_password(payload.password))
    
    if payload.is_admin is not None:
        # 不能取消自己的管理员权限
        if user_id == admin["id"] and not payload.is_admin:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="不能取消自己的管理员权限"
            )
        updates.append("is_admin = ?")
        params.append(int(payload.is_admin))
    
    if payload.quota is not None:
        updates.append("quota = ?")
        params.append(payload.quota)
    
    if updates:
        params.append(user_id)
        execute(
            f"UPDATE users SET {', '.join(updates)} WHERE id = ?",
            params
        )
    
    # 返回更新后的用户信息
    updated = fetch_one("SELECT id, username, is_admin, quota FROM users WHERE id = ?", [user_id])
    if not updated:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="更新失败"
        )
    return {
        "id": updated["id"],
        "username": updated["username"],
        "is_admin": bool(updated["is_admin"]),
        "quota": updated["quota"]
    }
