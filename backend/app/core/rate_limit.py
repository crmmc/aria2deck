"""速率限制器"""
import asyncio
from collections import defaultdict
from time import time


class LoginRateLimiter:
    """基于 IP 的登录速率限制器

    默认: 5 分钟内最多 5 次失败尝试
    """

    def __init__(self, max_attempts: int = 5, window_seconds: int = 300):
        self.max_attempts = max_attempts
        self.window = window_seconds
        self._attempts: dict[str, list[float]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def is_blocked(self, key: str) -> bool:
        async with self._lock:
            now = time()
            self._attempts[key] = [t for t in self._attempts[key] if now - t < self.window]
            return len(self._attempts[key]) >= self.max_attempts

    async def record_failure(self, key: str) -> None:
        async with self._lock:
            self._attempts[key].append(time())

    async def clear(self, key: str) -> None:
        async with self._lock:
            self._attempts.pop(key, None)


class ApiRateLimiter:
    """通用 API 速率限制器

    按 (用户ID, 接口) 组合限流，基于滑动窗口算法
    """

    def __init__(self):
        self._requests: dict[str, list[float]] = defaultdict(list)
        self._lock = asyncio.Lock()

    def _make_key(self, user_id: int, endpoint: str) -> str:
        return f"{user_id}:{endpoint}"

    async def is_allowed(self, user_id: int, endpoint: str, limit: int, window_seconds: int = 60) -> bool:
        key = self._make_key(user_id, endpoint)
        async with self._lock:
            now = time()
            self._requests[key] = [t for t in self._requests[key] if now - t < window_seconds]
            if len(self._requests[key]) >= limit:
                return False
            self._requests[key].append(now)
            return True

    async def get_remaining(self, user_id: int, endpoint: str, limit: int, window_seconds: int = 60) -> int:
        key = self._make_key(user_id, endpoint)
        async with self._lock:
            now = time()
            self._requests[key] = [t for t in self._requests[key] if now - t < window_seconds]
            return max(0, limit - len(self._requests[key]))

    def clear_all(self) -> None:
        self._requests.clear()


login_limiter = LoginRateLimiter()
api_limiter = ApiRateLimiter()
