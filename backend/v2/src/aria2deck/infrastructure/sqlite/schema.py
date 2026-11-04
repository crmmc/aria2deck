from __future__ import annotations

from typing import cast

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects import sqlite
from sqlalchemy.engine import Dialect
from sqlalchemy.schema import CreateIndex, CreateTable

SCHEMA_VERSION = 1
metadata = MetaData()

schema_meta = Table(
    "schema_meta",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("schema_version", Integer, nullable=False),
    Column("created_at_ms", BigInteger, nullable=False),
    CheckConstraint("id = 1", name="ck_schema_meta_singleton"),
)

users = Table(
    "users",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("username", Text, nullable=False),
    Column("username_key", Text, nullable=False),
    Column("password_hash", Text, nullable=False),
    Column("is_admin", Boolean, nullable=False, default=False),
    Column("quota_bytes", BigInteger, nullable=False),
    Column("is_initial_password", Boolean, nullable=False, default=False),
    Column("rpc_secret_ciphertext", Text),
    Column("rpc_secret_prefix", Text),
    Column("credential_generation", Integer, nullable=False, default=1),
    Column("pending_delete", Boolean, nullable=False, default=False),
    Column("delete_lease_owner", Text),
    Column("delete_lease_expires_at_ms", BigInteger),
    Column("delete_retry_count", Integer, nullable=False, default=0),
    Column("created_at_ms", BigInteger, nullable=False),
    Column("updated_at_ms", BigInteger, nullable=False),
    UniqueConstraint("username_key", name="uq_users_username_key"),
    CheckConstraint("quota_bytes >= 0", name="ck_users_quota_nonnegative"),
    CheckConstraint("credential_generation >= 1", name="ck_users_credential_generation"),
    CheckConstraint("delete_retry_count >= 0", name="ck_users_delete_retry"),
    CheckConstraint(
        "(rpc_secret_ciphertext IS NULL) = (rpc_secret_prefix IS NULL)",
        name="ck_users_rpc_secret_pair",
    ),
    CheckConstraint(
        "(delete_lease_owner IS NULL) = (delete_lease_expires_at_ms IS NULL)",
        name="ck_users_delete_lease_pair",
    ),
)

sessions = Table(
    "sessions",
    metadata,
    Column("id", Text, primary_key=True),
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("credential_generation", Integer, nullable=False),
    Column("created_at_ms", BigInteger, nullable=False),
    Column("expires_at_ms", BigInteger, nullable=False),
    Column("last_seen_at_ms", BigInteger),
    Column("revoked_at_ms", BigInteger),
    CheckConstraint("expires_at_ms > created_at_ms", name="ck_sessions_expiry"),
)

api_tokens = Table(
    "api_tokens",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("name", Text, nullable=False),
    Column("token_digest", Text, nullable=False),
    Column("prefix", Text, nullable=False),
    Column("created_at_ms", BigInteger, nullable=False),
    Column("last_used_at_ms", BigInteger),
    UniqueConstraint("token_digest", name="uq_api_tokens_digest"),
)

