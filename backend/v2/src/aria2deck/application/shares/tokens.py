from __future__ import annotations

import secrets
from collections import OrderedDict
from collections.abc import Callable

from ...domain.errors import DomainError, ErrorCode

DEFAULT_CAPACITY = 10_000
DEFAULT_TTL_SECONDS = 1800


class ShareAccessTokenStore:
    """In-memory access tokens bound to a single share code; 30-minute lifetime.

    shortcut: 单实例部署下内存即权威；多实例时需换共享存储，无持久化需求（公开页重验即可）。
    """

    def __init__(
        self,
        *,
        clock_ms: Callable[[], int],
        capacity: int = DEFAULT_CAPACITY,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        if capacity < 1 or ttl_seconds < 1:
            raise DomainError(ErrorCode.INVALID_INPUT, "令牌容量或有效期无效")
        self.clock_ms = clock_ms
        self.capacity = capacity
        self.ttl_seconds = ttl_seconds
        self._tokens: OrderedDict[tuple[str, str], int] = OrderedDict()

    def issue(self, code: str) -> tuple[str, int]:
        now = self.clock_ms()
        self._evict_expired(now)
        while len(self._tokens) >= self.capacity:
            self._evict_lru()
        token = secrets.token_urlsafe(32)
        self._tokens[(code, token)] = now + self.ttl_seconds * 1000
        return token, self.ttl_seconds

    def verify(self, code: str, token: str) -> bool:
        now = self.clock_ms()
        key = (code, token)
        expiry = self._tokens.get(key)
        if expiry is None:
            return False
        if now >= expiry:
            del self._tokens[key]
            return False
        self._tokens.move_to_end(key)
        return True

    def _evict_expired(self, now: int) -> None:
        for key, expiry in list(self._tokens.items()):
            if now >= expiry:
                del self._tokens[key]

    def _evict_lru(self) -> None:
        oldest_key = next(iter(self._tokens), None)
        if oldest_key is not None:
            del self._tokens[oldest_key]
