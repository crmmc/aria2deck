from __future__ import annotations

import pytest

from aria2deck.application.settings.rate_limit import RateLimiter
from aria2deck.domain.errors import DomainError, ErrorCode
from aria2deck.domain.settings import SystemSettings


async def test_login_rate_limit_uses_ip_username_and_300_second_window() -> None:
    now_ms = 0
    settings = SystemSettings(account_security_rate_limit=2)
    limiter = RateLimiter(settings=lambda: settings, clock_ms=lambda: now_ms)

    limiter.check_login("192.0.2.1", "Admin")
    limiter.check_login("192.0.2.1", "admin")
    with pytest.raises(DomainError) as caught:
        limiter.check_login("192.0.2.1", "other")
    assert caught.value.code is ErrorCode.RATE_LIMITED
    assert caught.value.retry_after_seconds == 300

    limiter.check_login("192.0.2.2", "admin")
    limiter.check_login("192.0.2.2", "admin")
    now_ms = 299_999
    with pytest.raises(DomainError) as boundary:
        limiter.check_login("192.0.2.2", "admin")
    assert boundary.value.retry_after_seconds == 1
    now_ms = 300_000
    limiter.check_login("192.0.2.1", "admin")


async def test_authenticated_rate_limit_shares_one_60_second_bucket_per_user() -> None:
    now_ms = 0
    settings = SystemSettings(authenticated_api_rate_limit=1)
    limiter = RateLimiter(settings=lambda: settings, clock_ms=lambda: now_ms)

    limiter.check_authenticated(user_id=1)
    with pytest.raises(DomainError):
        limiter.check_authenticated(user_id=1)
    limiter.check_authenticated(user_id=2)

    now_ms = 59_000
    with pytest.raises(DomainError) as caught:
        limiter.check_authenticated(user_id=2)
    assert caught.value.retry_after_seconds == 1
    now_ms = 60_000
    limiter.check_authenticated(user_id=1)


async def test_rate_limit_reads_the_latest_settings() -> None:
    settings = SystemSettings(authenticated_api_rate_limit=2)
    limiter = RateLimiter(settings=lambda: settings, clock_ms=lambda: 0)
    limiter.check_authenticated(user_id=1)

    settings = SystemSettings(authenticated_api_rate_limit=1)
    with pytest.raises(DomainError):
        limiter.check_authenticated(user_id=1)


@pytest.mark.parametrize(
    "method_name,setting_name,kwargs,window_seconds",
    [
        ("check_login", "account_security_rate_limit", {"ip": "192.0.2.1", "username": "admin"}, 300),
        ("check_authenticated", "authenticated_api_rate_limit", {"user_id": 1}, 60),
        ("check_public", "public_api_rate_limit", {"ip": "192.0.2.1"}, 60),
        ("check_share_access", "share_access_rate_limit", {"ip": "192.0.2.1"}, 60),
        ("check_create_task", "create_task_rate_limit", {"user_id": 1}, 60),
        ("check_create_torrent", "create_torrent_rate_limit", {"user_id": 1}, 60),
        ("check_create_pack", "create_pack_rate_limit", {"user_id": 1}, 60),
        ("check_aria2_test", "aria2_test_rate_limit", {"user_id": 1}, 60),
        ("check_rpc", "rpc_rate_limit", {"ip": "192.0.2.1"}, 60),
        ("check_file_search", "file_search_rate_limit", {"user_id": 1}, 60),
        ("check_create_share", "create_share_rate_limit", {"user_id": 1}, 60),
    ],
)
async def test_every_rate_limiter_method_enforces_its_non_default_setting(
    method_name: str,
    setting_name: str,
    kwargs: dict[str, object],
    window_seconds: int,
) -> None:
    settings = SystemSettings(**{setting_name: 1})
    limiter = RateLimiter(settings=lambda: settings, clock_ms=lambda: 0)
    check = getattr(limiter, method_name)

    check(**kwargs)
    with pytest.raises(DomainError) as caught:
        check(**kwargs)

    assert caught.value.code is ErrorCode.RATE_LIMITED
    assert caught.value.retry_after_seconds == window_seconds


