"""后台配置接口模块（管理员专用）及 Token 管理"""

from __future__ import annotations

import asyncio
import logging
import secrets
import string
from datetime import datetime, timezone
from time import time

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.auth import require_admin, require_user
from app.core.download_limiter import download_config
from app.core.request_rate_guard import RateLimitScope, ensure_authenticated_allowed
from app.core.rate_limit_config import rate_limit_config
from app.repositories import auth as auth_repo
from app.services import settings_service

_config_cache: dict[str, tuple[str | None, float]] = {}
_config_cache_lock = asyncio.Lock()  # 保护异步缓存访问
_CACHE_TTL = 60.0  # 缓存有效期（秒）


router = APIRouter(prefix="/api/config", tags=["config"])
logger = logging.getLogger(__name__)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


CONFIG_KEY_TO_COLUMN = settings_service.CONFIG_KEY_TO_COLUMN


def _ms_to_iso(timestamp_ms: int | None) -> str | None:
    if timestamp_ms is None:
        return None
    return datetime.fromtimestamp(timestamp_ms / 1000, timezone.utc).isoformat()


class ConfigUpdate(BaseModel):
    """配置更新请求体"""

    max_task_size: int | None = Field(None, ge=0, description="单任务最大大小（字节）")
    min_free_disk: int | None = Field(
        None, ge=0, description="磁盘最小剩余空间（字节）"
    )
    aria2_rpc_url: str | None = None
    aria2_rpc_secret: str | None = None
    aria2_bt_stop_timeout_seconds: int | None = Field(
        None,
        ge=0,
        le=365 * 24 * 60 * 60,
        description="BT 连续无数据传输停止超时（秒，0=禁用）",
    )
    hidden_file_extensions: list[str] | None = None
    pack_format: str | None = None
    pack_compression_level: int | None = Field(
        None, ge=0, le=9, description="压缩等级 (0-9)"
    )
    # WebSocket 重连参数
    ws_reconnect_max_delay: float | None = Field(
        None, ge=1.0, le=300.0, description="最大重连延迟（秒）"
    )
    ws_reconnect_jitter: float | None = Field(
        None, ge=0.0, le=1.0, description="抖动系数 (0-1)"
    )
    ws_reconnect_factor: float | None = Field(
        None, ge=1.1, le=5.0, description="指数因子"
    )
    site_title: str | None = Field(None, max_length=50, description="网站标题")
    # 请求频率限制
    rate_limit_account_security: int | None = Field(
        None, ge=1, le=100, description="账户安全限流（次/5分钟）"
    )
    rate_limit_authenticated_api: int | None = Field(
        None, ge=0, le=10000, description="普通已登录 API 限流（次/分钟，0=不限制）"
    )
    rate_limit_public_api: int | None = Field(
        None, ge=0, le=10000, description="普通匿名公开 API 限流（次/分钟，0=不限制）"
    )
    rate_limit_share_access: int | None = Field(
        None, ge=1, le=10000, description="分享密码验证限流（次/分钟）"
    )
    rate_limit_authenticated_download: int | None = Field(
        None, ge=0, le=10000, description="已登录下载限流（次/分钟，0=不限制）"
    )
    rate_limit_anonymous_download: int | None = Field(
        None, ge=0, le=10000, description="匿名下载限流（次/分钟，0=不限制）"
    )
    rate_limit_create_task: int | None = Field(
        None, ge=1, le=10000, description="创建任务限流（次/分钟）"
    )
    rate_limit_create_torrent: int | None = Field(
        None, ge=1, le=10000, description="创建种子限流（次/分钟）"
    )
    rate_limit_create_pack: int | None = Field(
        None, ge=1, le=10000, description="创建打包限流（次/分钟）"
    )
    rate_limit_aria2_test: int | None = Field(
        None, ge=1, le=10000, description="aria2测试限流（次/分钟）"
    )
    rate_limit_rpc: int | None = Field(
        None, ge=1, le=10000, description="JSON-RPC限流（次/分钟）"
    )
    # 下载并发限制
    download_total_connections: int | None = Field(
        None, ge=0, le=10000, description="系统总下载连接上限（0=不限制）"
    )
    download_authenticated_reserved_connections: int | None = Field(
        None, ge=0, le=10000, description="已登录保底连接数"
    )
    download_authenticated_per_user_connections: int | None = Field(
        None, ge=0, le=1000, description="已登录单用户最大并发（0=不限制）"
    )
    download_authenticated_per_file_connections: int | None = Field(
        None, ge=0, le=100, description="已登录单文件最大并发（0=不限制）"
    )
    download_anonymous_base_connections: int | None = Field(
        None, ge=0, le=10000, description="匿名基础连接数"
    )
    download_anonymous_borrow_connections: int | None = Field(
        None, ge=0, le=10000, description="匿名可借用连接数"
    )
    download_anonymous_per_ip_connections: int | None = Field(
        None, ge=0, le=1000, description="匿名单 IP 最大并发（0=不限制）"
    )
    download_anonymous_per_file_connections: int | None = Field(
        None, ge=0, le=100, description="匿名单文件最大并发（0=不限制）"
    )


