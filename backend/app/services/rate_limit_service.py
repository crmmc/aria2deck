from __future__ import annotations

from app.core.rate_limit import api_limiter, login_limiter
from app.core.rate_limit_config import rate_limit_config
from app.domain.errors import TooManyRequestsError

ACCOUNT_SECURITY_SCOPE = "account_security"


def login_account_key(username: str) -> str:
    return f"login:account:{username.lower()}"


def login_ip_key(client_ip: str) -> str:
    return f"login:ip:{client_ip}"


async def ensure_login_allowed(
    username: str,
    client_ip: str,
    detail: str = "登录尝试次数过多，请稍后再试",
) -> None:
    limit = rate_limit_config.limit_for(ACCOUNT_SECURITY_SCOPE)
    if limit <= 0:
        return

    window = rate_limit_config.window_for(ACCOUNT_SECURITY_SCOPE)
    retries = (
        await login_limiter.retry_after(
            login_account_key(username), limit=limit, window_seconds=window
        ),
        await login_limiter.retry_after(
            login_ip_key(client_ip), limit=limit, window_seconds=window
        ),
    )
    retry_after = min(retry for retry in retries if retry is not None) if any(retries) else None
    if retry_after is not None:
        raise TooManyRequestsError(detail, retry_after=retry_after)


async def record_login_failure(username: str, client_ip: str) -> None:
    if rate_limit_config.limit_for(ACCOUNT_SECURITY_SCOPE) <= 0:
        return
    await login_limiter.record_failure(login_account_key(username))
    await login_limiter.record_failure(login_ip_key(client_ip))


async def clear_login_failures(username: str) -> None:
    if rate_limit_config.limit_for(ACCOUNT_SECURITY_SCOPE) <= 0:
        return
    await login_limiter.clear(login_account_key(username))


async def ensure_account_security_allowed(
    client_ip: str,
    detail: str = "请求过于频繁，请稍后再试",
) -> None:
    limit = rate_limit_config.limit_for(ACCOUNT_SECURITY_SCOPE)
    if limit <= 0:
        return
    retry_after = await login_limiter.retry_after(
        login_ip_key(client_ip),
        limit=limit,
        window_seconds=rate_limit_config.window_for(ACCOUNT_SECURITY_SCOPE),
    )
    if retry_after is not None:
        raise TooManyRequestsError(detail, retry_after=retry_after)


async def ensure_authenticated_allowed(
    user_id: int,
    scope: str,
    detail: str = "请求过于频繁，请稍后再试",
) -> None:
    limit = rate_limit_config.limit_for(scope)
    if limit <= 0:
        return

    allowed, retry_after = await api_limiter.check(
        user_id,
        scope,
        limit=limit,
        window_seconds=rate_limit_config.window_for(scope),
    )
    if not allowed:
        raise TooManyRequestsError(detail, retry_after=retry_after)
