from __future__ import annotations

from collections import deque
from collections.abc import Callable

from ...domain.errors import DomainError, ErrorCode
from ...domain.settings import SystemSettings

_AUTHENTICATED_WINDOW_MS = 60_000
_LOGIN_WINDOW_MS = 300_000
# 摊销清扫边界：每 64 次检查触发一轮、每轮至多处理 128 个键，
# 清扫摊销 ≤2 次桶读/请求（不含 _check 自身的 1 次 get），过期桶在 ⌈队列长度/128⌉×64 次检查内删除。
_SWEEP_INTERVAL = 64
_SWEEP_BATCH_SIZE = 128


class RateLimiter:
    def __init__(
        self, *, settings: Callable[[], SystemSettings], clock_ms: Callable[[], int]
    ) -> None:
        self.settings = settings
        self.clock_ms = clock_ms
        self._buckets: dict[tuple[str, ...], tuple[int, int, int]] = {}
        self._sweep_keys: deque[tuple[str, ...]] = deque()
        self._queued_keys: set[tuple[str, ...]] = set()
        self._operations_since_sweep = 0

    def check_login(self, ip: str, username: str) -> None:
        limit = self.settings().account_security_rate_limit
        self._check(("login-ip", ip), limit, _LOGIN_WINDOW_MS)
        self._check(
            ("login-user", ip, username.strip().casefold()), limit, _LOGIN_WINDOW_MS
        )

    def check_authenticated(self, *, user_id: int) -> None:
        self._check(
            ("authenticated-api", str(user_id)),
            self.settings().authenticated_api_rate_limit,
            _AUTHENTICATED_WINDOW_MS,
        )

    def check_public(self, ip: str) -> None:
        self._check(
            ("public-api", ip),
            self.settings().public_api_rate_limit,
            _AUTHENTICATED_WINDOW_MS,
        )

    def check_create_task(self, *, user_id: int) -> None:
        self._check(
            ("create-task", str(user_id)),
            self.settings().create_task_rate_limit,
            _AUTHENTICATED_WINDOW_MS,
        )

    def check_create_torrent(self, *, user_id: int) -> None:
        self._check(
            ("create-torrent", str(user_id)),
            self.settings().create_torrent_rate_limit,
            _AUTHENTICATED_WINDOW_MS,
        )

    def check_file_search(self, *, user_id: int) -> None:
        self._check(
            ("file-search", str(user_id)),
            self.settings().file_search_rate_limit,
            _AUTHENTICATED_WINDOW_MS,
        )

    def check_create_pack(self, *, user_id: int) -> None:
        self._check(
            ("create-pack", str(user_id)),
            self.settings().create_pack_rate_limit,
            _AUTHENTICATED_WINDOW_MS,
        )

    def check_create_share(self, *, user_id: int) -> None:
        self._check(
            ("create-share", str(user_id)),
            self.settings().create_share_rate_limit,
            _AUTHENTICATED_WINDOW_MS,
        )

    def check_aria2_test(self, *, user_id: int) -> None:
        self._check(
            ("aria2-test", str(user_id)),
            self.settings().aria2_test_rate_limit,
            _AUTHENTICATED_WINDOW_MS,
        )

    def check_rpc(self, ip: str) -> None:
        self._check(
            ("rpc", ip),
            self.settings().rpc_rate_limit,
            _AUTHENTICATED_WINDOW_MS,
        )

    def check_share_access(self, ip: str) -> None:
        self._check(
            ("share-access", ip),
            self.settings().share_access_rate_limit,
            _AUTHENTICATED_WINDOW_MS,
        )

    def _queue_key(self, key: tuple[str, ...]) -> None:
        if key not in self._queued_keys:
            self._queued_keys.add(key)
            self._sweep_keys.append(key)

    def _sweep(self, now_ms: int) -> None:
        for _ in range(min(_SWEEP_BATCH_SIZE, len(self._sweep_keys))):
            key = self._sweep_keys.popleft()
            bucket = self._buckets.get(key)
            if bucket is None:
                self._queued_keys.discard(key)  # 桶已被外部移除，丢弃队列标记
                continue
            window_started_ms, _, window_ms = bucket
            if now_ms >= window_started_ms + window_ms:
                del self._buckets[key]
                self._queued_keys.discard(key)
            else:
                self._sweep_keys.append(key)  # 标记保留：键在队列中仍恰好出现一次

    def _check(self, key: tuple[str, ...], limit: int, window_ms: int) -> None:
        now_ms = self.clock_ms()
        self._operations_since_sweep += 1
        if self._operations_since_sweep >= _SWEEP_INTERVAL:
            self._operations_since_sweep = 0
            self._sweep(now_ms)
        bucket = self._buckets.get(key)
        if bucket is None:
            self._buckets[key] = (now_ms, 1, window_ms)
            self._queue_key(key)
            return
        window_started_ms, count, _ = bucket
        if now_ms >= window_started_ms + window_ms:
            self._buckets[key] = (now_ms, 1, window_ms)
            return
        if count >= limit:
            retry_ms = window_started_ms + window_ms - now_ms
            raise DomainError(
                ErrorCode.RATE_LIMITED,
                "请求过于频繁，请稍后重试",
                retry_after_seconds=max(1, (retry_ms + 999) // 1000),
            )
        self._buckets[key] = (window_started_ms, count + 1, window_ms)
