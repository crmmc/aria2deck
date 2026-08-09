"""Submit a registered tid to the download backend.

Responsibilities:
- Read the global download row for ``tid``.
- Call ``backend.submit(tid, uri, options)`` when the row is still queued
  and has no gid yet.
- Return the assigned gid on success; ``None`` when the tid is not
  submittable (already submitted, terminal, or missing).
"""

from __future__ import annotations

from typing import Any

from app.modules.backend.port import BackendPort
from app.repositories.task.downloads import get_global_download_by_id


async def submit_tid(
    *,
    backend: BackendPort,
    tid: int,
    options: dict[str, Any] | None = None,
) -> str | None:
    """Submit one tid to the backend; returns the backend-native gid or None."""
    download = await get_global_download_by_id(tid)
    if download is None:
        return None
    if download.get("aria2_gid"):
        return str(download["aria2_gid"])
    if str(download.get("status")) != "queued":
        return None
    uri = str(download.get("source_uri") or "")
    if not uri:
        return None
    return await backend.submit(tid=tid, uri=uri, options=options or {})
