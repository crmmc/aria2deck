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
from app.repositories.task.sources import get_download_source_by_id


async def resolve_submit_uri(download: dict[str, Any]) -> str:
    """Resolve aria2 submit payload from S when tid.source_uri is a short placeholder.

    New create path stores torrent payload only in download_sources.payload_text and
    writes ``torrent:{info_hash}`` on tid.source_uri. HTTP/magnet keep the full URI
    on both layers; reading S is still correct and keeps a single payload source.
    """
    uri = str(download.get("source_uri") or "")
    source_id = download.get("source_id")
    # Prefer S.payload_text when present and tid is not already carrying base64 payload.
    if source_id is not None and not uri.startswith("base64:"):
        source = await get_download_source_by_id(int(source_id))
        payload = str((source or {}).get("payload_text") or "")
        if payload:
            return payload
    return uri


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
    uri = await resolve_submit_uri(download)
    if not uri:
        return None
    return await backend.submit(tid=tid, uri=uri, options=options or {})
