"""速率限制器"""
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

    def is_blocked(self, key: str) -> bool:
        """检查是否被限制"""
        now = time()
        self._attempts[key] = [t for t in self._attempts[key] if now - t < self.window]
        return len(self._attempts[key]) >= self.max_attempts

    def record_failure(self, key: str) -> None:
        """记录失败尝试"""
        self._attempts[key].append(time())

    def clear(self, key: str) -> None:
        """清除记录（登录成功时调用）"""
        self._attempts.pop(key, None)


class ApiRateLimiter:
    """通用 API 速率限制器

    按 (用户ID, 接口) 组合限流，基于滑动窗口算法
    """

    def __init__(self):
        # key: "{user_id}:{endpoint}" -> list of timestamps
        self._requests: dict[str, list[float]] = defaultdict(list)

    def _make_key(self, user_id: int, endpoint: str) -> str:
        return f"{user_id}:{endpoint}"

    def is_allowed(self, user_id: int, endpoint: str, limit: int, window_seconds: int = 60) -> bool:
        """检查请求是否允许

        Args:
            user_id: 用户 ID
            endpoint: 接口标识（如 "create_task"）
            limit: 时间窗口内最大请求数
            window_seconds: 时间窗口（秒），默认 60 秒

        Returns:
            True 如果允许请求，False 如果超限
        """
        key = self._make_key(user_id, endpoint)
        now = time()

        # 清理过期记录
        self._requests[key] = [t for t in self._requests[key] if now - t < window_seconds]

        # 检查是否超限
        if len(self._requests[key]) >= limit:
            return False

        # 记录本次请求
        self._requests[key].append(now)
        return True

    def get_remaining(self, user_id: int, endpoint: str, limit: int, window_seconds: int = 60) -> int:
        """获取剩余可用次数"""
        key = self._make_key(user_id, endpoint)
        now = time()
        self._requests[key] = [t for t in self._requests[key] if now - t < window_seconds]
        return max(0, limit - len(self._requests[key]))

    def clear_all(self) -> None:
        """清除所有记录（用于测试）"""
        self._requests.clear()


login_limiter = LoginRateLimiter()
api_limiter = ApiRateLimiter()
