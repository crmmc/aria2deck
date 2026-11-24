from __future__ import annotations

from types import TracebackType

from sqlalchemy.ext.asyncio import AsyncConnection

from .engine import SQLiteDatabase


class SQLiteUnitOfWork:
    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database
        self._connection: AsyncConnection | None = None

    @property
    def connection(self) -> AsyncConnection:
        if self._connection is None:
            raise RuntimeError("Unit of Work 尚未进入事务")
        return self._connection

    async def __aenter__(self) -> SQLiteUnitOfWork:
        if self._connection is not None:
            raise RuntimeError("Unit of Work 不能重复进入")
        connection = await self.database.engine.connect()
        try:
            await self.database.configure(connection)
            await connection.exec_driver_sql("BEGIN IMMEDIATE")
        except BaseException:
            await connection.close()
            raise
        self._connection = connection
        return self

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        connection = self.connection
        try:
            if exception_type is None:
                await connection.commit()
            else:
                await connection.rollback()
        finally:
            self._connection = None
            await connection.close()
