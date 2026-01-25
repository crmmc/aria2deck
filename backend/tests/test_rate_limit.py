"""API 频率限制测试

测试场景：
1. 正常使用不触发限制
2. 超过频率限制返回 429
3. 不同用户互不影响
4. 时间窗口过后限制解除
"""
from time import sleep
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.rate_limit import ApiRateLimiter


class TestApiRateLimiter:
    """ApiRateLimiter 单元测试"""

    def test_allows_requests_under_limit(self):
        """测试正常请求不被限制"""
        limiter = ApiRateLimiter()
        user_id = 1
        endpoint = "test"

        # 5 次请求都应该允许
        for _ in range(5):
            assert limiter.is_allowed(user_id, endpoint, limit=5, window_seconds=60)

    def test_blocks_requests_over_limit(self):
        """测试超过限制后被阻止"""
        limiter = ApiRateLimiter()
        user_id = 1
        endpoint = "test"

        # 前 3 次允许
        for _ in range(3):
            assert limiter.is_allowed(user_id, endpoint, limit=3, window_seconds=60)

        # 第 4 次被阻止
        assert not limiter.is_allowed(user_id, endpoint, limit=3, window_seconds=60)

    def test_different_users_independent(self):
        """测试不同用户互不影响"""
        limiter = ApiRateLimiter()
        endpoint = "test"

        # 用户 1 用完配额
        for _ in range(3):
            limiter.is_allowed(1, endpoint, limit=3, window_seconds=60)

        # 用户 1 被阻止
        assert not limiter.is_allowed(1, endpoint, limit=3, window_seconds=60)

        # 用户 2 不受影响
        assert limiter.is_allowed(2, endpoint, limit=3, window_seconds=60)

    def test_different_endpoints_independent(self):
        """测试不同接口互不影响"""
        limiter = ApiRateLimiter()
        user_id = 1

        # 接口 A 用完配额
        for _ in range(3):
            limiter.is_allowed(user_id, "endpoint_a", limit=3, window_seconds=60)

        # 接口 A 被阻止
        assert not limiter.is_allowed(user_id, "endpoint_a", limit=3, window_seconds=60)

        # 接口 B 不受影响
        assert limiter.is_allowed(user_id, "endpoint_b", limit=3, window_seconds=60)

    def test_window_expires(self):
        """测试时间窗口过后限制解除"""
        limiter = ApiRateLimiter()
        user_id = 1
        endpoint = "test"

        # 用完配额
        for _ in range(3):
            limiter.is_allowed(user_id, endpoint, limit=3, window_seconds=1)

        # 被阻止
        assert not limiter.is_allowed(user_id, endpoint, limit=3, window_seconds=1)

        # 等待窗口过期
        sleep(1.1)

        # 应该重新允许
        assert limiter.is_allowed(user_id, endpoint, limit=3, window_seconds=1)

    def test_get_remaining(self):
        """测试获取剩余次数"""
        limiter = ApiRateLimiter()
        user_id = 1
        endpoint = "test"

        # 初始剩余 5 次
        assert limiter.get_remaining(user_id, endpoint, limit=5) == 5

        # 使用 2 次
        limiter.is_allowed(user_id, endpoint, limit=5)
        limiter.is_allowed(user_id, endpoint, limit=5)

        # 剩余 3 次
        assert limiter.get_remaining(user_id, endpoint, limit=5) == 3


class TestApiRateLimitIntegration:
    """API 频率限制集成测试"""

    def test_create_task_rate_limit(
        self, client: TestClient, test_user: dict, user_session: str
    ):
        """测试创建任务频率限制"""
        client.cookies.set(settings.session_cookie_name, user_session)

        # 使用 mock 来模拟频率限制
        with patch('app.routers.tasks.api_limiter') as mock_limiter:
            # 模拟第一次允许
            mock_limiter.is_allowed.return_value = True
            response = client.post(
                "/api/tasks",
                json={"uri": "https://example.com/file.zip"}
            )
            # 可能因为其他原因失败，但不应该是 429
            assert response.status_code != 429 or "频繁" not in response.text

            # 模拟超过限制
            mock_limiter.is_allowed.return_value = False
            response = client.post(
                "/api/tasks",
                json={"uri": "https://example.com/file.zip"}
            )
            assert response.status_code == 429
            assert "频繁" in response.json()["detail"]

    def test_create_pack_rate_limit(
        self, client: TestClient, test_user: dict, user_session: str
    ):
        """测试创建打包任务频率限制"""
        client.cookies.set(settings.session_cookie_name, user_session)

        with patch('app.routers.files.api_limiter') as mock_limiter:
            # 模拟超过限制
            mock_limiter.is_allowed.return_value = False
            response = client.post(
                "/api/files/pack",
                json={"folder_path": "/test"}
            )
            assert response.status_code == 429
            assert "频繁" in response.json()["detail"]