async def test_public_and_share_access_limits_use_independent_ip_buckets() -> None:
    settings = SystemSettings(public_api_rate_limit=1, share_access_rate_limit=1)
    limiter = RateLimiter(settings=lambda: settings, clock_ms=lambda: 0)

    limiter.check_public("192.0.2.1")
    limiter.check_share_access("192.0.2.1")

    with pytest.raises(DomainError):
        limiter.check_public("192.0.2.1")
    with pytest.raises(DomainError):
        limiter.check_share_access("192.0.2.1")


async def test_expired_buckets_are_reclaimed_after_sweep() -> None:
    now_ms = 0
    settings = SystemSettings(public_api_rate_limit=5)
    limiter = RateLimiter(settings=lambda: settings, clock_ms=lambda: now_ms)

    for index in range(300):
        limiter.check_public(f"192.0.2.{index}")
    assert len(limiter._buckets) == 300

    now_ms = 60_000  # 300 个窗口全部过期
    for _ in range(192):  # ≥ ⌈301/128⌉ × 64 次检查，保证 3 轮完整清扫
        try:
            limiter.check_public("198.51.100.1")
        except DomainError:
            pass  # limit=5，拒绝路径也必须驱动清扫

    assert len(limiter._buckets) == 1  # 修复前此处为 301：过期桶永久驻留
    assert ("public-api", "198.51.100.1") in limiter._buckets


async def test_semantic_anchor_rejection_keeps_window_start_and_retry() -> None:
    now_ms = 0
    settings = SystemSettings(public_api_rate_limit=1)
    limiter = RateLimiter(settings=lambda: settings, clock_ms=lambda: now_ms)

    limiter.check_public("192.0.2.1")
    for advance_ms, expected_retry in ((30_000, 30), (59_000, 1), (59_999, 1)):
        now_ms = advance_ms
        with pytest.raises(DomainError) as caught:
            limiter.check_public("192.0.2.1")
        assert caught.value.retry_after_seconds == expected_retry
    now_ms = 60_000  # 恰好在窗口边界 → 新窗口放行
    limiter.check_public("192.0.2.1")


async def test_semantic_anchor_expired_key_reappears_as_new_window() -> None:
    now_ms = 0
    settings = SystemSettings(public_api_rate_limit=1)
    limiter = RateLimiter(settings=lambda: settings, clock_ms=lambda: now_ms)

    limiter.check_public("192.0.2.1")
    with pytest.raises(DomainError):
        limiter.check_public("192.0.2.1")
    now_ms = 60_000
    limiter.check_public("192.0.2.1")  # 过期后重新出现的键按新窗口处理
    with pytest.raises(DomainError) as caught:
        limiter.check_public("192.0.2.1")
    assert caught.value.retry_after_seconds == 60


async def test_semantic_anchor_interleaved_keys_do_not_share_windows() -> None:
    now_ms = 0
    settings = SystemSettings(public_api_rate_limit=2)
    limiter = RateLimiter(settings=lambda: settings, clock_ms=lambda: now_ms)

    limiter.check_public("192.0.2.1")
    limiter.check_public("192.0.2.1")
    for index in range(500):  # 500 个旁路键（修复后会触发多轮清扫）不得影响受害键
        limiter.check_public(f"198.51.100.{index % 250}.{index // 250}")
    with pytest.raises(DomainError) as caught:
        limiter.check_public("192.0.2.1")
    assert caught.value.retry_after_seconds == 60  # 窗口起点未被旁路键或拒绝移动
    now_ms = 60_000
    limiter.check_public("192.0.2.1")


SCOPE_SWEEP_CASES = [
    ("check_login", {"ip": "192.0.2.1", "username": "admin"}, 300),
    ("check_authenticated", {"user_id": 1}, 60),
    ("check_public", {"ip": "192.0.2.1"}, 60),
    ("check_create_task", {"user_id": 1}, 60),
    ("check_create_torrent", {"user_id": 1}, 60),
    ("check_create_pack", {"user_id": 1}, 60),
    ("check_create_share", {"user_id": 1}, 60),
    ("check_file_search", {"user_id": 1}, 60),
    ("check_aria2_test", {"user_id": 1}, 60),
    ("check_rpc", {"ip": "192.0.2.1"}, 60),
    ("check_share_access", {"ip": "192.0.2.1"}, 60),
]


