"""数据模型定义"""
from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    """登录请求"""
    username: str = Field(min_length=1, max_length=50)
    password: str = Field(min_length=1, max_length=100)


class UserCreate(BaseModel):
    """创建用户请求"""
    username: str = Field(min_length=1, max_length=50)
    password: str = Field(min_length=6, max_length=100)
    is_admin: bool = False
    quota: int | None = Field(default=None, ge=0, le=10 * 1024 * 1024 * 1024 * 1024)  # 最大 10TB


class UserUpdate(BaseModel):
    """更新用户请求"""
    username: str | None = Field(default=None, min_length=1, max_length=50)
    password: str | None = Field(default=None, min_length=6, max_length=100)
    is_admin: bool | None = None
    quota: int | None = Field(default=None, ge=0, le=10 * 1024 * 1024 * 1024 * 1024)


class UserOut(BaseModel):
    """用户信息响应"""
    id: int
    username: str
    is_admin: bool
    quota: int
