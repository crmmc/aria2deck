"""Repository helpers for download_sources (S layer, M8)."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db.engine import transaction
from app.db.schema import download_sources

# G1 options whitelist — select-file is rebuilt from selection_json, never stored.
SOURCE_OPTIONS_WHITELIST = frozenset(
    {"mirrors", "header", "http-user", "http-passwd", "out"}
)


def now_ms() -> int:
    return int(time.time() * 1000)


def content_digest_for_payload(payload_text: str) -> str:
    return hashlib.sha256(payload_text.encode("utf-8")).hexdigest()


def encode_selection_json(
    selected_file_indexes: Sequence[int] | None,
) -> str | None:
    """Versioned selection payload; None for full selection / non-torrent."""
    if selected_file_indexes is None:
        return None
    indexes = sorted(int(i) for i in selected_file_indexes)
    return json.dumps(
        {"version": 1, "selected_file_indexes": indexes},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def encode_options_json(options: Mapping[str, Any] | None) -> str | None:
    """Persist only G1 whitelist keys; drop select-file and unknown keys."""
    if not options:
        return None
    filtered: dict[str, Any] = {}
    for key in SOURCE_OPTIONS_WHITELIST:
        if key in options:
            filtered[key] = options[key]
    if not filtered:
        return None
    return json.dumps(filtered, ensure_ascii=False, separators=(",", ":"))


def torrent_source_uri_placeholder(info_hash: str) -> str:
    return f"torrent:{info_hash.lower()}"


def detached_source_uri_placeholder(
    *,
    resource_kind: str,
    resource_key: str,
    bt_info_hash: str | None,
    source_uri: str,
) -> str:
    """Short NOT NULL placeholder after soft-expire detaches S from tid."""
    kind = (resource_kind or "").strip().lower()
    info_hash = str(bt_info_hash or "").strip().lower()
    if not info_hash and resource_key:
        # resource_key may be "torrent:<hash>" or include hash segments
        for part in str(resource_key).lower().replace(":", " ").split():
            if len(part) == 40 and all(c in "0123456789abcdef" for c in part):
                info_hash = part
                break
    if kind == "torrent":
        return torrent_source_uri_placeholder(info_hash or "unknown")
    if kind == "magnet":
        if info_hash:
            return f"magnet:?xt=urn:btih:{info_hash}"
        return "magnet:purged"
    uri = (source_uri or "").strip()
    if uri and len(uri) <= 128:
        return uri
    return f"{kind or 'http'}:purged"


async def strip_orphaned_download_source(
    conn: AsyncConnection,
    source_id: int,
    *,
    timestamp_ms: int | None = None,
) -> bool:
    """Clear large S fields and set purged_at_ms. Caller ensures zero tid refs."""
    ts = int(timestamp_ms if timestamp_ms is not None else now_ms())
    row = (
        await conn.execute(
            update(download_sources)
            .where(
                download_sources.c.id == source_id,
                download_sources.c.purged_at_ms.is_(None),
            )
            .values(
                payload_text="",
                selection_json=None,
                options_json=None,
                content_digest=None,
                purged_at_ms=ts,
                updated_at_ms=ts,
            )
            .returning(download_sources.c.id)
        )
    ).first()
    return row is not None


async def create_download_source(values: dict[str, Any]) -> dict[str, Any]:
    """Insert a new download_sources row (v1 always creates; no reuse)."""
    timestamp = now_ms()
    row_values = {
        "created_at_ms": timestamp,
        "updated_at_ms": timestamp,
        **values,
    }
    async with transaction() as conn:
        row = (
            (
                await conn.execute(
                    insert(download_sources)
                    .values(**row_values)
                    .returning(download_sources)
                )
            )
            .mappings()
            .one()
        )
    return dict(row)


async def get_download_source_by_id(source_id: int) -> dict[str, Any] | None:
    async with transaction() as conn:
        row = (
            (
                await conn.execute(
                    select(download_sources).where(download_sources.c.id == source_id)
                )
            )
            .mappings()
            .first()
        )
    return dict(row) if row else None
