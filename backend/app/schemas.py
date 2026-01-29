"""数据模型定义"""
from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    """登录请求"""
    username: str = Field(min_length=1, max_length=50)
    password: str = Field(min_length=1, max_length=200)  # client_hash (hex string, 64 chars)


class UserCreate(BaseModel):
    """创建用户请求"""
    username: str = Field(min_length=1, max_length=50)
    password: str = Field(min_length=1, max_length=200)  # client_hash (hex string, 64 chars)
    is_admin: bool = False
    quota: int | None = Field(default=None, ge=0, le=10 * 1024 * 1024 * 1024 * 1024)  # 最大 10TB


class UserUpdate(BaseModel):
    """更新用户请求"""
    username: str | None = Field(default=None, min_length=1, max_length=50)
    password: str | None = Field(default=None, min_length=1, max_length=200)  # client_hash
    is_admin: bool | None = None
    quota: int | None = Field(default=None, ge=0, le=10 * 1024 * 1024 * 1024 * 1024)


class UserOut(BaseModel):
    """用户信息响应"""
    id: int
    username: str
    is_admin: bool
    quota: int
    password_warning: str | None = None  # 密码安全警告
    is_default_password: bool = False  # deprecated, kept for compatibility
    is_initial_password: bool = False  # 是否为初始密码状态


class ChangePasswordRequest(BaseModel):
    """修改密码请求"""
    old_password: str = Field(min_length=1, max_length=200)  # client_hash
    new_password: str = Field(min_length=1, max_length=200)  # client_hash


class RpcAccessStatus(BaseModel):
    """RPC 访问状态"""
    enabled: bool
    secret: str | None = None
    created_at: str | None = None


class RpcAccessToggle(BaseModel):
    """RPC 访问开关请求"""
    enabled: bool
