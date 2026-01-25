"""Legacy database module for schema migration and admin credential management.

This module is retained for backward compatibility during the SQLModel migration.
New code should use `app.database` and `app.models` instead.

Kept functions:
- init_db(): Schema migration for existing databases (adds new columns)
- ensure_default_admin(): Admin user creation
"""

import sqlite3
import threading
from pathlib import Path
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterable

from app.core.config import settings
from app.core.security import hash_password


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
                artifact_path TEXT,
                artifact_token TEXT,
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
            ('pack_compression_level', '5'),
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
        except Exception:
            pass  # 列已存在

        # 为 users 表添加 RPC 访问字段（兼容旧数据库）
        cur.execute("PRAGMA table_info(users)")
        user_columns = [row[1] for row in cur.fetchall()]

        if "rpc_secret" not in user_columns:
            cur.execute("ALTER TABLE users ADD COLUMN rpc_secret VARCHAR(64) NULL")
            conn.commit()

        if "rpc_secret_created_at" not in user_columns:
            cur.execute("ALTER TABLE users ADD COLUMN rpc_secret_created_at TEXT NULL")
            conn.commit()

    finally:
        cur.close()
        conn.close()


def ensure_default_admin() -> None:
    """Ensure a default admin user exists, creating one if necessary.

    Uses password from settings.admin_password (env: ARIA2C_ADMIN_PASSWORD).
    Default password is '123456'.
    """
    existing = _fetch_one("SELECT id FROM users LIMIT 1")
    if existing:
        return

    # No users exist: create the first admin user
    _execute(
        """
        INSERT INTO users (username, password_hash, is_admin, created_at)
        VALUES (?, ?, ?, ?)
        """,
        ["admin", hash_password(settings.admin_password), 1, _utc_now()],
    )
