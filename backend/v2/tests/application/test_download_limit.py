"""A6：下载并发连接限流执行体（对齐旧版 DownloadAccessManager 语义）。"""

from __future__ import annotations

import pytest

from aria2deck.application.settings.download_limit import DownloadConnectionLimiter
from aria2deck.domain.errors import DomainError, ErrorCode
from aria2deck.domain.settings import SystemSettings


def limiter(limits: dict[str, int] | None = None) -> DownloadConnectionLimiter:
    settings = SystemSettings(
        download_connection_limits=limits or {}, aria2_rpc_secret=None
    )
    return DownloadConnectionLimiter(settings=lambda: settings)


async def test_defaults_apply_when_dict_empty() -> None:
    manager = limiter()
    # 默认：每文件 8、每用户 16。同一文件第 9 次先撞文件档。
    for _ in range(8):
        assert await manager.acquire_authenticated(user_id=1, file_key="f1") is not None
    with pytest.raises(DomainError) as per_file:
        await manager.acquire_authenticated(user_id=1, file_key="f1")
    assert str(per_file.value) == "当前文件的下载连接数已达上限，请稍后再试"
    # 换文件可继续，直到用户档 16 满。
    for _ in range(8):
        assert await manager.acquire_authenticated(user_id=1, file_key="f2") is not None
    with pytest.raises(DomainError) as per_user:
        await manager.acquire_authenticated(user_id=1, file_key="f3")
    assert per_user.value.code is ErrorCode.RATE_LIMITED
    assert str(per_user.value) == "当前账号的下载连接数已达上限，请稍后再试"


async def test_per_file_limit_is_independent_per_user_and_file() -> None:
    manager = limiter({"authenticated_per_file_connections": 2})
    first = await manager.acquire_authenticated(user_id=1, file_key="a")
    second = await manager.acquire_authenticated(user_id=1, file_key="a")
    assert first is not None and second is not None
    with pytest.raises(DomainError) as caught:
        await manager.acquire_authenticated(user_id=1, file_key="a")
    assert str(caught.value) == "当前文件的下载连接数已达上限，请稍后再试"
    # 其他文件 / 其他用户不受影响。
    assert await manager.acquire_authenticated(user_id=1, file_key="b") is not None
    assert await manager.acquire_authenticated(user_id=2, file_key="a") is not None


async def test_total_limit_spans_authenticated_and_anonymous() -> None:
    manager = limiter({"total_connections": 3})
    leases = [
        await manager.acquire_authenticated(user_id=1, file_key="a"),
        await manager.acquire_authenticated(user_id=2, file_key="b"),
        await manager.acquire_anonymous(client_ip="9.9.9.9", file_key="c"),
    ]
    assert all(lease is not None for lease in leases)
    with pytest.raises(DomainError) as caught_auth:
        await manager.acquire_authenticated(user_id=3, file_key="d")
    with pytest.raises(DomainError) as caught_anon:
        await manager.acquire_anonymous(client_ip="8.8.8.8", file_key="e")
    assert str(caught_auth.value) == "下载连接数已达系统上限，请稍后再试"
    assert str(caught_anon.value) == "下载连接数已达系统上限，请稍后再试"
    await leases[2].release()
    assert await manager.acquire_anonymous(client_ip="8.8.8.8", file_key="e") is not None


async def test_anonymous_tiers_pool_ip_and_file() -> None:
    manager = limiter(
        {
            "anonymous_total_connections": 2,
            "anonymous_per_ip_connections": 1,
            "anonymous_per_file_connections": 1,
        }
    )
    assert await manager.acquire_anonymous(client_ip="1.1.1.1", file_key="s") is not None
    with pytest.raises(DomainError) as per_ip:
        await manager.acquire_anonymous(client_ip="1.1.1.1", file_key="s2")
    assert str(per_ip.value) == "当前来源的下载连接数已达上限，请稍后再试"
    # 不同 IP 可用，但受匿名池总量约束：池=2，已占 1，再进一个后池满。
    assert await manager.acquire_anonymous(client_ip="2.2.2.2", file_key="s") is not None
    with pytest.raises(DomainError) as pool:
        await manager.acquire_anonymous(client_ip="3.3.3.3", file_key="s")
    assert str(pool.value) == "匿名下载连接数已达上限，请稍后再试"


async def test_release_is_idempotent_and_counts_never_go_negative() -> None:
    manager = limiter({"authenticated_per_user_connections": 1})
    lease = await manager.acquire_authenticated(user_id=5, file_key="x")
    await lease.release()
    await lease.release()
    assert await manager.acquire_authenticated(user_id=5, file_key="x") is not None


async def test_limits_reload_from_settings_without_rebuild() -> None:
    settings = SystemSettings()
    manager = DownloadConnectionLimiter(settings=lambda: settings)
    assert await manager.acquire_authenticated(user_id=1, file_key="a") is not None
    settings = SystemSettings(
        download_connection_limits={"authenticated_per_user_connections": 1}
    )
    manager.settings = lambda: settings
    with pytest.raises(DomainError):
        await manager.acquire_authenticated(user_id=1, file_key="a")


async def test_zero_total_and_authenticated_tiers_mean_unlimited() -> None:
    """0 值语义对齐旧版：系统总量与认证分档 0=不限（越过默认 8/16/100 仍放行）。"""
    manager = limiter(
        {
            "total_connections": 0,
            "authenticated_per_user_connections": 0,
            "authenticated_per_file_connections": 0,
        }
    )
    leases = [
        await manager.acquire_authenticated(user_id=1, file_key="a") for _ in range(101)
    ]
    await leases[0].release()
    assert await manager.acquire_authenticated(user_id=1, file_key="a") is not None


async def test_zero_anonymous_per_ip_and_per_file_mean_unlimited() -> None:
    """0 值语义对齐旧版：匿名每 IP / 每文件分档 0=不限（越过默认 4/2 仍放行）。"""
    manager = limiter(
        {
            "anonymous_per_ip_connections": 0,
            "anonymous_per_file_connections": 0,
        }
    )
    for _ in range(5):
        assert await manager.acquire_anonymous(client_ip="1.1.1.1", file_key="s") is not None


async def test_zero_anonymous_pool_rejects_all_anonymous_but_not_authenticated() -> None:
    """0 值语义对齐旧版：匿名池 0=全面拒绝匿名下载，认证下载不受影响。"""
    manager = limiter({"anonymous_total_connections": 0})
    with pytest.raises(DomainError) as caught:
        await manager.acquire_anonymous(client_ip="1.1.1.1", file_key="s")
    assert str(caught.value) == "匿名下载连接数已达上限，请稍后再试"
    assert await manager.acquire_authenticated(user_id=1, file_key="a") is not None
