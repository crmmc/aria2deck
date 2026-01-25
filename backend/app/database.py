from contextlib import asynccontextmanager
from typing import AsyncGenerator
import logging

from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncEngine

from app.core.config import settings


logger = logging.getLogger(__name__)


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
            # 数据库健壮性配置
            connect_args={
                "check_same_thread": False,
                # 启用 WAL 模式以提升并发性能
                # 启用 busy_timeout 自动重试（30 秒）
                "timeout": 30.0,
            },
            # 连接池配置
            pool_pre_ping=True,  # 连接前检测可用性
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
        # 设置 WAL 模式以提升并发性能和数据安全
        await conn.execute(text("PRAGMA journal_mode=WAL"))
        await conn.execute(text("PRAGMA synchronous=NORMAL"))
        await conn.run_sync(SQLModel.metadata.create_all)


async def check_database_integrity() -> bool:
    """检查数据库完整性

    在启动时调用，检测 SQLite 文件是否损坏。

    Returns:
        True 如果数据库完整，False 如果检测到问题
    """
    engine = _get_engine()
    try:
        async with engine.connect() as conn:
            result = await conn.execute(text("PRAGMA integrity_check"))
            rows = result.fetchall()

            if len(rows) == 1 and rows[0][0] == "ok":
                logger.info("数据库完整性检查通过")
                return True
            else:
                # 记录所有发现的问题
                for row in rows:
                    logger.error(f"数据库完整性问题: {row[0]}")
                return False
    except Exception as e:
        logger.error(f"数据库完整性检查失败: {e}")
        return False


async def check_wal_integrity() -> bool:
    """检查 WAL 文件完整性

    Returns:
        True 如果 WAL 正常，False 如果有问题
    """
    engine = _get_engine()
    try:
        async with engine.connect() as conn:
            # 执行 checkpoint 以合并 WAL
            result = await conn.execute(text("PRAGMA wal_checkpoint(PASSIVE)"))
            checkpoint_result = result.fetchone()

            if checkpoint_result:
                busy, log, checkpointed = checkpoint_result
                if busy:
                    logger.warning(f"WAL checkpoint 被阻塞，日志页: {log}，已检查点页: {checkpointed}")
                else:
                    logger.debug(f"WAL checkpoint 完成，日志页: {log}，已检查点页: {checkpointed}")

            return True
    except Exception as e:
        logger.error(f"WAL 完整性检查失败: {e}")
        return False


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