class Aria2TestRequest(BaseModel):
    """aria2 连接测试请求体"""

    aria2_rpc_url: str
    aria2_rpc_secret: str | None = None


def get_config_value(key: str) -> str | None:
    """获取单个配置值（带缓存）- 同步版本用于非异步上下文"""
    now = time()
    if key in _config_cache:
        value, ts = _config_cache[key]
        if now - ts < _CACHE_TTL:
            return value

    # 使用同步方式读取（用于向后兼容）
    import sqlite3
    from app.core.config import settings

    column_name = CONFIG_KEY_TO_COLUMN.get(key)
    if column_name is None:
        _config_cache[key] = (None, now)
        return None
    try:
        conn = sqlite3.connect(settings.database_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(f"SELECT {column_name} AS value FROM app_settings WHERE id = 1")
        row = cur.fetchone()
        value = settings_service.serialize_config_value(row["value"]) if row else None
        cur.close()
        conn.close()
        _config_cache[key] = (value, now)
        return value
    except Exception as exc:
        logger.warning("读取配置失败 key=%s error=%s", key, exc)
        return None


async def get_config_value_async(key: str) -> str | None:
    """获取单个配置值（带缓存）- 异步版本"""
    now = time()
    async with _config_cache_lock:
        if key in _config_cache:
            value, ts = _config_cache[key]
            if now - ts < _CACHE_TTL:
                return value

    value = await settings_service.get_config_value(key)

    async with _config_cache_lock:
        _config_cache[key] = (value, now)
    return value


async def set_config_value_async(key: str, value: str) -> None:
    """设置单个配置值 - 异步版本"""
    if key not in CONFIG_KEY_TO_COLUMN:
        async with _config_cache_lock:
            _config_cache[key] = (None, time())
        return

    await settings_service.set_config_value(key, value)
    async with _config_cache_lock:
        _config_cache[key] = (value, time())


def get_max_task_size() -> int:
    """获取单任务最大大小（字节），默认 10GB"""
    val = get_config_value("max_task_size")
    if val:
        try:
            return int(val)
        except (ValueError, TypeError):
            pass
    return 10 * 1024 * 1024 * 1024


def get_min_free_disk() -> int:
    """获取磁盘最小剩余空间（字节），默认 1GB"""
    val = get_config_value("min_free_disk")
    if val:
        try:
            return int(val)
        except (ValueError, TypeError):
            pass
    return 1 * 1024 * 1024 * 1024


def get_aria2_bt_stop_timeout_seconds() -> int:
    """获取 aria2 BT 无数据传输停止超时，默认 7 天。"""
    val = get_config_value("aria2_bt_stop_timeout_seconds")
    if val:
        try:
            return max(0, int(val))
        except (ValueError, TypeError):
            pass
    return 7 * 24 * 60 * 60


def get_hidden_file_extensions() -> list[str]:
    """获取隐藏的文件后缀名列表"""
    import json

    val = get_config_value("hidden_file_extensions")
    if val:
        try:
            return json.loads(val)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse hidden_file_extensions config: {e}")
            return []
    return []


def get_pack_format() -> str:
    val = get_config_value("pack_format")
    if val == "7z":
        return "tar.zst"
    return val if val in ("zip", "tar.zst") else "zip"


def get_pack_compression_level() -> int:
    """获取压缩等级 (0-9)，默认 5"""
    val = get_config_value("pack_compression_level")
    try:
        level = int(val) if val else 5
        return max(0, min(9, level))
    except ValueError:
        return 5


def get_ws_reconnect_max_delay() -> float:
    """获取 WebSocket 最大重连延迟（秒），默认 60"""
    val = get_config_value("ws_reconnect_max_delay")
    try:
        return float(val) if val else 60.0
    except ValueError:
        return 60.0


def get_ws_reconnect_jitter() -> float:
    """获取 WebSocket 重连抖动系数 (0-1)，默认 0.2"""
    val = get_config_value("ws_reconnect_jitter")
    try:
        jitter = float(val) if val else 0.2
        return max(0.0, min(1.0, jitter))
    except ValueError:
        return 0.2


def get_ws_reconnect_factor() -> float:
    """获取 WebSocket 重连指数因子，默认 2.0"""
    val = get_config_value("ws_reconnect_factor")
    try:
        factor = float(val) if val else 2.0
        return max(1.1, min(10.0, factor))  # 限制范围 1.1-10
    except ValueError:
        return 2.0


def get_site_title() -> str:
    """获取网站标题，默认 'Aria2 控制器'"""
    val = get_config_value("site_title")
    return val if val else "Aria2 控制器"


def _serialize_config(aria2_rpc_url: str, aria2_rpc_secret: str) -> dict:
    """构造配置响应体。"""
    masked_secret = ""
    if aria2_rpc_secret:
        masked_secret = "*" * min(len(aria2_rpc_secret), 8)

    return {
        "max_task_size": get_max_task_size(),
        "min_free_disk": get_min_free_disk(),
        "aria2_rpc_url": aria2_rpc_url,
        "aria2_rpc_secret": masked_secret,
        "aria2_bt_stop_timeout_seconds": get_aria2_bt_stop_timeout_seconds(),
        "hidden_file_extensions": get_hidden_file_extensions(),
        "pack_format": get_pack_format(),
        "pack_compression_level": get_pack_compression_level(),
        "ws_reconnect_max_delay": get_ws_reconnect_max_delay(),
        "ws_reconnect_jitter": get_ws_reconnect_jitter(),
        "ws_reconnect_factor": get_ws_reconnect_factor(),
        "site_title": get_site_title(),
        "rate_limit_account_security": rate_limit_config.account_security,
        "rate_limit_authenticated_api": rate_limit_config.authenticated_api,
        "rate_limit_public_api": rate_limit_config.public_api,
        "rate_limit_share_access": rate_limit_config.share_access,
        "rate_limit_authenticated_download": rate_limit_config.authenticated_download,
        "rate_limit_anonymous_download": rate_limit_config.anonymous_download,
        "rate_limit_create_task": rate_limit_config.create_task,
        "rate_limit_create_torrent": rate_limit_config.create_torrent,
        "rate_limit_create_pack": rate_limit_config.create_pack,
        "rate_limit_aria2_test": rate_limit_config.aria2_test,
        "rate_limit_rpc": rate_limit_config.rpc,
        "download_total_connections": download_config.total_connections,
        "download_authenticated_reserved_connections": download_config.authenticated_reserved_connections,
        "download_authenticated_per_user_connections": download_config.authenticated_per_user_connections,
        "download_authenticated_per_file_connections": download_config.authenticated_per_file_connections,
        "download_anonymous_base_connections": download_config.anonymous_base_connections,
        "download_anonymous_borrow_connections": download_config.anonymous_borrow_connections,
        "download_anonymous_per_ip_connections": download_config.anonymous_per_ip_connections,
        "download_anonymous_per_file_connections": download_config.anonymous_per_file_connections,
    }


def _merged_download_settings(payload: ConfigUpdate) -> dict[str, int]:
    """将当前并发配置与更新 payload 合并为最终值。"""
    return {
        "download_total_connections": (
            payload.download_total_connections
            if payload.download_total_connections is not None
            else download_config.total_connections
        ),
        "download_authenticated_reserved_connections": (
            payload.download_authenticated_reserved_connections
            if payload.download_authenticated_reserved_connections is not None
            else download_config.authenticated_reserved_connections
        ),
        "download_authenticated_per_user_connections": (
            payload.download_authenticated_per_user_connections
            if payload.download_authenticated_per_user_connections is not None
            else download_config.authenticated_per_user_connections
        ),
        "download_authenticated_per_file_connections": (
            payload.download_authenticated_per_file_connections
            if payload.download_authenticated_per_file_connections is not None
            else download_config.authenticated_per_file_connections
        ),
        "download_anonymous_base_connections": (
            payload.download_anonymous_base_connections
            if payload.download_anonymous_base_connections is not None
            else download_config.anonymous_base_connections
        ),
        "download_anonymous_borrow_connections": (
            payload.download_anonymous_borrow_connections
            if payload.download_anonymous_borrow_connections is not None
            else download_config.anonymous_borrow_connections
        ),
        "download_anonymous_per_ip_connections": (
            payload.download_anonymous_per_ip_connections
            if payload.download_anonymous_per_ip_connections is not None
            else download_config.anonymous_per_ip_connections
        ),
        "download_anonymous_per_file_connections": (
            payload.download_anonymous_per_file_connections
            if payload.download_anonymous_per_file_connections is not None
            else download_config.anonymous_per_file_connections
        ),
    }


def _validate_download_settings(settings_map: dict[str, int]) -> None:
    """校验下载并发配置的关键约束。"""
    total = settings_map["download_total_connections"]
    if total <= 0:
        return

    allocated = (
        settings_map["download_authenticated_reserved_connections"]
        + settings_map["download_anonymous_base_connections"]
        + settings_map["download_anonymous_borrow_connections"]
    )
    if allocated > total:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="下载并发配置无效：已登录保底与匿名配额总和不能超过系统总连接上限",
        )


