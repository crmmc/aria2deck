"""统一的请求频率限制入口。"""
from __future__ import annotations

from enum import StrEnum

from fastapi import HTTPException, Request, status

from app.core.rate_limit import RateLimiter, api_limiter, login_limiter, rpc_limiter
from app.core.rate_limit_config import rate_limit_config


class RateLimitScope(StrEnum):
    ACCOUNT_SECURITY = "account_security"
    AUTHENTICATED_API = "authenticated_api"
    CREATE_TASK = "create_task"
    CREATE_TORRENT = "create_torrent"
    CREATE_PACK = "create_pack"
    ARIA2_TEST = "aria2_test"
    PUBLIC_API = "public_api"
    SHARE_ACCESS = "share_access"
    AUTHENTICATED_DOWNLOAD = "authenticated_download"
    ANONYMOUS_DOWNLOAD = "anonymous_download"
    RPC = "rpc"


scoped_rate_limiter = RateLimiter(max_requests=0, window_seconds=60)


def client_ip_from_request(request: Request) -> str:
    """从请求对象中提取客户端 IP。"""
    return request.client.host if request.client and request.client.host else "unknown"


async def ensure_account_security_allowed(
    client_ip: str,
    detail: str = "请求过于频繁，请稍后再试",
) -> None:
    """校验账户安全相关请求限流。"""
    limit = rate_limit_config.limit_for(RateLimitScope.ACCOUNT_SECURITY.value)
    if await login_limiter.is_blocked(client_ip, limit=limit):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=detail)


async def record_account_security_failure(client_ip: str) -> None:
    """记录一次账户安全类失败尝试。"""
    await login_limiter.record_failure(client_ip)


async def clear_account_security_failures(client_ip: str) -> None:
    """清除账户安全类失败记录。"""
    await login_limiter.clear(client_ip)


async def ensure_authenticated_allowed(
    user_id: int,
    scope: RateLimitScope,
    detail: str = "请求过于频繁，请稍后再试",
) -> None:
    """校验已登录用户请求限流。"""
    limit = rate_limit_config.limit_for(scope.value)
    if limit <= 0:
        return

    if not await api_limiter.is_allowed(
        user_id,
        scope.value,
        limit=limit,
        window_seconds=rate_limit_config.window_for(scope.value),
    ):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=detail)


async def ensure_public_allowed(
    client_ip: str,
    scope: RateLimitScope,
    detail: str = "请求过于频繁，请稍后再试",
) -> None:
    """校验匿名公开请求限流。"""
    limit = rate_limit_config.limit_for(scope.value)
    if limit <= 0:
        return

    if not await scoped_rate_limiter.is_allowed(
        f"{scope.value}:{client_ip}",
        limit=limit,
        window_seconds=rate_limit_config.window_for(scope.value),
    ):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=detail)


async def ensure_share_access_allowed(
    client_ip: str,
    share_code: str,
    detail: str = "请求过于频繁，请稍后再试",
) -> None:
    """校验分享密码验证接口限流，同时限制 IP 和分享码。"""
    limit = rate_limit_config.limit_for(RateLimitScope.SHARE_ACCESS.value)
    if limit <= 0:
        return

    window = rate_limit_config.window_for(RateLimitScope.SHARE_ACCESS.value)
    ip_allowed = await scoped_rate_limiter.is_allowed(
        f"{RateLimitScope.SHARE_ACCESS.value}:ip:{client_ip}",
        limit=limit,
        window_seconds=window,
    )
    if not ip_allowed:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=detail)

    code_allowed = await scoped_rate_limiter.is_allowed(
        f"{RateLimitScope.SHARE_ACCESS.value}:code:{share_code}",
        limit=limit,
        window_seconds=window,
    )
    if not code_allowed:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=detail)


async def ensure_rpc_allowed(
    client_ip: str,
    detail: str = "请求过于频繁，请稍后再试",
) -> None:
    """校验 JSON-RPC 请求限流。"""
    limit = rate_limit_config.limit_for(RateLimitScope.RPC.value)
    if limit <= 0:
        return

    if not await rpc_limiter.is_allowed(
        client_ip,
        limit=limit,
        window_seconds=rate_limit_config.window_for(RateLimitScope.RPC.value),
    ):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=detail)
