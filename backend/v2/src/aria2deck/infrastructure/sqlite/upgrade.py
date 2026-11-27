"""把已发布的 schema 1 / schema 2 数据库原地升级到 schema 3（新增 `attempt_manifests`）。

只做一件事：应用停止后，校验源结构与完整性 → 生成不覆盖旧文件的备份 →
按版本逐级前进（1→2 重建 `users` 回填账号状态；2→3 纯增建 `attempt_manifests`）→
全部提交后用现有结构校验确认结果。逐级提交意味着 2→3 失败时库停在合法的 schema 2，
再跑一次只会补做 2→3。

不从应用启动流程调用；重复执行只做验证并报告，不会重新回填、也不会重复建表。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
import sys
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.pool import StaticPool

from ...domain.errors import DomainError
from ..instance_lock import InstanceLock
from .bootstrap import (
    SchemaSnapshot,
    # 与 validate_database 共用同一套规范化：两条路径对「同一份结构」必须给出同一结论。
    _normalized_sql,
    _snapshot,
    validate_database,
)
from .engine import SQLiteDatabase
from .schema import SCHEMA_VERSION, create_table_statement, users

# 与离线迁移工具 planner.PLACEHOLDER_PASSWORD 是同一个固定串；两边各自保留兼容识别，
# 由测试锚定口径一致，运行时代码不导入离线迁移包。
MIGRATED_PLACEHOLDER_PASSWORD = "!migrated-placeholder!"  # noqa: S105  # 占位串，不是密钥

# 上一版发布时 `users` 的建表语句原文（schema 1，没有 status 列与状态约束）。
# 升级只接受规范化后等于这段文本的源表：多一个 CHECK、少一个约束、改一个约束名
# 都属于异常结构，因为重建时 DROP TABLE 会把它们一并抹掉，等于悄悄改写用户库的结构。
RELEASED_SCHEMA_1_USERS_DDL = (
    "CREATE TABLE users (\n"
    "\tid INTEGER NOT NULL, \n"
    "\tusername TEXT NOT NULL, \n"
    "\tusername_key TEXT NOT NULL, \n"
    "\tpassword_hash TEXT NOT NULL, \n"
    "\tis_admin BOOLEAN NOT NULL, \n"
    "\tquota_bytes BIGINT NOT NULL, \n"
    "\tis_initial_password BOOLEAN NOT NULL, \n"
    "\trpc_secret_ciphertext TEXT, \n"
    "\trpc_secret_prefix TEXT, \n"
    "\tcredential_generation INTEGER NOT NULL, \n"
    "\tpending_delete BOOLEAN NOT NULL, \n"
    "\tdelete_lease_owner TEXT, \n"
    "\tdelete_lease_expires_at_ms BIGINT, \n"
    "\tdelete_retry_count INTEGER NOT NULL, \n"
    "\tcreated_at_ms BIGINT NOT NULL, \n"
    "\tupdated_at_ms BIGINT NOT NULL, \n"
    "\tPRIMARY KEY (id), \n"
    "\tCONSTRAINT uq_users_username_key UNIQUE (username_key), \n"
    "\tCONSTRAINT ck_users_quota_nonnegative CHECK (quota_bytes >= 0), \n"
    "\tCONSTRAINT ck_users_credential_generation CHECK (credential_generation >= 1), \n"
    "\tCONSTRAINT ck_users_delete_retry CHECK (delete_retry_count >= 0), \n"
    "\tCONSTRAINT ck_users_rpc_secret_pair CHECK ((rpc_secret_ciphertext IS NULL) = (rpc_secret_prefix IS NULL)), \n"
    "\tCONSTRAINT ck_users_delete_lease_pair CHECK ((delete_lease_owner IS NULL) = (delete_lease_expires_at_ms IS NULL))\n"
    ")"
)
RELEASED_SCHEMA_1_USERS_SQL = _normalized_sql(RELEASED_SCHEMA_1_USERS_DDL)

# 已发布 schema 2 的全部结构（16 张表 + 索引 + 触发器），逐字取自真实 schema 2 库的
# sqlite_master。2→3 只允许新增 `attempt_manifests`：升级前拿它当源结构的唯一合格样本，
# 多一个列、少一个 CHECK、改一个索引都按异常结构拒绝，而不是照单全收后再建表。
RELEASED_SCHEMA_2_DDL: tuple[str, ...] = (
    # app_settings
    """
