"""单元 0：造一份真实形状的 V1 样本（版本 18 结构 + 磁盘内容）。

样本覆盖：正常账号、待删除账号、大小写冲突账号组、文件型与目录型内容、
多条同内容引用、待删除内容、磁盘缺失内容、两种分享状态、带密码分享、完整设置行。
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

V1_SCHEMA_VERSION = 18

V1_DDL = """
CREATE TABLE schema_meta (
    id INTEGER NOT NULL,
    version INTEGER NOT NULL,
    created_at_ms INTEGER NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT ck_schema_meta_single_row CHECK (id = 1),
    CONSTRAINT ck_schema_meta_non_negative_version CHECK (version >= 0)
);
CREATE TABLE users (
    id INTEGER NOT NULL,
    username VARCHAR(50) NOT NULL,
    password_hash TEXT NOT NULL,
    is_admin INTEGER DEFAULT '0' NOT NULL,
    quota_bytes INTEGER NOT NULL,
    rpc_secret_digest VARCHAR(64),
    rpc_secret_prefix VARCHAR(24),
    rpc_secret_encrypted TEXT,
    rpc_secret_created_at_ms INTEGER,
    is_initial_password INTEGER DEFAULT '0' NOT NULL,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL,
    pending_delete INTEGER DEFAULT '0' NOT NULL,
    delete_attempts INTEGER DEFAULT '0' NOT NULL,
    delete_next_retry_at_ms INTEGER,
    delete_lease_token VARCHAR(64),
    delete_lease_expires_at_ms INTEGER,
    delete_error TEXT,
    PRIMARY KEY (id),
    CONSTRAINT ck_users_is_admin_bool CHECK (is_admin IN (0, 1)),
    CONSTRAINT ck_users_initial_password_bool CHECK (is_initial_password IN (0, 1)),
    CONSTRAINT ck_users_pending_delete_bool CHECK (pending_delete IN (0, 1)),
    CONSTRAINT ck_users_delete_attempts_non_negative CHECK (delete_attempts >= 0),
    UNIQUE (username),
    UNIQUE (rpc_secret_digest)
);
CREATE TABLE stored_files (
    id INTEGER NOT NULL,
    content_hash VARCHAR(128) NOT NULL,
    content_hash_version VARCHAR(8) DEFAULT 'v1' NOT NULL,
    content_object_kind VARCHAR(16) DEFAULT 'legacy' NOT NULL,
    content_digest VARCHAR(128),
    real_path TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    is_directory INTEGER DEFAULT '0' NOT NULL,
    original_name TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL,
    pending_delete INTEGER DEFAULT '0' NOT NULL,
    delete_attempts INTEGER DEFAULT '0' NOT NULL,
    delete_next_retry_at_ms INTEGER,
    delete_lease_token VARCHAR(64),
    delete_lease_expires_at_ms INTEGER,
    delete_error TEXT,
    PRIMARY KEY (id),
    CONSTRAINT ck_stored_files_is_directory_bool CHECK (is_directory IN (0, 1)),
    CONSTRAINT ck_stored_files_pending_delete_bool CHECK (pending_delete IN (0, 1)),
    CONSTRAINT ck_stored_files_delete_attempts_non_negative CHECK (delete_attempts >= 0),
    UNIQUE (content_hash),
    UNIQUE (real_path)
);
CREATE TABLE user_files (
    id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    stored_file_id INTEGER NOT NULL,
    display_name TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_user_files_user_stored UNIQUE (user_id, stored_file_id),
    FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE,
    FOREIGN KEY(stored_file_id) REFERENCES stored_files (id) ON DELETE CASCADE
);
CREATE TABLE share_links (
    id INTEGER NOT NULL,
    share_code VARCHAR(32) NOT NULL,
    owner_id INTEGER NOT NULL,
    user_file_id INTEGER NOT NULL,
    password_hash TEXT,
    password_encrypted TEXT,
    expires_at_ms INTEGER,
    max_downloads INTEGER,
    download_count INTEGER DEFAULT '0' NOT NULL,
    status VARCHAR(16) NOT NULL,
    created_at_ms INTEGER NOT NULL,
    last_accessed_at_ms INTEGER,
    PRIMARY KEY (id),
    CONSTRAINT ck_share_links_status CHECK (status IN ('active', 'revoked')),
    UNIQUE (share_code),
    FOREIGN KEY(owner_id) REFERENCES users (id) ON DELETE CASCADE,
    FOREIGN KEY(user_file_id) REFERENCES user_files (id) ON DELETE CASCADE
);
CREATE TABLE app_settings (
    id INTEGER NOT NULL,
    max_task_size_bytes INTEGER NOT NULL,
    min_free_disk_bytes INTEGER NOT NULL,
    aria2_rpc_url TEXT NOT NULL,
    aria2_rpc_secret TEXT,
    aria2_bt_stop_timeout_seconds INTEGER NOT NULL,
    hidden_file_extensions_json TEXT NOT NULL,
    pack_format VARCHAR(32) NOT NULL,
    pack_compression_level INTEGER NOT NULL,
    ws_reconnect_max_delay INTEGER NOT NULL,
    ws_reconnect_jitter VARCHAR(16) NOT NULL,
    ws_reconnect_factor VARCHAR(16) NOT NULL,
    site_title VARCHAR(50) NOT NULL,
    rate_limit_account_security INTEGER NOT NULL,
    rate_limit_authenticated_api INTEGER NOT NULL,
    rate_limit_public_api INTEGER NOT NULL,
    rate_limit_share_access INTEGER NOT NULL,
    rate_limit_authenticated_download INTEGER NOT NULL,
    rate_limit_anonymous_download INTEGER NOT NULL,
    rate_limit_create_task INTEGER NOT NULL,
    rate_limit_create_torrent INTEGER NOT NULL,
    rate_limit_create_pack INTEGER NOT NULL,
    rate_limit_aria2_test INTEGER NOT NULL,
    rate_limit_rpc INTEGER NOT NULL,
    rate_limit_file_search INTEGER NOT NULL,
    rate_limit_create_share INTEGER NOT NULL,
    download_total_connections INTEGER NOT NULL,
    download_authenticated_reserved_connections INTEGER NOT NULL,
    download_authenticated_per_user_connections INTEGER NOT NULL,
    download_authenticated_per_file_connections INTEGER NOT NULL,
    download_anonymous_base_connections INTEGER NOT NULL,
    download_anonymous_borrow_connections INTEGER NOT NULL,
    download_anonymous_per_ip_connections INTEGER NOT NULL,
    download_anonymous_per_file_connections INTEGER NOT NULL,
    history_retention_days INTEGER DEFAULT '30' NOT NULL,
    tracker_fixed_list TEXT DEFAULT '' NOT NULL,
    tracker_remote_urls TEXT DEFAULT '' NOT NULL,
    tracker_refresh_interval_minutes INTEGER DEFAULT '0' NOT NULL,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT ck_app_settings_single_row CHECK (id = 1),
    CONSTRAINT ck_app_settings_history_retention_days_min CHECK (history_retention_days >= 1)
);
"""

# 夹具里出现的「敏感值」：测试要断言报告里一个都不出现。
# 这些不是真实凭证，只是用来证明报告不泄漏；因此显式豁免硬编码凭证告警。
V1_USER_PASSWORD_HASH = "pbkdf2_sha256$120000$aabbccdd$deadbeefdeadbeef"  # noqa: S105
V1_SECOND_PASSWORD_HASH = "pbkdf2_sha256$120000$11223344$cafebabecafebabe"  # noqa: S105
V1_SHARE_PASSWORD_HASH = "pbkdf2_sha256$120000$99887766$0badf00d0badf00d"  # noqa: S105
V1_RPC_SECRET = "v1-rpc-secret-plaintext"  # noqa: S105
V1_RPC_SECRET_ENCRYPTED = "gAAAAAB-v1-rpc-secret-ciphertext"  # noqa: S105


@dataclass(frozen=True, slots=True)
class SampleLayout:
    database: Path
    download_dir: Path
    store_dir: Path
    file_paths: dict[str, Path] = field(default_factory=dict)


def _write_file(path: Path, payload: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def _legacy_content_hash(real_path: Path, is_directory: bool) -> str:
    """V1 的 legacy 摘要：目录按「相对路径 + 文件 sha256」拼接，文件直接取 sha256。"""
    if not is_directory:
        return hashlib.sha256(real_path.read_bytes()).hexdigest()
    digest = hashlib.sha256()
    for item in sorted(real_path.rglob("*")):
        relative = item.relative_to(real_path).as_posix()
        if item.is_dir():
            continue
        digest.update(relative.encode())
        digest.update(hashlib.sha256(item.read_bytes()).hexdigest().encode())
    return digest.hexdigest()


def create_v1_sample(root: Path) -> SampleLayout:
    """在 `root` 下造一份完整 V1 样本：数据库 + store/ 下的文件与目录。"""
    root.mkdir(parents=True, exist_ok=True)
    download_dir = root / "download_dir"
    store_dir = download_dir / "store"
    store_dir.mkdir(parents=True)

    shared = _write_file(store_dir / "aa" / "shared.bin", b"shared-payload" * 100)
    solo = _write_file(store_dir / "bb" / "solo.bin", b"solo-payload")
    missing = store_dir / "cc" / "gone.bin"
    pending = _write_file(store_dir / "dd" / "pending.bin", b"pending-payload")
    folder = store_dir / "ee" / "folder"
    _write_file(folder / "one.txt", b"one")
    _write_file(folder / "nested" / "two.txt", b"two-two")

    database = root / "v1.db"
    connection = sqlite3.connect(database)
    try:
        connection.executescript(V1_DDL)
        connection.execute(
            "INSERT INTO schema_meta (id, version, created_at_ms) VALUES (1, ?, 1000)",
            (V1_SCHEMA_VERSION,),
        )
        connection.executemany(
            "INSERT INTO users (id, username, password_hash, is_admin, quota_bytes, "
            "rpc_secret_digest, rpc_secret_prefix, rpc_secret_encrypted, "
            "rpc_secret_created_at_ms, is_initial_password, created_at_ms, updated_at_ms, "
            "pending_delete) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)",
            [
                (1, "alice", V1_USER_PASSWORD_HASH, 1, 10 * 1024**3,
                 "a" * 64, "rpc_abcdef", V1_RPC_SECRET_ENCRYPTED, 1000, 1000, 1000, 0),
                (2, "bob", V1_SECOND_PASSWORD_HASH, 0, 1024**3,
                 None, None, None, None, 2000, 2000, 0),
                (3, "carol", V1_SECOND_PASSWORD_HASH, 0, 1024**3,
                 None, None, None, None, 3000, 3000, 1),
                (4, "dave", V1_USER_PASSWORD_HASH, 0, 1024**3,
                 None, None, None, None, 4000, 4000, 0),
            ],
        )
        connection.executemany(
            "INSERT INTO stored_files (id, content_hash, content_hash_version, "
            "content_object_kind, content_digest, real_path, size_bytes, is_directory, "
            "original_name, created_at_ms, pending_delete) "
            "VALUES (?, ?, 'v1', 'legacy', NULL, ?, ?, ?, ?, ?, ?)",
            [
                (1, _legacy_content_hash(shared, False), str(shared), shared.stat().st_size,
                 0, "shared.bin", 1000, 0),
                (2, _legacy_content_hash(solo, False), str(solo), solo.stat().st_size,
                 0, "solo.bin", 1000, 0),
                (3, "f" * 64, str(missing), 10, 0, "gone.bin", 1000, 0),
                (4, _legacy_content_hash(pending, False), str(pending),
                 pending.stat().st_size, 0, "pending.bin", 1000, 1),
                (5, _legacy_content_hash(folder, True), str(folder),
                 sum(item.stat().st_size for item in folder.rglob("*") if item.is_file()),
                 1, "folder", 1000, 0),
            ],
        )
        connection.executemany(
            "INSERT INTO user_files (id, user_id, stored_file_id, display_name, "
            "created_at_ms, updated_at_ms) VALUES (?, ?, ?, ?, ?, ?)",
            [
                (1, 1, 1, "shared.bin", 1000, 1000),
                (2, 2, 1, "bob-shared.bin", 2000, 2000),
                (3, 2, 2, "solo.bin", 2000, 2000),
                (4, 1, 3, "gone.bin", 1000, 1000),
                (5, 1, 4, "pending.bin", 1000, 1000),
                (6, 1, 5, "folder", 1000, 1000),
                (7, 3, 2, "carol-solo.bin", 3000, 3000),
            ],
        )
        connection.executemany(
            "INSERT INTO share_links (id, share_code, owner_id, user_file_id, "
            "password_hash, password_encrypted, expires_at_ms, max_downloads, "
            "download_count, status, created_at_ms, last_accessed_at_ms) "
            "VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)",
            [
                (1, "shareaaa", 1, 1, None, None, None, 0, "active", 1000, 1500),
                (2, "sharebbb", 2, 2, V1_SHARE_PASSWORD_HASH, 5000, 10, 3,
                 "active", 2000, 2500),
                (3, "shareccc", 2, 4, None, None, None, 0, "revoked", 2000, None),
                (4, "shareddd", 3, 7, None, None, None, 0, "active", 3000, None),
            ],
        )
        connection.execute(
            "INSERT INTO app_settings (id, max_task_size_bytes, min_free_disk_bytes, "
            "aria2_rpc_url, aria2_rpc_secret, aria2_bt_stop_timeout_seconds, "
            "hidden_file_extensions_json, pack_format, pack_compression_level, "
            "ws_reconnect_max_delay, ws_reconnect_jitter, ws_reconnect_factor, site_title, "
            "rate_limit_account_security, rate_limit_authenticated_api, "
            "rate_limit_public_api, rate_limit_share_access, "
            "rate_limit_authenticated_download, rate_limit_anonymous_download, "
            "rate_limit_create_task, rate_limit_create_torrent, rate_limit_create_pack, "
            "rate_limit_aria2_test, rate_limit_rpc, rate_limit_file_search, "
            "rate_limit_create_share, download_total_connections, "
            "download_authenticated_reserved_connections, "
            "download_authenticated_per_user_connections, "
            "download_authenticated_per_file_connections, "
            "download_anonymous_base_connections, download_anonymous_borrow_connections, "
            "download_anonymous_per_ip_connections, download_anonymous_per_file_connections, "
            "history_retention_days, tracker_fixed_list, tracker_remote_urls, "
            "tracker_refresh_interval_minutes, created_at_ms, updated_at_ms) "
            "VALUES (1, 2048, 1024, 'http://127.0.0.1:6800/jsonrpc', ?, 300, "
            "'[\".part\"]', 'zip', 5, 30, '0.5', '3.0', '我的站点', 11, 601, 301, 31, 0, 0, "
            "61, 32, 33, 12, 602, 121, 62, 90, 8, 12, 4, 30, 20, 3, 2, 45, "
            "'udp://tracker.one:6969/announce,http://tracker.two/announce', "
            "'https://tracker.three/list', 10, 1000, 1000)",
            (V1_RPC_SECRET,),
        )
        connection.commit()
    finally:
        connection.close()

    return SampleLayout(
        database=database,
        download_dir=download_dir,
        store_dir=store_dir,
        file_paths={
            "shared": shared,
            "solo": solo,
            "missing": missing,
            "pending": pending,
            "folder": folder,
        },
    )


@pytest.fixture
def v1_sample(tmp_path: Path) -> SampleLayout:
    return create_v1_sample(tmp_path / "v1")


def checksum_tree(root: Path) -> dict[str, str]:
    """目录树逐文件 sha256，用来证明 V1 侧前后未被改动。"""
    result: dict[str, str] = {}
    for item in sorted(root.rglob("*")):
        if item.is_file():
            result[item.relative_to(root).as_posix()] = hashlib.sha256(
                item.read_bytes()
            ).hexdigest()
    return result


@pytest.fixture
def tree_checksum() -> Iterator[object]:
    yield checksum_tree
