from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any, cast
from urllib.parse import SplitResult, urlsplit

from .errors import DomainError, ErrorCode

SECRET_MASK = "********"  # noqa: S105  # 掩码不是密钥
DEFAULT_SITE_TITLE = "Aria2Deck"
# 下载并发连接上限的合法键与默认值（旧版 DownloadConfig 默认同源；
# 匿名池 = 旧版 anonymous_base+borrow 之和 40）。
# 0 值语义对齐旧版：总量与各分档 0=不限，匿名池 0=全面拒绝匿名下载。
DOWNLOAD_CONNECTION_LIMIT_KEYS = (
    "total_connections",
    "authenticated_per_user_connections",
    "authenticated_per_file_connections",
    "anonymous_total_connections",
    "anonymous_per_ip_connections",
    "anonymous_per_file_connections",
)
DEFAULT_DOWNLOAD_CONNECTION_LIMITS: tuple[tuple[str, int], ...] = (
    ("total_connections", 100),
    ("authenticated_per_user_connections", 16),
    ("authenticated_per_file_connections", 8),
    ("anonymous_total_connections", 40),
    ("anonymous_per_ip_connections", 4),
    ("anonymous_per_file_connections", 2),
)
_RATE_LIMIT_FIELDS = (
    "account_security_rate_limit",
    "authenticated_api_rate_limit",
    "public_api_rate_limit",
    "share_access_rate_limit",
    "create_task_rate_limit",
    "create_torrent_rate_limit",
    "create_pack_rate_limit",
    "aria2_test_rate_limit",
    "rpc_rate_limit",
    "file_search_rate_limit",
    "create_share_rate_limit",
)


@dataclass(frozen=True, slots=True)
class SystemSettings:
    site_title: str = DEFAULT_SITE_TITLE
    max_task_size: int = 100 * 1024**3
    min_free_disk: int = 1024**3
    aria2_rpc_url: str = "http://127.0.0.1:6800/jsonrpc"
    aria2_rpc_secret: str | None = field(default=None, repr=False)
    aria2_bt_stop_timeout_seconds: int = 300
    hidden_file_extensions: tuple[str, ...] = ()
    pack_format: str = "zip"
    pack_compression_level: int = 6
    ws_reconnect_max_delay_seconds: int = 30
    ws_reconnect_jitter_seconds: int = 1
    ws_reconnect_factor: int = 2
    history_retention_days: int = 30
    tracker_fixed_list: tuple[str, ...] = ()
    tracker_remote_urls: tuple[str, ...] = ()
    tracker_refresh_interval_minutes: int = 0
    download_connection_limits: dict[str, int] = field(default_factory=dict)
    account_security_rate_limit: int = 10
    authenticated_api_rate_limit: int = 600
    public_api_rate_limit: int = 300
    share_access_rate_limit: int = 30
    create_task_rate_limit: int = 60
    create_torrent_rate_limit: int = 30
    create_pack_rate_limit: int = 30
    aria2_test_rate_limit: int = 10
    rpc_rate_limit: int = 600
    file_search_rate_limit: int = 120
    create_share_rate_limit: int = 60
    updated_at_ms: int = 0

    def __post_init__(self) -> None:
        if not 1 <= len(self.site_title) <= 100 or "\x00" in self.site_title:
            raise DomainError(ErrorCode.INVALID_INPUT, "站点标题长度或内容无效")
        if not 0 <= self.max_task_size <= 2**63 - 1:
            raise DomainError(ErrorCode.INVALID_INPUT, "单任务大小上限无效")
        if not 0 <= self.min_free_disk <= 2**63 - 1:
            raise DomainError(ErrorCode.INVALID_INPUT, "磁盘保留空间无效")
        _validate_http_url(self.aria2_rpc_url, "aria2 RPC 地址无效")
        if self.aria2_rpc_secret is not None and not 1 <= len(self.aria2_rpc_secret) <= 4096:
            raise DomainError(ErrorCode.INVALID_INPUT, "aria2 RPC 密钥长度无效")
        if not 1 <= self.aria2_bt_stop_timeout_seconds <= 86400:
            raise DomainError(ErrorCode.INVALID_INPUT, "BT 停止超时时间无效")
        if self.pack_format not in {"zip", "tar.zst"}:
            raise DomainError(ErrorCode.INVALID_INPUT, "打包格式无效")
        if not 0 <= self.pack_compression_level <= 22:
            raise DomainError(ErrorCode.INVALID_INPUT, "打包压缩级别无效")
        if not 1 <= self.ws_reconnect_max_delay_seconds <= 300:
            raise DomainError(ErrorCode.INVALID_INPUT, "WebSocket 最大重连延迟无效")
        if not 0 <= self.ws_reconnect_jitter_seconds <= 30:
            raise DomainError(ErrorCode.INVALID_INPUT, "WebSocket 重连抖动无效")
        if not 1 <= self.ws_reconnect_factor <= 10:
            raise DomainError(ErrorCode.INVALID_INPUT, "WebSocket 重连倍数无效")
        if not 1 <= self.history_retention_days <= 3650:
            raise DomainError(ErrorCode.INVALID_INPUT, "历史保留天数无效")
        if self.tracker_refresh_interval_minutes != 0 and not 5 <= self.tracker_refresh_interval_minutes <= 10080:
            raise DomainError(ErrorCode.INVALID_INPUT, "Tracker 刷新间隔无效")
        if len(self.tracker_fixed_list) + len(self.tracker_remote_urls) > 5000:
            raise DomainError(ErrorCode.INVALID_INPUT, "Tracker 数量不能超过 5000")
        for tracker in self.tracker_fixed_list:
            _validate_tracker(tracker)
        for url in self.tracker_remote_urls:
            _validate_http_url(url, "Tracker 远程地址无效")
        for key, value in self.download_connection_limits.items():
            if key not in DOWNLOAD_CONNECTION_LIMIT_KEYS or not 0 <= value <= 1000:
                raise DomainError(ErrorCode.INVALID_INPUT, "下载连接数配置无效")
        for name in _RATE_LIMIT_FIELDS:
            if not 1 <= getattr(self, name) <= 1_000_000:
                raise DomainError(ErrorCode.INVALID_INPUT, "限流配置必须介于 1 和 1000000 之间")
        if self.updated_at_ms < 0:
            raise DomainError(ErrorCode.INVALID_INPUT, "配置更新时间无效")


