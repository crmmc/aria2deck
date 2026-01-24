from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncEngine

from app.core.config import settings


# Lazy-initialized engine and session maker
# This allows tests to patch settings.database_path before initialization
_async_engine: AsyncEngine | None = None
_async_session_maker: async_sessionmaker[AsyncSession] | None = None


def _get_engine() -> AsyncEngine:
    """Get or create the async engine (lazy initialization)."""
    global _async_engine
    if _async_engine is None:
        _async_engine = create_async_engine(
            f"sqlite+aiosqlite:///{settings.database_path}",
            echo=settings.debug,
        )
    return _async_engine


def _get_session_maker() -> async_sessionmaker[AsyncSession]:
    """Get or create the session maker (lazy initialization)."""
    global _async_session_maker
    if _async_session_maker is None:
        _async_session_maker = async_sessionmaker(
            _get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
        )
    return _async_session_maker


def reset_engine() -> None:
    """Reset the engine (for testing purposes)."""
    global _async_engine, _async_session_maker
    _async_engine = None
    _async_session_maker = None


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Get an async database session with automatic commit/rollback."""
    session_maker = _get_session_maker()
    async with session_maker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_db() -> None:
    """Initialize database tables using SQLModel metadata."""
    engine = _get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)


async def init_default_config(session: AsyncSession) -> None:
    """Initialize default configuration values."""
    from sqlmodel import select
    from app.models import Config

    default_configs = [
        ("max_task_size", "10737418240"),
        ("min_free_disk", "1073741824"),
        ("pack_format", "zip"),
        ("pack_compression_level", "5"),
        ("pack_extra_args", ""),
    ]

    for key, value in default_configs:
        result = await session.exec(select(Config).where(Config.key == key))
        existing = result.first()
        if not existing:
            session.add(Config(key=key, value=value))

    await session.commit()
