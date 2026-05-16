from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)

SCHEMA_VERSION = 0

GLOBAL_DOWNLOAD_STATUSES = ("queued", "active", "waiting", "paused", "completed", "failed", "cancelled")
USER_TASK_STATUSES = ("queued", "active", "waiting", "paused", "completed", "failed", "cancelled")
PACK_TASK_STATUSES = ("pending", "packing", "completed", "failed", "cancelled")
SHARE_STATUSES = ("active", "revoked")
RESOURCE_KINDS = ("http", "magnet", "torrent", "other")

metadata = MetaData()


def _in_check(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({quoted})"


schema_meta = Table(
    "schema_meta",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("version", Integer, nullable=False),
    Column("created_at_ms", Integer, nullable=False),
    CheckConstraint("id = 1", name="ck_schema_meta_single_row"),
    CheckConstraint("version = 0", name="ck_schema_meta_version_0"),
)

users = Table(
    "users",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("username", String(50), nullable=False, unique=True),
    Column("password_hash", Text, nullable=False),
    Column("is_admin", Integer, nullable=False, server_default="0"),
    Column("quota_bytes", Integer, nullable=False),
    Column("rpc_secret", String(128), unique=True),
    Column("rpc_secret_created_at_ms", Integer),
    Column("is_initial_password", Integer, nullable=False, server_default="0"),
    Column("created_at_ms", Integer, nullable=False),
    Column("updated_at_ms", Integer, nullable=False),
    CheckConstraint("is_admin IN (0, 1)", name="ck_users_is_admin_bool"),
    CheckConstraint("is_initial_password IN (0, 1)", name="ck_users_initial_password_bool"),
)

sessions = Table(
    "sessions",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("expires_at_ms", Integer, nullable=False),
    Column("created_at_ms", Integer, nullable=False),
    Index("ix_sessions_user_id", "user_id"),
    Index("ix_sessions_expires_at_ms", "expires_at_ms"),
)

api_tokens = Table(
    "api_tokens",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("token", String(128), nullable=False, unique=True),
    Column("name", String(200)),
    Column("created_at_ms", Integer, nullable=False),
    Column("last_used_at_ms", Integer),
    Index("ix_api_tokens_user_created", "user_id", "created_at_ms"),
)

app_settings = Table(
    "app_settings",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("max_task_size_bytes", Integer, nullable=False),
    Column("min_free_disk_bytes", Integer, nullable=False),
    Column("aria2_rpc_url", Text, nullable=False),
    Column("aria2_rpc_secret", Text),
    Column("hidden_file_extensions_json", Text, nullable=False),
    Column("pack_format", String(32), nullable=False),
    Column("pack_compression_level", Integer, nullable=False),
    Column("ws_reconnect_max_delay", Integer, nullable=False),
    Column("ws_reconnect_jitter", String(16), nullable=False),
    Column("ws_reconnect_factor", String(16), nullable=False),
    Column("site_title", String(50), nullable=False),
    Column("rate_limit_account_security", Integer, nullable=False),
    Column("rate_limit_authenticated_api", Integer, nullable=False),
    Column("rate_limit_public_api", Integer, nullable=False),
    Column("rate_limit_share_access", Integer, nullable=False),
    Column("rate_limit_authenticated_download", Integer, nullable=False),
    Column("rate_limit_anonymous_download", Integer, nullable=False),
    Column("rate_limit_create_task", Integer, nullable=False),
    Column("rate_limit_create_torrent", Integer, nullable=False),
    Column("rate_limit_create_pack", Integer, nullable=False),
    Column("rate_limit_aria2_test", Integer, nullable=False),
    Column("rate_limit_rpc", Integer, nullable=False),
    Column("download_total_connections", Integer, nullable=False),
    Column("download_authenticated_reserved_connections", Integer, nullable=False),
    Column("download_authenticated_per_user_connections", Integer, nullable=False),
    Column("download_authenticated_per_file_connections", Integer, nullable=False),
    Column("download_anonymous_base_connections", Integer, nullable=False),
    Column("download_anonymous_borrow_connections", Integer, nullable=False),
    Column("download_anonymous_per_ip_connections", Integer, nullable=False),
    Column("download_anonymous_per_file_connections", Integer, nullable=False),
    Column("created_at_ms", Integer, nullable=False),
    Column("updated_at_ms", Integer, nullable=False),
    CheckConstraint("id = 1", name="ck_app_settings_single_row"),
)

stored_files = Table(
    "stored_files",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("content_hash", String(128), nullable=False, unique=True),
    Column("real_path", Text, nullable=False, unique=True),
    Column("size_bytes", Integer, nullable=False),
    Column("is_directory", Integer, nullable=False, server_default="0"),
    Column("original_name", Text, nullable=False),
    Column("created_at_ms", Integer, nullable=False),
    CheckConstraint("is_directory IN (0, 1)", name="ck_stored_files_is_directory_bool"),
)

global_downloads = Table(
    "global_downloads",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("resource_key", String(128), nullable=False, unique=True),
    Column("resource_kind", String(16), nullable=False),
    Column("source_uri", Text, nullable=False),
    Column("display_name", Text),
    Column("aria2_gid", String(32), unique=True),
    Column("status", String(16), nullable=False),
    Column("total_bytes", Integer, nullable=False, server_default="0"),
    Column("completed_bytes", Integer, nullable=False, server_default="0"),
    Column("error_code", String(64)),
    Column("error_message", Text),
    Column("completed_file_id", Integer, ForeignKey("stored_files.id")),
    Column("created_at_ms", Integer, nullable=False),
    Column("updated_at_ms", Integer, nullable=False),
    Column("completed_at_ms", Integer),
    CheckConstraint(_in_check("resource_kind", RESOURCE_KINDS), name="ck_global_downloads_resource_kind"),
    CheckConstraint(_in_check("status", GLOBAL_DOWNLOAD_STATUSES), name="ck_global_downloads_status"),
    Index("ix_global_downloads_status_gid", "status", "aria2_gid"),
    Index("ix_global_downloads_completed_file_id", "completed_file_id"),
)

user_tasks = Table(
    "user_tasks",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("global_download_id", Integer, ForeignKey("global_downloads.id", ondelete="CASCADE"), nullable=False),
    Column("status", String(16), nullable=False),
    Column("reserved_bytes", Integer, nullable=False, server_default="0"),
    Column("display_name", Text),
    Column("error_message", Text),
    Column("created_at_ms", Integer, nullable=False),
    Column("updated_at_ms", Integer, nullable=False),
    Column("finished_at_ms", Integer),
    UniqueConstraint("user_id", "global_download_id", name="uq_user_tasks_user_download"),
    CheckConstraint(_in_check("status", USER_TASK_STATUSES), name="ck_user_tasks_status"),
    Index("ix_user_tasks_user_status_updated", "user_id", "status", "updated_at_ms"),
    Index("ix_user_tasks_download_status", "global_download_id", "status"),
    Index("ix_user_tasks_user_finished", "user_id", "finished_at_ms"),
)

stored_file_entries = Table(
    "stored_file_entries",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("stored_file_id", Integer, ForeignKey("stored_files.id", ondelete="CASCADE"), nullable=False),
    Column("relative_path", Text, nullable=False),
    Column("parent_path", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("size_bytes", Integer, nullable=False, server_default="0"),
    Column("is_dir", Integer, nullable=False, server_default="0"),
    Column("mtime_ms", Integer),
    Column("sort_key", Text),
    UniqueConstraint("stored_file_id", "relative_path", name="uq_stored_file_entries_path"),
    CheckConstraint("is_dir IN (0, 1)", name="ck_stored_file_entries_is_dir_bool"),
    Index("ix_stored_file_entries_parent", "stored_file_id", "parent_path"),
    Index("ix_stored_file_entries_dir_name", "stored_file_id", "is_dir", "name"),
)

user_files = Table(
    "user_files",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("stored_file_id", Integer, ForeignKey("stored_files.id", ondelete="CASCADE"), nullable=False),
    Column("display_name", Text, nullable=False),
    Column("created_at_ms", Integer, nullable=False),
    Column("updated_at_ms", Integer, nullable=False),
    UniqueConstraint("user_id", "stored_file_id", name="uq_user_files_user_stored"),
    Index("ix_user_files_user_created", "user_id", "created_at_ms"),
    Index("ix_user_files_stored_file_id", "stored_file_id"),
)

share_links = Table(
    "share_links",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("share_code", String(32), nullable=False, unique=True),
    Column("owner_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("user_file_id", Integer, ForeignKey("user_files.id", ondelete="CASCADE"), nullable=False),
    Column("password_hash", Text),
    Column("expires_at_ms", Integer),
    Column("max_downloads", Integer),
    Column("download_count", Integer, nullable=False, server_default="0"),
    Column("status", String(16), nullable=False),
    Column("created_at_ms", Integer, nullable=False),
    Column("last_accessed_at_ms", Integer),
    CheckConstraint(_in_check("status", SHARE_STATUSES), name="ck_share_links_status"),
    Index("ix_share_links_owner_status", "owner_id", "status"),
    Index("ix_share_links_file_status", "user_file_id", "status"),
)

pack_tasks = Table(
    "pack_tasks",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("source_user_file_ids_json", Text, nullable=False),
    Column("source_size_bytes", Integer, nullable=False),
    Column("reserved_bytes", Integer, nullable=False),
    Column("output_name", Text),
    Column("output_stored_file_id", Integer, ForeignKey("stored_files.id")),
    Column("delete_source", Integer, nullable=False, server_default="0"),
    Column("status", String(16), nullable=False),
    Column("progress", Integer, nullable=False, server_default="0"),
    Column("error_message", Text),
    Column("created_at_ms", Integer, nullable=False),
    Column("updated_at_ms", Integer, nullable=False),
    Column("finished_at_ms", Integer),
    CheckConstraint("delete_source IN (0, 1)", name="ck_pack_tasks_delete_source_bool"),
    CheckConstraint(_in_check("status", PACK_TASK_STATUSES), name="ck_pack_tasks_status"),
    Index("ix_pack_tasks_user_status_created", "user_id", "status", "created_at_ms"),
    Index("ix_pack_tasks_output_stored_file_id", "output_stored_file_id"),
)

user_storage_usage = Table(
    "user_storage_usage",
    metadata,
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column("used_bytes", Integer, nullable=False, server_default="0"),
    Column("reserved_bytes", Integer, nullable=False, server_default="0"),
    Column("updated_at_ms", Integer, nullable=False),
)
