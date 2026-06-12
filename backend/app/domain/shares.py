from __future__ import annotations


SHARE_ACTIVE_STATUS = "active"
SHARE_REVOKED_STATUS = "revoked"
MAX_ACTIVE_SHARES_PER_FILE = 10


def is_share_expired(expires_at_ms: int | None, *, now_ms: int) -> bool:
    return expires_at_ms is not None and int(expires_at_ms) <= now_ms


def is_share_exhausted(max_downloads: int | None, *, download_count: int) -> bool:
    return max_downloads is not None and download_count >= int(max_downloads)


def is_share_active(
    *,
    status: str,
    expires_at_ms: int | None,
    max_downloads: int | None,
    download_count: int,
    now_ms: int,
) -> bool:
    return (
        status == SHARE_ACTIVE_STATUS
        and not is_share_expired(expires_at_ms, now_ms=now_ms)
        and not is_share_exhausted(max_downloads, download_count=download_count)
    )