CREATE TABLE app_settings (
	id INTEGER NOT NULL,
	site_title TEXT NOT NULL,
	max_task_size BIGINT NOT NULL,
	min_free_disk BIGINT NOT NULL,
	aria2_rpc_url TEXT NOT NULL,
	aria2_rpc_secret_ciphertext TEXT,
	aria2_bt_stop_timeout_seconds INTEGER NOT NULL,
	hidden_file_extensions_json TEXT NOT NULL,
	pack_format TEXT NOT NULL,
	pack_compression_level INTEGER NOT NULL,
	ws_reconnect_max_delay_seconds INTEGER NOT NULL,
	ws_reconnect_jitter_seconds INTEGER NOT NULL,
	ws_reconnect_factor INTEGER NOT NULL,
	history_retention_days INTEGER NOT NULL,
	tracker_fixed_list_json TEXT NOT NULL,
	tracker_remote_urls_json TEXT NOT NULL,
	tracker_refresh_interval_minutes INTEGER NOT NULL,
	download_connection_limits_json TEXT NOT NULL,
	account_security_rate_limit INTEGER NOT NULL,
	authenticated_api_rate_limit INTEGER NOT NULL,
	public_api_rate_limit INTEGER NOT NULL,
	share_access_rate_limit INTEGER NOT NULL,
	create_task_rate_limit INTEGER NOT NULL,
	create_torrent_rate_limit INTEGER NOT NULL,
	create_pack_rate_limit INTEGER NOT NULL,
	aria2_test_rate_limit INTEGER NOT NULL,
	rpc_rate_limit INTEGER NOT NULL,
	file_search_rate_limit INTEGER NOT NULL,
	create_share_rate_limit INTEGER NOT NULL,
	updated_at_ms BIGINT NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_app_settings_singleton CHECK (id = 1),
	CONSTRAINT ck_app_settings_task_size CHECK (max_task_size >= 0),
	CONSTRAINT ck_app_settings_free_disk CHECK (min_free_disk >= 0),
	CONSTRAINT ck_app_settings_pack_format CHECK (pack_format IN ('zip', 'tar.zst')),
	CONSTRAINT ck_app_settings_pack_level CHECK (pack_compression_level BETWEEN 0 AND 22),
	CONSTRAINT ck_app_settings_history CHECK (history_retention_days >= 1),
	CONSTRAINT ck_app_settings_tracker_interval CHECK (tracker_refresh_interval_minutes = 0 OR tracker_refresh_interval_minutes >= 5)
)
    """,
    # content_objects
    """
CREATE TABLE content_objects (
	id INTEGER NOT NULL,
	content_key TEXT NOT NULL,
	kind TEXT NOT NULL,
	digest TEXT NOT NULL,
	size BIGINT NOT NULL,
	storage_path TEXT NOT NULL,
	deletion_state TEXT NOT NULL,
	read_leases INTEGER NOT NULL,
	delete_lease_owner TEXT,
	delete_lease_expires_at_ms BIGINT,
	delete_retry_count INTEGER NOT NULL,
	next_delete_attempt_at_ms BIGINT,
	tombstone_path TEXT,
	created_at_ms BIGINT NOT NULL,
	updated_at_ms BIGINT NOT NULL,
	deleted_at_ms BIGINT,
	PRIMARY KEY (id),
	CONSTRAINT uq_content_objects_key UNIQUE (content_key),
	CONSTRAINT uq_content_objects_path UNIQUE (storage_path),
	CONSTRAINT ck_content_objects_kind CHECK (kind IN ('file', 'directory')),
	CONSTRAINT ck_content_digest CHECK (length(digest) = 64 AND digest NOT GLOB '*[^0-9a-f]*'),
	CONSTRAINT ck_content_size CHECK (size >= 0),
	CONSTRAINT ck_content_relative_path CHECK (storage_path NOT LIKE '/%'),
	CONSTRAINT ck_content_deletion_state CHECK (deletion_state IN ('active', 'pending_delete', 'tombstoned')),
	CONSTRAINT ck_content_read_leases CHECK (read_leases >= 0),
	CONSTRAINT ck_content_delete_retry CHECK (delete_retry_count >= 0),
	CONSTRAINT ck_content_delete_lease_pair CHECK ((delete_lease_owner IS NULL) = (delete_lease_expires_at_ms IS NULL)),
	CONSTRAINT ck_content_tombstone CHECK ((deletion_state = 'tombstoned') = (tombstone_path IS NOT NULL))
)
    """,
    # download_sources
    """
CREATE TABLE download_sources (
	id INTEGER NOT NULL,
	kind TEXT NOT NULL,
	resource_key TEXT NOT NULL,
	payload_json TEXT NOT NULL,
	options_json TEXT NOT NULL,
	selected_file_indexes_json TEXT,
	display_name TEXT NOT NULL,
	display_uri TEXT,
	created_at_ms BIGINT NOT NULL,
	detached_at_ms BIGINT,
	PRIMARY KEY (id),
	CONSTRAINT uq_download_sources_resource UNIQUE (resource_key),
	CONSTRAINT uq_download_sources_identity UNIQUE (id, resource_key),
	CONSTRAINT ck_download_sources_kind CHECK (kind IN ('http', 'magnet', 'torrent'))
)
    """,
    # schema_meta
    """
CREATE TABLE schema_meta (
	id INTEGER NOT NULL,
	schema_version INTEGER NOT NULL,
	created_at_ms BIGINT NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_schema_meta_singleton CHECK (id = 1)
)
    """,
    # tracker_cache
    """
