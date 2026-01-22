"""后台配置接口模块（管理员专用）"""
from __future__ import annotations

from time import time

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.auth import require_admin
from app.db import execute, fetch_all, fetch_one

_config_cache: dict[str, tuple[str | None, float]] = {}
_CACHE_TTL = 60.0  # 缓存有效期（秒）


router = APIRouter(prefix="/api/config", tags=["config"])


class ConfigUpdate(BaseModel):
    """配置更新请求体"""
    max_task_size: int | None = None  # 单任务最大大小（字节）
    min_free_disk: int | None = None  # 磁盘最小剩余空间（字节）
    aria2_rpc_url: str | None = None  # aria2 RPC URL
    aria2_rpc_secret: str | None = None  # aria2 RPC Secret
    hidden_file_extensions: list[str] | None = None  # 隐藏的文件后缀名列表


class Aria2TestRequest(BaseModel):
    """aria2 连接测试请求体"""
    aria2_rpc_url: str
    aria2_rpc_secret: str | None = None


def get_config_value(key: str) -> str | None:
    """获取单个配置值（带缓存）"""
    now = time()
    if key in _config_cache:
        value, ts = _config_cache[key]
        if now - ts < _CACHE_TTL:
            return value
    row = fetch_one("SELECT value FROM config WHERE key = ?", [key])
    value = row["value"] if row else None
    _config_cache[key] = (value, now)
    return value


def set_config_value(key: str, value: str) -> None:
    """设置单个配置值"""
    execute(
        "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
        [key, value]
    )
    _config_cache[key] = (value, time())  # 更新缓存


def get_max_task_size() -> int:
    """获取单任务最大大小（字节），默认 10GB"""
    val = get_config_value("max_task_size")
    return int(val) if val else 10 * 1024 * 1024 * 1024


def get_min_free_disk() -> int:
    """获取磁盘最小剩余空间（字节），默认 1GB"""
    val = get_config_value("min_free_disk")
    return int(val) if val else 1 * 1024 * 1024 * 1024


def get_hidden_file_extensions() -> list[str]:
    """获取隐藏的文件后缀名列表"""
    import json
    val = get_config_value("hidden_file_extensions")
    if val:
        try:
            return json.loads(val)
        except Exception:
            return []
    return []


@router.get("")
def get_config(admin: dict = Depends(require_admin)) -> dict:
    """获取系统配置（管理员）
    
    返回:
    - max_task_size: 单任务最大允许大小（字节）
    - min_free_disk: 磁盘最小剩余空间阈值（字节）
    - aria2_rpc_url: aria2 RPC URL
    - aria2_rpc_secret: aria2 RPC Secret（脱敏显示）
    - hidden_file_extensions: 隐藏的文件后缀名列表
    """
    aria2_rpc_url = get_config_value("aria2_rpc_url") or "http://localhost:6800/jsonrpc"
    aria2_rpc_secret = get_config_value("aria2_rpc_secret") or ""
    
    # 脱敏处理 secret
    masked_secret = ""
    if aria2_rpc_secret:
        masked_secret = "*" * min(len(aria2_rpc_secret), 8)
    
    return {
        "max_task_size": get_max_task_size(),
        "min_free_disk": get_min_free_disk(),
        "aria2_rpc_url": aria2_rpc_url,
        "aria2_rpc_secret": masked_secret,
        "hidden_file_extensions": get_hidden_file_extensions(),
    }