@router.get("/public/site-info")
async def get_public_site_info() -> dict:
    """获取公开的网站信息（无需认证）"""
    return {
        "site_title": get_site_title(),
    }


@router.get("")
async def get_config(admin=Depends(require_admin)) -> dict:
    """获取系统配置（管理员）

    返回:
    - max_task_size: 单任务最大允许大小（字节）
    - min_free_disk: 磁盘最小剩余空间阈值（字节）
    - aria2_rpc_url: aria2 RPC URL
    - aria2_rpc_secret: aria2 RPC Secret（脱敏显示）
    - hidden_file_extensions: 隐藏的文件后缀名列表
    """
    logger.debug("获取系统配置 admin_id=%s", admin.id)
    return await settings_service.get_api_settings()


@router.put("")
async def update_config(
    payload: ConfigUpdate, request: Request, admin=Depends(require_admin)
) -> dict:
    """更新系统配置（管理员）

    可更新字段:
    - max_task_size: 单任务最大允许大小（字节）
    - min_free_disk: 磁盘最小剩余空间阈值（字节）
    - aria2_rpc_url: aria2 RPC URL
    - aria2_rpc_secret: aria2 RPC Secret
    - hidden_file_extensions: 隐藏的文件后缀名列表
    """
    payload_values = {
        key: value for key, value in payload.model_dump().items() if value is not None
    }

    # 下载并发限制
    _download_config_keys = [
        "download_total_connections",
        "download_authenticated_reserved_connections",
        "download_authenticated_per_user_connections",
        "download_authenticated_per_file_connections",
        "download_anonymous_base_connections",
        "download_anonymous_borrow_connections",
        "download_anonymous_per_ip_connections",
        "download_anonymous_per_file_connections",
    ]
    _download_config_changed = any(
        getattr(payload, key) is not None for key in _download_config_keys
    )
    if _download_config_changed:
        _validate_download_settings(_merged_download_settings(payload))

    # API 频率限制
    _rate_limit_keys = [
        "rate_limit_account_security",
        "rate_limit_authenticated_api",
        "rate_limit_public_api",
        "rate_limit_share_access",
        "rate_limit_authenticated_download",
        "rate_limit_anonymous_download",
        "rate_limit_create_task",
        "rate_limit_create_torrent",
        "rate_limit_create_pack",
        "rate_limit_aria2_test",
        "rate_limit_rpc",
    ]
    _rate_limit_changed = any(
        getattr(payload, key) is not None for key in _rate_limit_keys
    )

    result = await settings_service.update_api_settings(payload_values)
    changed_keys = result.changed_keys

    async with _config_cache_lock:
        _config_cache.clear()

    if _download_config_changed:
        await download_config.refresh()
    if _rate_limit_changed:
        await rate_limit_config.refresh()
    # 如果 aria2 配置变更，刷新缓存
    if "aria2_rpc_url" in changed_keys or "aria2_rpc_secret" in changed_keys:
        from app.core.state import refresh_aria2_config

        if hasattr(request.app.state, "app_state"):
            await refresh_aria2_config(request.app.state.app_state)

    logger.info(
        "更新系统配置成功 admin_id=%s changed_keys=%s",
        admin.id,
        ",".join(changed_keys) if changed_keys else "none",
    )
    return result.settings