CREATE TABLE tracker_cache (
	id INTEGER NOT NULL,
	merged_json TEXT NOT NULL,
	remote_json TEXT NOT NULL,
	refresh_status TEXT NOT NULL,
	failed_urls_json TEXT NOT NULL,
	updated_at_ms BIGINT,
	last_refresh_at_ms BIGINT,
	PRIMARY KEY (id),
	CONSTRAINT ck_tracker_cache_singleton CHECK (id = 1),
	CONSTRAINT ck_tracker_cache_status CHECK (refresh_status IN ('never', 'refreshing', 'success', 'partial', 'failed'))
)
    """,
    # users
    """
CREATE TABLE users (
	id INTEGER NOT NULL,
	username TEXT NOT NULL,
	username_key TEXT NOT NULL,
	password_hash TEXT NOT NULL,
	is_admin BOOLEAN NOT NULL,
	quota_bytes BIGINT NOT NULL,
	is_initial_password BOOLEAN NOT NULL,
	status TEXT DEFAULT 'active' NOT NULL,
	rpc_secret_ciphertext TEXT,
	rpc_secret_prefix TEXT,
	credential_generation INTEGER NOT NULL,
	pending_delete BOOLEAN NOT NULL,
	delete_lease_owner TEXT,
	delete_lease_expires_at_ms BIGINT,
	delete_retry_count INTEGER NOT NULL,
	created_at_ms BIGINT NOT NULL,
	updated_at_ms BIGINT NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_users_username_key UNIQUE (username_key),
	CONSTRAINT ck_users_quota_nonnegative CHECK (quota_bytes >= 0),
	CONSTRAINT ck_users_credential_generation CHECK (credential_generation >= 1),
	CONSTRAINT ck_users_delete_retry CHECK (delete_retry_count >= 0),
	CONSTRAINT ck_users_status CHECK (status IN ('active', 'frozen', 'locked')),
	CONSTRAINT ck_users_rpc_secret_pair CHECK ((rpc_secret_ciphertext IS NULL) = (rpc_secret_prefix IS NULL)),
	CONSTRAINT ck_users_delete_lease_pair CHECK ((delete_lease_owner IS NULL) = (delete_lease_expires_at_ms IS NULL))
)
    """,
    # api_tokens
    """
CREATE TABLE api_tokens (
	id INTEGER NOT NULL,
	user_id INTEGER NOT NULL,
	name TEXT NOT NULL,
	token_digest TEXT NOT NULL,
	prefix TEXT NOT NULL,
	created_at_ms BIGINT NOT NULL,
	last_used_at_ms BIGINT,
	PRIMARY KEY (id),
	CONSTRAINT uq_api_tokens_digest UNIQUE (token_digest),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
)
    """,
    # content_entries
    """
CREATE TABLE content_entries (
	id INTEGER NOT NULL,
	content_id INTEGER NOT NULL,
	parent_path TEXT NOT NULL,
	name TEXT NOT NULL,
	path TEXT NOT NULL,
	entry_kind TEXT NOT NULL,
	size BIGINT NOT NULL,
	modified_at_ms BIGINT,
	PRIMARY KEY (id),
	CONSTRAINT uq_content_entries_path UNIQUE (content_id, path),
	CONSTRAINT uq_content_entries_sibling UNIQUE (content_id, parent_path, name),
	CONSTRAINT ck_content_entries_kind CHECK (entry_kind IN ('file', 'directory')),
	CONSTRAINT ck_content_entries_size CHECK (size >= 0),
	FOREIGN KEY(content_id) REFERENCES content_objects (id) ON DELETE CASCADE
)
    """,
    # download_attempts
    """
CREATE TABLE download_attempts (
	id INTEGER NOT NULL,
	source_id INTEGER NOT NULL,
	resource_key TEXT NOT NULL,
	gid TEXT NOT NULL,
	status TEXT NOT NULL,
	completed_content_id INTEGER,
	ingestion_started_at_ms BIGINT,
	submission_state TEXT NOT NULL,
	total_length BIGINT,
	completed_length BIGINT NOT NULL,
	physical_reserved_bytes BIGINT NOT NULL,
	error_code TEXT,
	created_at_ms BIGINT NOT NULL,
	updated_at_ms BIGINT NOT NULL,
	finished_at_ms BIGINT,
	result_consumed_at_ms BIGINT,
	PRIMARY KEY (id),
	CONSTRAINT fk_download_attempts_source_identity FOREIGN KEY(source_id, resource_key) REFERENCES download_sources (id, resource_key) ON DELETE RESTRICT,
	CONSTRAINT uq_download_attempts_gid UNIQUE (gid),
	CONSTRAINT ck_download_attempts_status CHECK (status IN ('queued', 'active', 'waiting', 'paused', 'completed', 'failed', 'cancelled')),
	CONSTRAINT ck_download_attempts_submission CHECK (submission_state IN ('planned', 'submitted', 'uncertain')),
	CONSTRAINT ck_attempt_total CHECK (total_length IS NULL OR total_length >= 0),
	CONSTRAINT ck_attempt_completed CHECK (completed_length >= 0),
	CONSTRAINT ck_attempt_progress CHECK (total_length IS NULL OR completed_length <= total_length),
	CONSTRAINT ck_attempt_physical_reserved CHECK (physical_reserved_bytes >= 0),
	FOREIGN KEY(completed_content_id) REFERENCES content_objects (id) ON DELETE SET NULL
)
    """,
    # pack_jobs
    """
