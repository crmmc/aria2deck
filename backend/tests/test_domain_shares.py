from __future__ import annotations

from app.domain.shares import (
    MAX_ACTIVE_SHARES_PER_FILE,
    SHARE_ACTIVE_STATUS,
    SHARE_REVOKED_STATUS,
    is_share_active,
    is_share_exhausted,
    is_share_expired,
)


def test_share_status_constants_capture_current_language() -> None:
    assert SHARE_ACTIVE_STATUS == "active"
    assert SHARE_REVOKED_STATUS == "revoked"
    assert MAX_ACTIVE_SHARES_PER_FILE == 10


def test_share_expiry_uses_caller_supplied_time() -> None:
    assert is_share_expired(None, now_ms=1000) is False
    assert is_share_expired(1001, now_ms=1000) is False
    assert is_share_expired(1000, now_ms=1000) is True
    assert is_share_expired(999, now_ms=1000) is True


def test_share_exhaustion_uses_download_limit() -> None:
    assert is_share_exhausted(None, download_count=100) is False
    assert is_share_exhausted(3, download_count=2) is False
    assert is_share_exhausted(3, download_count=3) is True
    assert is_share_exhausted(3, download_count=4) is True


def test_share_active_requires_active_status_not_expired_and_not_exhausted() -> None:
    assert (
        is_share_active(
            status="active",
            expires_at_ms=None,
            max_downloads=None,
            download_count=0,
            now_ms=1000,
        )
        is True
    )
    assert (
        is_share_active(
            status="revoked",
            expires_at_ms=None,
            max_downloads=None,
            download_count=0,
            now_ms=1000,
        )
        is False
    )
    assert (
        is_share_active(
            status="active",
            expires_at_ms=1000,
            max_downloads=None,
            download_count=0,
            now_ms=1000,
        )
        is False
    )
    assert (
        is_share_active(
            status="active",
            expires_at_ms=None,
            max_downloads=1,
            download_count=1,
            now_ms=1000,
        )
        is False
    )