@router.get("/aria2/version")
async def get_aria2_version(admin=Depends(require_admin)) -> dict:
    """获取当前连接的 aria2 版本信息（管理员）

    返回:
    - version: aria2 版本号
    - enabled_features: 启用的功能列表
    - connected: 是否成功连接
    - error: 错误信息（如果连接失败）
    """
    from app.aria2.client import Aria2Client

    aria2_rpc_url = (
        await get_config_value_async("aria2_rpc_url") or "http://localhost:6800/jsonrpc"
    )
    aria2_rpc_secret = await get_config_value_async("aria2_rpc_secret") or ""

    client = Aria2Client(aria2_rpc_url, aria2_rpc_secret)

    try:
        version_info = await client.get_version()
        logger.info("获取aria2版本成功 admin_id=%s", admin.id)
        return {
            "connected": True,
            "version": version_info.get("version"),
            "enabled_features": version_info.get("enabledFeatures", []),
        }
    except Exception as exc:
        logger.warning("获取aria2版本失败 admin_id=%s error=%s", admin.id, exc)
        return {
            "connected": False,
            "error": "无法连接到 aria2 服务",
        }


@router.post("/aria2/test")
async def test_aria2_connection(
    payload: Aria2TestRequest, admin=Depends(require_admin)
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
    admin_id = admin.id
    if admin_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未登录")
    admin_id_int = int(admin_id)

    try:
        await ensure_authenticated_allowed(
            admin_id_int,
            RateLimitScope.ARIA2_TEST,
            detail="操作过于频繁，请稍后再试",
        )
    except HTTPException:
        logger.warning("测试aria2连接被限流 admin_id=%s", admin.id)
        raise

    from app.aria2.client import Aria2Client

    if not payload.aria2_rpc_url:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="aria2 RPC URL 不能为空"
        )

    # secret 处理逻辑：
    # - None: 前端发送 undefined，表示用户未修改（显示掩码），使用数据库密码
    # - 以 * 开头: 掩码（兜底），使用数据库密码
    # - 空字符串: 用户主动清空，用空密码测试
    # - 其他: 用户输入的新密码
    secret = payload.aria2_rpc_secret
    if secret is None or (isinstance(secret, str) and secret.startswith("*")):
        secret = await get_config_value_async("aria2_rpc_secret") or ""

    client = Aria2Client(payload.aria2_rpc_url, secret)

    try:
        version_info = await client.get_version()
        logger.info(
            "测试aria2连接成功 admin_id=%s url=%s", admin.id, payload.aria2_rpc_url
        )
        return {
            "connected": True,
            "version": version_info.get("version"),
            "enabled_features": version_info.get("enabledFeatures", []),
        }
    except Exception as exc:
        logger.warning(
            "测试aria2连接失败 admin_id=%s url=%s error=%s",
            admin.id,
            payload.aria2_rpc_url,
            exc,
        )
        return {
            "connected": False,
            "error": "无法连接到 aria2 服务",
        }


