from __future__ import annotations

import logging
import os
import shutil
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings

logger = logging.getLogger(__name__)
CREDENTIAL_SCRUB_MARKER_SUFFIX = ".v6-credential-scrub"

_engine: AsyncEngine | None = None
_session_maker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            f"sqlite+aiosqlite:///{settings.database_path}",
            echo=settings.debug,
            connect_args={"check_same_thread": False, "timeout": 30.0},
            pool_pre_ping=True,
        )

        @event.listens_for(_engine.sync_engine, "connect")
        def _apply_connection_pragmas(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA secure_delete=ON")
                cursor.execute("PRAGMA synchronous=NORMAL")
            finally:
                cursor.close()

    return _engine


def get_session_maker() -> async_sessionmaker[AsyncSession]:
    global _session_maker
    if _session_maker is None:
        _session_maker = async_sessionmaker(
            get_engine(), class_=AsyncSession, expire_on_commit=False
        )
    return _session_maker


@asynccontextmanager
async def session_scope() -> AsyncGenerator[AsyncSession, None]:
    session_maker = get_session_maker()
    async with session_maker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def transaction() -> AsyncGenerator[AsyncConnection, None]:
    async with get_engine().begin() as conn:
        await conn.execute(text("PRAGMA foreign_keys=ON"))
        await conn.execute(text("PRAGMA secure_delete=ON"))
        yield conn


async def apply_sqlite_pragmas() -> None:
    async with get_engine().begin() as conn:
        await conn.execute(text("PRAGMA journal_mode=WAL"))
        await conn.execute(text("PRAGMA synchronous=NORMAL"))
        await conn.execute(text("PRAGMA foreign_keys=ON"))
        await conn.execute(text("PRAGMA secure_delete=ON"))


def credential_scrub_marker_path() -> Path:
    database = Path(settings.database_path)
    return database.with_name(database.name + CREDENTIAL_SCRUB_MARKER_SUFFIX)


def mark_credential_scrub_pending() -> None:
    marker = credential_scrub_marker_path()
    marker.parent.mkdir(parents=True, exist_ok=True)
    with marker.open("w", encoding="utf-8") as handle:
        handle.write("v6 credential scrub pending\n")
        handle.flush()
        os.fsync(handle.fileno())


def clear_credential_scrub_marker() -> None:
    credential_scrub_marker_path().unlink(missing_ok=True)


async def scrub_legacy_credential_pages() -> bool:
    database = Path(settings.database_path)
    try:
        async with get_engine().connect() as conn:
            checkpoint = (
                await conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
            ).first()
            if checkpoint is None or int(checkpoint[0]) != 0:
                logger.warning("v6 凭证清理等待 WAL checkpoint 可用")
                return False
            await conn.commit()
            required_free_bytes = max(1, database.stat().st_size)
            available_bytes = shutil.disk_usage(database.parent).free
            if available_bytes < required_free_bytes:
                logger.warning("v6 凭证清理磁盘空间不足，保留清理标记")
                return False
            await conn.exec_driver_sql("VACUUM")
            await conn.commit()
            checkpoint = (
                await conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
            ).first()
            await conn.commit()
    except (OSError, ValueError) as exc:
        logger.warning("v6 凭证清理无法检查磁盘空间: %s", type(exc).__name__)
        return False
    except Exception:
        logger.exception("v6 凭证清理失败，保留清理标记")
        return False
    return checkpoint is not None and int(checkpoint[0]) == 0


async def check_database_integrity() -> bool:
    try:
        async with get_engine().connect() as conn:
            result = await conn.execute(text("PRAGMA integrity_check"))
            rows = result.fetchall()
        return len(rows) == 1 and rows[0][0] == "ok"
    except Exception:  # noqa: BLE001  # external boundary preserves failure isolation
        return False


async def check_wal_integrity() -> bool:
    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("PRAGMA wal_checkpoint(PASSIVE)"))
        return True
    except Exception:  # noqa: BLE001  # external boundary preserves failure isolation
        return False


async def dispose_engine() -> None:
    if _engine is not None:
        await _engine.dispose()


def reset_engine() -> None:
    global _engine, _session_maker
    _engine = None
    _session_maker = None
