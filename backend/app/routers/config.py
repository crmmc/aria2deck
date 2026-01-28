"""后台配置接口模块（管理员专用）及 Token 管理"""
from __future__ import annotations

import secrets
import string
from datetime import datetime, timezone
from time import time

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlmodel import select

from app.auth import require_admin, require_user
from app.database import get_session
from app.models import Config, User

_config_cache: dict[str, tuple[str | None, float]] = {}
_CACHE_TTL = 60.0  # 缓存有效期（秒）


router = APIRouter(prefix="/api/config", tags=["config"])


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ConfigUpdate(BaseModel):
    """配置更新请求体"""
    max_task_size: int | None = None  # 单任务最大大小（字节）
    min_free_disk: int | None = None  # 磁盘最小剩余空间（字节）
    aria2_rpc_url: str | None = None  # aria2 RPC URL
    aria2_rpc_secret: str | None = None  # aria2 RPC Secret
    hidden_file_extensions: list[str] | None = None  # 隐藏的文件后缀名列表
    pack_format: str | None = None  # 打包格式 (zip 或 7z)
    pack_compression_level: int | None = None  # 压缩等级 (1-9)
    pack_extra_args: str | None = None  # 7za 附加参数
    # WebSocket 重连参数
    ws_reconnect_max_delay: float | None = None  # 最大重连延迟（秒）
    ws_reconnect_jitter: float | None = None  # 抖动系数 (0-1)
    ws_reconnect_factor: float | None = None  # 指数因子
    # 下载链接 Token 有效期
    download_token_expiry: int | None = None  # 下载链接 Token 有效期（秒）


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
    try:
        conn = sqlite3.connect(settings.database_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT value FROM config WHERE key = ?", [key])
        row = cur.fetchone()
        value = row["value"] if row else None
        cur.close()
        conn.close()
        _config_cache[key] = (value, now)
        return value
    except Exception:
        return None


async def get_config_value_async(key: str) -> str | None:
    """获取单个配置值（带缓存）- 异步版本"""
    now = time()
    if key in _config_cache:
        value, ts = _config_cache[key]
        if now - ts < _CACHE_TTL:
            return value

    async with get_session() as db:
        result = await db.exec(select(Config).where(Config.key == key))
        config = result.first()
        value = config.value if config else None
        _config_cache[key] = (value, now)
        return value


async def set_config_value_async(key: str, value: str) -> None:
    """设置单个配置值 - 异步版本"""
    async with get_session() as db:
        result = await db.exec(select(Config).where(Config.key == key))
        config = result.first()
        if config:
            config.value = value
            db.add(config)
        else:
            db.add(Config(key=key, value=value))
    _config_cache[key] = (value, time())  # 更新缓存


def set_config_value(key: str, value: str) -> None:
    """设置单个配置值 - 同步版本（用于向后兼容）"""
    import sqlite3
    from app.core.config import settings
    conn = sqlite3.connect(settings.database_path)
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
        [key, value]
    )
    conn.commit()
    cur.close()
    conn.close()
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


def get_pack_format() -> str:
    """获取打包格式 (zip 或 7z)，默认 zip"""
    val = get_config_value("pack_format")
    return val if val in ("zip", "7z") else "zip"


def get_pack_compression_level() -> int:
    """获取压缩等级 (1-9)，默认 5"""
    val = get_config_value("pack_compression_level")
    try:
        level = int(val) if val else 5
        return max(1, min(9, level))
    except ValueError:
        return 5


def get_pack_extra_args() -> str:
    """获取 7za 附加参数，默认空字符串"""
    val = get_config_value("pack_extra_args")
    return val if val else ""


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


def get_download_token_expiry() -> int:
    """获取下载链接 Token 有效期（秒），默认 7200（2小时）"""
    val = get_config_value("download_token_expiry")
    try:
        expiry = int(val) if val else 7200
        return max(60, min(86400 * 7, expiry))  # 限制范围 1分钟-7天
    except ValueError:
        return 7200


def generate_download_token(user_id: int, file_path: str) -> str:
    """生成下载链接临时 Token"""
    from itsdangerous import URLSafeTimedSerializer
    from app.core.config import settings
    serializer = URLSafeTimedSerializer(settings.secret_key)
    return serializer.dumps({"user_id": user_id, "path": file_path})


