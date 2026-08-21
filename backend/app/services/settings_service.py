from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from time import time
from typing import Any

from app.core.download_limiter import download_config
from app.core.rate_limit_config import rate_limit_config
from app.repositories import settings as settings_repo

logger = logging.getLogger(__name__)

_config_cache: dict[str, tuple[str | None, float]] = {}
_config_cache_lock = asyncio.Lock()
_CACHE_TTL = 60.0
_SYNC_DEFAULTS: dict[str, str] = {
    "max_task_size": str(10 * 1024 * 1024 * 1024),
    "min_free_disk": str(1 * 1024 * 1024 * 1024),
    "aria2_bt_stop_timeout_seconds": str(7 * 24 * 60 * 60),
    "hidden_file_extensions": "[]",
    "pack_format": "zip",
    "pack_compression_level": "5",
    "ws_reconnect_max_delay": "60",
    "ws_reconnect_jitter": "0.2",
    "ws_reconnect_factor": "2.0",
    "site_title": "Aria2 控制器",
}

CONFIG_KEY_TO_COLUMN: dict[str, str] = {
    "max_task_size": "max_task_size_bytes",
    "min_free_disk": "min_free_disk_bytes",
    "aria2_rpc_url": "aria2_rpc_url",
    "aria2_rpc_secret": "aria2_rpc_secret",
    "aria2_bt_stop_timeout_seconds": "aria2_bt_stop_timeout_seconds",
    "hidden_file_extensions": "hidden_file_extensions_json",
    "pack_format": "pack_format",
    "pack_compression_level": "pack_compression_level",
    "ws_reconnect_max_delay": "ws_reconnect_max_delay",
    "ws_reconnect_jitter": "ws_reconnect_jitter",
    "ws_reconnect_factor": "ws_reconnect_factor",
    "site_title": "site_title",
    "rate_limit_account_security": "rate_limit_account_security",
    "rate_limit_authenticated_api": "rate_limit_authenticated_api",
    "rate_limit_public_api": "rate_limit_public_api",
    "rate_limit_share_access": "rate_limit_share_access",
    "rate_limit_create_task": "rate_limit_create_task",
    "rate_limit_create_torrent": "rate_limit_create_torrent",
    "rate_limit_create_pack": "rate_limit_create_pack",
    "rate_limit_aria2_test": "rate_limit_aria2_test",
    "rate_limit_rpc": "rate_limit_rpc",
    "rate_limit_file_search": "rate_limit_file_search",
    "download_total_connections": "download_total_connections",
    "download_authenticated_reserved_connections": "download_authenticated_reserved_connections",
    "download_authenticated_per_user_connections": "download_authenticated_per_user_connections",
    "download_authenticated_per_file_connections": "download_authenticated_per_file_connections",
    "download_anonymous_base_connections": "download_anonymous_base_connections",
    "download_anonymous_borrow_connections": "download_anonymous_borrow_connections",
    "download_anonymous_per_ip_connections": "download_anonymous_per_ip_connections",
    "download_anonymous_per_file_connections": "download_anonymous_per_file_connections",
    "history_retention_days": "history_retention_days",
}

INT_CONFIG_COLUMNS = {
    "max_task_size_bytes",
    "min_free_disk_bytes",
    "pack_compression_level",
    "aria2_bt_stop_timeout_seconds",
    "ws_reconnect_max_delay",
    "rate_limit_account_security",
    "rate_limit_authenticated_api",
    "rate_limit_public_api",
    "rate_limit_share_access",
    "rate_limit_create_task",
    "rate_limit_create_torrent",
    "rate_limit_create_pack",
    "rate_limit_aria2_test",
    "rate_limit_rpc",
    "rate_limit_file_search",
    "download_total_connections",
    "download_authenticated_reserved_connections",
    "download_authenticated_per_user_connections",
    "download_authenticated_per_file_connections",
    "download_anonymous_base_connections",
    "download_anonymous_borrow_connections",
    "download_anonymous_per_ip_connections",
    "download_anonymous_per_file_connections",
    "history_retention_days",
}


@dataclass(slots=True)
class SettingsUpdateResult:
    settings: dict[str, Any]
    changed_keys: list[str]


