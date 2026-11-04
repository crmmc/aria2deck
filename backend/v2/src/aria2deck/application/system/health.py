from __future__ import annotations

from collections.abc import Awaitable, Callable

from .supervisor import WorkerSupervisor


class HealthService:
    def __init__(
        self,
        *,
        database_check: Callable[[], Awaitable[bool]],
        directory_check: Callable[[], bool],
        supervisor: WorkerSupervisor,
    ) -> None:
        self.database_check = database_check
        self.directory_check = directory_check
        self.supervisor = supervisor

    async def errors(self) -> list[str]:
        errors: list[str] = []
        if not await self.database_check():
            errors.append("数据库不可用")
        if not self.directory_check():
            errors.append("下载目录不可用")
        errors.extend(self.supervisor.readiness_errors())
        return errors

    async def is_ready(self) -> bool:
        return not await self.errors()