def verify_download_token(token: str) -> dict | None:
    """验证下载链接 Token，返回 {user_id, path} 或 None"""
    from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadSignature
    from app.core.config import settings
    serializer = URLSafeTimedSerializer(settings.secret_key)
    try:
        data = serializer.loads(token, max_age=get_download_token_expiry())
        return data
    except (SignatureExpired, BadSignature):
        return None


@router.get("")
async def get_config(admin: User = Depends(require_admin)) -> dict:
    """获取系统配置（管理员）

    返回:
    - max_task_size: 单任务最大允许大小（字节）
    - min_free_disk: 磁盘最小剩余空间阈值（字节）
    - aria2_rpc_url: aria2 RPC URL
    - aria2_rpc_secret: aria2 RPC Secret（脱敏显示）
    - hidden_file_extensions: 隐藏的文件后缀名列表
    """
    aria2_rpc_url = await get_config_value_async("aria2_rpc_url") or "http://localhost:6800/jsonrpc"
    aria2_rpc_secret = await get_config_value_async("aria2_rpc_secret") or ""

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
        "pack_format": get_pack_format(),
        "pack_compression_level": get_pack_compression_level(),
        "pack_extra_args": get_pack_extra_args(),
        "ws_reconnect_max_delay": get_ws_reconnect_max_delay(),
        "ws_reconnect_jitter": get_ws_reconnect_jitter(),
        "ws_reconnect_factor": get_ws_reconnect_factor(),
        "download_token_expiry": get_download_token_expiry(),
    }


@router.put("")
async def update_config(payload: ConfigUpdate, admin: User = Depends(require_admin)) -> dict:
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
        await set_config_value_async("max_task_size", str(payload.max_task_size))
    if payload.min_free_disk is not None:
        await set_config_value_async("min_free_disk", str(payload.min_free_disk))
    if payload.aria2_rpc_url is not None:
        await set_config_value_async("aria2_rpc_url", payload.aria2_rpc_url)
    if payload.aria2_rpc_secret is not None:
        # 如果是掩码，不更新
        if not payload.aria2_rpc_secret.startswith("*"):
            await set_config_value_async("aria2_rpc_secret", payload.aria2_rpc_secret)
    if payload.hidden_file_extensions is not None:
        # 规范化后缀名：统一小写，确保以点开头
        normalized = []
        for ext in payload.hidden_file_extensions:
            ext = ext.strip().lower()
            if ext and not ext.startswith("."):
                ext = "." + ext
            if ext and ext not in normalized:
                normalized.append(ext)
        await set_config_value_async("hidden_file_extensions", json.dumps(normalized))
    if payload.pack_format is not None:
        if payload.pack_format in ("zip", "7z"):
            await set_config_value_async("pack_format", payload.pack_format)
    if payload.pack_compression_level is not None:
        level = max(1, min(9, payload.pack_compression_level))
        await set_config_value_async("pack_compression_level", str(level))
    if payload.pack_extra_args is not None:
        await set_config_value_async("pack_extra_args", payload.pack_extra_args)
    # WebSocket 重连参数
    if payload.ws_reconnect_max_delay is not None:
        delay = max(1.0, min(300.0, payload.ws_reconnect_max_delay))  # 1-300秒
        await set_config_value_async("ws_reconnect_max_delay", str(delay))
    if payload.ws_reconnect_jitter is not None:
        jitter = max(0.0, min(1.0, payload.ws_reconnect_jitter))  # 0-1
        await set_config_value_async("ws_reconnect_jitter", str(jitter))
    if payload.ws_reconnect_factor is not None:
        factor = max(1.1, min(10.0, payload.ws_reconnect_factor))  # 1.1-10
        await set_config_value_async("ws_reconnect_factor", str(factor))
    if payload.download_token_expiry is not None:
        expiry = max(60, min(86400 * 7, payload.download_token_expiry))  # 1分钟-7天
        await set_config_value_async("download_token_expiry", str(expiry))

    # 返回更新后的配置（secret 脱敏）
    aria2_rpc_url = await get_config_value_async("aria2_rpc_url") or "http://localhost:6800/jsonrpc"
    aria2_rpc_secret = await get_config_value_async("aria2_rpc_secret") or ""
    masked_secret = ""
    if aria2_rpc_secret:
        masked_secret = "*" * min(len(aria2_rpc_secret), 8)

    return {
        "max_task_size": get_max_task_size(),
        "min_free_disk": get_min_free_disk(),
        "aria2_rpc_url": aria2_rpc_url,
        "aria2_rpc_secret": masked_secret,
        "hidden_file_extensions": get_hidden_file_extensions(),
        "pack_format": get_pack_format(),
        "pack_compression_level": get_pack_compression_level(),
        "pack_extra_args": get_pack_extra_args(),
        "ws_reconnect_max_delay": get_ws_reconnect_max_delay(),
        "ws_reconnect_jitter": get_ws_reconnect_jitter(),
        "ws_reconnect_factor": get_ws_reconnect_factor(),
        "download_token_expiry": get_download_token_expiry(),
    }


