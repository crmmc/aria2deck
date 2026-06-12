from __future__ import annotations

from app.core.rate_limit import api_limiter, login_limiter
from app.core.rate_limit_config import rate_limit_config
from app.domain.errors import TooManyRequestsError

ACCOUNT_SECURITY_SCOPE = "account_security"


async def ensure_account_security_allowed(
    client_ip: str,
    detail: str = "请求过于频繁，请稍后再试",
) -> None:
    limit = rate_limit_config.limit_for(ACCOUNT_SECURITY_SCOPE)
    if await login_limiter.is_blocked(client_ip, limit=limit):
        raise TooManyRequestsError(detail)


async def record_account_security_failure(client_ip: str) -> None:
    await login_limiter.record_failure(client_ip)


async def clear_account_security_failures(client_ip: str) -> None:
    await login_limiter.clear(client_ip)


async def ensure_authenticated_allowed(
    user_id: int,
    scope: str,
    detail: str = "请求过于频繁，请稍后再试",
) -> None:
    limit = rate_limit_config.limit_for(scope)
    if limit <= 0:
        return

    if not await api_limiter.is_allowed(
        user_id,
        scope,
        limit=limit,
        window_seconds=rate_limit_config.window_for(scope),
    ):
        raise TooManyRequestsError(detail)
