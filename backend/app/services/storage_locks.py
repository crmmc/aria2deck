from __future__ import annotations

import asyncio
import threading
from weakref import WeakValueDictionary

_content_hash_locks: WeakValueDictionary[tuple[int, str], asyncio.Lock] = (
    WeakValueDictionary()
)
_content_hash_locks_guard = threading.Lock()


def _loop_id() -> int:
    return id(asyncio.get_running_loop())


class _ContentReadState:
    def __init__(self) -> None:
        self.active_readers = 0
        self.drained = asyncio.Event()
        self.drained.set()


_content_read_states: WeakValueDictionary[tuple[int, str], _ContentReadState] = (
    WeakValueDictionary()
)


class ContentReadLease:
    def __init__(self, state: _ContentReadState) -> None:
        self._state = state
        self._released = False

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._state.active_readers -= 1
        if self._state.active_readers == 0:
            self._state.drained.set()


def _read_state(content_hash: str) -> _ContentReadState:
    key = (_loop_id(), content_hash)
    with _content_hash_locks_guard:
        state = _content_read_states.get(key)
        if state is None:
            state = _ContentReadState()
            _content_read_states[key] = state
        return state


async def get_content_hash_lock(content_hash: str) -> asyncio.Lock:
    key = (_loop_id(), content_hash)
    with _content_hash_locks_guard:
        lock = _content_hash_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _content_hash_locks[key] = lock
        return lock


def acquire_content_read_lease_locked(content_hash: str) -> ContentReadLease:
    state = _read_state(content_hash)
    state.active_readers += 1
    state.drained.clear()
    return ContentReadLease(state)


async def wait_for_content_readers_locked(content_hash: str) -> None:
    await _read_state(content_hash).drained.wait()
