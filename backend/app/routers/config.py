"""后台配置接口模块（管理员专用）及 Token 管理"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from app.auth import require_admin, require_user
from app.core.request_rate_guard import RateLimitScope, ensure_authenticated_allowed
from app.domain.errors import DomainError
from app.http.errors import raise_http
from app.services import aria2_admin_service, settings_service, token_service

router = APIRouter(prefix="/api/config", tags=["config"])
logger = logging.getLogger(__name__)


class ConfigUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_task_size: int | None = Field(None, ge=0, description="单任务最大大小（字节）")
    min_free_disk: int | None = Field(None, ge=0, description="磁盘最小剩余空间（字节）")
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
    pack_compression_level: int | None = Field(None, ge=0, le=9, description="压缩等级 (0-9)")
    ws_reconnect_max_delay: float | None = Field(None, ge=1.0, le=300.0, description="最大重连延迟（秒）")
    ws_reconnect_jitter: float | None = Field(None, ge=0.0, le=1.0, description="抖动系数 (0-1)")
    ws_reconnect_factor: float | None = Field(None, ge=1.1, le=5.0, description="指数因子")
    site_title: str | None = Field(None, max_length=50, description="网站标题")
    rate_limit_account_security: int | None = Field(None, ge=1, le=100, description="账户安全限流（次/5分钟）")
    rate_limit_authenticated_api: int | None = Field(None, ge=0, le=10000, description="普通已登录 API 限流（次/分钟，0=不限制）")
    rate_limit_public_api: int | None = Field(None, ge=0, le=10000, description="普通匿名公开 API 限流（次/分钟，0=不限制）")
    rate_limit_share_access: int | None = Field(None, ge=1, le=10000, description="分享密码验证限流（次/分钟）")
    rate_limit_create_task: int | None = Field(None, ge=1, le=10000, description="创建任务限流（次/分钟）")
    rate_limit_create_torrent: int | None = Field(None, ge=1, le=10000, description="创建种子限流（次/分钟）")
    rate_limit_create_pack: int | None = Field(None, ge=1, le=10000, description="创建打包限流（次/分钟）")
    rate_limit_aria2_test: int | None = Field(None, ge=1, le=10000, description="aria2测试限流（次/分钟）")
    rate_limit_rpc: int | None = Field(None, ge=1, le=10000, description="JSON-RPC限流（次/分钟）")
    download_total_connections: int | None = Field(None, ge=0, le=10000, description="系统总下载连接上限（0=不限制）")
    download_authenticated_reserved_connections: int | None = Field(None, ge=0, le=10000, description="已登录保底连接数")
    download_authenticated_per_user_connections: int | None = Field(None, ge=0, le=1000, description="已登录单用户最大并发（0=不限制）")
    download_authenticated_per_file_connections: int | None = Field(None, ge=0, le=100, description="已登录单文件最大并发（0=不限制）")
    download_anonymous_base_connections: int | None = Field(None, ge=0, le=10000, description="匿名基础连接数")
    download_anonymous_borrow_connections: int | None = Field(None, ge=0, le=10000, description="匿名可借用连接数")
    download_anonymous_per_ip_connections: int | None = Field(None, ge=0, le=1000, description="匿名单 IP 最大并发（0=不限制）")
    download_anonymous_per_file_connections: int | None = Field(None, ge=0, le=100, description="匿名单文件最大并发（0=不限制）")


class Aria2TestRequest(BaseModel):
    aria2_rpc_url: str
    aria2_rpc_secret: str | None = None


class TokenCreateRequest(BaseModel):
    name: str | None = None


@router.get("/public/site-info")
async def get_public_site_info() -> dict:
    return {"site_title": settings_service.get_site_title()}


@router.get("")
async def get_config(admin=Depends(require_admin)) -> dict:
    logger.debug("获取系统配置 admin_id=%s", admin.id)
    return await settings_service.get_api_settings()


@router.put("")
async def update_config(
    payload: ConfigUpdate, admin=Depends(require_admin)
) -> dict:
    payload_values = {
        key: value for key, value in payload.model_dump().items() if value is not None
    }
    try:
        result = await settings_service.update_api_settings_with_runtime_refresh(
            payload_values,
        )
    except DomainError as exc:
        raise_http(exc)
    logger.info(
        "更新系统配置成功 admin_id=%s changed_keys=%s",
        admin.id,
        ",".join(result.changed_keys) if result.changed_keys else "none",
    )
    return result.settings


@router.get("/aria2/version")
async def get_aria2_version(admin=Depends(require_admin)) -> dict:
    return await aria2_admin_service.get_aria2_version(admin.id)


@router.post("/aria2/test")
async def test_aria2_connection(
    payload: Aria2TestRequest, admin=Depends(require_admin)
) -> dict:
    admin_id = admin.id
    if admin_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未登录")

    try:
        await ensure_authenticated_allowed(
            int(admin_id),
            RateLimitScope.ARIA2_TEST,
            detail="操作过于频繁，请稍后再试",
        )
    except HTTPException:
        logger.warning("测试aria2连接被限流 admin_id=%s", admin.id)
        raise

    try:
        return await aria2_admin_service.test_aria2_connection(
            admin_id=admin.id,
            aria2_rpc_url=payload.aria2_rpc_url,
            aria2_rpc_secret=payload.aria2_rpc_secret,
        )
    except DomainError as exc:
        raise_http(exc)


@router.get("/tokens")
async def list_tokens(user=Depends(require_user)) -> list[dict]:
    return await token_service.list_tokens(user.id)


@router.post("/tokens")
async def create_token(
    payload: TokenCreateRequest | None = None, user=Depends(require_user)
) -> dict:
    return await token_service.create_token(user.id, payload.name if payload else None)


@router.delete("/tokens/{token_id}")
async def delete_token(token_id: int, user=Depends(require_user)) -> dict:
    try:
        return await token_service.delete_token(user.id, token_id)
    except DomainError as exc:
        raise_http(exc)