CREATE TABLE pack_jobs (
	id INTEGER NOT NULL,
	owner_id INTEGER NOT NULL,
	status TEXT NOT NULL,
	step TEXT,
	step_progress INTEGER NOT NULL,
	step_started_at_ms BIGINT,
	pack_format TEXT NOT NULL,
	output_name TEXT NOT NULL,
	source_size BIGINT NOT NULL,
	reserved_bytes BIGINT NOT NULL,
	physical_reserved_bytes BIGINT NOT NULL,
	prepared_path TEXT,
	prepared_size BIGINT,
	prepared_digest TEXT,
	output_content_id INTEGER,
	delete_source BOOLEAN NOT NULL,
	cleanup_pending BOOLEAN NOT NULL,
	retry_count INTEGER NOT NULL,
	next_attempt_at_ms BIGINT,
	error_code TEXT,
	created_at_ms BIGINT NOT NULL,
	updated_at_ms BIGINT NOT NULL,
	finished_at_ms BIGINT,
	PRIMARY KEY (id),
	CONSTRAINT ck_pack_status CHECK (status IN ('pending', 'packing', 'done', 'failed', 'cancelled')),
	CONSTRAINT ck_pack_step CHECK (step IS NULL OR step IN ('validating', 'compressing', 'verifying')),
	CONSTRAINT ck_pack_step_progress CHECK (step_progress BETWEEN 0 AND 100),
	CONSTRAINT ck_pack_format CHECK (pack_format IN ('zip', 'tar.zst')),
	CONSTRAINT ck_pack_source_size CHECK (source_size >= 0),
	CONSTRAINT ck_pack_reserved CHECK (reserved_bytes >= 0),
	CONSTRAINT ck_pack_physical_reserved CHECK (physical_reserved_bytes >= 0),
	CONSTRAINT ck_pack_retry CHECK (retry_count >= 0),
	CONSTRAINT ck_pack_prepared_pair CHECK ((prepared_path IS NULL AND prepared_size IS NULL AND prepared_digest IS NULL) OR (prepared_path IS NOT NULL AND prepared_size IS NOT NULL AND prepared_digest IS NOT NULL)),
	CONSTRAINT ck_pack_done_output CHECK (status <> 'done' OR output_content_id IS NOT NULL),
	FOREIGN KEY(owner_id) REFERENCES users (id) ON DELETE RESTRICT,
	FOREIGN KEY(output_content_id) REFERENCES content_objects (id) ON DELETE RESTRICT
)
    """,
    # quota_accounts
    """
CREATE TABLE quota_accounts (
	user_id INTEGER NOT NULL,
	used_bytes BIGINT NOT NULL,
	reserved_bytes BIGINT NOT NULL,
	revision INTEGER NOT NULL,
	updated_at_ms BIGINT NOT NULL,
	PRIMARY KEY (user_id),
	CONSTRAINT ck_quota_used CHECK (used_bytes >= 0),
	CONSTRAINT ck_quota_reserved CHECK (reserved_bytes >= 0),
	CONSTRAINT ck_quota_revision CHECK (revision >= 0),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
)
    """,
    # sessions
    """
CREATE TABLE sessions (
	id TEXT NOT NULL,
	user_id INTEGER NOT NULL,
	credential_generation INTEGER NOT NULL,
	created_at_ms BIGINT NOT NULL,
	expires_at_ms BIGINT NOT NULL,
	last_seen_at_ms BIGINT,
	revoked_at_ms BIGINT,
	PRIMARY KEY (id),
	CONSTRAINT ck_sessions_expiry CHECK (expires_at_ms > created_at_ms),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
)
    """,
    # user_files
    """
CREATE TABLE user_files (
	id INTEGER NOT NULL,
	user_id INTEGER NOT NULL,
	content_id INTEGER NOT NULL,
	display_name TEXT NOT NULL,
	created_at_ms BIGINT NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_user_files_user_content UNIQUE (user_id, content_id),
	CONSTRAINT uq_user_files_user_name UNIQUE (user_id, display_name),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE RESTRICT,
	FOREIGN KEY(content_id) REFERENCES content_objects (id) ON DELETE RESTRICT
)
    """,
    # pack_sources
    """
