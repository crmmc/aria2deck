from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.repositories import settings as settings_repo

logger = logging.getLogger(__name__)

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
    "rate_limit_authenticated_download": "rate_limit_authenticated_download",
    "rate_limit_anonymous_download": "rate_limit_anonymous_download",
    "rate_limit_create_task": "rate_limit_create_task",
    "rate_limit_create_torrent": "rate_limit_create_torrent",
    "rate_limit_create_pack": "rate_limit_create_pack",
    "rate_limit_aria2_test": "rate_limit_aria2_test",
    "rate_limit_rpc": "rate_limit_rpc",
    "download_total_connections": "download_total_connections",
    "download_authenticated_reserved_connections": "download_authenticated_reserved_connections",
    "download_authenticated_per_user_connections": "download_authenticated_per_user_connections",
    "download_authenticated_per_file_connections": "download_authenticated_per_file_connections",
    "download_anonymous_base_connections": "download_anonymous_base_connections",
    "download_anonymous_borrow_connections": "download_anonymous_borrow_connections",
    "download_anonymous_per_ip_connections": "download_anonymous_per_ip_connections",
    "download_anonymous_per_file_connections": "download_anonymous_per_file_connections",
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
    "rate_limit_authenticated_download",
    "rate_limit_anonymous_download",
    "rate_limit_create_task",
    "rate_limit_create_torrent",
    "rate_limit_create_pack",
    "rate_limit_aria2_test",
    "rate_limit_rpc",
    "download_total_connections",
    "download_authenticated_reserved_connections",
    "download_authenticated_per_user_connections",
    "download_authenticated_per_file_connections",
    "download_anonymous_base_connections",
    "download_anonymous_borrow_connections",
    "download_anonymous_per_ip_connections",
    "download_anonymous_per_file_connections",
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
        "rate_limit_authenticated_download": int(
            row["rate_limit_authenticated_download"]
        ),
        "rate_limit_anonymous_download": int(row["rate_limit_anonymous_download"]),
        "rate_limit_create_task": int(row["rate_limit_create_task"]),
        "rate_limit_create_torrent": int(row["rate_limit_create_torrent"]),
        "rate_limit_create_pack": int(row["rate_limit_create_pack"]),
        "rate_limit_aria2_test": int(row["rate_limit_aria2_test"]),
        "rate_limit_rpc": int(row["rate_limit_rpc"]),
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
    return SettingsUpdateResult(
        settings=row_to_api_settings(row), changed_keys=changed_keys
    )


async def get_config_value(key: str) -> str | None:
    column_name = CONFIG_KEY_TO_COLUMN.get(key)
    if column_name is None:
        return None
    row = await settings_repo.get_settings_row()
    if row is None:
        return None
    return serialize_config_value(row.get(column_name))


async def set_config_value(key: str, value: str) -> None:
    column_name = CONFIG_KEY_TO_COLUMN.get(key)
    if column_name is None:
        return
    coerced = coerce_raw_config_value(column_name, value)
    await settings_repo.update_settings_row({column_name: coerced})
