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


# ========== Share Schemas ==========


class CreateShareRequest(BaseModel):
    """创建分享请求"""
    user_file_id: int
    password: str | None = Field(default=None, max_length=100)
    expires_in: int | None = Field(default=None, gt=0, le=2592000)  # 秒，最大30天
    max_downloads: int | None = Field(default=None, gt=0, le=10000)


class ShareLinkOut(BaseModel):
    """分享链接响应"""
    id: int
    share_code: str
    file_name: str
    file_size: int
    has_password: bool
    expires_at: str | None
    max_downloads: int | None
    download_count: int
    status: str
    created_at: str
    last_accessed_at: str | None


class ShareInfoOut(BaseModel):
    """公开分享信息（无需登录）"""
    file_name: str
    file_size: int
    is_directory: bool
    has_password: bool
    is_expired: bool
    is_exhausted: bool  # 下载次数已用完


class ShareAccessRequest(BaseModel):
    """分享密码验证请求"""
    password: str = Field(min_length=1, max_length=100)


class ShareAccessResponse(BaseModel):
    """分享密码验证响应"""
    access_token: str