CREATE TABLE pack_sources (
	id INTEGER NOT NULL,
	pack_id INTEGER NOT NULL,
	source_user_file_id INTEGER NOT NULL,
	source_content_id INTEGER NOT NULL,
	content_key TEXT NOT NULL,
	display_name TEXT NOT NULL,
	source_created_at_ms BIGINT NOT NULL,
	source_size BIGINT NOT NULL,
	cleanup_status TEXT NOT NULL,
	cleanup_error_code TEXT,
	PRIMARY KEY (id),
	CONSTRAINT uq_pack_sources_snapshot UNIQUE (pack_id, source_user_file_id),
	CONSTRAINT ck_pack_sources_size CHECK (source_size >= 0),
	CONSTRAINT ck_pack_source_cleanup CHECK (cleanup_status IN ('pending', 'deleted', 'kept')),
	FOREIGN KEY(pack_id) REFERENCES pack_jobs (id) ON DELETE CASCADE
)
    """,
    # share_links
    """
CREATE TABLE share_links (
	id INTEGER NOT NULL,
	code TEXT NOT NULL,
	owner_id INTEGER NOT NULL,
	user_file_id INTEGER,
	file_name TEXT NOT NULL,
	file_size BIGINT NOT NULL,
	file_kind TEXT NOT NULL,
	content_key TEXT NOT NULL,
	password_hash TEXT,
	expires_at_ms BIGINT,
	max_downloads INTEGER,
	download_count INTEGER NOT NULL,
	revoked_at_ms BIGINT,
	created_at_ms BIGINT NOT NULL,
	last_accessed_at_ms BIGINT,
	PRIMARY KEY (id),
	CONSTRAINT uq_share_links_code UNIQUE (code),
	CONSTRAINT ck_share_file_size CHECK (file_size >= 0),
	CONSTRAINT ck_share_file_kind CHECK (file_kind IN ('file', 'directory')),
	CONSTRAINT ck_share_limit CHECK (max_downloads IS NULL OR max_downloads BETWEEN 1 AND 10000),
	CONSTRAINT ck_share_count CHECK (download_count >= 0),
	CONSTRAINT ck_share_count_limit CHECK (max_downloads IS NULL OR download_count <= max_downloads),
	FOREIGN KEY(owner_id) REFERENCES users (id) ON DELETE CASCADE,
	FOREIGN KEY(user_file_id) REFERENCES user_files (id) ON DELETE SET NULL
)
    """,
    # task_subscriptions
    """
CREATE TABLE task_subscriptions (
	id INTEGER NOT NULL,
	attempt_id INTEGER NOT NULL,
	user_id INTEGER NOT NULL,
	status TEXT NOT NULL,
	reserved_bytes BIGINT NOT NULL,
	display_name TEXT NOT NULL,
	display_uri TEXT,
	error_code TEXT,
	created_at_ms BIGINT NOT NULL,
	updated_at_ms BIGINT NOT NULL,
	finished_at_ms BIGINT,
	expired_at_ms BIGINT,
	PRIMARY KEY (id),
	CONSTRAINT ck_task_subscriptions_status CHECK (status IN ('queued', 'active', 'waiting', 'paused', 'completed', 'failed', 'cancelled', 'expired')),
	CONSTRAINT ck_task_subscriptions_reserved CHECK (reserved_bytes >= 0),
	FOREIGN KEY(attempt_id) REFERENCES download_attempts (id) ON DELETE RESTRICT,
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE RESTRICT
)
    """,
    # ix_api_tokens_user
    """
CREATE INDEX ix_api_tokens_user ON api_tokens (user_id)
    """,
    # ix_content_entries_browse
    """
CREATE INDEX ix_content_entries_browse ON content_entries (content_id, parent_path, entry_kind, name)
    """,
    # ix_content_objects_pending_delete
    """
CREATE INDEX ix_content_objects_pending_delete ON content_objects (next_delete_attempt_at_ms) WHERE deletion_state = 'pending_delete'
    """,
    # ix_download_attempts_status
    """
CREATE INDEX ix_download_attempts_status ON download_attempts (status, updated_at_ms)
    """,
    # ix_download_sources_detached
    """
CREATE INDEX ix_download_sources_detached ON download_sources (detached_at_ms)
    """,
    # ix_pack_jobs_dispatch
    """
CREATE INDEX ix_pack_jobs_dispatch ON pack_jobs (status, next_attempt_at_ms) WHERE status IN ('pending', 'packing')
    """,
    # ix_pack_sources_cleanup
    """
CREATE INDEX ix_pack_sources_cleanup ON pack_sources (cleanup_status, pack_id)
    """,
    # ix_sessions_expiry
    """
CREATE INDEX ix_sessions_expiry ON sessions (expires_at_ms)
    """,
    # ix_sessions_user
    """
CREATE INDEX ix_sessions_user ON sessions (user_id)
    """,
    # ix_share_links_active_file
    """
CREATE INDEX ix_share_links_active_file ON share_links (user_file_id, revoked_at_ms) WHERE revoked_at_ms IS NULL
    """,
    # ix_task_subscriptions_attempt
    """
