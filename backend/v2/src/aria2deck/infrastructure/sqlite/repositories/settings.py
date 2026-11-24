from __future__ import annotations

import json
from typing import cast

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from ....domain.errors import DomainError, ErrorCode
from ....domain.settings import SystemSettings
from ...security import CredentialManager
from ..schema import app_settings


def _string_tuple(value: object) -> tuple[str, ...]:
    parsed: object = json.loads(str(value))
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise DomainError(ErrorCode.INVALID_STATE, "配置中的列表数据无效")
    return tuple(parsed)


def _connection_limits(value: object) -> dict[str, int]:
    parsed: object = json.loads(str(value))
    if not isinstance(parsed, dict):
        raise DomainError(ErrorCode.INVALID_STATE, "配置中的连接数数据无效")
    result: dict[str, int] = {}
    for key, item in parsed.items():
        if not isinstance(key, str) or not isinstance(item, int):
            raise DomainError(ErrorCode.INVALID_STATE, "配置中的连接数数据无效")
        result[key] = item
    return result



class SettingsRepository:
    def __init__(self, connection: AsyncConnection, credentials: CredentialManager) -> None:
        self.connection = connection
        self.credentials = credentials

    async def get_settings(self) -> SystemSettings:
        row = (await self.connection.execute(select(app_settings))).mappings().first()
        if row is None:
            raise DomainError(ErrorCode.INVALID_STATE, "系统配置不存在")
        ciphertext = cast(str | None, row["aria2_rpc_secret_ciphertext"])
        secret = self.credentials.decrypt(ciphertext) if ciphertext is not None else None
        return SystemSettings(
            site_title=str(row["site_title"]),
            max_task_size=int(cast(int, row["max_task_size"])),
            min_free_disk=int(cast(int, row["min_free_disk"])),
            aria2_rpc_url=str(row["aria2_rpc_url"]),
            aria2_rpc_secret=secret,
            aria2_bt_stop_timeout_seconds=int(cast(int, row["aria2_bt_stop_timeout_seconds"])),
            hidden_file_extensions=_string_tuple(row["hidden_file_extensions_json"]),
            pack_format=str(row["pack_format"]),
            pack_compression_level=int(cast(int, row["pack_compression_level"])),
            ws_reconnect_max_delay_seconds=int(cast(int, row["ws_reconnect_max_delay_seconds"])),
            ws_reconnect_jitter_seconds=int(cast(int, row["ws_reconnect_jitter_seconds"])),
            ws_reconnect_factor=int(cast(int, row["ws_reconnect_factor"])),
            history_retention_days=int(cast(int, row["history_retention_days"])),
            tracker_fixed_list=_string_tuple(row["tracker_fixed_list_json"]),
            tracker_remote_urls=_string_tuple(row["tracker_remote_urls_json"]),
            tracker_refresh_interval_minutes=int(cast(int, row["tracker_refresh_interval_minutes"])),
            download_connection_limits=_connection_limits(row["download_connection_limits_json"]),
            account_security_rate_limit=int(cast(int, row["account_security_rate_limit"])),
            authenticated_api_rate_limit=int(cast(int, row["authenticated_api_rate_limit"])),
            public_api_rate_limit=int(cast(int, row["public_api_rate_limit"])),
            share_access_rate_limit=int(cast(int, row["share_access_rate_limit"])),
            create_task_rate_limit=int(cast(int, row["create_task_rate_limit"])),
            create_torrent_rate_limit=int(cast(int, row["create_torrent_rate_limit"])),
            create_pack_rate_limit=int(cast(int, row["create_pack_rate_limit"])),
            aria2_test_rate_limit=int(cast(int, row["aria2_test_rate_limit"])),
            rpc_rate_limit=int(cast(int, row["rpc_rate_limit"])),
            file_search_rate_limit=int(cast(int, row["file_search_rate_limit"])),
            create_share_rate_limit=int(cast(int, row["create_share_rate_limit"])),
            updated_at_ms=int(cast(int, row["updated_at_ms"])),
        )


    async def save_settings(self, settings: SystemSettings) -> None:
        secret = settings.aria2_rpc_secret
        result = await self.connection.execute(
            update(app_settings)
            .where(app_settings.c.id == 1)
            .values(
                site_title=settings.site_title,
                max_task_size=settings.max_task_size,
                min_free_disk=settings.min_free_disk,
                aria2_rpc_url=settings.aria2_rpc_url,
                aria2_rpc_secret_ciphertext=(
                    self.credentials.encrypt(secret) if secret is not None else None
                ),
                aria2_bt_stop_timeout_seconds=settings.aria2_bt_stop_timeout_seconds,
                hidden_file_extensions_json=self._json(settings.hidden_file_extensions),
                pack_format=settings.pack_format,
                pack_compression_level=settings.pack_compression_level,
                ws_reconnect_max_delay_seconds=settings.ws_reconnect_max_delay_seconds,
                ws_reconnect_jitter_seconds=settings.ws_reconnect_jitter_seconds,
                ws_reconnect_factor=settings.ws_reconnect_factor,
                history_retention_days=settings.history_retention_days,
                tracker_fixed_list_json=self._json(settings.tracker_fixed_list),
                tracker_remote_urls_json=self._json(settings.tracker_remote_urls),
                tracker_refresh_interval_minutes=settings.tracker_refresh_interval_minutes,
                download_connection_limits_json=self._json(settings.download_connection_limits),
                account_security_rate_limit=settings.account_security_rate_limit,
                authenticated_api_rate_limit=settings.authenticated_api_rate_limit,
                public_api_rate_limit=settings.public_api_rate_limit,
                share_access_rate_limit=settings.share_access_rate_limit,
                create_task_rate_limit=settings.create_task_rate_limit,
                create_torrent_rate_limit=settings.create_torrent_rate_limit,
                create_pack_rate_limit=settings.create_pack_rate_limit,
                aria2_test_rate_limit=settings.aria2_test_rate_limit,
                rpc_rate_limit=settings.rpc_rate_limit,
                file_search_rate_limit=settings.file_search_rate_limit,
                create_share_rate_limit=settings.create_share_rate_limit,
                updated_at_ms=settings.updated_at_ms,
            )
        )
        if result.rowcount != 1:
            raise DomainError(ErrorCode.INVALID_STATE, "系统配置保存失败")

    @staticmethod
    def _json(value: object) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
