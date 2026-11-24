from __future__ import annotations

import os
import time
from collections.abc import Awaitable, Mapping, MutableMapping
from pathlib import Path

from app.core.config import settings

REQUIRED_WORKERS = ("sync", "listener", "deletion", "pack")


def _state_value(application: object, name: str) -> object | None:
    state = getattr(application, "state", None)
    return getattr(state, name, None) if state is not None else None


def _download_directory_ready() -> bool:
    directory = Path(settings.download_dir)
    return directory.is_dir() and os.access(directory, os.R_OK | os.W_OK | os.X_OK)


def _worker_failures(application: object) -> list[str]:
    registry = _state_value(application, "background_workers")
    workers = registry if isinstance(registry, Mapping) else {}
    failures: list[str] = []

    for name in REQUIRED_WORKERS:
        worker = workers.get(name)
        if not isinstance(worker, Mapping):
            failures.append(f"{name} 未启动")
            continue

        task = worker.get("task")
        done = getattr(task, "done", None)
        if not callable(done):
            failures.append(f"{name} 未启动")
            continue

        try:
            completed = done()
        except Exception:  # noqa: BLE001  # external boundary preserves failure isolation
            failures.append(f"{name} 状态异常")
            continue

        if not isinstance(completed, bool):
            failures.append(f"{name} 状态异常")
        elif completed:
            error = worker.get("error")
            failures.append(error if isinstance(error, str) and error else f"{name} 已退出")
        elif isinstance(worker, MutableMapping):
            worker["status"] = "running"
            worker["last_observed_at"] = time.monotonic()
    return failures


async def _database_ready(application: object) -> bool:
    probe = _state_value(application, "database_ready")
    if not callable(probe):
        return False
    try:
        result = probe()
        if not isinstance(result, Awaitable):
            return False
        return (await result) is True
    except Exception:  # noqa: BLE001  # external boundary preserves failure isolation
        return False


async def readiness_errors(application: object) -> list[str]:
    errors = _worker_failures(application)
    if not _download_directory_ready():
        errors.append("下载目录不可用")
    if not await _database_ready(application):
        errors.append("数据库不可用")
    return errors