CREATE INDEX ix_task_subscriptions_attempt ON task_subscriptions (attempt_id, status)
    """,
    # ix_task_subscriptions_user_status
    """
CREATE INDEX ix_task_subscriptions_user_status ON task_subscriptions (user_id, status, updated_at_ms)
    """,
    # ix_user_files_content
    """
CREATE INDEX ix_user_files_content ON user_files (content_id)
    """,
    # ix_user_files_user
    """
CREATE INDEX ix_user_files_user ON user_files (user_id, created_at_ms)
    """,
    # uq_download_attempts_live_resource
    """
CREATE UNIQUE INDEX uq_download_attempts_live_resource ON download_attempts (resource_key) WHERE status IN ('queued', 'active', 'waiting', 'paused')
    """,
    # uq_task_subscriptions_live_user_attempt
    """
CREATE UNIQUE INDEX uq_task_subscriptions_live_user_attempt ON task_subscriptions (user_id, attempt_id) WHERE status IN ('queued', 'active', 'waiting', 'paused')
    """,
    # trg_pack_jobs_prepared_consistency
    """
CREATE TRIGGER trg_pack_jobs_prepared_consistency
    BEFORE UPDATE OF prepared_path, prepared_size, prepared_digest ON pack_jobs
    WHEN NOT (
        (NEW.prepared_path IS NULL AND NEW.prepared_size IS NULL AND NEW.prepared_digest IS NULL)
        OR (NEW.prepared_path IS NOT NULL AND NEW.prepared_size IS NOT NULL AND NEW.prepared_digest IS NOT NULL)
    )
    BEGIN
        SELECT RAISE(ABORT, 'prepared output must be complete');
    END
    """,
    # trg_quota_accounts_nonnegative
    """
CREATE TRIGGER trg_quota_accounts_nonnegative
    BEFORE UPDATE OF used_bytes, reserved_bytes ON quota_accounts
    WHEN NEW.used_bytes < 0 OR NEW.reserved_bytes < 0
    BEGIN
        SELECT RAISE(ABORT, 'quota account cannot be negative');
    END
    """,
    # trg_share_links_count_monotonic
    """
CREATE TRIGGER trg_share_links_count_monotonic
    BEFORE UPDATE OF download_count ON share_links
    WHEN NEW.download_count < OLD.download_count
       OR (NEW.max_downloads IS NOT NULL AND NEW.download_count > NEW.max_downloads)
    BEGIN
        SELECT RAISE(ABORT, 'share download count invariant');
    END
    """,
    # trg_user_files_identity_immutable
    """
