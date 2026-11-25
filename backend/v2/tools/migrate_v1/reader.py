"""只读 V1：打开库、校验结构与版本、读出迁移所需的五行数据。

V1 侧只有 `SELECT` / `PRAGMA`，连接串强制 `mode=ro`，物理上无法写入。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from . import MigrationAbort

V1_SCHEMA_VERSION = 18
_REQUIRED_TABLES = ("schema_meta", "users", "stored_files", "user_files", "share_links", "app_settings")


@dataclass(frozen=True, slots=True)
class V1User:
    id: int
    username: str
    is_admin: bool
    quota_bytes: int
    created_at_ms: int
    updated_at_ms: int
    pending_delete: bool


@dataclass(frozen=True, slots=True)
class V1StoredFile:
    id: int
    real_path: str
    size_bytes: int
    is_directory: bool
    original_name: str
    created_at_ms: int
    pending_delete: bool


@dataclass(frozen=True, slots=True)
class V1UserFile:
    id: int
    user_id: int
    stored_file_id: int
    display_name: str
    created_at_ms: int


@dataclass(frozen=True, slots=True)
class V1ShareLink:
    id: int
    share_code: str
    owner_id: int
    user_file_id: int
    has_password: bool
    expires_at_ms: int | None
    max_downloads: int | None
    download_count: int
    status: str
    created_at_ms: int
    last_accessed_at_ms: int | None


@dataclass(frozen=True, slots=True)
class V1Settings:
    site_title: str
    aria2_rpc_url: str
    max_task_size_bytes: int
    min_free_disk_bytes: int
    aria2_bt_stop_timeout_seconds: int
    hidden_file_extensions_json: str
    pack_format: str
    pack_compression_level: int
    ws_reconnect_max_delay: int
    ws_reconnect_jitter: str
    ws_reconnect_factor: str
    history_retention_days: int
    tracker_fixed_list: str
    tracker_remote_urls: str
    tracker_refresh_interval_minutes: int
    account_security_rate_limit: int
    authenticated_api_rate_limit: int
    public_api_rate_limit: int
    share_access_rate_limit: int
    create_task_rate_limit: int
    create_torrent_rate_limit: int
    create_pack_rate_limit: int
    aria2_test_rate_limit: int
    rpc_rate_limit: int
    file_search_rate_limit: int
    create_share_rate_limit: int


@dataclass(frozen=True, slots=True)
class V1Snapshot:
    version: int
    integrity_check: str
    users: tuple[V1User, ...]
    stored_files: tuple[V1StoredFile, ...]
    user_files: tuple[V1UserFile, ...]
    share_links: tuple[V1ShareLink, ...]
    settings: V1Settings


def _int(row: sqlite3.Row, key: str) -> int:
    return int(cast(int, row[key]))


def _opt_int(row: sqlite3.Row, key: str) -> int | None:
    value = cast(int | None, row[key])
    return None if value is None else int(value)


def _str(row: sqlite3.Row, key: str) -> str:
    return str(cast(str, row[key]))


@contextmanager
def open_v1(database_path: Path) -> Iterator[sqlite3.Connection]:
    if not database_path.is_file():
        raise MigrationAbort(f"V1 数据库不存在：{database_path}")
    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    except sqlite3.DatabaseError as error:
        raise MigrationAbort(
            "V1 数据库无法作为 SQLite 库打开，可能已损坏", (str(error),)
        ) from None
    finally:
        connection.close()


def _check_integrity(connection: sqlite3.Connection) -> str:
    rows = [str(cast(str, row[0])) for row in connection.execute("PRAGMA integrity_check").fetchall()]
    result = ", ".join(rows) if rows else "空结果"
    if result != "ok":
        raise MigrationAbort("V1 数据库完整性校验失败", (result,))
    return result


def _check_tables(connection: sqlite3.Connection) -> None:
    rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    existing = {str(cast(str, row[0])) for row in rows}
    missing = sorted(set(_REQUIRED_TABLES) - existing)
    if missing:
        raise MigrationAbort("V1 数据库缺少迁移所需的表", tuple(missing))


def _read_version(connection: sqlite3.Connection) -> int:
    row = connection.execute("SELECT version FROM schema_meta WHERE id = 1").fetchone()
    if row is None:
        raise MigrationAbort("V1 数据库的 schema_meta 缺少唯一记录")
    version = int(cast(int, row[0]))
    if version != V1_SCHEMA_VERSION:
        raise MigrationAbort(
            "V1 数据库结构版本不符，拒绝迁移",
            (f"当前版本 {version}，期望版本 {V1_SCHEMA_VERSION}",),
        )
    return version


def _read_users(connection: sqlite3.Connection) -> tuple[V1User, ...]:
    rows = connection.execute(
        "SELECT id, username, is_admin, quota_bytes, created_at_ms, updated_at_ms, "
        "pending_delete FROM users ORDER BY id"
    ).fetchall()
    return tuple(
        V1User(
            id=_int(row, "id"),
            username=_str(row, "username"),
            is_admin=bool(_int(row, "is_admin")),
            quota_bytes=_int(row, "quota_bytes"),
            created_at_ms=_int(row, "created_at_ms"),
            updated_at_ms=_int(row, "updated_at_ms"),
            pending_delete=bool(_int(row, "pending_delete")),
        )
        for row in rows
    )


def _read_stored_files(connection: sqlite3.Connection) -> tuple[V1StoredFile, ...]:
    rows = connection.execute(
        "SELECT id, real_path, size_bytes, is_directory, original_name, created_at_ms, "
        "pending_delete FROM stored_files ORDER BY id"
    ).fetchall()
    return tuple(
        V1StoredFile(
            id=_int(row, "id"),
            real_path=_str(row, "real_path"),
            size_bytes=_int(row, "size_bytes"),
            is_directory=bool(_int(row, "is_directory")),
            original_name=_str(row, "original_name"),
            created_at_ms=_int(row, "created_at_ms"),
            pending_delete=bool(_int(row, "pending_delete")),
        )
        for row in rows
    )


def _read_user_files(connection: sqlite3.Connection) -> tuple[V1UserFile, ...]:
    rows = connection.execute(
        "SELECT id, user_id, stored_file_id, display_name, created_at_ms "
        "FROM user_files ORDER BY id"
    ).fetchall()
    return tuple(
        V1UserFile(
            id=_int(row, "id"),
            user_id=_int(row, "user_id"),
            stored_file_id=_int(row, "stored_file_id"),
            display_name=_str(row, "display_name"),
            created_at_ms=_int(row, "created_at_ms"),
        )
        for row in rows
    )


def _read_share_links(connection: sqlite3.Connection) -> tuple[V1ShareLink, ...]:
    rows = connection.execute(
        "SELECT id, share_code, owner_id, user_file_id, password_hash, expires_at_ms, "
        "max_downloads, download_count, status, created_at_ms, last_accessed_at_ms "
        "FROM share_links ORDER BY id"
    ).fetchall()
    return tuple(
        V1ShareLink(
            id=_int(row, "id"),
            share_code=_str(row, "share_code"),
            owner_id=_int(row, "owner_id"),
            user_file_id=_int(row, "user_file_id"),
            has_password=row["password_hash"] is not None,
            expires_at_ms=_opt_int(row, "expires_at_ms"),
            max_downloads=_opt_int(row, "max_downloads"),
            download_count=_int(row, "download_count"),
            status=_str(row, "status"),
            created_at_ms=_int(row, "created_at_ms"),
            last_accessed_at_ms=_opt_int(row, "last_accessed_at_ms"),
        )
        for row in rows
    )


def _read_settings(connection: sqlite3.Connection) -> V1Settings:
    row = connection.execute(
        "SELECT site_title, aria2_rpc_url, max_task_size_bytes, min_free_disk_bytes, "
        "aria2_bt_stop_timeout_seconds, hidden_file_extensions_json, pack_format, "
        "pack_compression_level, ws_reconnect_max_delay, ws_reconnect_jitter, "
        "ws_reconnect_factor, history_retention_days, tracker_fixed_list, "
        "tracker_remote_urls, tracker_refresh_interval_minutes, "
        "rate_limit_account_security, rate_limit_authenticated_api, rate_limit_public_api, "
        "rate_limit_share_access, rate_limit_create_task, rate_limit_create_torrent, "
        "rate_limit_create_pack, rate_limit_aria2_test, rate_limit_rpc, "
        "rate_limit_file_search, rate_limit_create_share FROM app_settings WHERE id = 1"
    ).fetchone()
    if row is None:
        raise MigrationAbort("V1 数据库的 app_settings 缺少唯一记录")
    return V1Settings(
        site_title=_str(row, "site_title"),
        aria2_rpc_url=_str(row, "aria2_rpc_url"),
        max_task_size_bytes=_int(row, "max_task_size_bytes"),
        min_free_disk_bytes=_int(row, "min_free_disk_bytes"),
        aria2_bt_stop_timeout_seconds=_int(row, "aria2_bt_stop_timeout_seconds"),
        hidden_file_extensions_json=_str(row, "hidden_file_extensions_json"),
        pack_format=_str(row, "pack_format"),
        pack_compression_level=_int(row, "pack_compression_level"),
        ws_reconnect_max_delay=_int(row, "ws_reconnect_max_delay"),
        ws_reconnect_jitter=_str(row, "ws_reconnect_jitter"),
        ws_reconnect_factor=_str(row, "ws_reconnect_factor"),
        history_retention_days=_int(row, "history_retention_days"),
        tracker_fixed_list=_str(row, "tracker_fixed_list"),
        tracker_remote_urls=_str(row, "tracker_remote_urls"),
        tracker_refresh_interval_minutes=_int(row, "tracker_refresh_interval_minutes"),
        account_security_rate_limit=_int(row, "rate_limit_account_security"),
        authenticated_api_rate_limit=_int(row, "rate_limit_authenticated_api"),
        public_api_rate_limit=_int(row, "rate_limit_public_api"),
        share_access_rate_limit=_int(row, "rate_limit_share_access"),
        create_task_rate_limit=_int(row, "rate_limit_create_task"),
        create_torrent_rate_limit=_int(row, "rate_limit_create_torrent"),
        create_pack_rate_limit=_int(row, "rate_limit_create_pack"),
        aria2_test_rate_limit=_int(row, "rate_limit_aria2_test"),
        rpc_rate_limit=_int(row, "rate_limit_rpc"),
        file_search_rate_limit=_int(row, "rate_limit_file_search"),
        create_share_rate_limit=_int(row, "rate_limit_create_share"),
    )


def read_snapshot(database_path: Path) -> V1Snapshot:
    """读 V1 全量快照；结构、版本、完整性任一不符即中止。"""
    with open_v1(database_path) as connection:
        integrity_check = _check_integrity(connection)
        _check_tables(connection)
        version = _read_version(connection)
        return V1Snapshot(
            version=version,
            integrity_check=integrity_check,
            users=_read_users(connection),
            stored_files=_read_stored_files(connection),
            user_files=_read_user_files(connection),
            share_links=_read_share_links(connection),
            settings=_read_settings(connection),
        )
