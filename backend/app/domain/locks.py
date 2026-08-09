from __future__ import annotations

import asyncio
import threading

_lifecycle_locks: dict[tuple[int, int], asyncio.Lock] = {}
_lifecycle_locks_guard = threading.Lock()


def _loop_id() -> int:
    return id(asyncio.get_running_loop())


async def get_download_lifecycle_lock(download_id: int) -> asyncio.Lock:
    key = (_loop_id(), download_id)
    with _lifecycle_locks_guard:
        lock = _lifecycle_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _lifecycle_locks[key] = lock
        return lock