app_settings = Table(
    "app_settings",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("site_title", Text, nullable=False, default="Aria2Deck"),
    Column("max_task_size", BigInteger, nullable=False, default=100 * 1024**3),
    Column("min_free_disk", BigInteger, nullable=False, default=1024**3),
    Column("aria2_rpc_url", Text, nullable=False, default="http://127.0.0.1:6800/jsonrpc"),
    Column("aria2_rpc_secret_ciphertext", Text),
    Column("aria2_bt_stop_timeout_seconds", Integer, nullable=False, default=300),
    Column("hidden_file_extensions_json", Text, nullable=False, default="[]"),
    Column("pack_format", Text, nullable=False, default="zip"),
    Column("pack_compression_level", Integer, nullable=False, default=6),
    Column("ws_reconnect_max_delay_seconds", Integer, nullable=False, default=30),
    Column("ws_reconnect_jitter_seconds", Integer, nullable=False, default=1),
    Column("ws_reconnect_factor", Integer, nullable=False, default=2),
    Column("history_retention_days", Integer, nullable=False, default=30),
    Column("tracker_fixed_list_json", Text, nullable=False, default="[]"),
    Column("tracker_remote_urls_json", Text, nullable=False, default="[]"),
    Column("tracker_refresh_interval_minutes", Integer, nullable=False, default=0),
    Column("download_connection_limits_json", Text, nullable=False, default="{}"),
    Column("account_security_rate_limit", Integer, nullable=False, default=10),
    Column("authenticated_api_rate_limit", Integer, nullable=False, default=600),
    Column("public_api_rate_limit", Integer, nullable=False, default=300),
    Column("share_access_rate_limit", Integer, nullable=False, default=30),
    Column("create_task_rate_limit", Integer, nullable=False, default=60),
    Column("create_torrent_rate_limit", Integer, nullable=False, default=30),
    Column("create_pack_rate_limit", Integer, nullable=False, default=30),
    Column("aria2_test_rate_limit", Integer, nullable=False, default=10),
    Column("rpc_rate_limit", Integer, nullable=False, default=600),
    Column("file_search_rate_limit", Integer, nullable=False, default=120),
    Column("create_share_rate_limit", Integer, nullable=False, default=60),
    Column("updated_at_ms", BigInteger, nullable=False, default=0),
    CheckConstraint("id = 1", name="ck_app_settings_singleton"),
    CheckConstraint("max_task_size >= 0", name="ck_app_settings_task_size"),
    CheckConstraint("min_free_disk >= 0", name="ck_app_settings_free_disk"),
    CheckConstraint("pack_format IN ('zip', 'tar.zst')", name="ck_app_settings_pack_format"),
    CheckConstraint("pack_compression_level BETWEEN 0 AND 22", name="ck_app_settings_pack_level"),
    CheckConstraint("history_retention_days >= 1", name="ck_app_settings_history"),
    CheckConstraint(
        "tracker_refresh_interval_minutes = 0 OR tracker_refresh_interval_minutes >= 5",
        name="ck_app_settings_tracker_interval",
    ),
)

download_sources = Table(
    "download_sources",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("kind", Text, nullable=False),
    Column("resource_key", Text, nullable=False),
    Column("payload_json", Text, nullable=False),
    Column("options_json", Text, nullable=False),
    Column("selected_file_indexes_json", Text),
    Column("display_name", Text, nullable=False),
    Column("display_uri", Text),
    Column("created_at_ms", BigInteger, nullable=False),
    Column("detached_at_ms", BigInteger),
    UniqueConstraint("resource_key", name="uq_download_sources_resource"),
    UniqueConstraint("id", "resource_key", name="uq_download_sources_identity"),
    CheckConstraint("kind IN ('http', 'magnet', 'torrent')", name="ck_download_sources_kind"),
)

download_attempts = Table(
    "download_attempts",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("source_id", Integer, nullable=False),
    Column("resource_key", Text, nullable=False),
    Column("gid", Text, nullable=False),
    Column("status", Text, nullable=False, default="queued"),
    Column(
        "completed_content_id",
        Integer,
        ForeignKey("content_objects.id", ondelete="SET NULL"),
    ),
    Column("submission_state", Text, nullable=False, default="planned"),
    Column("total_length", BigInteger),
    Column("completed_length", BigInteger, nullable=False, default=0),
    Column("physical_reserved_bytes", BigInteger, nullable=False, default=0),
    Column("error_code", Text),
    Column("created_at_ms", BigInteger, nullable=False),
    Column("updated_at_ms", BigInteger, nullable=False),
    Column("finished_at_ms", BigInteger),
    Column("result_consumed_at_ms", BigInteger),
    ForeignKeyConstraint(
        ["source_id", "resource_key"],
        ["download_sources.id", "download_sources.resource_key"],
        ondelete="RESTRICT",
        name="fk_download_attempts_source_identity",
    ),
    UniqueConstraint("gid", name="uq_download_attempts_gid"),
    CheckConstraint(
        "status IN ('queued', 'active', 'waiting', 'paused', 'completed', 'failed', 'cancelled')",
        name="ck_download_attempts_status",
    ),
    CheckConstraint(
        "submission_state IN ('planned', 'submitted', 'uncertain')",
        name="ck_download_attempts_submission",
    ),
    CheckConstraint("total_length IS NULL OR total_length >= 0", name="ck_attempt_total"),
    CheckConstraint("completed_length >= 0", name="ck_attempt_completed"),
    CheckConstraint(
        "total_length IS NULL OR completed_length <= total_length",
        name="ck_attempt_progress",
    ),
    CheckConstraint("physical_reserved_bytes >= 0", name="ck_attempt_physical_reserved"),
)