@router.get("/aria2/version")
async def get_aria2_version(admin: User = Depends(require_admin)) -> dict:
    """获取当前连接的 aria2 版本信息（管理员）

    返回:
    - version: aria2 版本号
    - enabled_features: 启用的功能列表
    - connected: 是否成功连接
    - error: 错误信息（如果连接失败）
    """
    from app.aria2.client import Aria2Client

    aria2_rpc_url = await get_config_value_async("aria2_rpc_url") or "http://localhost:6800/jsonrpc"
    aria2_rpc_secret = await get_config_value_async("aria2_rpc_secret") or ""

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
    admin: User = Depends(require_admin)
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
        secret = await get_config_value_async("aria2_rpc_secret") or ""

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


# ============================================================
# Token 管理 API（登录用户）- 注意：api_tokens 表暂未迁移到 SQLModel
# ============================================================

class TokenCreateRequest(BaseModel):
    """Token 创建请求体"""
    name: str | None = None  # Token 名称（可选）


def generate_api_token() -> str:
    """生成 API Token，格式: aria2_{24位随机字符}"""
    chars = string.ascii_letters + string.digits
    random_part = ''.join(secrets.choice(chars) for _ in range(24))
    return f"aria2_{random_part}"


@router.get("/tokens")
async def list_tokens(user: User = Depends(require_user)) -> list[dict]:
    """获取当前用户的 Token 列表

    返回:
    - id: Token ID
    - name: Token 名称
    - token: Token 值
    - created_at: 创建时间
    - last_used_at: 最后使用时间
    """
    # api_tokens 表暂未迁移，使用原生 SQL
    import sqlite3
    from app.core.config import settings

    conn = sqlite3.connect(settings.database_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        "SELECT id, name, token, created_at, last_used_at FROM api_tokens WHERE user_id = ? ORDER BY created_at DESC",
        [user.id]
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [dict(row) for row in rows]


@router.post("/tokens")
async def create_token(
    payload: TokenCreateRequest = None,
    user: User = Depends(require_user)
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
    import sqlite3
    from app.core.config import settings

    token = generate_api_token()
    name = payload.name if payload else None
    created_at = utc_now()

    conn = sqlite3.connect(settings.database_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO api_tokens (user_id, token, name, created_at) VALUES (?, ?, ?, ?)",
        [user.id, token, name, created_at]
    )
    conn.commit()

    # 获取刚插入的记录
    cur.execute(
        "SELECT id, name, token, created_at FROM api_tokens WHERE token = ?",
        [token]
    )
    row = cur.fetchone()
    cur.close()
    conn.close()

    return dict(row)


@router.delete("/tokens/{token_id}")
async def delete_token(token_id: int, user: User = Depends(require_user)) -> dict:
    """删除 API Token

    路径参数:
    - token_id: Token ID

    返回:
    - ok: 是否删除成功
    """
    import sqlite3
    from app.core.config import settings

    conn = sqlite3.connect(settings.database_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # 检查 Token 是否存在且属于当前用户
    cur.execute(
        "SELECT id, user_id FROM api_tokens WHERE id = ?",
        [token_id]
    )
    row = cur.fetchone()

    if not row:
        cur.close()
        conn.close()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Token 不存在"
        )

    if row["user_id"] != user.id:
        cur.close()
        conn.close()
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="无权删除此 Token"
        )

    cur.execute("DELETE FROM api_tokens WHERE id = ?", [token_id])
    conn.commit()
    cur.close()
    conn.close()

    return {"ok": True}
