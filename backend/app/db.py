import os
import random
import sqlite3
import string
import threading
from pathlib import Path
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterable

from app.core.config import settings
from app.core.security import hash_password, verify_password


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.database_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


_db_lock = threading.Lock()


@contextmanager
def db_cursor():
    with _db_lock:
        conn = get_connection()
        cur = conn.cursor()
        try:
            yield cur
            conn.commit()
        finally:
            cur.close()
            conn.close()


def init_db() -> None:
    conn = get_connection()
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
        # 检查 quota 列是否存在
        cur.execute("PRAGMA table_info(users)")
        columns = [row[1] for row in cur.fetchall()]
        
        if "quota" not in columns:
            # quota 字段不存在，添加它（默认 100GB）
            cur.execute("ALTER TABLE users ADD COLUMN quota INTEGER DEFAULT 107374182400")
            # 更新所有现有用户的配额
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


def ensure_default_admin() -> str | None:
    existing = fetch_one("SELECT id FROM users LIMIT 1")
    data_dir = Path(settings.database_path).parent
    data_dir.mkdir(parents=True, exist_ok=True)
    credential_path = data_dir / "admin_credentials.txt"

    if existing:
        # Keep the admin credential file in sync with the stored password.
        admin = fetch_one("SELECT id, password_hash FROM users WHERE username = ?", ["admin"])
        if not admin:
            return None
        if credential_path.exists():
            content = credential_path.read_text(encoding="utf-8")
            for line in content.splitlines():
                if line.startswith("password:"):
                    password = line.split("password:", 1)[1].strip()
                    if password and not verify_password(password, admin["password_hash"]):
                        execute(
                            "UPDATE users SET password_hash = ? WHERE id = ?",
                            [hash_password(password), admin["id"]],
                        )
                    return None
        # Credentials file missing: generate a new admin password and persist it.
        password_chars = [
            random.SystemRandom().choice(string.ascii_lowercase),
            random.SystemRandom().choice(string.ascii_uppercase),
            random.SystemRandom().choice(string.digits),
        ]
        password_chars.extend(
            random.SystemRandom().choice(string.ascii_letters + string.digits)
            for _ in range(18 - len(password_chars))
        )
        random.SystemRandom().shuffle(password_chars)
        password = "".join(password_chars)
        execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            [hash_password(password), admin["id"]],
        )
        credential_path.write_text(
            f"username: admin\npassword: {password}\n", encoding="utf-8"
        )
        try:
            os.chmod(credential_path, 0o600)
        except OSError:
            pass
        return password
    password_chars = [
        random.SystemRandom().choice(string.ascii_lowercase),
        random.SystemRandom().choice(string.ascii_uppercase),
        random.SystemRandom().choice(string.digits),
    ]
    password_chars.extend(
        random.SystemRandom().choice(string.ascii_letters + string.digits)
        for _ in range(18 - len(password_chars))
    )
    random.SystemRandom().shuffle(password_chars)
    password = "".join(password_chars)
    execute(
        """
        INSERT INTO users (username, password_hash, is_admin, created_at)
        VALUES (?, ?, ?, ?)
        """,
        ["admin", hash_password(password), 1, utc_now()],
    )
    data_dir = Path(settings.database_path).parent
    data_dir.mkdir(parents=True, exist_ok=True)
    credential_path = data_dir / "admin_credentials.txt"
    credential_path.write_text(
        f"username: admin\npassword: {password}\n", encoding="utf-8")
    try:
        os.chmod(credential_path, 0o600)
    except OSError:
        pass
    return password


def execute(query: str, params: Iterable | None = None) -> int:
    with db_cursor() as cur:
        cur.execute(query, params or [])
        return cur.lastrowid


def fetch_one(query: str, params: Iterable | None = None) -> dict | None:
    with db_cursor() as cur:
        cur.execute(query, params or [])
        row = cur.fetchone()
        return dict(row) if row else None


def fetch_all(query: str, params: Iterable | None = None) -> list[dict]:
    with db_cursor() as cur:
        cur.execute(query, params or [])
        rows = cur.fetchall()
        return [dict(row) for row in rows]
