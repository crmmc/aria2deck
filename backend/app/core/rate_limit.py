"""速率限制器 - 统一的异步限流实现"""
import asyncio
from collections import defaultdict
from time import time

from app.core.config import settings


class RateLimiter:
    """统一的异步速率限制器，基于滑动窗口算法

    支持两种使用模式：
    1. 固定参数模式：初始化时指定 max_requests 和 window_seconds
    2. 动态参数模式：每次调用时传入 limit 和 window_seconds
    """

    def __init__(self, max_requests: int = 0, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._requests: dict[str, list[float]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def is_allowed(
        self,
        key: str,
        limit: int | None = None,
        window_seconds: int | None = None
    ) -> bool:
        """检查是否允许请求，并记录本次请求"""
        max_req = limit if limit is not None else self.max_requests
        window = window_seconds if window_seconds is not None else self.window_seconds

        async with self._lock:
            now = time()
            self._requests[key] = [t for t in self._requests[key] if now - t < window]
            if len(self._requests[key]) >= max_req:
                return False
            self._requests[key].append(now)
            return True

    async def is_blocked(self, key: str) -> bool:
        """检查是否被阻止（不记录请求）"""
        async with self._lock:
            now = time()
            self._requests[key] = [
                t for t in self._requests[key] if now - t < self.window_seconds
            ]
            return len(self._requests[key]) >= self.max_requests

    async def record(self, key: str) -> None:
        """记录一次请求"""
        async with self._lock:
            self._requests[key].append(time())

    async def clear(self, key: str) -> None:
        """清除指定键的记录"""
        async with self._lock:
            self._requests.pop(key, None)

    async def get_remaining(
        self,
        key: str,
        limit: int | None = None,
        window_seconds: int | None = None
    ) -> int:
        """获取剩余配额"""
        max_req = limit if limit is not None else self.max_requests
        window = window_seconds if window_seconds is not None else self.window_seconds

        async with self._lock:
            now = time()
            self._requests[key] = [t for t in self._requests[key] if now - t < window]
            return max(0, max_req - len(self._requests[key]))

    def clear_all(self) -> None:
        """清除所有记录"""
        self._requests.clear()


# 向后兼容的别名
class LoginRateLimiter(RateLimiter):
    """登录限流器（向后兼容）"""

    def __init__(self, max_attempts: int = 5, window_seconds: int = 300):
        super().__init__(max_requests=max_attempts, window_seconds=window_seconds)

    async def record_failure(self, key: str) -> None:
        await self.record(key)


class ApiRateLimiter(RateLimiter):
    """API 限流器（向后兼容）"""

    def __init__(self) -> None:
        super().__init__(max_requests=0, window_seconds=60)

    @staticmethod
    def _make_key(user_id: int, endpoint: str) -> str:
        return f"{user_id}:{endpoint}"

    async def is_allowed(  # type: ignore[override]
        self,
        user_id: int,
        endpoint: str,
        limit: int,
        window_seconds: int = 60
    ) -> bool:
        key = self._make_key(user_id, endpoint)
        return await super().is_allowed(key, limit, window_seconds)

    async def get_remaining(  # type: ignore[override]
        self,
        user_id: int,
        endpoint: str,
        limit: int,
        window_seconds: int = 60
    ) -> int:
        key = self._make_key(user_id, endpoint)
        return await super().get_remaining(key, limit, window_seconds)


# 预配置的限流器实例
login_limiter = LoginRateLimiter()  # 5次/5分钟
api_limiter = ApiRateLimiter()  # 动态参数
rpc_limiter = RateLimiter(max_requests=settings.rate_limit_rpc, window_seconds=60)  # 默认600次/分钟
