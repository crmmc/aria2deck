from __future__ import annotations

from collections.abc import Callable

from ...domain.errors import DomainError, ErrorCode
from ...domain.settings import SystemSettings

_AUTHENTICATED_WINDOW_MS = 60_000
_LOGIN_WINDOW_MS = 300_000


class RateLimiter:
    def __init__(
        self, *, settings: Callable[[], SystemSettings], clock_ms: Callable[[], int]
    ) -> None:
        self.settings = settings
        self.clock_ms = clock_ms
        self._buckets: dict[tuple[str, ...], tuple[int, int]] = {}

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

    def check_share_access(self, ip: str) -> None:
        self._check(
            ("share-access", ip),
            self.settings().share_access_rate_limit,
            _AUTHENTICATED_WINDOW_MS,
        )

    def _check(self, key: tuple[str, ...], limit: int, window_ms: int) -> None:
        now_ms = self.clock_ms()
        bucket = self._buckets.get(key)
        if bucket is None or now_ms >= bucket[0] + window_ms:
            self._buckets[key] = (now_ms, 1)
            return
        window_started_ms, count = bucket
        if count >= limit:
            retry_ms = window_started_ms + window_ms - now_ms
            raise DomainError(
                ErrorCode.RATE_LIMITED,
                "请求过于频繁，请稍后重试",
                retry_after_seconds=max(1, (retry_ms + 999) // 1000),
            )
        self._buckets[key] = (window_started_ms, count + 1)