@pytest.mark.parametrize("method_name,kwargs,window_seconds", SCOPE_SWEEP_CASES)
async def test_sweep_lifecycle_covers_every_rate_limit_scope(
    method_name: str,
    kwargs: dict[str, object],
    window_seconds: int,
) -> None:
    now_ms = 0
    settings = SystemSettings(public_api_rate_limit=1, account_security_rate_limit=1)
    limiter = RateLimiter(settings=lambda: settings, clock_ms=lambda: now_ms)
    check = getattr(limiter, method_name)

    check(**kwargs)  # 造一个该 scope 的活跃桶
    now_ms = window_seconds * 1000  # 推进越过该 scope 的窗口
    pump_key = "198.51.100.1"
    for _ in range(256):  # 4 轮清扫，足以清空任意小队列
        try:
            limiter.check_public(pump_key)
        except DomainError:
            pass
    scope_keys = [key for key in limiter._buckets if key != ("public-api", pump_key)]
    assert scope_keys == []  # 该 scope 的过期桶已被清理
    assert list(limiter._buckets) == [("public-api", pump_key)]


async def test_sweep_lifecycle_active_bucket_survives_high_churn() -> None:
    now_ms = 0
    settings = SystemSettings(public_api_rate_limit=10_000)
    limiter = RateLimiter(settings=lambda: settings, clock_ms=lambda: now_ms)

    victim_checks = 0
    for index in range(500):
        limiter.check_public(f"198.51.100.{index % 250}.{index // 250}")
        if index % 10 == 0:
            limiter.check_public("192.0.2.1")
            victim_checks += 1

    victim = limiter._buckets[("public-api", "192.0.2.1")]
    assert victim == (0, victim_checks, 60_000)  # 计数与窗口起点精确不变
    assert len(limiter._buckets) == 501
    assert len(limiter._sweep_keys) == 501  # 不变量 I1：字典/队列/集合三方一致
    assert len(limiter._queued_keys) == 501


