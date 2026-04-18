import pytest

from app.core.download_limiter import (
    DownloadRejectReason,
    download_config,
    download_limiter,
)


@pytest.fixture
def download_limits_snapshot():
    snapshot = {
        "total_connections": download_config.total_connections,
        "authenticated_reserved_connections": download_config.authenticated_reserved_connections,
        "authenticated_per_user_connections": download_config.authenticated_per_user_connections,
        "authenticated_per_file_connections": download_config.authenticated_per_file_connections,
        "anonymous_base_connections": download_config.anonymous_base_connections,
        "anonymous_borrow_connections": download_config.anonymous_borrow_connections,
        "anonymous_per_ip_connections": download_config.anonymous_per_ip_connections,
        "anonymous_per_file_connections": download_config.anonymous_per_file_connections,
    }
    yield snapshot
    for key, value in snapshot.items():
        setattr(download_config, key, value)


@pytest.mark.asyncio
async def test_authenticated_per_user_limit(download_limits_snapshot):
    download_config.total_connections = 10
    download_config.authenticated_reserved_connections = 6
    download_config.authenticated_per_user_connections = 2
    download_config.authenticated_per_file_connections = 4
    download_config.anonymous_base_connections = 2
    download_config.anonymous_borrow_connections = 2
    download_config.anonymous_per_ip_connections = 2
    download_config.anonymous_per_file_connections = 1

    first = await download_limiter.acquire_authenticated(1, "file-a")
    second = await download_limiter.acquire_authenticated(1, "file-b")
    blocked = await download_limiter.acquire_authenticated(1, "file-c")

    assert first.allowed is True
    assert second.allowed is True
    assert blocked.allowed is False
    assert blocked.reason == DownloadRejectReason.AUTHENTICATED_PER_USER

    await first.lease.release()
    await second.lease.release()


@pytest.mark.asyncio
async def test_anonymous_limit_preserves_authenticated_reserved_capacity(download_limits_snapshot):
    download_config.total_connections = 10
    download_config.authenticated_reserved_connections = 6
    download_config.authenticated_per_user_connections = 3
    download_config.authenticated_per_file_connections = 3
    download_config.anonymous_base_connections = 2
    download_config.anonymous_borrow_connections = 2
    download_config.anonymous_per_ip_connections = 4
    download_config.anonymous_per_file_connections = 4

    anonymous_leases = []
    for idx in range(4):
        result = await download_limiter.acquire_anonymous(f"10.0.0.{idx}", f"file-{idx}")
        assert result.allowed is True
        anonymous_leases.append(result.lease)

    blocked_anonymous = await download_limiter.acquire_anonymous("10.0.0.99", "file-99")
    assert blocked_anonymous.allowed is False
    assert blocked_anonymous.reason == DownloadRejectReason.ANONYMOUS_POOL

    authenticated_leases = []
    for idx in range(6):
        result = await download_limiter.acquire_authenticated(idx + 1, f"auth-{idx}")
        assert result.allowed is True
        authenticated_leases.append(result.lease)

    blocked_authenticated = await download_limiter.acquire_authenticated(99, "auth-overflow")
    assert blocked_authenticated.allowed is False
    assert blocked_authenticated.reason == DownloadRejectReason.SYSTEM_TOTAL

    for lease in anonymous_leases + authenticated_leases:
        await lease.release()


@pytest.mark.asyncio
async def test_anonymous_per_ip_and_file_limits(download_limits_snapshot):
    download_config.total_connections = 10
    download_config.authenticated_reserved_connections = 6
    download_config.authenticated_per_user_connections = 3
    download_config.authenticated_per_file_connections = 3
    download_config.anonymous_base_connections = 2
    download_config.anonymous_borrow_connections = 2
    download_config.anonymous_per_ip_connections = 2
    download_config.anonymous_per_file_connections = 1

    first = await download_limiter.acquire_anonymous("10.0.0.1", "shared-file")
    same_file = await download_limiter.acquire_anonymous("10.0.0.1", "shared-file")
    second_file = await download_limiter.acquire_anonymous("10.0.0.1", "other-file")
    third_file = await download_limiter.acquire_anonymous("10.0.0.1", "third-file")

    assert first.allowed is True
    assert same_file.allowed is False
    assert same_file.reason == DownloadRejectReason.ANONYMOUS_PER_FILE
    assert second_file.allowed is True
    assert third_file.allowed is False
    assert third_file.reason == DownloadRejectReason.ANONYMOUS_PER_IP

    await first.lease.release()
    await second_file.lease.release()
