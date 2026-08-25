"""API 频率限制测试

测试场景：
1. 正常使用不触发限制
2. 超过频率限制返回 429
3. 不同用户互不影响
4. 时间窗口过后限制解除
"""
import asyncio
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.rate_limit import ApiRateLimiter, RateLimiter


class TestApiRateLimiter:
    """ApiRateLimiter 单元测试"""

    async def test_allows_requests_under_limit(self):
        """测试正常请求不被限制"""
        limiter = ApiRateLimiter()
        user_id = 1
        endpoint = "test"

        # 5 次请求都应该允许
        for _ in range(5):
            assert await limiter.is_allowed(user_id, endpoint, limit=5, window_seconds=60)

    async def test_blocks_requests_over_limit(self):
        """测试超过限制后被阻止"""
        limiter = ApiRateLimiter()
        user_id = 1
        endpoint = "test"

        # 前 3 次允许
        for _ in range(3):
            assert await limiter.is_allowed(user_id, endpoint, limit=3, window_seconds=60)

        # 第 4 次被阻止
        assert not await limiter.is_allowed(user_id, endpoint, limit=3, window_seconds=60)

    async def test_different_users_independent(self):
        """测试不同用户互不影响"""
        limiter = ApiRateLimiter()
        endpoint = "test"

        # 用户 1 用完配额
        for _ in range(3):
            await limiter.is_allowed(1, endpoint, limit=3, window_seconds=60)

        # 用户 1 被阻止
        assert not await limiter.is_allowed(1, endpoint, limit=3, window_seconds=60)

        # 用户 2 不受影响
        assert await limiter.is_allowed(2, endpoint, limit=3, window_seconds=60)

    async def test_different_endpoints_independent(self):
        """测试不同接口互不影响"""
        limiter = ApiRateLimiter()
        user_id = 1

        # 接口 A 用完配额
        for _ in range(3):
            await limiter.is_allowed(user_id, "endpoint_a", limit=3, window_seconds=60)

        # 接口 A 被阻止
        assert not await limiter.is_allowed(user_id, "endpoint_a", limit=3, window_seconds=60)

        # 接口 B 不受影响
        assert await limiter.is_allowed(user_id, "endpoint_b", limit=3, window_seconds=60)

    async def test_window_expires(self, monkeypatch):
        """测试时间窗口过后限制解除（时钟推进，不真实 sleep）"""
        current_time = [10.0]
        monkeypatch.setattr(
            "app.core.rate_limit.monotonic", lambda: current_time[0]
        )
        limiter = ApiRateLimiter()
        user_id = 1
        endpoint = "test"

        # 用完配额
        for _ in range(3):
            await limiter.is_allowed(user_id, endpoint, limit=3, window_seconds=1)

        # 被阻止
        assert not await limiter.is_allowed(user_id, endpoint, limit=3, window_seconds=1)

        # 推进时钟使窗口过期
        current_time[0] = 11.1

        # 应该重新允许
        assert await limiter.is_allowed(user_id, endpoint, limit=3, window_seconds=1)

    async def test_get_remaining(self):
        """测试获取剩余次数"""
        limiter = ApiRateLimiter()
        user_id = 1
        endpoint = "test"

        # 初始剩余 5 次
        assert await limiter.get_remaining(user_id, endpoint, limit=5) == 5

        # 使用 2 次
        await limiter.is_allowed(user_id, endpoint, limit=5)
        await limiter.is_allowed(user_id, endpoint, limit=5)

        # 剩余 3 次
        assert await limiter.get_remaining(user_id, endpoint, limit=5) == 3


    async def test_zero_limit_means_unlimited(self):
        limiter = RateLimiter(max_requests=1, window_seconds=60)
        await limiter.record("client")

        assert await limiter.is_allowed("client", limit=0)
        assert not await limiter.is_blocked("client", limit=0)
        assert "client" not in limiter._requests

    async def test_expired_key_is_deleted(self, monkeypatch):
        current_time = [10.0]
        monkeypatch.setattr(
            "app.core.rate_limit.monotonic", lambda: current_time[0]
        )
        limiter = RateLimiter(max_requests=1, window_seconds=1)
        await limiter.record("expired")

        current_time[0] = 11.0

        assert not await limiter.is_blocked("expired")
        assert "expired" not in limiter._requests

    async def test_periodic_sweep_removes_unvisited_keys_in_bounded_batches(
        self, monkeypatch
    ):
        current_time = [10.0]
        monkeypatch.setattr(
            "app.core.rate_limit.monotonic", lambda: current_time[0]
        )
        monkeypatch.setattr("app.core.rate_limit._SWEEP_INTERVAL", 1)
        monkeypatch.setattr("app.core.rate_limit._SWEEP_BATCH_SIZE", 1)
        limiter = RateLimiter(max_requests=1, window_seconds=1)
        stale_keys = {f"stale-{index}" for index in range(3)}
        for key in stale_keys:
            await limiter.record(key)

        current_time[0] = 11.0
        await limiter.is_allowed("fresh-0")
        assert len(stale_keys.intersection(limiter._requests)) == 2

        await limiter.is_allowed("fresh-1")
        await limiter.is_allowed("fresh-2")
        assert stale_keys.isdisjoint(limiter._requests)

    async def test_concurrent_requests_respect_limit(self):
        limiter = RateLimiter(max_requests=10, window_seconds=60)

        results = await asyncio.gather(
            *(limiter.is_allowed("client") for _ in range(50))
        )

        assert sum(results) == 10
        assert len(limiter._requests["client"]) == 10


    async def test_check_returns_retry_after(self, monkeypatch):
        now = [10.0]
        monkeypatch.setattr("app.core.rate_limit.monotonic", lambda: now[0])
        limiter = RateLimiter(max_requests=1, window_seconds=60)

        assert await limiter.is_allowed("client")
        allowed, retry_after = await limiter.check("client")

        assert allowed is False
        assert retry_after == 60

    async def test_cost_is_atomic_when_quota_is_insufficient(self, monkeypatch):
        now = [10.0]
        monkeypatch.setattr("app.core.rate_limit.monotonic", lambda: now[0])
        limiter = RateLimiter(max_requests=5, window_seconds=60)

        assert await limiter.check("client", cost=3) == (True, None)
        assert await limiter.check("client", cost=3) == (False, 60)
        assert len(limiter._requests["client"]) == 3
        assert await limiter.get_remaining("client") == 2

    async def test_cost_larger_than_limit_does_not_create_a_bucket(self):
        limiter = RateLimiter(max_requests=2, window_seconds=60)

        allowed, retry_after = await limiter.check("client", cost=3)

        assert allowed is False
        assert retry_after == 60
        assert "client" not in limiter._requests

    async def test_concurrent_cost_checks_do_not_partially_deduct(self):
        limiter = RateLimiter(max_requests=10, window_seconds=60)

        results = await asyncio.gather(
            *(limiter.check("client", cost=3) for _ in range(5))
        )

        assert sum(allowed for allowed, _ in results) == 3
        assert len(limiter._requests["client"]) == 9


