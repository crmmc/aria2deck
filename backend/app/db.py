"""Legacy database module for schema migration and admin credential management.

This module is retained for backward compatibility during the SQLModel migration.
New code should use `app.database` and `app.models` instead.

Kept functions:
- init_db(): Schema migration for existing databases (adds new columns)
- ensure_default_admin(): Admin user creation
"""

import logging

logger = logging.getLogger(__name__)

import logging
import hashlib
import sqlite3
import sys
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterable

from app.core.security import hash_password

from app.core.config import settings


logger = logging.getLogger(__name__)

DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD = "123456"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _derive_client_hash(password: str, username: str) -> str:
    """Derive client-side hash compatible with frontend hashPassword()."""
    salt = hashlib.sha256(username.lower().encode("utf-8")).digest()
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        10000,
    )
    return digest.hex()


def _get_connection() -> sqlite3.Connection:
    """Internal: Get a raw SQLite connection for legacy operations."""
    conn = sqlite3.connect(settings.database_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


_db_lock = threading.Lock()


@contextmanager
def _db_cursor():
    """Internal: Context manager for legacy database operations."""
    with _db_lock:
        conn = _get_connection()
        cur = conn.cursor()
        try:
            yield cur
            conn.commit()
        finally:
            cur.close()
            conn.close()


def _execute(query: str, params: Iterable | None = None) -> int:
    """Internal: Execute a query and return lastrowid."""
    with _db_cursor() as cur:
        cur.execute(query, params or [])
        return cur.lastrowid


def _fetch_one(query: str, params: Iterable | None = None) -> dict | None:
    """Internal: Fetch a single row as dict."""
    with _db_cursor() as cur:
        cur.execute(query, params or [])
        row = cur.fetchone()
        return dict(row) if row else None


def _fetch_all(query: str, params: Iterable | None = None) -> list[dict]:
    """Internal: Fetch all rows as list of dicts."""
    with _db_cursor() as cur:
        cur.execute(query, params or [])
        rows = cur.fetchall()
        return [dict(row) for row in rows]


# Public aliases for backward compatibility
# These are kept for code that still uses synchronous database access
execute = _execute
fetch_one = _fetch_one
fetch_all = _fetch_all
utc_now = _utc_now


def init_db() -> None:
    """Initialize database schema and perform migrations for existing databases.

    This function handles:
    - Creating tables if they don't exist
    - Adding new columns to existing tables (schema migration)
    - Initializing default config values

    Note: For new tables and columns, prefer using Alembic migrations.
    This function is kept for backward compatibility with existing deployments.
    """
    conn = _get_connection()
    cur = conn.cursor()

    try:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                is_admin INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                quota INTEGER DEFAULT 107374182400
            )
            """
        )
        conn.commit()

        # 为已存在的表添加 quota 字段（如果不存在）
        cur.execute("PRAGMA table_info(users)")
        columns = [row[1] for row in cur.fetchall()]

        if "quota" not in columns:
            cur.execute("ALTER TABLE users ADD COLUMN quota INTEGER DEFAULT 107374182400")
            cur.execute("UPDATE users SET quota = 107374182400 WHERE quota IS NULL")
            conn.commit()

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                expires_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
            """
        )
        conn.commit()

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER NOT NULL,
                gid TEXT,
                uri TEXT NOT NULL,
                status TEXT NOT NULL,
                name TEXT,
                total_length INTEGER DEFAULT 0,
                completed_length INTEGER DEFAULT 0,
                download_speed INTEGER DEFAULT 0,
                upload_speed INTEGER DEFAULT 0,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,

                peak_download_speed INTEGER DEFAULT 0,
                peak_connections INTEGER DEFAULT 0,
                FOREIGN KEY(owner_id) REFERENCES users(id)
            )
            """
        )
        conn.commit()

        # 为已存在的 tasks 表添加峰值字段（如果不存在）
        cur.execute("PRAGMA table_info(tasks)")
        task_columns = [row[1] for row in cur.fetchall()]

        if "peak_download_speed" not in task_columns:
            cur.execute("ALTER TABLE tasks ADD COLUMN peak_download_speed INTEGER DEFAULT 0")
            conn.commit()

        if "peak_connections" not in task_columns:
            cur.execute("ALTER TABLE tasks ADD COLUMN peak_connections INTEGER DEFAULT 0")
            conn.commit()

        # 系统配置表
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        conn.commit()

        # 初始化默认配置
        cur.execute(
            """
            INSERT OR IGNORE INTO config (key, value) VALUES
            ('max_task_size', '10737418240'),
            ('min_free_disk', '1073741824'),
            ('pack_format', 'zip'),
            ('pack_7z_method', 'lzma2'),
            ('pack_compression_level', '5'),
            ('pack_memory_limit', '128'),
            ('pack_extra_args', '')
            """
        )
        conn.commit()

        # 打包任务表
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS pack_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER NOT NULL,
                folder_path TEXT NOT NULL,
                folder_size INTEGER NOT NULL,
                reserved_space INTEGER NOT NULL,
                output_path TEXT,
                output_name TEXT,
                output_size INTEGER,
                status TEXT NOT NULL DEFAULT 'pending',
                progress INTEGER DEFAULT 0,
                error_message TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(owner_id) REFERENCES users(id)
            )
            """
        )
        conn.commit()

        # 添加 output_name 列（兼容旧数据库）
        try:
            cur.execute("ALTER TABLE pack_tasks ADD COLUMN output_name TEXT")
            conn.commit()
        except sqlite3.OperationalError:
            # 列已存在
            logger.debug("Column output_name already exists in pack_tasks")
            pass

        # 添加 stored_file_id 和 delete_source 列（兼容旧数据库）
        try:
            cur.execute("ALTER TABLE pack_tasks ADD COLUMN stored_file_id INTEGER")
            conn.commit()
        except sqlite3.OperationalError:
            # 列已存在
            logger.debug("Column stored_file_id already exists in pack_tasks")
            pass

        try:
            cur.execute("ALTER TABLE pack_tasks ADD COLUMN delete_source INTEGER DEFAULT 0")
            conn.commit()
        except sqlite3.OperationalError:
            # 列已存在
            logger.debug("Column delete_source already exists in pack_tasks")
            pass

        # 为 users 表添加 RPC 访问字段（兼容旧数据库）
        cur.execute("PRAGMA table_info(users)")
        user_columns = [row[1] for row in cur.fetchall()]

        if "rpc_secret" not in user_columns:
            cur.execute("ALTER TABLE users ADD COLUMN rpc_secret VARCHAR(64) NULL")
            conn.commit()

        if "rpc_secret_created_at" not in user_columns:
            cur.execute("ALTER TABLE users ADD COLUMN rpc_secret_created_at TEXT NULL")
            conn.commit()

        # 为 users 表添加 is_initial_password 字段
        cur.execute("PRAGMA table_info(users)")
        user_columns_updated = [row[1] for row in cur.fetchall()]

        if "is_initial_password" not in user_columns_updated:
            cur.execute("ALTER TABLE users ADD COLUMN is_initial_password INTEGER DEFAULT 0")
            conn.commit()

        # API Tokens 表
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS api_tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                token TEXT NOT NULL UNIQUE,
                name TEXT,
                created_at TEXT NOT NULL,
                last_used_at TEXT,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
            """
        )
        conn.commit()

    finally:
        cur.close()
        conn.close()


def ensure_default_admin() -> None:
    """Ensure a default admin user exists, creating one if necessary.

    Creates admin with default password '123456' and is_initial_password=1.
    User should reset password on first login.
    """
    existing = _fetch_one("SELECT id FROM users LIMIT 1")
    if existing:
        return

    # No users exist: create the first admin user with default password
    default_client_hash = _derive_client_hash(DEFAULT_ADMIN_PASSWORD, DEFAULT_ADMIN_USERNAME)
    default_password_hash = hash_password(default_client_hash)
    _execute(
        """
        INSERT INTO users (username, password_hash, is_admin, created_at, is_initial_password)
        VALUES (?, ?, ?, ?, ?)
        """,
        [DEFAULT_ADMIN_USERNAME, default_password_hash, 1, _utc_now(), 1],
    )


def reset_admin_password_for_dev() -> bool:
    """Reset admin password to default for local development.

    This only updates password_hash and is_initial_password,
    keeping all other user data and system config unchanged.
    """
    admin = _fetch_one(
        "SELECT id, username FROM users WHERE username = ? LIMIT 1",
        [DEFAULT_ADMIN_USERNAME],
    )
    if not admin:
        admin = _fetch_one(
            "SELECT id, username FROM users WHERE is_admin = 1 ORDER BY id LIMIT 1"
        )
        if not admin:
            logger.warning("开发模式密码重置失败：未找到管理员账号")
            return False
        logger.warning(
            "开发模式未找到 admin 用户，改为重置首个管理员密码 username=%s user_id=%s",
            admin["username"],
            admin["id"],
        )

    target_username = str(admin["username"])
    default_client_hash = _derive_client_hash(DEFAULT_ADMIN_PASSWORD, target_username)
    default_password_hash = hash_password(default_client_hash)
    _execute(
        "UPDATE users SET password_hash = ?, is_initial_password = 1 WHERE id = ?",
        [default_password_hash, admin["id"]],
    )
    logger.info(
        "开发模式已重置管理员密码 username=%s user_id=%s",
        admin["username"],
        admin["id"],
    )
    return True


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        print("Usage: python -m app.db reset-admin-password")
        return 1

    command = args[0]
    if command == "reset-admin-password":
        ok = reset_admin_password_for_dev()
        return 0 if ok else 2

    print(f"Unknown command: {command}")
    print("Usage: python -m app.db reset-admin-password")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
