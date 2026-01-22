"""登录速率限制器"""
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


login_limiter = LoginRateLimiter()