def _parse_url(value: str, message: str) -> SplitResult:
    try:
        parsed = urlsplit(value)
        _port = parsed.port
    except ValueError:
        raise DomainError(ErrorCode.INVALID_INPUT, message) from None
    return parsed


def _validate_http_url(value: str, message: str) -> None:
    parsed = _parse_url(value, message)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise DomainError(ErrorCode.INVALID_INPUT, message)


def _validate_tracker(value: str) -> None:
    if not 1 <= len(value) <= 2048 or any(character.isspace() for character in value):
        raise DomainError(ErrorCode.INVALID_INPUT, "Tracker 地址无效")
    parsed = _parse_url(value, "Tracker 地址无效")
    if parsed.scheme not in {"http", "https", "udp"} or not parsed.hostname:
        raise DomainError(ErrorCode.INVALID_INPUT, "Tracker 地址无效")


def normalize_site_title(value: str) -> str:
    return value.strip() or DEFAULT_SITE_TITLE


def normalize_extensions(values: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    normalized: list[str] = []
    for raw in values:
        value = raw.strip().lower()
        if value and not value.startswith("."):
            value = "." + value
        if not 2 <= len(value) <= 32 or any(character in value for character in (chr(47), chr(92), chr(0))):
            raise DomainError(ErrorCode.INVALID_INPUT, "隐藏文件后缀无效")
        if value not in normalized:
            normalized.append(value)
    return tuple(normalized)


def normalize_string_list(values: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    normalized: list[str] = []
    for raw in values:
        value = raw.strip()
        if value and value not in normalized:
            normalized.append(value)
    return tuple(normalized)


def apply_settings_patch(
    current: SystemSettings, changes: Mapping[str, object]
) -> SystemSettings:
    allowed = set(SystemSettings.__dataclass_fields__) - {"updated_at_ms"}
    if any(key not in allowed for key in changes):
        raise DomainError(ErrorCode.INVALID_INPUT, "配置包含未知字段")
    normalized = {key: value for key, value in changes.items() if value is not None}
    if normalized.get("aria2_rpc_secret") == SECRET_MASK:
        normalized.pop("aria2_rpc_secret")
    if "site_title" in normalized:
        normalized["site_title"] = normalize_site_title(str(normalized["site_title"]))
    if "hidden_file_extensions" in normalized:
        value = normalized["hidden_file_extensions"]
        if not isinstance(value, list | tuple):
            raise DomainError(ErrorCode.INVALID_INPUT, "隐藏文件后缀格式无效")
        normalized["hidden_file_extensions"] = normalize_extensions(value)
    for name in ("tracker_fixed_list", "tracker_remote_urls"):
        if name in normalized:
            value = normalized[name]
            if not isinstance(value, list | tuple):
                raise DomainError(ErrorCode.INVALID_INPUT, "Tracker 列表格式无效")
            normalized[name] = normalize_string_list(value)
    return replace(current, **cast(Any, normalized))


def merge_trackers(*groups: tuple[str, ...]) -> tuple[str, ...]:
    """Merge fixed, remote and source trackers without changing their order."""
    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for raw in group:
            tracker = raw.strip()
            if not tracker:
                continue
            _validate_tracker(tracker)
            if tracker not in seen and len(merged) < 5000:
                seen.add(tracker)
                merged.append(tracker)
    return tuple(merged)


def effective_download_limits(config: Mapping[str, int]) -> dict[str, int]:
    """补全下载并发上限：未配置的键回落到默认值。"""
    return {key: int(config.get(key, default)) for key, default in DEFAULT_DOWNLOAD_CONNECTION_LIMITS}


def parse_tracker_text(text: str) -> tuple[str, ...]:
    """Read newline-delimited remote trackers; comments and bad lines are ignored."""
    values: list[str] = []
    for line in text.splitlines():
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        try:
            _validate_tracker(value)
        except DomainError:
            continue
        values.append(value)
    return tuple(values)
