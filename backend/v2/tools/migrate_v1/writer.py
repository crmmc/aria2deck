"""写 V2：建库、按外键顺序逐表写入、按 V2 口径重算配额。

建库与结构定义复用 V2 自身代码（`initialize_database` / `schema`），
写入走同一个 `SQLiteDatabase` 连接，避免两套 DDL。
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from aria2deck.domain.settings import SystemSettings
from aria2deck.infrastructure.security import CredentialManager
from aria2deck.infrastructure.sqlite.bootstrap import initialize_database
from aria2deck.infrastructure.sqlite.engine import SQLiteDatabase
from aria2deck.infrastructure.sqlite.schema import (
    app_settings,
    content_entries,
    content_objects,
    quota_accounts,
    share_links,
    user_files,
    users,
)

from .planner import PLACEHOLDER_PASSWORD, MigratedContent, WritePlan

BATCH_SIZE = 500
GENERATED_PASSWORD_BYTES = 18  # token_urlsafe(18) 出 24 个字符，远超 V2 的 16 位下限

# 密码哈希链（derive_password_hash + hash_password）不碰 pepper，只有会话/Token
# 摘要和 RPC 密钥加密用得到它。工具在离线机器上跑，没有 V2 的凭证密钥配置，
# 但写出的哈希与生产环境同算法，V2 用同一个 verify_password 就能验证。
_CREDENTIALS = CredentialManager(pepper=None, debug=True)


@dataclass(frozen=True, slots=True)
class WriteResult:
    user_ids: dict[int, int]
    content_ids: dict[str, int]
    user_file_ids: dict[int, int]
    used_bytes: dict[int, int]
    # 引导管理员的（用户名, 明文密码）；只在内存里传给 CLI 打到终端
    bootstrap_admin: tuple[str, str] | None = None


def _generated_password(username: str) -> tuple[str, str]:
    """给引导管理员造随机密码，返回（存库哈希, 明文）。"""
    password = secrets.token_urlsafe(GENERATED_PASSWORD_BYTES)
    derived = _CREDENTIALS.derive_password_hash(username=username, password=password)
    return _CREDENTIALS.hash_password(derived), password


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _quota_decisions(plan: WritePlan, user_ids: dict[int, int]) -> dict[int, int]:
    return {user_ids[v1_id]: used for v1_id, used in plan.used_bytes.items() if v1_id in user_ids}


async def _write_users(
    connection: AsyncConnection, plan: WritePlan
) -> tuple[dict[int, int], tuple[str, str] | None]:
    mapping: dict[int, int] = {}
    bootstrap: tuple[str, str] | None = None
    for user in plan.users:
        if user.accepts_generated_password:
            stored_hash, plain = _generated_password(user.username)
            bootstrap = (user.username, plain)
        else:
            stored_hash = PLACEHOLDER_PASSWORD
        result = await connection.execute(
            users.insert().values(
                username=user.username,
                username_key=user.username_key,
                password_hash=stored_hash,
                is_admin=user.is_admin,
                quota_bytes=user.quota_bytes,
                is_initial_password=True,
                rpc_secret_ciphertext=None,
                rpc_secret_prefix=None,
                pending_delete=False,
                created_at_ms=user.created_at_ms,
                updated_at_ms=user.updated_at_ms,
            )
        )
        mapping[user.v1_id] = int(result.inserted_primary_key[0])
    await connection.execute(
        quota_accounts.insert(),
        [
            {"user_id": user_id, "used_bytes": 0, "reserved_bytes": 0, "revision": 0,
             "updated_at_ms": 0}
            for user_id in mapping.values()
        ],
    )
    return mapping, bootstrap


def _content_row(content: MigratedContent) -> dict[str, object]:
    return {
        "content_key": content.content_key,
        "kind": content.kind,
        "digest": content.digest,
        "size": content.size,
        "storage_path": content.storage_path,
        "deletion_state": "active",
        "read_leases": 0,
        "delete_retry_count": 0,
        "created_at_ms": content.created_at_ms,
        "updated_at_ms": content.created_at_ms,
    }


async def _write_contents(
    connection: AsyncConnection, plan: WritePlan
) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for content in plan.contents:
        result = await connection.execute(
            content_objects.insert().values(**_content_row(content))
        )
        mapping[content.content_key] = int(result.inserted_primary_key[0])
    entry_rows: list[dict[str, object]] = []
    for content in plan.contents:
        content_id = mapping[content.content_key]
        for entry in content.entries:
            parent, _, name = entry.path.rpartition("/")
            entry_rows.append(
                {
                    "content_id": content_id,
                    "parent_path": parent,
                    "name": name,
                    "path": entry.path,
                    "entry_kind": entry.kind.value,
                    "size": entry.size,
                    "modified_at_ms": None,
                }
            )
    for start in range(0, len(entry_rows), BATCH_SIZE):
        await connection.execute(
            content_entries.insert(), entry_rows[start : start + BATCH_SIZE]
        )
    return mapping


async def _write_user_files(
    connection: AsyncConnection,
    plan: WritePlan,
    user_ids: dict[int, int],
    content_ids: dict[str, int],
) -> dict[int, int]:
    mapping: dict[int, int] = {}
    for row in plan.user_files:
        result = await connection.execute(
            user_files.insert().values(
                user_id=user_ids[row.v1_user_id],
                content_id=content_ids[row.content_key],
                display_name=row.display_name,
                created_at_ms=row.created_at_ms,
            )
        )
        mapping[row.v1_id] = int(result.inserted_primary_key[0])
    return mapping


async def _write_shares(
    connection: AsyncConnection,
    plan: WritePlan,
    user_ids: dict[int, int],
    user_file_ids: dict[int, int],
) -> None:
    for share in plan.shares:
        await connection.execute(
            share_links.insert().values(
                code=share.code,
                owner_id=user_ids[share.owner_v1_id],
                user_file_id=user_file_ids[share.user_file_v1_id],
                file_name=share.file_name,
                file_size=share.file_size,
                file_kind=share.file_kind,
                content_key=share.content_key,
                password_hash=None,
                expires_at_ms=share.expires_at_ms,
                max_downloads=share.max_downloads,
                download_count=share.download_count,
                revoked_at_ms=share.revoked_at_ms,
                created_at_ms=share.created_at_ms,
                last_accessed_at_ms=share.last_accessed_at_ms,
            )
        )


async def _apply_settings(connection: AsyncConnection, settings: SystemSettings) -> None:
    await connection.execute(
        update(app_settings)
        .where(app_settings.c.id == 1)
        .values(
            site_title=settings.site_title,
            max_task_size=settings.max_task_size,
            min_free_disk=settings.min_free_disk,
            aria2_rpc_url=settings.aria2_rpc_url,
            aria2_rpc_secret_ciphertext=None,
            aria2_bt_stop_timeout_seconds=settings.aria2_bt_stop_timeout_seconds,
            hidden_file_extensions_json=_json(settings.hidden_file_extensions),
            pack_format=settings.pack_format,
            pack_compression_level=settings.pack_compression_level,
            ws_reconnect_max_delay_seconds=settings.ws_reconnect_max_delay_seconds,
            ws_reconnect_jitter_seconds=settings.ws_reconnect_jitter_seconds,
            ws_reconnect_factor=settings.ws_reconnect_factor,
            history_retention_days=settings.history_retention_days,
            tracker_fixed_list_json=_json(settings.tracker_fixed_list),
            tracker_remote_urls_json=_json(settings.tracker_remote_urls),
            tracker_refresh_interval_minutes=settings.tracker_refresh_interval_minutes,
            download_connection_limits_json=_json(settings.download_connection_limits),
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


async def _apply_quota(connection: AsyncConnection, used: dict[int, int]) -> None:
    for user_id, used_bytes in used.items():
        await connection.execute(
            update(quota_accounts)
            .where(quota_accounts.c.user_id == user_id)
            .values(used_bytes=used_bytes, reserved_bytes=0)
        )


async def write_v2(
    v2_root: Path, plan: WritePlan, settings: SystemSettings
) -> WriteResult:
    """在 `v2_root/data/v2.db` 建库并按外键顺序写入；单一事务，失败整体回滚。"""
    database_path = v2_root / "data" / "v2.db"
    database_path.parent.mkdir(parents=True, exist_ok=True)
    database = SQLiteDatabase(database_path)
    try:
        await initialize_database(database)
        async with database.connection() as connection:
            await connection.exec_driver_sql("BEGIN IMMEDIATE")
            user_ids, bootstrap_admin = await _write_users(connection, plan)
            content_ids = await _write_contents(connection, plan)
            user_file_ids = await _write_user_files(connection, plan, user_ids, content_ids)
            await _write_shares(connection, plan, user_ids, user_file_ids)
            await _apply_settings(connection, settings)
            await _apply_quota(connection, _quota_decisions(plan, user_ids))
            await connection.commit()
    finally:
        await database.close()
    return WriteResult(
        user_ids=user_ids,
        content_ids=content_ids,
        user_file_ids=user_file_ids,
        used_bytes=_quota_decisions(plan, user_ids),
        bootstrap_admin=bootstrap_admin,
    )


@dataclass(frozen=True, slots=True)
class ObservedState:
    counts: dict[str, int]
    content_bytes: int
    quota_used: dict[int, int]
    settings: dict[str, object]


async def read_back(v2_root: Path) -> ObservedState:
    """读出迁移结果用于校验：各表行数、磁盘字节、配额与设置行。"""
    database = SQLiteDatabase(v2_root / "data" / "v2.db")
    try:
        async with database.connection() as connection:
            counts: dict[str, int] = {}
            for name, table in (
                ("users", users),
                ("content_objects", content_objects),
                ("content_entries", content_entries),
                ("user_files", user_files),
                ("share_links", share_links),
            ):
                rows = (await connection.execute(select(table.c.id))).all()
                counts[name] = len(rows)
            sizes = (await connection.execute(select(content_objects.c.size))).all()
            quotas = (
                await connection.execute(
                    select(quota_accounts.c.user_id, quota_accounts.c.used_bytes)
                )
            ).all()
            settings_row = (
                await connection.execute(select(app_settings).where(app_settings.c.id == 1))
            ).mappings().one()
            return ObservedState(
                counts=counts,
                content_bytes=sum(int(row[0]) for row in sizes),
                quota_used={int(row[0]): int(row[1]) for row in quotas},
                settings=dict(settings_row),
            )
    finally:
        await database.close()
