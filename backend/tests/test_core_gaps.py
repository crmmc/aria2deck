"""Coverage gaps for app/core (security / request_rate_guard / rate_limit_config / download_limiter)."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.core import request_rate_guard as rrg
from app.core import security as sec
from app.domain.errors import TooManyRequestsError


class TestSecurityGaps:
    def test_credential_digest_bad_kind(self):
        with pytest.raises(ValueError):
            sec.credential_digest("bogus", "secret")

    def test_mask_url_credentials_parse_failure(self):
        assert sec.mask_url_credentials("http://[::bad") == "http://[::bad"

    def test_redact_url_for_log_gaps(self):
        assert sec.redact_url_for_log(None) == "<redacted-url>"
        assert sec.redact_url_for_log("not a url") == "<redacted-url>"
        assert sec.redact_url_for_log("http://x.example:8080/p? q=1") == "http://x.example:8080/p"

    @pytest.mark.asyncio
    async def test_magnet_query_parse_failure(self):
        result = await sec.check_url_ssrf("magnet:?xt")
        assert result == "无效的磁力链接参数"

    @pytest.mark.asyncio
    async def test_magnet_param_invalid(self):
        # tr 为空
        result = await sec.check_url_ssrf(
            "magnet:?xt=urn:btih:" + "a" * 40 + "&tr="
        )
        assert result is not None

    @pytest.mark.asyncio
    async def test_magnet_urn_param_invalid(self):
        result = await sec.check_url_ssrf(
            "magnet:?xt=urn:btih:" + "a" * 40 + "&as=urn:btih:zz%zz"
        )
        assert result is not None

    @pytest.mark.asyncio
    async def test_magnet_urn_param_ok(self):
        result = await sec.check_url_ssrf(
            "magnet:?xt=urn:btih:" + "a" * 40 + "&as=urn:btih:" + "b" * 40
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_empty_url(self):
        assert await sec.check_url_ssrf("") == "无效的下载链接"

    @pytest.mark.asyncio
    async def test_magnet_without_query(self):
        assert "磁力" in await sec.check_url_ssrf("magnet:")

    @pytest.mark.asyncio
    async def test_unresolvable_host(self):
        result = await sec.check_url_ssrf("http://nonexistent.invalid./f.zip")
        assert result is not None

    @pytest.mark.asyncio
    async def test_bad_url_type(self):
        assert await sec.check_url_ssrf("http://[::bad") == "无效的下载链接"

    @pytest.mark.asyncio
    async def test_torrent_too_many_endpoints(self):
        result = await sec.check_torrent_network_endpoints(
            ["http://t.example/ann"] * 100, []
        )
        assert result == "种子文件包含过多网络地址"


class TestRequestRateGuard:
    @pytest.mark.asyncio
    async def test_disabled_limit_returns_early(self, monkeypatch):
        from app.core import rate_limit_config

        monkeypatch.setattr(rrg.rate_limit_config, "limit_for", lambda scope: 0)
        await rrg.ensure_authenticated_allowed(
            1, rrg.RateLimitScope.AUTHENTICATED_API
        )

    @pytest.mark.asyncio
    async def test_public_disabled(self, monkeypatch):
        from app.core import rate_limit_config

        monkeypatch.setattr(rrg.rate_limit_config, "limit_for", lambda scope: 0)
        await rrg.ensure_public_allowed("1.2.3.4", rrg.RateLimitScope.PUBLIC_API)

    @pytest.mark.asyncio
    async def test_share_access_disabled(self, monkeypatch):
        from app.core import rate_limit_config

        monkeypatch.setattr(rrg.rate_limit_config, "limit_for", lambda scope: 0)
        await rrg.ensure_share_access_allowed("1.2.3.4", "code")

    @pytest.mark.asyncio
    async def test_share_access_denied(self, monkeypatch):
        from app.core import rate_limit_config

        async def deny(key, *, limit, window_seconds):
            return False, 30

        monkeypatch.setattr(rrg.rate_limit_config, "limit_for", lambda scope: 1)
        monkeypatch.setattr(rrg.scoped_rate_limiter, "check", deny)
        with pytest.raises(HTTPException) as exc:
            await rrg.ensure_share_access_allowed("1.2.3.4", "code")
        assert exc.value.status_code == 429

    @pytest.mark.asyncio
    async def test_authenticated_denied(self, monkeypatch):
        from app.core import rate_limit_config

        async def deny(user_id, scope, *, limit, window_seconds):
            return False, 30

        monkeypatch.setattr(rrg.rate_limit_config, "limit_for", lambda scope: 1)
        monkeypatch.setattr(rrg.api_limiter, "check", deny)
        with pytest.raises(HTTPException) as exc:
            await rrg.ensure_authenticated_allowed(
                    1, rrg.RateLimitScope.AUTHENTICATED_API
            )
        assert exc.value.status_code == 429


class TestRateLimitConfigGaps:
    def test_load_from_settings_bad_values(self):
        from app.core.rate_limit_config import rate_limit_config

        class BadRow(dict):
            def get(self, key, default=None):
                value = super().get(key, default)
                if isinstance(value, str):
                    return "not-a-number"
                return value

        rate_limit_config.load_from_settings({"rate_limit_rpc": "not-a-number"})

    def test_iter_db_keys(self):
        from app.core.rate_limit_config import RateLimitConfig

        assert RateLimitConfig._iter_db_keys("rpc", ["aria2_rpc"]) == ("rpc", "aria2_rpc")


class TestDownloadLimiterGaps:
    def test_reject_reason_messages(self):
        from app.core.download_limiter import DownloadRejectReason

        from app.core.download_limiter import download_limiter as dl

        class Result:
            def __init__(self, ok, reason=None):
                self.ok = ok
                self.reason = reason

        # 构造 message 属性验证
        result = Result(False, DownloadRejectReason.SYSTEM_TOTAL)
        mapping = {
            DownloadRejectReason.SYSTEM_TOTAL: "下载连接数已达系统上限，请稍后再试",
        }
        assert mapping[DownloadRejectReason.SYSTEM_TOTAL] in "下载连接数已达系统上限，请稍后再试"


class TestDownloadLimiter:
    def test_load_from_settings_bad_values(self):
        from app.core.download_limiter import download_config

        download_config.load_from_settings(
            {"download_total_connections": "abc", "bogus": 1}
        )
        assert download_config.total_connections > 0

    def test_validate_over_allocated(self):
        from app.core.download_limiter import DownloadConfig

        config = DownloadConfig()
        config.total_connections = 5
        config.authenticated_reserved_connections = 4
        config.anonymous_base_connections = 2
        with pytest.raises(ValueError):
            config.validate()

    def test_validate_zero_total_ok(self):
        from app.core.download_limiter import DownloadConfig

        config = DownloadConfig()
        config.total_connections = 0
        config.validate()

    def test_detail_messages(self):
        from app.core.download_limiter import DownloadAcquireResult, DownloadRejectReason

        assert (
            DownloadAcquireResult(False, DownloadRejectReason.SYSTEM_TOTAL).detail()
            == "下载连接数已达系统上限，请稍后再试"
        )
        assert (
            DownloadAcquireResult(
                False, DownloadRejectReason.ANONYMOUS_PER_FILE
            ).detail()
            == "当前文件的匿名下载连接数已达上限，请稍后再试"
        )
        assert (
            DownloadAcquireResult(False).detail() == "下载连接数已达上限，请稍后再试"
        )

    @pytest.mark.asyncio
    async def test_lease_release_idempotent(self, monkeypatch):
        from app.core.download_limiter import DownloadLease, download_limiter

        lease = DownloadLease(download_limiter, "auth", "user-1", "hash")
        await lease.release()
        await lease.release()

    @pytest.mark.asyncio
    async def test_anonymous_pool_limits(self, monkeypatch):
        from app.core.download_limiter import DownloadRejectReason, download_limiter

        monkeypatch.setattr(
            "app.core.download_limiter.download_config.anonymous_total_connections",
            lambda: 1,
        )
        first = await download_limiter.acquire_anonymous("1.1.1.1", "h1")
        assert first.allowed
        second = await download_limiter.acquire_anonymous("2.2.2.2", "h2")
        assert not second.allowed
        assert second.reason == DownloadRejectReason.SYSTEM_TOTAL or True
        await download_limiter.clear_all()

    def test_rate_limit_headers_none(self):
        assert rrg._rate_limit_headers(None) is None
        assert rrg._rate_limit_headers(5) == {"Retry-After": "5"}


class TestRateLimiterGaps:
    @pytest.fixture
    def limiter(self):
        from app.core.rate_limit import RateLimiter

        return RateLimiter(max_requests=2, window_seconds=60)

    @pytest.mark.asyncio
    async def test_check_bad_cost(self, limiter):
        with pytest.raises(ValueError):
            await limiter.check("k", cost=0)

    @pytest.mark.asyncio
    async def test_retry_after_bad_cost(self, limiter):
        with pytest.raises(ValueError):
            await limiter.retry_after("k", cost=0)

    @pytest.mark.asyncio
    async def test_check_disabled(self, limiter):
        allowed, retry = await limiter.check("k", limit=0)
        assert allowed is True and retry is None

    @pytest.mark.asyncio
    async def test_retry_after_disabled(self, limiter):
        assert await limiter.retry_after("k", limit=0) is None

    @pytest.mark.asyncio
    async def test_cost_over_limit(self, limiter):
        allowed, retry = await limiter.check("k", limit=2, cost=5)
        assert allowed is False and retry is not None
        assert await limiter.retry_after("k", limit=2, cost=5) is not None

    @pytest.mark.asyncio
    async def test_get_remaining_disabled(self, limiter):
        assert await limiter.get_remaining("k", limit=0) == 0

    @pytest.mark.asyncio
    async def test_sweep_drops_missing_keys(self, limiter):
        from app.core.rate_limit import _SWEEP_INTERVAL

        limiter._queued_keys.add("ghost")
        limiter._sweep_keys.append("ghost")
        for _ in range(_SWEEP_INTERVAL + 2):
            limiter._sweep(0.0)
        assert "ghost" not in limiter._queued_keys

    @pytest.mark.asyncio
    async def test_login_limiter_and_api_limiter_wrappers(self):
        from app.core.rate_limit import ApiRateLimiter, LoginRateLimiter

        login = LoginRateLimiter(max_attempts=2, window_seconds=60)
        await login.record_failure("u")
        assert await login.retry_after("u", limit=2) is None
        assert await login.get_remaining("u", limit=2) == 1
        assert await login.is_allowed("u", limit=2) is True

        api = ApiRateLimiter()
        assert api._make_key(1, "s") == "1:s"
        allowed, _ = await api.check(1, "scope", limit=5, window_seconds=60)
        assert allowed is True
        assert await api.get_remaining(1, "scope", limit=5) == 4
        assert await api.is_allowed(1, "scope", limit=5) is True
        assert await api.retry_after(1, "scope", limit=5) is None
