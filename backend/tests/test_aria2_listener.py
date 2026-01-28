"""测试 aria2 WebSocket 监听器

测试场景：
1. HTTP URL 转换为 WebSocket URL
2. 指数退避算法
3. 事件映射
"""
import pytest

from app.aria2.listener import (
    _http_to_ws_url,
    _calculate_backoff,
    EVENT_MAP,
    RECONNECT_BASE_DELAY,
)


# 测试用默认值
DEFAULT_MAX_DELAY = 60.0
DEFAULT_JITTER = 0.2
DEFAULT_FACTOR = 2.0


class TestHttpToWsUrl:
    """测试 HTTP URL 转 WebSocket URL"""

    def test_http_to_ws(self):
        assert _http_to_ws_url("http://localhost:6800/jsonrpc") == "ws://localhost:6800/jsonrpc"

    def test_https_to_wss(self):
        assert _http_to_ws_url("https://aria2.example.com/jsonrpc") == "wss://aria2.example.com/jsonrpc"

    def test_with_port(self):
        assert _http_to_ws_url("http://192.168.1.100:6800/jsonrpc") == "ws://192.168.1.100:6800/jsonrpc"

    def test_with_custom_port(self):
        assert _http_to_ws_url("http://localhost:8080/jsonrpc") == "ws://localhost:8080/jsonrpc"

    def test_https_with_port(self):
        assert _http_to_ws_url("https://aria2.example.com:443/jsonrpc") == "wss://aria2.example.com:443/jsonrpc"


class TestCalculateBackoff:
    """测试指数退避算法"""

    def test_first_attempt(self):
        delay = _calculate_backoff(0, max_delay=DEFAULT_MAX_DELAY, jitter=DEFAULT_JITTER, factor=DEFAULT_FACTOR)
        # 1s +/- 20% = 0.8s ~ 1.2s
        assert 0.8 <= delay <= 1.2

    def test_second_attempt(self):
        delay = _calculate_backoff(1, max_delay=DEFAULT_MAX_DELAY, jitter=DEFAULT_JITTER, factor=DEFAULT_FACTOR)
        # 2s +/- 20% = 1.6s ~ 2.4s
        assert 1.6 <= delay <= 2.4

    def test_third_attempt(self):
        delay = _calculate_backoff(2, max_delay=DEFAULT_MAX_DELAY, jitter=DEFAULT_JITTER, factor=DEFAULT_FACTOR)
        # 4s +/- 20% = 3.2s ~ 4.8s
        assert 3.2 <= delay <= 4.8

    def test_fourth_attempt(self):
        delay = _calculate_backoff(3, max_delay=DEFAULT_MAX_DELAY, jitter=DEFAULT_JITTER, factor=DEFAULT_FACTOR)
        # 8s +/- 20% = 6.4s ~ 9.6s
        assert 6.4 <= delay <= 9.6

    def test_max_delay(self):
        delay = _calculate_backoff(100, max_delay=DEFAULT_MAX_DELAY, jitter=DEFAULT_JITTER, factor=DEFAULT_FACTOR)
        # 不超过 60s + jitter
        assert delay <= DEFAULT_MAX_DELAY * (1 + DEFAULT_JITTER)

    def test_max_delay_reached_at_attempt_6(self):
        # 2^6 * 1 = 64 > 60, should cap at 60
        delay = _calculate_backoff(6, max_delay=DEFAULT_MAX_DELAY, jitter=DEFAULT_JITTER, factor=DEFAULT_FACTOR)
        # 60s +/- 20% = 48s ~ 72s
        assert 48 <= delay <= 72

    def test_exponential_growth(self):
        # 收集多次采样，验证平均值大致翻倍
        samples = 100
        avg_delays = []
        for attempt in range(5):
            total = sum(
                _calculate_backoff(attempt, max_delay=DEFAULT_MAX_DELAY, jitter=DEFAULT_JITTER, factor=DEFAULT_FACTOR)
                for _ in range(samples)
            )
            avg_delays.append(total / samples)

        # 验证每次大约翻倍（考虑抖动，使用宽松的阈值）
        for i in range(1, 4):
            ratio = avg_delays[i] / avg_delays[i-1]
            # 考虑抖动，比例应该在 1.5 ~ 2.5 之间
            assert 1.5 <= ratio <= 2.5, f"attempt {i}: ratio={ratio}"

    def test_jitter_randomness(self):
        """验证抖动的随机性"""
        delays = [
            _calculate_backoff(0, max_delay=DEFAULT_MAX_DELAY, jitter=DEFAULT_JITTER, factor=DEFAULT_FACTOR)
            for _ in range(100)
        ]
        # 应该有多个不同的值
        unique_delays = set(round(d, 4) for d in delays)
        assert len(unique_delays) > 10, "抖动应该产生足够的随机性"

    def test_custom_factor(self):
        """测试自定义指数因子"""
        # 使用因子 3
        delay = _calculate_backoff(2, max_delay=100, jitter=0, factor=3.0)
        # 1 * 3^2 = 9
        assert delay == 9.0

    def test_custom_max_delay(self):
        """测试自定义最大延迟"""
        delay = _calculate_backoff(10, max_delay=30, jitter=0, factor=2.0)
        # 应该被限制在 30
        assert delay == 30.0

    def test_zero_jitter(self):
        """测试零抖动"""
        delays = [_calculate_backoff(0, max_delay=60, jitter=0, factor=2.0) for _ in range(10)]
        # 所有值应该相同
        assert all(d == 1.0 for d in delays)


class TestEventMap:
    """测试事件映射"""

    def test_all_events_mapped(self):
        expected_events = {"start", "pause", "stop", "complete", "error", "bt_complete"}
        assert set(EVENT_MAP.values()) == expected_events

    def test_aria2_methods(self):
        assert EVENT_MAP["aria2.onDownloadStart"] == "start"
        assert EVENT_MAP["aria2.onDownloadPause"] == "pause"
        assert EVENT_MAP["aria2.onDownloadStop"] == "stop"
        assert EVENT_MAP["aria2.onDownloadComplete"] == "complete"
        assert EVENT_MAP["aria2.onDownloadError"] == "error"
        assert EVENT_MAP["aria2.onBtDownloadComplete"] == "bt_complete"

    def test_event_count(self):
        """验证有 6 种事件"""
        assert len(EVENT_MAP) == 6


class TestConstants:
    """测试常量配置"""

    def test_reconnect_base_delay(self):
        assert RECONNECT_BASE_DELAY == 1.0
