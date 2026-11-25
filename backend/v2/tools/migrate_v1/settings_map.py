"""V1 → V2 系统设置映射：改名、换算、越界回退，并记录丢弃与回退项。

`SystemSettings` 的校验即 V2 的约束来源；回退值取 V2 默认值，不自己发明边界。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any, cast

from aria2deck.domain.errors import DomainError
from aria2deck.domain.settings import SystemSettings

from .reader import V1Settings

# V1 有、V2 已移除的设置项：只记录名字，不迁移。
DROPPED_V1_SETTINGS: tuple[str, ...] = (
    "aria2_rpc_secret",
    "rate_limit_authenticated_download",
    "rate_limit_anonymous_download",
    "download_total_connections",
    "download_authenticated_reserved_connections",
    "download_authenticated_per_user_connections",
    "download_authenticated_per_file_connections",
    "download_anonymous_base_connections",
    "download_anonymous_borrow_connections",
    "download_anonymous_per_ip_connections",
    "download_anonymous_per_file_connections",
)

# V1 列名 → V2 列名（同一个含义，只是改了名）。
RENAMED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("max_task_size_bytes", "max_task_size"),
    ("min_free_disk_bytes", "min_free_disk"),
    ("rate_limit_account_security", "account_security_rate_limit"),
    ("rate_limit_authenticated_api", "authenticated_api_rate_limit"),
    ("rate_limit_public_api", "public_api_rate_limit"),
    ("rate_limit_share_access", "share_access_rate_limit"),
    ("rate_limit_create_task", "create_task_rate_limit"),
    ("rate_limit_create_torrent", "create_torrent_rate_limit"),
    ("rate_limit_create_pack", "create_pack_rate_limit"),
    ("rate_limit_aria2_test", "aria2_test_rate_limit"),
    ("rate_limit_rpc", "rpc_rate_limit"),
    ("rate_limit_file_search", "file_search_rate_limit"),
    ("rate_limit_create_share", "create_share_rate_limit"),
    ("ws_reconnect_max_delay", "ws_reconnect_max_delay_seconds"),
    ("ws_reconnect_jitter", "ws_reconnect_jitter_seconds"),
    ("tracker_fixed_list", "tracker_fixed_list"),
    ("tracker_remote_urls", "tracker_remote_urls"),
)

_TRACKER_SEPARATORS = (",", "\n")


@dataclass(frozen=True, slots=True)
class SettingsOutcome:
    settings: SystemSettings
    fallbacks: tuple[str, ...]
    dropped: tuple[str, ...]


def split_trackers(raw: str) -> tuple[str, ...]:
    """V1 用换行/逗号分隔文本；空行与重复项剔除，其余原样保留。"""
    values: list[str] = []
    for chunk in raw.replace(_TRACKER_SEPARATORS[0], _TRACKER_SEPARATORS[1]).splitlines():
        value = chunk.strip()
        if value and value not in values:
            values.append(value)
    return tuple(values)


def _hidden_extensions(raw: str) -> tuple[str, ...]:
    try:
        parsed: object = json.loads(raw)
    except json.JSONDecodeError:
        return ()
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        return ()
    return tuple(parsed)


def _jitter_seconds(raw: str, max_delay: int) -> int:
    """V1 的 jitter 是 0..1 的倍率；V2 是秒数，保留「最大延迟的同等比例」。"""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 1
    if not 0 <= value <= 1:
        return 1
    return max(round(value * max_delay), 1)


def _factor(raw: str) -> int:
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return 0


def map_settings(v1: V1Settings) -> SettingsOutcome:
    """把 V1 设置行映射成 V2 设置对象。

    逐字段用 V2 的 `SystemSettings` 校验（`replace` 会重跑 `__post_init__`，其余字段
    保持已接受的合法值），因此一个字段越界只回退它自己，不会牵连别的字段。
    """
    defaults = SystemSettings()
    fallbacks: list[str] = []
    max_delay = v1.ws_reconnect_max_delay
    if not 1 <= max_delay <= 300:
        fallbacks.append(
            f"ws_reconnect_max_delay_seconds（原始值 {max_delay} 超出 1..300），"
            "已回退为 V2 默认值"
        )
        max_delay = defaults.ws_reconnect_max_delay_seconds
    candidates: dict[str, object] = {
        "site_title": v1.site_title,
        "max_task_size": v1.max_task_size_bytes,
        "min_free_disk": v1.min_free_disk_bytes,
        "aria2_rpc_url": v1.aria2_rpc_url,
        "aria2_bt_stop_timeout_seconds": v1.aria2_bt_stop_timeout_seconds,
        "hidden_file_extensions": _hidden_extensions(v1.hidden_file_extensions_json),
        "pack_format": v1.pack_format,
        "pack_compression_level": v1.pack_compression_level,
        "ws_reconnect_max_delay_seconds": max_delay,
        "ws_reconnect_jitter_seconds": _jitter_seconds(v1.ws_reconnect_jitter, max_delay),
        "ws_reconnect_factor": _factor(v1.ws_reconnect_factor),
        "history_retention_days": v1.history_retention_days,
        "tracker_fixed_list": split_trackers(v1.tracker_fixed_list),
        "tracker_remote_urls": split_trackers(v1.tracker_remote_urls),
        "tracker_refresh_interval_minutes": v1.tracker_refresh_interval_minutes,
        "account_security_rate_limit": v1.account_security_rate_limit,
        "authenticated_api_rate_limit": v1.authenticated_api_rate_limit,
        "public_api_rate_limit": v1.public_api_rate_limit,
        "share_access_rate_limit": v1.share_access_rate_limit,
        "create_task_rate_limit": v1.create_task_rate_limit,
        "create_torrent_rate_limit": v1.create_torrent_rate_limit,
        "create_pack_rate_limit": v1.create_pack_rate_limit,
        "aria2_test_rate_limit": v1.aria2_test_rate_limit,
        "rpc_rate_limit": v1.rpc_rate_limit,
        "file_search_rate_limit": v1.file_search_rate_limit,
        "create_share_rate_limit": v1.create_share_rate_limit,
    }
    settings = defaults
    for name, value in candidates.items():
        try:
            settings = replace(settings, **cast(Any, {name: value}))
        except DomainError as error:
            fallbacks.append(f"{name}（{error}），已回退为 V2 默认值")
    return SettingsOutcome(
        settings=settings, fallbacks=tuple(fallbacks), dropped=DROPPED_V1_SETTINGS
    )