def serialize_config_value(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def coerce_raw_config_value(column_name: str, value: str) -> Any:
    if column_name in INT_CONFIG_COLUMNS:
        if column_name == "ws_reconnect_max_delay":
            return int(float(value))
        return int(value)
    return value


def _column_for_key(key: str) -> str | None:
    return CONFIG_KEY_TO_COLUMN.get(key)


def clear_config_cache() -> None:
    _config_cache.clear()


async def clear_config_cache_async() -> None:
    async with _config_cache_lock:
        _config_cache.clear()


def _cache_settings_row(
    row: Mapping[str, Any] | None, timestamp: float | None = None
) -> None:
    if row is None:
        return
    ts = time() if timestamp is None else timestamp
    for key, column_name in CONFIG_KEY_TO_COLUMN.items():
        _config_cache[key] = (serialize_config_value(row.get(column_name)), ts)


def get_config_value_sync(key: str) -> str | None:
    now = time()
    cached = _config_cache.get(key)
    if cached is not None:
        # 缓存命中：TTL 内直接返回；过期后 serve-stale 返回最后加载的库值，不回退默认值
        value, _ts = cached
        return value

    value = _SYNC_DEFAULTS.get(key)
    _config_cache[key] = (value, now)
    return value


def _int_config(
    key: str,
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    value = get_config_value_sync(key)
    try:
        resolved = int(value) if value else default
    except (TypeError, ValueError):
        resolved = default
    if minimum is not None:
        resolved = max(minimum, resolved)
    if maximum is not None:
        resolved = min(maximum, resolved)
    return resolved


def _float_config(
    key: str,
    default: float,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    value = get_config_value_sync(key)
    try:
        resolved = float(value) if value else default
    except (TypeError, ValueError):
        resolved = default
    if minimum is not None:
        resolved = max(minimum, resolved)
    if maximum is not None:
        resolved = min(maximum, resolved)
    return resolved


def get_max_task_size() -> int:
    return _int_config("max_task_size", 10 * 1024 * 1024 * 1024)


def get_min_free_disk() -> int:
    return _int_config("min_free_disk", 1 * 1024 * 1024 * 1024)


def get_aria2_bt_stop_timeout_seconds() -> int:
    return _int_config("aria2_bt_stop_timeout_seconds", 7 * 24 * 60 * 60, minimum=0)


def get_hidden_file_extensions() -> list[str]:
    return _decode_hidden_extensions(get_config_value_sync("hidden_file_extensions"))


def get_pack_format() -> str:
    return _api_pack_format(get_config_value_sync("pack_format"))


def get_pack_compression_level() -> int:
    return _int_config("pack_compression_level", 5, minimum=0, maximum=9)


def get_ws_reconnect_max_delay() -> float:
    return _float_config("ws_reconnect_max_delay", 60.0)


def get_ws_reconnect_jitter() -> float:
    return _float_config("ws_reconnect_jitter", 0.2, minimum=0.0, maximum=1.0)


def get_ws_reconnect_factor() -> float:
    return _float_config("ws_reconnect_factor", 2.0, minimum=1.1, maximum=10.0)


def get_site_title() -> str:
    value = get_config_value_sync("site_title")
    return value if value else "Aria2 控制器"


def _masked_secret(secret: str | None) -> str:
    if not secret:
        return ""
    return "*" * min(len(secret), 8)


def _decode_hidden_extensions(value: Any) -> list[str]:
    if not value:
        return []
    try:
        decoded = json.loads(str(value))
    except json.JSONDecodeError as exc:
        logger.error("Failed to parse hidden_file_extensions config: %s", exc)
        return []
    return decoded if isinstance(decoded, list) else []


def _normalize_hidden_extensions(extensions: list[str]) -> list[str]:
    normalized: list[str] = []
    for ext in extensions:
        ext = ext.strip().lower()
        if ext and not ext.startswith("."):
            ext = "." + ext
        if ext and ext not in normalized:
            normalized.append(ext)
    return normalized


def _api_pack_format(value: Any) -> str:
    if value == "7z":
        return "tar.zst"
    return value if value in ("zip", "tar.zst") else "zip"


def row_to_api_settings(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "max_task_size": int(row["max_task_size_bytes"]),
        "min_free_disk": int(row["min_free_disk_bytes"]),
        "aria2_rpc_url": row["aria2_rpc_url"],
        "aria2_rpc_secret": _masked_secret(row.get("aria2_rpc_secret")),
        "aria2_bt_stop_timeout_seconds": int(row["aria2_bt_stop_timeout_seconds"]),
        "hidden_file_extensions": _decode_hidden_extensions(
            row["hidden_file_extensions_json"]
        ),
        "pack_format": _api_pack_format(row["pack_format"]),
        "pack_compression_level": int(row["pack_compression_level"]),
        "ws_reconnect_max_delay": float(row["ws_reconnect_max_delay"]),
        "ws_reconnect_jitter": float(row["ws_reconnect_jitter"]),
        "ws_reconnect_factor": float(row["ws_reconnect_factor"]),
        "site_title": row["site_title"],
        "rate_limit_account_security": int(row["rate_limit_account_security"]),
        "rate_limit_authenticated_api": int(row["rate_limit_authenticated_api"]),
        "rate_limit_public_api": int(row["rate_limit_public_api"]),
        "rate_limit_share_access": int(row["rate_limit_share_access"]),
        "rate_limit_create_task": int(row["rate_limit_create_task"]),
        "rate_limit_create_torrent": int(row["rate_limit_create_torrent"]),
        "rate_limit_create_pack": int(row["rate_limit_create_pack"]),
        "rate_limit_aria2_test": int(row["rate_limit_aria2_test"]),
        "rate_limit_rpc": int(row["rate_limit_rpc"]),
        "rate_limit_file_search": int(row["rate_limit_file_search"]),
        "download_total_connections": int(row["download_total_connections"]),
        "download_authenticated_reserved_connections": int(
            row["download_authenticated_reserved_connections"]
        ),
        "download_authenticated_per_user_connections": int(
            row["download_authenticated_per_user_connections"]
        ),
        "download_authenticated_per_file_connections": int(
            row["download_authenticated_per_file_connections"]
        ),
        "download_anonymous_base_connections": int(
            row["download_anonymous_base_connections"]
        ),
        "download_anonymous_borrow_connections": int(
            row["download_anonymous_borrow_connections"]
        ),
        "download_anonymous_per_ip_connections": int(
            row["download_anonymous_per_ip_connections"]
        ),
        "download_anonymous_per_file_connections": int(
            row["download_anonymous_per_file_connections"]
        ),
        "history_retention_days": max(1, int(row["history_retention_days"])),
    }


def payload_to_db_columns(
    payload: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    values: dict[str, Any] = {}
    changed_keys: list[str] = []
    for key, value in payload.items():
        if value is None:
            continue

        column_name = CONFIG_KEY_TO_COLUMN.get(key)
        if column_name is None:
            continue

        if (
            key == "aria2_rpc_secret"
            and isinstance(value, str)
            and value.startswith("*")
        ):
            continue
        if key == "hidden_file_extensions":
            value = json.dumps(_normalize_hidden_extensions(value))
        elif key == "pack_format":
            value = "tar.zst" if value == "7z" else value
            if value not in ("zip", "tar.zst"):
                continue
        elif key in {"ws_reconnect_jitter", "ws_reconnect_factor"}:
            value = str(float(value))
        elif key == "ws_reconnect_max_delay":
            value = int(float(value))

        values[column_name] = value
        changed_keys.append(key)
    return values, changed_keys


async def get_api_settings() -> dict[str, Any]:
    row = await settings_repo.get_settings_row()
    if row is None:
        raise RuntimeError("app_settings row is missing")
    return row_to_api_settings(row)


async def update_api_settings(payload: Mapping[str, Any]) -> SettingsUpdateResult:
    values, changed_keys = payload_to_db_columns(payload)
    row = await settings_repo.update_settings_row(values)
    if row is None:
        raise RuntimeError("app_settings row is missing")
    async with _config_cache_lock:
        _cache_settings_row(row)
    return SettingsUpdateResult(
        settings=row_to_api_settings(row), changed_keys=changed_keys
    )


DOWNLOAD_CONFIG_KEYS = {
    "download_total_connections",
    "download_authenticated_reserved_connections",
    "download_authenticated_per_user_connections",
    "download_authenticated_per_file_connections",
    "download_anonymous_base_connections",
    "download_anonymous_borrow_connections",
    "download_anonymous_per_ip_connections",
    "download_anonymous_per_file_connections",
}

RATE_LIMIT_KEYS = {
    "rate_limit_account_security",
    "rate_limit_authenticated_api",
    "rate_limit_public_api",
    "rate_limit_share_access",
    "rate_limit_create_task",
    "rate_limit_create_torrent",
    "rate_limit_create_pack",
    "rate_limit_aria2_test",
    "rate_limit_rpc",
    "rate_limit_file_search",
}


def _int_payload_value(payload: Mapping[str, Any], key: str, default: int) -> int:
    value = payload.get(key)
    if value is None:
        return default
    return int(value)


def merged_download_settings(payload: Mapping[str, Any]) -> dict[str, int]:
    return {
        "download_total_connections": _int_payload_value(
            payload,
            "download_total_connections",
            download_config.total_connections,
        ),
        "download_authenticated_reserved_connections": _int_payload_value(
            payload,
            "download_authenticated_reserved_connections",
            download_config.authenticated_reserved_connections,
        ),
        "download_authenticated_per_user_connections": _int_payload_value(
            payload,
            "download_authenticated_per_user_connections",
            download_config.authenticated_per_user_connections,
        ),
        "download_authenticated_per_file_connections": _int_payload_value(
            payload,
            "download_authenticated_per_file_connections",
            download_config.authenticated_per_file_connections,
        ),
        "download_anonymous_base_connections": _int_payload_value(
            payload,
            "download_anonymous_base_connections",
            download_config.anonymous_base_connections,
        ),
        "download_anonymous_borrow_connections": _int_payload_value(
            payload,
            "download_anonymous_borrow_connections",
            download_config.anonymous_borrow_connections,
        ),
        "download_anonymous_per_ip_connections": _int_payload_value(
            payload,
            "download_anonymous_per_ip_connections",
            download_config.anonymous_per_ip_connections,
        ),
        "download_anonymous_per_file_connections": _int_payload_value(
            payload,
            "download_anonymous_per_file_connections",
            download_config.anonymous_per_file_connections,
        ),
    }


def validate_download_settings(settings_map: Mapping[str, int]) -> None:
    from app.domain.errors import BadRequestError

    total = settings_map["download_total_connections"]
    if total <= 0:
        return

    allocated = (
        settings_map["download_authenticated_reserved_connections"]
        + settings_map["download_anonymous_base_connections"]
        + settings_map["download_anonymous_borrow_connections"]
    )
    if allocated > total:
        raise BadRequestError("下载并发配置无效：已登录保底与匿名配额总和不能超过系统总连接上限")


async def update_api_settings_with_runtime_refresh(
    payload: Mapping[str, Any],
) -> SettingsUpdateResult:
    if any(key in payload for key in DOWNLOAD_CONFIG_KEYS):
        validate_download_settings(merged_download_settings(payload))

    rate_limit_changed = any(key in payload for key in RATE_LIMIT_KEYS)
    download_config_changed = any(key in payload for key in DOWNLOAD_CONFIG_KEYS)

    result = await update_api_settings(payload)
    changed_keys = result.changed_keys

    await clear_config_cache_async()
    await load_runtime_config()

    if not download_config_changed and not rate_limit_changed:
        logger.debug("运行配置缓存已刷新 changed_keys=%s", changed_keys)
    if "aria2_rpc_url" in changed_keys or "aria2_rpc_secret" in changed_keys:
        await refresh_aria2_config()

    return result


async def load_runtime_config() -> None:
    row = await settings_repo.get_settings_row()
    download_config.load_from_settings(row)
    rate_limit_config.load_from_settings(row)
    async with _config_cache_lock:
        _cache_settings_row(row)


async def refresh_aria2_config() -> None:
    from app.aria2.gateway import update_cached_aria2_config

    rpc_url = await get_config_value("aria2_rpc_url")
    rpc_secret = await get_config_value("aria2_rpc_secret")
    update_cached_aria2_config(
        rpc_url=rpc_url,
        rpc_secret=rpc_secret,
    )


async def get_config_value(key: str) -> str | None:
    now = time()
    async with _config_cache_lock:
        cached = _config_cache.get(key)
        if cached is not None:
            value, ts = cached
            if now - ts < _CACHE_TTL:
                return value

    column_name = _column_for_key(key)
    if column_name is None:
        async with _config_cache_lock:
            _config_cache[key] = (None, now)
        return None

    row = await settings_repo.get_settings_row()
    value = serialize_config_value(row.get(column_name)) if row else None

    async with _config_cache_lock:
        if row is not None:
            _cache_settings_row(row, now)
        else:
            _config_cache[key] = (value, now)
    return value


async def set_config_value(key: str, value: str) -> None:
    column_name = _column_for_key(key)
    if column_name is None:
        async with _config_cache_lock:
            _config_cache[key] = (None, time())
        return

    coerced = coerce_raw_config_value(column_name, value)
    row = await settings_repo.update_settings_row({column_name: coerced})
    async with _config_cache_lock:
        if row is not None:
            _cache_settings_row(row)
        else:
            _config_cache[key] = (value, time())
