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

    async def tell_status(self, gid: str) -> dict[str, Any]:
        """Fetch the backend-native status dict for ``gid``."""
        ...

    async def pause_gid(self, gid: str) -> str:
        """Pause the backend writer identified by its native gid."""
        ...

    async def unpause_gid(self, gid: str) -> str:
        """Resume the backend writer identified by its native gid."""
        ...

    async def tell_active(self) -> list[dict[str, Any]]:
        """List active downloads in the backend."""
        ...

    async def tell_waiting(
        self, offset: int = 0, num: int = 1000
    ) -> list[dict[str, Any]]:
        """List waiting downloads in the backend."""
        ...

    async def tell_stopped(
        self, offset: int = 0, num: int = 1000
    ) -> list[dict[str, Any]]:
        """List stopped downloads in the backend."""
        ...

    async def force_remove_gid(self, gid: str) -> str:
        """Force-stop the backend writer identified by its native gid.

        Physical stop primitive for claim-authorized reclamation paths; the
        aria2 adapter delegates to ``force_remove``."""
        ...

    async def remove_download_result_gid(self, gid: str) -> str:
        """Best-effort removal of the backend-side result record for ``gid``."""
        ...

    async def join_submission(
        self, *, tid: int, gid: str, uris: list[str]
    ) -> None:
        """把已提交 attempt 的补发 URI 下发到 backend（多 mirror/capability）。"""
        ...
