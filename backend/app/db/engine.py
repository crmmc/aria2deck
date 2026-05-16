from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings

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
                cursor.execute("PRAGMA synchronous=NORMAL")
            finally:
                cursor.close()

    return _engine


def get_session_maker() -> async_sessionmaker[AsyncSession]:
    global _session_maker
    if _session_maker is None:
        _session_maker = async_sessionmaker(get_engine(), class_=AsyncSession, expire_on_commit=False)
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
        yield conn


async def apply_sqlite_pragmas() -> None:
    async with get_engine().begin() as conn:
        await conn.execute(text("PRAGMA journal_mode=WAL"))
        await conn.execute(text("PRAGMA synchronous=NORMAL"))
        await conn.execute(text("PRAGMA foreign_keys=ON"))


async def check_database_integrity() -> bool:
    try:
        async with get_engine().connect() as conn:
            result = await conn.execute(text("PRAGMA integrity_check"))
            rows = result.fetchall()
        return len(rows) == 1 and rows[0][0] == "ok"
    except Exception:
        return False


async def check_wal_integrity() -> bool:
    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("PRAGMA wal_checkpoint(PASSIVE)"))
        return True
    except Exception:
        return False


async def dispose_engine() -> None:
    global _engine
    if _engine is not None:
        await _engine.dispose()


def reset_engine() -> None:
    global _engine, _session_maker
    _engine = None
    _session_maker = None