CREATE TRIGGER trg_user_files_identity_immutable
    BEFORE UPDATE OF user_id, content_id ON user_files
    WHEN NEW.user_id <> OLD.user_id OR NEW.content_id <> OLD.content_id
    BEGIN
        SELECT RAISE(ABORT, 'user file identity is immutable');
    END
    """,
)

RELEASED_SCHEMA_1 = 1
RELEASED_SCHEMA_2 = 2
SOURCE_VERSIONS = (RELEASED_SCHEMA_1, RELEASED_SCHEMA_2)
_MANIFESTS = "attempt_manifests"
_USERS = "users"
_STATUS = "status"
_TEMP_TABLE = "_users_schema_1"
_BACKUP_SUFFIX = "-pre-status-upgrade"
_MAX_BACKUP_CANDIDATES = 100
Step = Callable[[AsyncConnection], Awaitable[None]]


class UpgradeRefused(RuntimeError):
    """源库不满足升级前提，或升级过程失败；调用方必须把库当成未升级。"""


@dataclass(frozen=True, slots=True)
class UpgradeReport:
    version: int
    active_accounts: int
    locked_accounts: int
    backup_path: Path | None
    changed: bool


def _refuse(message: str) -> UpgradeRefused:
    return UpgradeRefused(message)


def _instance_lock_path(path: Path) -> Path:
    return path.with_name(path.name + ".lock")


async def _read_version(connection: AsyncConnection) -> int:
    try:
        row = (
            await connection.exec_driver_sql(
                "SELECT schema_version FROM schema_meta WHERE id = 1"
            )
        ).first()
    except SQLAlchemyError as error:
        raise _refuse(f"无法读取 schema 版本：{error}") from error
    if row is None:
        raise _refuse("schema_meta 缺少单行版本记录")
    return int(row[0])


def _ensure_schema_1_structure(actual: SchemaSnapshot, expected: SchemaSnapshot) -> None:
    """schema 1 与已发布 schema 2 的唯一差异只能是 `users.status` 缺席，其他一律按漂移拒绝。"""
    if set(actual.tables) != set(expected.tables):
        raise _refuse("数据库表集合不是已发布的 schema 2 定义")
    if actual.indexes != expected.indexes or actual.triggers != expected.triggers:
        raise _refuse("数据库索引或触发器不是已发布的 schema 2 定义")
    for name, expected_table in expected.tables.items():
        actual_table = actual.tables[name]
        if name != _USERS:
            if actual_table != expected_table:
                raise _refuse(f"表 {name} 的结构与预期不一致")
            continue
        without_status = tuple(
            column for column in expected_table.columns if column.name != _STATUS
        )
        if actual_table.columns != without_status:
            raise _refuse("users 表不是预期的 schema 1 结构")
        if actual_table.foreign_keys != expected_table.foreign_keys:
            raise _refuse("users 表外键与预期不一致")
        # 列与外键都由建表语句派生，能通过上面两关仍可能多出 CHECK 或改写约束文本；
        # 逐字比对已发布 DDL 才能保证重建后不悄悄丢弃源库上的任何结构。
        if actual_table.sql != RELEASED_SCHEMA_1_USERS_SQL:
            raise _refuse("users 表建表语句不是已发布的 schema 1 定义")


def _ensure_schema_2_structure(actual: SchemaSnapshot, expected: SchemaSnapshot) -> None:
    """2→3 是纯增量：源库必须逐项等于已发布 schema 2 结构，多出或缺少任一对象都拒绝。"""
    if set(actual.tables) != set(expected.tables):
        raise _refuse("数据库表集合不是已发布的 schema 2 定义")
    if actual.indexes != expected.indexes or actual.triggers != expected.triggers:
        raise _refuse("数据库索引或触发器不是已发布的 schema 2 定义")
    for name, expected_table in expected.tables.items():
        if actual.tables[name] != expected_table:
            raise _refuse(f"表 {name} 的结构不是已发布的 schema 2 定义")


async def _released_schema_2() -> SchemaSnapshot:
    """用已发布 schema 2 的原文建一份内存库再快照：与被检库走同一套规范化与比对口径。"""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    try:
        async with engine.connect() as connection:
            for statement in RELEASED_SCHEMA_2_DDL:
                await connection.exec_driver_sql(statement)
            return await _snapshot(connection)
    finally:
        await engine.dispose()


async def _inspect(path: Path) -> tuple[int, SchemaSnapshot]:
    database = SQLiteDatabase(path)
    try:
        try:
            async with database.connection() as connection:
                integrity = (
                    await connection.exec_driver_sql("PRAGMA integrity_check")
                ).scalar_one()
                if str(integrity).strip().lower() != "ok":
                    raise _refuse(f"SQLite 完整性检查未通过：{integrity}")
                return await _read_version(connection), await _snapshot(connection)
        except SQLAlchemyError as error:
            raise _refuse(f"无法读取数据库：{error}") from error
    finally:
        await database.close()


def backup_path_for(path: Path, *, now: datetime | None = None) -> Path:
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    for index in range(_MAX_BACKUP_CANDIDATES):
        suffix = "" if index == 0 else f"-{index}"
        candidate = path.with_name(f"{path.stem}{_BACKUP_SUFFIX}-{stamp}{suffix}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise _refuse("备份文件名候选已用尽，请先整理数据目录")


def create_backup(path: Path, *, now: datetime | None = None) -> Path:
    """用 SQLite 备份 API 生成独立备份；WAL 内容一并落进单文件，不覆盖已有文件。"""
    target = backup_path_for(path, now=now)
    try:
        source = sqlite3.connect(path)
        try:
            destination = sqlite3.connect(target)
            try:
                source.backup(destination)
                destination.execute("PRAGMA journal_mode = delete")
            finally:
                destination.close()
        finally:
            source.close()
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return target


async def _apply_schema_2(connection: AsyncConnection) -> None:
    await connection.exec_driver_sql("PRAGMA foreign_keys = OFF")
    await connection.exec_driver_sql("BEGIN IMMEDIATE")
    try:
        await connection.exec_driver_sql(
            f"CREATE TABLE {_TEMP_TABLE} AS SELECT * FROM {_USERS}"  # noqa: S608  # 表名来自内部常量
        )
        await connection.exec_driver_sql(f"DROP TABLE {_USERS}")  # noqa: S608
        await connection.exec_driver_sql(create_table_statement(_USERS))
        columns = [column.name for column in users.columns]
        select_columns = ", ".join(
            (
                "CASE WHEN password_hash = :placeholder THEN 'locked' ELSE 'active' END"
                if column == _STATUS
                else column
            )
            for column in columns
        )
        statement = (
            f"INSERT INTO {_USERS} ({', '.join(columns)}) "  # noqa: S608  # 内部常量，值走绑定参数
            f"SELECT {select_columns} FROM {_TEMP_TABLE}"
        )
        await connection.exec_driver_sql(
            statement, {"placeholder": MIGRATED_PLACEHOLDER_PASSWORD}
        )
        await connection.exec_driver_sql(f"DROP TABLE {_TEMP_TABLE}")  # noqa: S608
        await connection.exec_driver_sql(
            "UPDATE schema_meta SET schema_version = :version WHERE id = 1",
            {"version": RELEASED_SCHEMA_2},
        )
        broken = (await connection.exec_driver_sql("PRAGMA foreign_key_check")).all()
        if broken:
            raise _refuse(f"外键校验失败（{len(broken)} 行）")
        await connection.commit()
    except BaseException:
        await connection.rollback()
        raise


async def _apply_schema_3(connection: AsyncConnection) -> None:
    """2→3 不改动任何既有对象：一条建表语句加版本号，失败整体回滚。"""
    await connection.exec_driver_sql("BEGIN IMMEDIATE")
    try:
        await connection.exec_driver_sql(create_table_statement(_MANIFESTS))
        await connection.exec_driver_sql(
            "UPDATE schema_meta SET schema_version = :version WHERE id = 1",
            {"version": SCHEMA_VERSION},
        )
        broken = (await connection.exec_driver_sql("PRAGMA foreign_key_check")).all()
        if broken:
            raise _refuse(f"外键校验失败（{len(broken)} 行）")
        await connection.commit()
    except BaseException:
        await connection.rollback()
        raise


async def _apply_step(path: Path, step: Step) -> None:
    database = SQLiteDatabase(path)
    try:
        async with database.connection() as connection:
            await step(connection)
    except Exception as error:
        raise _refuse(f"升级失败，已回滚：{error}") from error
    finally:
        await database.close()


async def _counts(path: Path) -> tuple[int, int]:
    database = SQLiteDatabase(path)
    try:
        async with database.connection() as connection:
            rows = (
                await connection.exec_driver_sql(
                    "SELECT status, COUNT(*) FROM users GROUP BY status"
                )
            ).all()
    finally:
        await database.close()
    counted = {str(status): int(count) for status, count in rows}
    return counted.get("active", 0), counted.get("locked", 0)


async def _validate(path: Path) -> None:
    database = SQLiteDatabase(path)
    try:
        await validate_database(database)
    except DomainError as error:
        raise _refuse(f"升级后结构校验失败：{error}") from error
    finally:
        await database.close()


async def upgrade_database(path: Path, *, now: datetime | None = None) -> UpgradeReport:
    if not path.is_file():
        raise _refuse(f"数据库文件不存在：{path}")
    lock = InstanceLock(_instance_lock_path(path))
    try:
        lock.acquire()
    except DomainError as error:
        raise _refuse(f"无法取得实例锁，应用可能仍在运行：{error}") from error
    try:
        version, snapshot = await _inspect(path)
        if version == SCHEMA_VERSION:
            await _validate(path)
            active, locked = await _counts(path)
            return UpgradeReport(version, active, locked, None, False)
        if version not in SOURCE_VERSIONS:
            raise _refuse(f"不支持的 schema 版本：{version}")
        released = await _released_schema_2()
        if version == RELEASED_SCHEMA_1:
            _ensure_schema_1_structure(snapshot, released)
        else:
            _ensure_schema_2_structure(snapshot, released)
        try:
            backup = create_backup(path, now=now)
        except (OSError, sqlite3.Error) as error:
            raise _refuse(f"备份失败，未写入数据库：{error}") from error
        # 逐级前进：schema 1 库先做 1→2 再做 2→3；某一级失败时库停在已发布的旧版本，重跑只补缺的那级。
        if version == RELEASED_SCHEMA_1:
            await _apply_step(path, _apply_schema_2)
        await _apply_step(path, _apply_schema_3)
        await _validate(path)
        active, locked = await _counts(path)
        return UpgradeReport(SCHEMA_VERSION, active, locked, backup, True)
    finally:
        lock.release()


def _database_path() -> Path:
    value = os.environ.get("ARIA2DECK_DATABASE_PATH")
    if value is None or not value.strip():
        raise _refuse("未指定数据库：请设置 ARIA2DECK_DATABASE_PATH")
    return Path(value).expanduser()


def run(path: Path, *, now: datetime | None = None) -> UpgradeReport:
    return asyncio.run(upgrade_database(path, now=now))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m aria2deck.infrastructure.sqlite.upgrade",
        description=(
            "把已发布的 schema 1 / schema 2 数据库升级到 schema 3（新增 attempt_manifests）。\n"
            "数据库路径取自 ARIA2DECK_DATABASE_PATH；必须先停止应用，"
            "升级前自动生成不覆盖旧文件的备份。"
        ),
    )
    parser.parse_args(argv)
    try:
        report = run(_database_path())
    except (UpgradeRefused, DomainError) as error:
        print(f"升级未执行：{error}", file=sys.stderr)
        return 1
    if report.backup_path is not None:
        print(f"升级前备份: {report.backup_path}")
        print(f"数据库已升级到 schema {SCHEMA_VERSION}。")
    else:
        print(f"数据库已是 schema {SCHEMA_VERSION}，无需升级。")
    print(f"schema 版本: {report.version}")
    print(
        f"正常账号: {report.active_accounts}，锁定账号: {report.locked_accounts}"
    )
    if report.locked_accounts:
        print("锁定账号需要管理员在「用户管理」逐个重置密码后才能登录。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