@router.put("")
def update_config(payload: ConfigUpdate, admin: dict = Depends(require_admin)) -> dict:
    """更新系统配置（管理员）
    
    可更新字段:
    - max_task_size: 单任务最大允许大小（字节）
    - min_free_disk: 磁盘最小剩余空间阈值（字节）
    - aria2_rpc_url: aria2 RPC URL
    - aria2_rpc_secret: aria2 RPC Secret
    - hidden_file_extensions: 隐藏的文件后缀名列表
    """
    import json
    
    if payload.max_task_size is not None:
        set_config_value("max_task_size", str(payload.max_task_size))
    if payload.min_free_disk is not None:
        set_config_value("min_free_disk", str(payload.min_free_disk))
    if payload.aria2_rpc_url is not None:
        set_config_value("aria2_rpc_url", payload.aria2_rpc_url)
    if payload.aria2_rpc_secret is not None:
        # 如果是掩码，不更新
        if not payload.aria2_rpc_secret.startswith("*"):
            set_config_value("aria2_rpc_secret", payload.aria2_rpc_secret)
    if payload.hidden_file_extensions is not None:
        # 规范化后缀名：统一小写，确保以点开头
        normalized = []
        for ext in payload.hidden_file_extensions:
            ext = ext.strip().lower()
            if ext and not ext.startswith("."):
                ext = "." + ext
            if ext and ext not in normalized:
                normalized.append(ext)
        set_config_value("hidden_file_extensions", json.dumps(normalized))
    
    # 返回更新后的配置（secret 脱敏）
    aria2_rpc_url = get_config_value("aria2_rpc_url") or "http://localhost:6800/jsonrpc"
    aria2_rpc_secret = get_config_value("aria2_rpc_secret") or ""
    masked_secret = ""
    if aria2_rpc_secret:
        masked_secret = "*" * min(len(aria2_rpc_secret), 8)
    
    return {
        "max_task_size": get_max_task_size(),
        "min_free_disk": get_min_free_disk(),
        "aria2_rpc_url": aria2_rpc_url,
        "aria2_rpc_secret": masked_secret,
        "hidden_file_extensions": get_hidden_file_extensions(),
    }



@router.get("/aria2/version")
async def get_aria2_version(admin: dict = Depends(require_admin)) -> dict:
    """获取当前连接的 aria2 版本信息（管理员）
    
    返回:
    - version: aria2 版本号
    - enabled_features: 启用的功能列表
    - connected: 是否成功连接
    - error: 错误信息（如果连接失败）
    """
    from app.aria2.client import Aria2Client
    
    aria2_rpc_url = get_config_value("aria2_rpc_url") or "http://localhost:6800/jsonrpc"
    aria2_rpc_secret = get_config_value("aria2_rpc_secret") or ""
    
    client = Aria2Client(aria2_rpc_url, aria2_rpc_secret)
    
    try:
        version_info = await client.get_version()
        return {
            "connected": True,
            "version": version_info.get("version"),
            "enabled_features": version_info.get("enabledFeatures", []),
        }
    except Exception as exc:
        return {
            "connected": False,
            "error": str(exc),
        }


@router.post("/aria2/test")
async def test_aria2_connection(
    payload: Aria2TestRequest,
    admin: dict = Depends(require_admin)
) -> dict:
    """测试 aria2 连接（管理员）
    
    参数:
    - aria2_rpc_url: aria2 RPC URL
    - aria2_rpc_secret: aria2 RPC Secret（可选）
    
    返回:
    - connected: 是否成功连接
    - version: aria2 版本号（如果连接成功）
    - enabled_features: 启用的功能列表（如果连接成功）
    - error: 错误信息（如果连接失败）
    """
    from app.aria2.client import Aria2Client
    
    if not payload.aria2_rpc_url:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="aria2 RPC URL 不能为空"
        )
    
    # secret 处理逻辑：
    # - None: 前端发送 undefined，表示用户未修改（显示掩码），使用数据库密码
    # - 以 * 开头: 掩码（兜底），使用数据库密码
    # - 空字符串: 用户主动清空，用空密码测试
    # - 其他: 用户输入的新密码
    secret = payload.aria2_rpc_secret
    if secret is None or (isinstance(secret, str) and secret.startswith("*")):
        secret = get_config_value("aria2_rpc_secret") or ""
    
    client = Aria2Client(payload.aria2_rpc_url, secret)
    
    try:
        version_info = await client.get_version()
        return {
            "connected": True,
            "version": version_info.get("version"),
            "enabled_features": version_info.get("enabledFeatures", []),
        }
    except Exception as exc:
        return {
            "connected": False,
            "error": str(exc),
        }