class TestApiRateLimitIntegration:
    """API 频率限制集成测试"""

    @patch("app.services.task_service._get_client")
    def test_create_task_rate_limit(
        self,
        mock_get_client: AsyncMock,
        client: TestClient,
        test_user: dict,
        user_session: str,
    ):
        """数组契约下 create_task 限流为逐项拒绝：HTTP 200，item accepted=false + 中文 error"""
        class _NoCallClient:
            async def multicall(self, calls):
                raise AssertionError("限流项不应触发 aria2 提交")

        mock_get_client.return_value = _NoCallClient()
        client.cookies.set(settings.session_cookie_name, user_session)

        with patch(
            "app.routers.tasks.ensure_authenticated_allowed",
            new=AsyncMock(
                side_effect=HTTPException(429, "操作过于频繁，请稍后再试")
            ),
        ):
            response = client.post(
                "/api/tasks",
                json={"tasks": [{"uri": "https://example.com/file.zip"}]},
            )
            assert response.status_code == 200
            body = response.json()
            assert body["accepted_count"] == 0
            assert body["failed_count"] == 1
            item = body["results"][0]
            assert item["accepted"] is False
            assert "频繁" in item["error"]
            assert item["task_id"] is None

    def test_create_pack_rate_limit(
        self, client: TestClient, test_user: dict, user_session: str
    ):
        """测试创建打包任务频率限制"""
        client.cookies.set(settings.session_cookie_name, user_session)

        with patch("app.routers.files.ensure_authenticated_allowed", new=AsyncMock(side_effect=HTTPException(429, "操作过于频繁，请稍后再试"))):
            response = client.post(
                "/api/files/pack",
                json={"file_ids": [1]}
            )
            assert response.status_code == 429
            assert "频繁" in response.json()["detail"]


    def test_cookie_and_bearer_share_authenticated_api_bucket(
        self, client: TestClient, test_user: dict, user_session: str
    ) -> None:
        from app.core.rate_limit_config import rate_limit_config

        client.cookies.set(settings.session_cookie_name, user_session)
        issued = client.post("/api/config/tokens", json={"name": "shared-bucket"})
        assert issued.status_code == 200

        from app.core.rate_limit import api_limiter

        asyncio.run(api_limiter.clear_all())
        original_limit = rate_limit_config.authenticated_api
        rate_limit_config.authenticated_api = 1
        try:
            assert client.get("/api/tasks").status_code == 200
            client.cookies.clear()
            blocked = client.get(
                "/api/tasks",
                headers={"Authorization": f"Bearer {issued.json()['token']}"},
            )
        finally:
            rate_limit_config.authenticated_api = original_limit

        assert blocked.status_code == 429
        assert blocked.headers["Retry-After"].isdigit()