# ============================================================
# Token 管理 API（登录用户）
# ============================================================


class TokenCreateRequest(BaseModel):
    """Token 创建请求体"""

    name: str | None = None  # Token 名称（可选）


def generate_api_token() -> str:
    """生成 API Token，格式: aria2_{24位随机字符}"""
    chars = string.ascii_letters + string.digits
    random_part = "".join(secrets.choice(chars) for _ in range(24))
    return f"aria2_{random_part}"


@router.get("/tokens")
async def list_tokens(user=Depends(require_user)) -> list[dict]:
    """获取当前用户的 Token 列表

    返回:
    - id: Token ID
    - name: Token 名称
    - token: Token 值
    - created_at: 创建时间
    - last_used_at: 最后使用时间
    """
    rows = await auth_repo.list_api_tokens(user.id)
    logger.debug("查询Token列表 user_id=%s count=%s", user.id, len(rows))
    return [
        {
            "id": row["id"],
            "name": row["name"],
            "token": row["token"],
            "created_at": _ms_to_iso(row["created_at_ms"]),
            "last_used_at": _ms_to_iso(row["last_used_at_ms"]),
        }
        for row in rows
    ]


@router.post("/tokens")
async def create_token(
    payload: TokenCreateRequest | None = None, user=Depends(require_user)
) -> dict:
    """生成新的 API Token

    请求体（可选）:
    - name: Token 名称

    返回:
    - id: Token ID
    - name: Token 名称
    - token: Token 值
    - created_at: 创建时间
    """
    token = generate_api_token()
    name = payload.name if payload else None
    row = await auth_repo.create_api_token(user.id, token, name)
    logger.info(
        "创建API Token user_id=%s token_id=%s token_name=%s", user.id, row["id"], name
    )
    return {
        "id": row["id"],
        "name": row["name"],
        "token": row["token"],
        "created_at": _ms_to_iso(row["created_at_ms"]),
    }


@router.delete("/tokens/{token_id}")
async def delete_token(token_id: int, user=Depends(require_user)) -> dict:
    """删除 API Token

    路径参数:
    - token_id: Token ID

    返回:
    - ok: 是否删除成功
    """
    deleted = await auth_repo.delete_api_token(user.id, token_id)
    if not deleted:
        logger.warning(
            "删除Token失败 user_id=%s token_id=%s reason=not_found_or_forbidden",
            user.id,
            token_id,
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Token 不存在"
        )

    logger.info("删除Token成功 user_id=%s token_id=%s", user.id, token_id)

    return {"ok": True}
