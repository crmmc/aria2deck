"""速率限制器 - 统一的异步限流实现"""
import asyncio
from collections import deque
from math import ceil
from time import monotonic

_SWEEP_INTERVAL = 64
_SWEEP_BATCH_SIZE = 128


class RateLimiter:
    """统一的异步速率限制器，基于滑动窗口算法

    支持两种使用模式：
    1. 固定参数模式：初始化时指定 max_requests 和 window_seconds
    2. 动态参数模式：每次调用时传入 limit 和 window_seconds
    """

    def __init__(self, max_requests: int = 0, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._requests: dict[str, list[float]] = {}
        self._windows: dict[str, int] = {}
        self._sweep_keys: deque[str] = deque()
        self._queued_keys: set[str] = set()
        self._operations_since_sweep = 0
        self._lock = asyncio.Lock()

    def _prune_key(self, key: str, now: float, window: int) -> list[float]:
        timestamps = self._requests.get(key)
        if not timestamps:
            self._requests.pop(key, None)
            self._windows.pop(key, None)
            return []

        active = [timestamp for timestamp in timestamps if now - timestamp < window]
        if active:
            self._requests[key] = active
            self._windows[key] = window
        else:
            self._requests.pop(key, None)
            self._windows.pop(key, None)
        return active

    def _queue_key(self, key: str) -> None:
        if key not in self._queued_keys:
            self._queued_keys.add(key)
            self._sweep_keys.append(key)

    def _record(self, key: str, now: float, window: int, cost: int = 1) -> None:
        self._queue_key(key)
        self._requests.setdefault(key, []).extend([now] * cost)
        self._windows[key] = window

    @staticmethod
    def _retry_after(
        timestamps: list[float], max_requests: int, cost: int, now: float, window: int
    ) -> int:
        expired_count = len(timestamps) + cost - max_requests
        retry_timestamp = timestamps[expired_count - 1]
        return max(1, ceil(window - (now - retry_timestamp)))

    def _sweep(self, now: float) -> None:
        self._operations_since_sweep += 1
        if self._operations_since_sweep < _SWEEP_INTERVAL:
            return
        self._operations_since_sweep = 0

        for _ in range(min(_SWEEP_BATCH_SIZE, len(self._sweep_keys))):
            key = self._sweep_keys.popleft()
            timestamps = self._requests.get(key)
            if timestamps is None:
                self._queued_keys.discard(key)
                continue

            window = self._windows.get(key, self.window_seconds)
            if self._prune_key(key, now, window):
                self._sweep_keys.append(key)
            else:
                self._queued_keys.discard(key)

    async def check(
        self,
        key: str,
        limit: int | None = None,
        window_seconds: int | None = None,
        cost: int = 1,
    ) -> tuple[bool, int | None]:
        """原子检查并记录 ``cost`` 次请求，拒绝时不扣减任何额度。"""
        if cost < 1:
            raise ValueError("cost must be positive")

        max_req = limit if limit is not None else self.max_requests
        window = window_seconds if window_seconds is not None else self.window_seconds
        if max_req <= 0:
            await self.clear(key)
            return True, None
        if cost > max_req:
            return False, max(1, ceil(window))

        async with self._lock:
            now = monotonic()
            self._sweep(now)
            timestamps = self._prune_key(key, now, window)
            if len(timestamps) + cost > max_req:
                return False, self._retry_after(timestamps, max_req, cost, now, window)
            self._record(key, now, window, cost)
            return True, None

    async def retry_after(
        self,
        key: str,
        limit: int | None = None,
        window_seconds: int | None = None,
        cost: int = 1,
    ) -> int | None:
        """返回为 ``cost`` 次请求腾出容量所需的最早重试秒数。"""
        if cost < 1:
            raise ValueError("cost must be positive")

        max_req = limit if limit is not None else self.max_requests
        window = window_seconds if window_seconds is not None else self.window_seconds
        if max_req <= 0:
            await self.clear(key)
            return None
        if cost > max_req:
            return max(1, ceil(window))

        async with self._lock:
            now = monotonic()
            self._sweep(now)
            timestamps = self._prune_key(key, now, window)
            if len(timestamps) + cost <= max_req:
                return None
            return self._retry_after(timestamps, max_req, cost, now, window)

    async def is_allowed(
        self,
        key: str,
        limit: int | None = None,
        window_seconds: int | None = None,
        cost: int = 1,
    ) -> bool:
        """原子检查并记录 ``cost`` 次请求。"""
        allowed, _ = await RateLimiter.check(
            self, key, limit, window_seconds, cost
        )
        return allowed

    async def is_blocked(self, key: str, limit: int | None = None) -> bool:
        """检查是否被阻止（不记录请求）"""
        max_req = limit if limit is not None else self.max_requests
        if max_req <= 0:
            await self.clear(key)
            return False

        async with self._lock:
            now = monotonic()
            self._sweep(now)
            timestamps = self._prune_key(key, now, self.window_seconds)
            return len(timestamps) >= max_req

    async def record(self, key: str) -> None:
        """记录一次请求"""
        async with self._lock:
            now = monotonic()
            self._sweep(now)
            self._record(key, now, self.window_seconds)

    async def clear(self, key: str) -> None:
        """清除指定键的记录"""
        async with self._lock:
            self._sweep(monotonic())
            self._requests.pop(key, None)
            self._windows.pop(key, None)

    async def get_remaining(
        self,
        key: str,
        limit: int | None = None,
        window_seconds: int | None = None,
    ) -> int:
        """获取剩余配额"""
        max_req = limit if limit is not None else self.max_requests
        window = window_seconds if window_seconds is not None else self.window_seconds
        if max_req <= 0:
            await self.clear(key)
            return 0

        async with self._lock:
            now = monotonic()
            self._sweep(now)
            timestamps = self._prune_key(key, now, window)
            return max(0, max_req - len(timestamps))

    async def clear_all(self) -> None:
        """清除所有记录"""
        async with self._lock:
            self._requests.clear()
            self._windows.clear()
            self._sweep_keys.clear()
            self._queued_keys.clear()
            self._operations_since_sweep = 0


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

    async def check(  # type: ignore[override]
        self,
        user_id: int,
        endpoint: str,
        limit: int,
        window_seconds: int = 60,
    ) -> tuple[bool, int | None]:
        return await super().check(
            self._make_key(user_id, endpoint), limit, window_seconds
        )

    async def retry_after(  # type: ignore[override]
        self,
        user_id: int,
        endpoint: str,
        limit: int,
        window_seconds: int = 60,
    ) -> int | None:
        return await super().retry_after(
            self._make_key(user_id, endpoint), limit, window_seconds
        )

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
rpc_limiter = RateLimiter(max_requests=0, window_seconds=60)  # 动态参数，由 rate_limit_config 提供 limit
