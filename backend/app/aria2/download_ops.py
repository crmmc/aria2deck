"""Shared download operations for aria2 listener and sync paths."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, overload

if TYPE_CHECKING:
    from app.aria2.client import Aria2Client

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
ARIA2_TO_V0_STATUS = {
    "active": "active",
    "waiting": "waiting",
    "paused": "paused",
    "complete": "completed",
    "error": "failed",
    "removed": "failed",
}


def safe_int(value: str | int | None, default: int = 0) -> int:
    try:
        return int(value) if value is not None else default
    except (ValueError, TypeError):
        return default


def map_aria2_status(status: dict[str, Any] | None, default: str = "active") -> str:
    if not isinstance(status, dict):
        return default
    raw_status = str(status.get("status") or "")
    return ARIA2_TO_V0_STATUS.get(raw_status, default)


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
    is_metadata = skip_total_on_metadata and is_metadata_phase_status(aria2_status)
    display_name = extract_display_name(aria2_status, display_name_fallback)
    if (
        not is_metadata
        and display_name
        and not display_name.startswith(METADATA_NAME_PREFIX)
    ):
        values["display_name"] = display_name
    if is_metadata:
        pass
    else:
        values["total_bytes"] = safe_int(aria2_status.get("totalLength"))
    values["completed_bytes"] = safe_int(aria2_status.get("completedLength"))
    return values


@overload
async def guarded_update_global_download(
    download_id: int,
    values: dict[str, Any],
    *,
    return_row: Literal[False] = False,
) -> bool: ...


@overload
async def guarded_update_global_download(
    download_id: int,
    values: dict[str, Any],
    *,
    return_row: Literal[True],
) -> dict[str, Any] | None: ...


async def guarded_update_global_download(
    download_id: int,
    values: dict[str, Any],
    *,
    return_row: bool = False,
) -> dict[str, Any] | bool | None:
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


async def switch_to_followed_download(
    *,
    client: "Aria2Client",
    download: dict[str, Any],
    metadata_gid: str | None,
    followed_gid: str,
    display_name_fallback: str | None,
    log_prefix: str,
) -> bool:
    download_id = int(download["id"])
    logger.info(
        "%s Metadata download complete, updating GID: %s -> %s",
        log_prefix,
        metadata_gid,
        followed_gid,
    )

    real_status: dict[str, Any] | None = None
    try:
        real_status = await client.tell_status(followed_gid)
    except Exception as exc:
        logger.debug(
            "%s Failed to refresh followed download gid=%s error=%s",
            log_prefix,
            followed_gid,
            exc,
        )

    global_values: dict[str, Any] = {
        "aria2_gid": followed_gid,
        "status": "active",
    }
    display_name: str | None = None

    if real_status:
        if first_followed_gid(real_status) is None:
            global_values["status"] = map_aria2_status(real_status)
        progress = map_progress_values(real_status, display_name_fallback)
        global_values.update(progress)
        display_name = progress.get("display_name")

        bt_hash = bt_info_hash_from_status(real_status)
        if bt_hash:
            global_values["bt_info_hash"] = bt_hash

    if not is_bt_resource_kind(download):
        global_values["resource_kind"] = "torrent"

    changed = await guarded_update_global_download(download_id, global_values)
    if not changed:
        return False

    await update_active_user_tasks(
        download_id,
        status=str(global_values["status"]),
        display_name=display_name,
        force_display_name=True,
    )

    if metadata_gid and metadata_gid != followed_gid:
        try:
            await client.remove_download_result(metadata_gid)
        except Exception as exc:
            logger.debug(
                "%s Failed to remove metadata result gid=%s error=%s",
                log_prefix,
                metadata_gid,
                exc,
            )

    return True


def first_followed_gid(aria2_status: dict[str, Any]) -> str | None:
    followed_by = aria2_status.get("followedBy")
    if not isinstance(followed_by, list):
        return None

    for gid in followed_by:
        if isinstance(gid, (str, int)) and str(gid):
            return str(gid)
    return None


def following_gid(aria2_status: dict[str, Any]) -> str | None:
    following = aria2_status.get("following") or aria2_status.get("followingGid")
    if isinstance(following, (str, int)) and str(following):
        return str(following)
    return None


def _is_magnet_download(download: dict[str, Any]) -> bool:
    if str(download.get("resource_kind") or "").lower() == "magnet":
        return True
    return str(download.get("source_uri") or "").lower().startswith("magnet:")


def _has_unresolved_magnet_display_name(download: dict[str, Any]) -> bool:
    display_name = str(download.get("display_name") or "").strip().lower()
    if not display_name:
        return True
    return display_name.startswith(("magnet:", "torrent:"))


def _has_bittorrent_payload_info(aria2_status: dict[str, Any]) -> bool:
    bittorrent = aria2_status.get("bittorrent")
    if not isinstance(bittorrent, dict):
        return False

    info = bittorrent.get("info")
    if not isinstance(info, dict):
        return False

    name = str(info.get("name") or "").strip()
    return bool(name) and not name.startswith(METADATA_NAME_PREFIX)


def _has_payload_like_file_path(aria2_status: dict[str, Any]) -> bool:
    files = aria2_status.get("files")
    if not isinstance(files, list):
        return False

    for item in files:
        if not isinstance(item, dict):
            continue
        raw_path = item.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            continue
        name = Path(raw_path).name.lower()
        if (
            name
            and name != "metadata"
            and not name.endswith(".torrent")
            and not name.startswith("[metadata]")
        ):
            return True
    return False


def is_metadata_handoff_pending(
    download: dict[str, Any],
    aria2_status: dict[str, Any],
) -> bool:
    if str(aria2_status.get("status") or "") != "complete":
        return False
    if not _is_magnet_download(download):
        return False
    if not _has_unresolved_magnet_display_name(download):
        return False
    if first_followed_gid(aria2_status) is not None:
        return False
    if following_gid(aria2_status) is not None:
        return False

    return not _has_bittorrent_payload_info(
        aria2_status
    ) and not _has_payload_like_file_path(aria2_status)
