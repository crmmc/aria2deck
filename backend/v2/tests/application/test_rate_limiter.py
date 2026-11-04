from __future__ import annotations

import pytest

from aria2deck.application.settings.rate_limit import RateLimiter
from aria2deck.domain.errors import DomainError, ErrorCode
from aria2deck.domain.settings import SystemSettings


async def test_login_rate_limit_uses_ip_username_and_300_second_window() -> None:
    now_ms = 0
    settings = SystemSettings(account_security_rate_limit=2)
    limiter = RateLimiter(settings=lambda: settings, clock_ms=lambda: now_ms)

    limiter.check_login("192.0.2.1", "Admin")
    limiter.check_login("192.0.2.1", "admin")
    with pytest.raises(DomainError) as caught:
        limiter.check_login("192.0.2.1", "other")
    assert caught.value.code is ErrorCode.RATE_LIMITED
    assert caught.value.retry_after_seconds == 300

    limiter.check_login("192.0.2.2", "admin")
    limiter.check_login("192.0.2.2", "admin")
    now_ms = 299_999
    with pytest.raises(DomainError) as boundary:
        limiter.check_login("192.0.2.2", "admin")
    assert boundary.value.retry_after_seconds == 1
    now_ms = 300_000
    limiter.check_login("192.0.2.1", "admin")


async def test_authenticated_rate_limit_shares_one_60_second_bucket_per_user() -> None:
    now_ms = 0
    settings = SystemSettings(authenticated_api_rate_limit=1)
    limiter = RateLimiter(settings=lambda: settings, clock_ms=lambda: now_ms)

    limiter.check_authenticated(user_id=1)
    with pytest.raises(DomainError):
        limiter.check_authenticated(user_id=1)
    limiter.check_authenticated(user_id=2)

    now_ms = 59_000
    with pytest.raises(DomainError) as caught:
        limiter.check_authenticated(user_id=2)
    assert caught.value.retry_after_seconds == 1
    now_ms = 60_000
    limiter.check_authenticated(user_id=1)


async def test_rate_limit_reads_the_latest_settings() -> None:
    settings = SystemSettings(authenticated_api_rate_limit=2)
    limiter = RateLimiter(settings=lambda: settings, clock_ms=lambda: 0)
    limiter.check_authenticated(user_id=1)

    settings = SystemSettings(authenticated_api_rate_limit=1)
    with pytest.raises(DomainError):
        limiter.check_authenticated(user_id=1)
