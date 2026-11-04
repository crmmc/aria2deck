from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from functools import partial
from typing import Any

from ...domain.errors import DomainError, ErrorCode

logger = logging.getLogger(__name__)
Runner = Callable[[], Coroutine[Any, Any, None]]


@dataclass(frozen=True, slots=True)
class WorkerStatus:
    name: str
    state: str
    required: bool
    error_type: str | None


class WorkerSupervisor:
    def __init__(self) -> None:
        self._definitions: dict[str, tuple[Runner, bool]] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._states: dict[str, str] = {}
        self._errors: dict[str, str | None] = {}
        self._stopping = False

    def register(self, name: str, runner: Runner, *, required: bool = True) -> None:
        if not name or name in self._definitions or self._tasks:
            raise DomainError(ErrorCode.INVALID_STATE, "后台任务注册无效")
        self._definitions[name] = (runner, required)
        self._states[name] = "pending"
        self._errors[name] = None

    async def start(self) -> None:
        if self._tasks:
            return
        self._stopping = False
        for name, (runner, _required) in self._definitions.items():
            self._states[name] = "running"
            self._errors[name] = None
            task: asyncio.Task[None] = asyncio.create_task(runner(), name=f"aria2deck:{name}")
            self._tasks[name] = task
            task.add_done_callback(partial(self._finished, name))

    def _finished(self, name: str, task: asyncio.Task[None]) -> None:
        if self._stopping or task.cancelled():
            self._states[name] = "stopped"
            return
        error = task.exception()
        self._states[name] = "failed"
        self._errors[name] = type(error).__name__ if error is not None else "WorkerExited"
        logger.warning("后台任务异常退出 worker=%s error_type=%s", name, self._errors[name])

    async def stop(self) -> None:
        self._stopping = True
        tasks = list(self._tasks.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for name in self._tasks:
            self._states[name] = "stopped"
        self._tasks.clear()

    def snapshot(self) -> list[WorkerStatus]:
        return [
            WorkerStatus(name, self._states[name], required, self._errors[name])
            for name, (_runner, required) in self._definitions.items()
        ]

    def readiness_errors(self) -> list[str]:
        errors: list[str] = []
        for name, (_runner, required) in self._definitions.items():
            if required and self._states[name] != "running":
                errors.append(f"后台任务 {name} 未正常运行")
        return errors
