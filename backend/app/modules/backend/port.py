"""Backend adapter port.

Task Core only knows this Protocol. The concrete adapter (aria2 today) lives
behind it and is not part of this skeleton task.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence


@dataclass(frozen=True)
class Snapshot:
    """Read-model the Task Core consumes for one tid."""

    tid: int
    status: str
    error_code: str | None = None
    # Raw backend-specific payload (aria2 tellStatus dict, etc.). Kept
    # opaque so the Task Core doesn't grow a dependency on the backend.
    raw: dict[str, Any] = field(default_factory=dict)


class BackendPort(Protocol):
    """Logical operations Task Core can request from a download backend."""

    async def submit(self, *, tid: int, uri: str, options: dict[str, Any]) -> str:
        """Submit a new download for ``tid``; returns the backend-native id."""
        ...

    async def tell_many(self, tids: Sequence[int]) -> list[Snapshot]:
        """Fetch snapshots for the given tids."""
        ...

    async def pause(self, tid: int) -> None: ...

    async def unpause(self, tid: int) -> None: ...

    async def remove(self, tid: int) -> None: ...
