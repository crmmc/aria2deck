"""Shared download operations for aria2 listener and sync paths."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from sqlalchemy import update

from app.aria2.display_name import refreshable_user_task_display_name_condition
from app.core.security import sanitize_string
from app.db.engine import transaction
from app.db.schema import global_downloads, user_tasks
from app.repositories.downloads import ACTIVE_USER_TASK_STATUSES, now_ms
from app.services.task_projection import (
    METADATA_NAME_PREFIX,
    is_bt_resource_kind,
    is_metadata_phase_status,
)

logger = logging.getLogger(__name__)

INFO_HASH_HEX_PATTERN = re.compile(r"^[a-fA-F0-9]{40}$")


def safe_int(value: str | int | None, default: int = 0) -> int:
    try:
        return int(value) if value is not None else default
    except (ValueError, TypeError):
        return default


def bt_info_hash_from_status(status: dict[str, Any] | None) -> str | None:
    if not isinstance(status, dict):
        return None
    info_hash = str(status.get("infoHash") or "").strip()
    if INFO_HASH_HEX_PATTERN.fullmatch(info_hash):
        return info_hash.lower()
    return None


def extract_display_name(
    aria2_status: dict[str, Any],
    fallback: str | None,
) -> str | None:
    raw_name = aria2_status.get("bittorrent", {}).get("info", {}).get("name") or (
        aria2_status.get("files") or [{}]
    )[0].get("path")
    if isinstance(raw_name, str) and raw_name:
        name = Path(raw_name).name or raw_name
        if name.startswith(METADATA_NAME_PREFIX):
            return fallback
        return sanitize_string(name)
    return fallback


def map_progress_values(
    aria2_status: dict[str, Any],
    display_name_fallback: str | None,
    *,
    skip_total_on_metadata: bool = True,
) -> dict[str, Any]:
    values: dict[str, Any] = {}
    if not aria2_status:
        return values
    display_name = extract_display_name(aria2_status, display_name_fallback)
    if display_name and not display_name.startswith(METADATA_NAME_PREFIX):
        values["display_name"] = display_name
    if skip_total_on_metadata and is_metadata_phase_status(aria2_status):
        pass
    else:
        values["total_bytes"] = safe_int(aria2_status.get("totalLength"))
    values["completed_bytes"] = safe_int(aria2_status.get("completedLength"))
    return values


async def guarded_update_global_download(
    download_id: int,
    values: dict[str, Any],
    *,
    return_row: bool = False,
) -> dict[str, Any] | bool:
    if not values:
        return None if return_row else False

    row_values = {**values}
    row_values.setdefault("updated_at_ms", now_ms())
    stmt = (
        update(global_downloads)
        .where(
            global_downloads.c.id == download_id,
            global_downloads.c.status.in_(ACTIVE_USER_TASK_STATUSES),
            global_downloads.c.completed_file_id.is_(None),
        )
        .values(**row_values)
    )
    if return_row:
        stmt = stmt.returning(global_downloads)
    else:
        stmt = stmt.returning(global_downloads.c.id)

    async with transaction() as conn:
        row = (await conn.execute(stmt)).mappings().first()

    if return_row:
        return dict(row) if row else None
    return row is not None


async def update_active_user_tasks(
    download_id: int,
    *,
    status: str | None = None,
    display_name: str | None = None,
    force_display_name: bool = False,
) -> None:
    timestamp = now_ms()
    base_condition = [
        user_tasks.c.global_download_id == download_id,
        user_tasks.c.status.in_(ACTIVE_USER_TASK_STATUSES),
    ]
    async with transaction() as conn:
        if status is not None:
            await conn.execute(
                update(user_tasks)
                .where(*base_condition)
                .values(status=status, updated_at_ms=timestamp)
            )
        if display_name:
            if force_display_name:
                await conn.execute(
                    update(user_tasks)
                    .where(*base_condition)
                    .values(display_name=display_name, updated_at_ms=timestamp)
                )
            else:
                await conn.execute(
                    update(user_tasks)
                    .where(
                        *base_condition,
                        refreshable_user_task_display_name_condition(),
                    )
                    .values(display_name=display_name, updated_at_ms=timestamp)
                )
