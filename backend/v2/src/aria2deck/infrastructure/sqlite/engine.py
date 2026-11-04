from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool


class SQLiteDatabase:
    def __init__(self, path: Path) -> None:
        self.path = path
        url = URL.create("sqlite+aiosqlite", database=str(path))
        self.engine: AsyncEngine = create_async_engine(
            url,
            poolclass=NullPool,
            connect_args={"timeout": 30},
        )

    async def configure(self, connection: AsyncConnection) -> None:
        await connection.exec_driver_sql("PRAGMA foreign_keys = ON")
        await connection.exec_driver_sql("PRAGMA busy_timeout = 30000")
        await connection.exec_driver_sql("PRAGMA journal_mode = WAL")
        await connection.exec_driver_sql("PRAGMA synchronous = NORMAL")

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[AsyncConnection]:
        connection = await self.engine.connect()
        try:
            await self.configure(connection)
            yield connection
        finally:
            await connection.close()

    async def close(self) -> None:
        await self.engine.dispose()