task_subscriptions = Table(
    "task_subscriptions",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("attempt_id", Integer, ForeignKey("download_attempts.id", ondelete="RESTRICT"), nullable=False),
    Column("user_id", Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
    Column("status", Text, nullable=False, default="queued"),
    Column("reserved_bytes", BigInteger, nullable=False, default=0),
    Column("display_name", Text, nullable=False),
    Column("display_uri", Text),
    Column("error_code", Text),
    Column("created_at_ms", BigInteger, nullable=False),
    Column("updated_at_ms", BigInteger, nullable=False),
    Column("finished_at_ms", BigInteger),
    Column("expired_at_ms", BigInteger),
    CheckConstraint(
        "status IN ('queued', 'active', 'waiting', 'paused', 'completed', 'failed', 'cancelled', 'expired')",
        name="ck_task_subscriptions_status",
    ),
    CheckConstraint("reserved_bytes >= 0", name="ck_task_subscriptions_reserved"),
)

content_objects = Table(
    "content_objects",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("content_key", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("digest", Text, nullable=False),
    Column("size", BigInteger, nullable=False),
    Column("storage_path", Text, nullable=False),
    Column("deletion_state", Text, nullable=False, default="active"),
    Column("read_leases", Integer, nullable=False, default=0),
    Column("delete_lease_owner", Text),
    Column("delete_lease_expires_at_ms", BigInteger),
    Column("delete_retry_count", Integer, nullable=False, default=0),
    Column("next_delete_attempt_at_ms", BigInteger),
    Column("tombstone_path", Text),
    Column("created_at_ms", BigInteger, nullable=False),
    Column("updated_at_ms", BigInteger, nullable=False),
    Column("deleted_at_ms", BigInteger),
    UniqueConstraint("content_key", name="uq_content_objects_key"),
    UniqueConstraint("storage_path", name="uq_content_objects_path"),
    CheckConstraint("kind IN ('file', 'directory')", name="ck_content_objects_kind"),
    CheckConstraint("length(digest) = 64 AND digest NOT GLOB '*[^0-9a-f]*'", name="ck_content_digest"),
    CheckConstraint("size >= 0", name="ck_content_size"),
    CheckConstraint("storage_path NOT LIKE '/%'", name="ck_content_relative_path"),
    CheckConstraint(
        "deletion_state IN ('active', 'pending_delete', 'tombstoned')",
        name="ck_content_deletion_state",
    ),
    CheckConstraint("read_leases >= 0", name="ck_content_read_leases"),
    CheckConstraint("delete_retry_count >= 0", name="ck_content_delete_retry"),
    CheckConstraint(
        "(delete_lease_owner IS NULL) = (delete_lease_expires_at_ms IS NULL)",
        name="ck_content_delete_lease_pair",
    ),
    CheckConstraint(
        "(deletion_state = 'tombstoned') = (tombstone_path IS NOT NULL)",
        name="ck_content_tombstone",
    ),
)

content_entries = Table(
    "content_entries",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("content_id", Integer, ForeignKey("content_objects.id", ondelete="CASCADE"), nullable=False),
    Column("parent_path", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("path", Text, nullable=False),
    Column("entry_kind", Text, nullable=False),
    Column("size", BigInteger, nullable=False),
    Column("modified_at_ms", BigInteger),
    UniqueConstraint("content_id", "path", name="uq_content_entries_path"),
    UniqueConstraint("content_id", "parent_path", "name", name="uq_content_entries_sibling"),
    CheckConstraint("entry_kind IN ('file', 'directory')", name="ck_content_entries_kind"),
    CheckConstraint("size >= 0", name="ck_content_entries_size"),
)

user_files = Table(
    "user_files",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("user_id", Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
    Column("content_id", Integer, ForeignKey("content_objects.id", ondelete="RESTRICT"), nullable=False),
    Column("display_name", Text, nullable=False),
    Column("created_at_ms", BigInteger, nullable=False),
    UniqueConstraint("user_id", "content_id", name="uq_user_files_user_content"),
    UniqueConstraint("user_id", "display_name", name="uq_user_files_user_name"),
)

share_links = Table(
    "share_links",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("code", Text, nullable=False),
    Column("owner_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("user_file_id", Integer, ForeignKey("user_files.id", ondelete="SET NULL")),
    Column("file_name", Text, nullable=False),
    Column("file_size", BigInteger, nullable=False),
    Column("file_kind", Text, nullable=False),
    Column("content_key", Text, nullable=False),
    Column("password_hash", Text),
    Column("expires_at_ms", BigInteger),
    Column("max_downloads", Integer),
    Column("download_count", Integer, nullable=False, default=0),
    Column("revoked_at_ms", BigInteger),
    Column("created_at_ms", BigInteger, nullable=False),
    Column("last_accessed_at_ms", BigInteger),
    UniqueConstraint("code", name="uq_share_links_code"),
    CheckConstraint("file_size >= 0", name="ck_share_file_size"),
    CheckConstraint("file_kind IN ('file', 'directory')", name="ck_share_file_kind"),
    CheckConstraint("max_downloads IS NULL OR max_downloads BETWEEN 1 AND 10000", name="ck_share_limit"),
    CheckConstraint("download_count >= 0", name="ck_share_count"),
    CheckConstraint(
        "max_downloads IS NULL OR download_count <= max_downloads",
        name="ck_share_count_limit",
    ),
)

pack_jobs = Table(
    "pack_jobs",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("owner_id", Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
    Column("status", Text, nullable=False, default="pending"),
    Column("step", Text),
    Column("step_progress", Integer, nullable=False, default=0),
    Column("step_started_at_ms", BigInteger),
    Column("pack_format", Text, nullable=False),
    Column("output_name", Text, nullable=False),
    Column("source_size", BigInteger, nullable=False),
    Column("reserved_bytes", BigInteger, nullable=False),
    Column("physical_reserved_bytes", BigInteger, nullable=False),
    Column("prepared_path", Text),
    Column("prepared_size", BigInteger),
    Column("prepared_digest", Text),
    Column("output_content_id", Integer, ForeignKey("content_objects.id", ondelete="RESTRICT")),
    Column("delete_source", Boolean, nullable=False, default=False),
    Column("cleanup_pending", Boolean, nullable=False, default=False),
    Column("retry_count", Integer, nullable=False, default=0),
    Column("next_attempt_at_ms", BigInteger),
    Column("error_code", Text),
    Column("created_at_ms", BigInteger, nullable=False),
    Column("updated_at_ms", BigInteger, nullable=False),
    Column("finished_at_ms", BigInteger),
    CheckConstraint("status IN ('pending', 'packing', 'done', 'failed', 'cancelled')", name="ck_pack_status"),
    CheckConstraint("step IS NULL OR step IN ('validating', 'compressing', 'verifying')", name="ck_pack_step"),
    CheckConstraint("step_progress BETWEEN 0 AND 100", name="ck_pack_step_progress"),
    CheckConstraint("pack_format IN ('zip', 'tar.zst')", name="ck_pack_format"),
    CheckConstraint("source_size >= 0", name="ck_pack_source_size"),
    CheckConstraint("reserved_bytes >= 0", name="ck_pack_reserved"),
    CheckConstraint("physical_reserved_bytes >= 0", name="ck_pack_physical_reserved"),
    CheckConstraint("retry_count >= 0", name="ck_pack_retry"),
    CheckConstraint(
        "(prepared_path IS NULL AND prepared_size IS NULL AND prepared_digest IS NULL) "
        "OR (prepared_path IS NOT NULL AND prepared_size IS NOT NULL AND prepared_digest IS NOT NULL)",
        name="ck_pack_prepared_pair",
    ),
    CheckConstraint("status <> 'done' OR output_content_id IS NOT NULL", name="ck_pack_done_output"),
)

pack_sources = Table(
    "pack_sources",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("pack_id", Integer, ForeignKey("pack_jobs.id", ondelete="CASCADE"), nullable=False),
    Column("source_user_file_id", Integer, nullable=False),
    Column("source_content_id", Integer, nullable=False),
    Column("content_key", Text, nullable=False),
    Column("display_name", Text, nullable=False),
    Column("source_created_at_ms", BigInteger, nullable=False),
    Column("source_size", BigInteger, nullable=False),
    Column("cleanup_status", Text, nullable=False, default="pending"),
    Column("cleanup_error_code", Text),
    UniqueConstraint("pack_id", "source_user_file_id", name="uq_pack_sources_snapshot"),
    CheckConstraint("source_size >= 0", name="ck_pack_sources_size"),
    CheckConstraint("cleanup_status IN ('pending', 'deleted', 'kept')", name="ck_pack_source_cleanup"),
)

quota_accounts = Table(
    "quota_accounts",
    metadata,
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column("used_bytes", BigInteger, nullable=False, default=0),
    Column("reserved_bytes", BigInteger, nullable=False, default=0),
    Column("revision", Integer, nullable=False, default=0),
    Column("updated_at_ms", BigInteger, nullable=False, default=0),
    CheckConstraint("used_bytes >= 0", name="ck_quota_used"),
    CheckConstraint("reserved_bytes >= 0", name="ck_quota_reserved"),
    CheckConstraint("revision >= 0", name="ck_quota_revision"),
)

tracker_cache = Table(
    "tracker_cache",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("merged_json", Text, nullable=False, default="[]"),
    Column("remote_json", Text, nullable=False, default="[]"),
    Column("refresh_status", Text, nullable=False, default="never"),
    Column("failed_urls_json", Text, nullable=False, default="[]"),
    Column("updated_at_ms", BigInteger),
    Column("last_refresh_at_ms", BigInteger),
    CheckConstraint("id = 1", name="ck_tracker_cache_singleton"),
    CheckConstraint(
        "refresh_status IN ('never', 'refreshing', 'success', 'partial', 'failed')",
        name="ck_tracker_cache_status",
    ),
)

Index("ix_sessions_user", sessions.c.user_id)
Index("ix_sessions_expiry", sessions.c.expires_at_ms)
Index("ix_api_tokens_user", api_tokens.c.user_id)
Index("ix_download_sources_detached", download_sources.c.detached_at_ms)
Index("ix_download_attempts_status", download_attempts.c.status, download_attempts.c.updated_at_ms)
Index(
    "uq_download_attempts_live_resource",
    download_attempts.c.resource_key,
    unique=True,
    sqlite_where=download_attempts.c.status.in_(("queued", "active", "waiting", "paused")),
)
Index(
    "ix_task_subscriptions_user_status",
    task_subscriptions.c.user_id,
    task_subscriptions.c.status,
    task_subscriptions.c.updated_at_ms,
)
Index("ix_task_subscriptions_attempt", task_subscriptions.c.attempt_id, task_subscriptions.c.status)
Index(
    "uq_task_subscriptions_live_user_attempt",
    task_subscriptions.c.user_id,
    task_subscriptions.c.attempt_id,
    unique=True,
    sqlite_where=task_subscriptions.c.status.in_(("queued", "active", "waiting", "paused")),
)
Index(
    "ix_content_entries_browse",
    content_entries.c.content_id,
    content_entries.c.parent_path,
    content_entries.c.entry_kind,
    content_entries.c.name,
)
Index("ix_user_files_user", user_files.c.user_id, user_files.c.created_at_ms)
Index("ix_user_files_content", user_files.c.content_id)
Index(
    "ix_share_links_active_file",
    share_links.c.user_file_id,
    share_links.c.revoked_at_ms,
    sqlite_where=share_links.c.revoked_at_ms.is_(None),
)
Index(
    "ix_content_objects_pending_delete",
    content_objects.c.next_delete_attempt_at_ms,
    sqlite_where=content_objects.c.deletion_state == "pending_delete",
)
Index(
    "ix_pack_jobs_dispatch",
    pack_jobs.c.status,
    pack_jobs.c.next_attempt_at_ms,
    sqlite_where=pack_jobs.c.status.in_(("pending", "packing")),
)
Index("ix_pack_sources_cleanup", pack_sources.c.cleanup_status, pack_sources.c.pack_id)

TRIGGER_STATEMENTS = (
    """
    CREATE TRIGGER trg_share_links_count_monotonic
    BEFORE UPDATE OF download_count ON share_links
    WHEN NEW.download_count < OLD.download_count
       OR (NEW.max_downloads IS NOT NULL AND NEW.download_count > NEW.max_downloads)
    BEGIN
        SELECT RAISE(ABORT, 'share download count invariant');
    END
    """.strip(),
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
    """.strip(),
    """
    CREATE TRIGGER trg_user_files_identity_immutable
    BEFORE UPDATE OF user_id, content_id ON user_files
    WHEN NEW.user_id <> OLD.user_id OR NEW.content_id <> OLD.content_id
    BEGIN
        SELECT RAISE(ABORT, 'user file identity is immutable');
    END
    """.strip(),
    """
    CREATE TRIGGER trg_quota_accounts_nonnegative
    BEFORE UPDATE OF used_bytes, reserved_bytes ON quota_accounts
    WHEN NEW.used_bytes < 0 OR NEW.reserved_bytes < 0
    BEGIN
        SELECT RAISE(ABORT, 'quota account cannot be negative');
    END
    """.strip(),
)

TARGET_TABLES = tuple(sorted(metadata.tables))
_DIALECT = cast(type[Dialect], sqlite.dialect)()
DDL_STATEMENTS = tuple(
    [str(CreateTable(table).compile(dialect=_DIALECT)).strip() for table in metadata.sorted_tables]
    + [
        str(CreateIndex(index).compile(dialect=_DIALECT)).strip()
        for index in sorted(
            (index for table in metadata.tables.values() for index in table.indexes),
            key=lambda item: str(item.name),
        )
    ]
    + list(TRIGGER_STATEMENTS)
)