@pytest.mark.parametrize("stale_count", [1, 127, 128, 129, 300])
async def test_sweep_lifecycle_expired_bucket_deleted_within_bounded_ops(
    stale_count: int,
) -> None:
    now_ms = 0
    settings = SystemSettings(public_api_rate_limit=1)
    limiter = RateLimiter(settings=lambda: settings, clock_ms=lambda: now_ms)

    for index in range(stale_count):
        limiter.check_public(f"192.0.2.{index}")
    now_ms = 60_000
    pump_ops = -(-(stale_count + 1) // 128) * 64  # ⌈队列/128⌉ × 64 次检查
    for _ in range(pump_ops):
        try:
            limiter.check_public("198.51.100.1")
        except DomainError:
            pass
    remaining_stale = [
        key for key in limiter._buckets if key != ("public-api", "198.51.100.1")
    ]
    assert remaining_stale == []  # 过期桶在声明的上界内全部删除
    assert list(limiter._buckets) == [("public-api", "198.51.100.1")]


async def test_sweep_lifecycle_stray_bucket_removed_externally_is_tolerated() -> None:
    now_ms = 0
    settings = SystemSettings(public_api_rate_limit=1)
    limiter = RateLimiter(settings=lambda: settings, clock_ms=lambda: now_ms)

    limiter.check_public("192.0.2.1")  # 入队后被外部移除（模拟不变量被破坏）
    limiter.check_public("192.0.2.2")  # 过期键，正常等待清扫
    del limiter._buckets[("public-api", "192.0.2.1")]
    now_ms = 60_000
    for _ in range(64):  # 触发一轮清扫：stray 键走防御分支，过期键被删除
        try:
            limiter.check_public("198.51.100.1")
        except DomainError:
            pass
    assert ("public-api", "192.0.2.2") not in limiter._buckets  # 正常过期键被清理
    assert ("public-api", "192.0.2.1") not in limiter._queued_keys  # stray 标记已丢弃
    limiter.check_public("192.0.2.1")  # 重建：标记重新加入，队列中恰好出现一次
    assert sum(1 for queued in limiter._sweep_keys if queued == ("public-api", "192.0.2.1")) == 1
    for _ in range(64):
        try:
            limiter.check_public("198.51.100.2")
        except DomainError:
            pass
    assert len(limiter._queued_keys) == len(limiter._sweep_keys) == len(limiter._buckets)


async def test_sweep_lifecycle_recreated_key_does_not_duplicate_queue_entry() -> None:
    now_ms = 0
    settings = SystemSettings(public_api_rate_limit=1)
    limiter = RateLimiter(settings=lambda: settings, clock_ms=lambda: now_ms)

    limiter.check_public("192.0.2.1")  # 创建并入队
    del limiter._buckets[("public-api", "192.0.2.1")]  # 外部移除，队列标记仍在
    limiter.check_public("192.0.2.1")  # 重建：标记仍在 → 不重复入队
    assert len(limiter._buckets) == 1
    assert len(limiter._sweep_keys) == 1
    assert len(limiter._queued_keys) == 1


async def test_sweep_lifecycle_window_reuse_keeps_single_queue_entry() -> None:
    now_ms = 0
    settings = SystemSettings(public_api_rate_limit=1)
    limiter = RateLimiter(settings=lambda: settings, clock_ms=lambda: now_ms)

    key = ("public-api", "192.0.2.1")
    for _ in range(4):  # 同一键跨 4 个窗口原地复用
        limiter.check_public("192.0.2.1")
        now_ms += 60_000
    assert len(limiter._sweep_keys) == 1  # 原地重置不重复入队
    for _ in range(64):  # 触发清扫：过期键删除、标记出队
        try:
            limiter.check_public("198.51.100.1")
        except DomainError:
            pass
    assert key not in limiter._buckets
    assert len(limiter._queued_keys) == len(limiter._sweep_keys)


async def test_sweep_bounds_high_cardinality_full_reclaim() -> None:
    now_ms = 0
    settings = SystemSettings(public_api_rate_limit=1)
    limiter = RateLimiter(settings=lambda: settings, clock_ms=lambda: now_ms)

    churn_keys = [f"203.0.113.{index % 256}.{index // 256}" for index in range(10_000)]
    for ip in churn_keys:
        limiter.check_public(ip)
    assert len(limiter._buckets) == 10_000

    now_ms = 60_000
    pump_ops = -(-10_001 // 128) * 64  # 上界内必清空：79 × 64 次检查
    for _ in range(pump_ops):
        try:
            limiter.check_public("198.51.100.1")
        except DomainError:
            pass
    assert len(limiter._buckets) == 1  # 10 万级以内同公式：内存随活跃键数收敛
    assert len(limiter._sweep_keys) == len(limiter._queued_keys) == 1


async def test_sweep_bounds_interleaved_active_and_expired_keys() -> None:
    now_ms = 0
    settings = SystemSettings(public_api_rate_limit=10_000)
    limiter = RateLimiter(settings=lambda: settings, clock_ms=lambda: now_ms)

    for index in range(2_000):
        limiter.check_public(f"203.0.113.{index % 256}.{index // 256}")
    now_ms = 60_000  # B 集合（2000 键）全部过期，A 集合仍活跃
    pump_ops = -(-2_004 // 128) * 64
    for index in range(pump_ops):
        limiter.check_public(f"192.0.2.{index % 3 + 1}")  # 3 个活跃键持续访问
    assert len(limiter._buckets) == 3  # 过期键清空后只剩活跃集合，计数精确
    for suffix in (1, 2, 3):
        bucket = limiter._buckets[("public-api", f"192.0.2.{suffix}")]
        assert bucket[1] == pump_ops // 3 + (1 if suffix <= pump_ops % 3 else 0)


async def test_sweep_bounds_empty_and_tiny_limiter_unaffected() -> None:
    now_ms = 0
    settings = SystemSettings(public_api_rate_limit=1)
    limiter = RateLimiter(settings=lambda: settings, clock_ms=lambda: now_ms)

    for _ in range(192):  # 空队列上触发 3 轮清扫，不得出错也不得产生状态
        try:
            limiter.check_public("192.0.2.1")
        except DomainError:
            pass
    assert len(limiter._buckets) == 1
    assert len(limiter._sweep_keys) == len(limiter._queued_keys) == 1
