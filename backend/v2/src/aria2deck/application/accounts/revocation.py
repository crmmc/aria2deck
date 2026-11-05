from __future__ import annotations

from collections.abc import Callable

from ..ports import SessionRevocation


class InMemorySessionRevocationBus:
    """进程内会话撤销总线：accounts 发布、长连接方订阅即收。"""

    def __init__(self) -> None:
        self._subscribers: list[Callable[[SessionRevocation], None]] = []

    def publish(self, revocation: SessionRevocation) -> None:
        for callback in list(self._subscribers):
            callback(revocation)

    def subscribe(self, callback: Callable[[SessionRevocation], None]) -> None:
        self._subscribers.append(callback)

    def unsubscribe(self, callback: Callable[[SessionRevocation], None]) -> None:
        if callback in self._subscribers:
            self._subscribers.remove(callback)
