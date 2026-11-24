"""Unified task retry: rebuild from download_sources (S) with lazy migration.

Spec: M8 §3.4 / §3.9 / §3.10
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.domain.errors import BadRequestError, NotFoundError
from app.domain.status import (
    ACTIVE_USER_TASK_STATUSES,
    TERMINAL_USER_TASK_STATUSES,
)
from app.repositories.task.downloads import (
    get_global_download_by_id,
    update_global_download,
)
from app.repositories.task.sources import (
    content_digest_for_payload,
    create_download_source,
    encode_options_json,
    encode_selection_json,
    get_download_source_by_id,
)
from app.repositories.task.user_tasks import get_user_task_by_id
from app.services import task_service

logger = logging.getLogger(__name__)

MSG_NOT_FOUND = "任务不存在"
MSG_HISTORY_EXPIRED = "任务历史已过期，无法重试"
MSG_COMPLETED = "已完成任务不可重试"
MSG_LIVE = "进行中的任务不可重试"
MSG_INCOMPLETE = "任务创建数据不完整，无法重试，请重新添加"
MSG_CANCELLED_OR_FAILED_ONLY = "仅失败或已取消的任务可重试"

RETRYABLE_STATUSES = frozenset({"failed", "cancelled"})


def _parse_selection_indexes(selection_json: str | None) -> list[int] | None:
    if not selection_json:
        return None
    try:
        data = json.loads(selection_json)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    indexes = data.get("selected_file_indexes")
    if indexes is None:
        return None
    if not isinstance(indexes, list):
        return None
    return [int(i) for i in indexes]


def _parse_options(options_json: str | None) -> dict[str, Any] | None:
    if not options_json:
        return None
    try:
        data = json.loads(options_json)
    except (TypeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _incomplete() -> BadRequestError:
    return BadRequestError(MSG_INCOMPLETE)


async def _lazy_migrate_source(
    *,
    tid: int,
    global_download: dict[str, Any],
) -> dict[str, Any]:
    """Build S from legacy tid fields and write back source_id (§3.9)."""
    resource_kind = str(global_download.get("resource_kind") or "")
    source_uri = str(global_download.get("source_uri") or "")
    resource_key = str(global_download.get("resource_key") or "")

    if resource_kind in {"http", "magnet"}:
        if not source_uri:
            raise _incomplete()
        payload_text = source_uri
        selection_indexes = None
    elif resource_kind == "torrent":
        is_partial = ":files:" in resource_key
        if is_partial:
            # Partial selection without selection_json cannot be reconstructed.
            raise _incomplete()
        # Full selection: payload from base64: source_uri (legacy) only.
        if source_uri.startswith("base64:"):
            payload_text = source_uri
        else:
            raise _incomplete()
        selection_indexes = None
    else:
        raise _incomplete()

    source_row = await create_download_source(
        {
            "resource_kind": resource_kind,
            "payload_text": payload_text,
            "selection_json": encode_selection_json(selection_indexes),
            "options_json": encode_options_json(None),
            "content_digest": content_digest_for_payload(payload_text),
            "resource_identity": resource_key or None,
        }
    )
    await update_global_download(tid, {"source_id": int(source_row["id"])})
    logger.info(
        "懒迁移 download_source 完成 tid=%s source_id=%s kind=%s",
        tid,
        source_row["id"],
        resource_kind,
    )
    return source_row


async def _resolve_source(
    *,
    tid: int,
    global_download: dict[str, Any],
) -> dict[str, Any]:
    source_id = global_download.get("source_id")
    if source_id is not None:
        source = await get_download_source_by_id(int(source_id))
        if source is None:
            raise _incomplete()
        if source.get("purged_at_ms") is not None:
            raise _incomplete()
        payload = str(source.get("payload_text") or "")
        if not payload:
            raise _incomplete()
        # Partial torrent must still carry indexes.
        resource_kind = str(source.get("resource_kind") or global_download.get("resource_kind") or "")
        resource_key = str(global_download.get("resource_key") or "")
        if resource_kind == "torrent" and ":files:" in resource_key:
            indexes = _parse_selection_indexes(source.get("selection_json"))
            if not indexes:
                raise _incomplete()
        return source

    return await _lazy_migrate_source(tid=tid, global_download=global_download)


async def _rebuild_from_source(
    *,
    user_id: int,
    quota_bytes: int,
    source: dict[str, Any],
    global_download: dict[str, Any],
) -> dict[str, Any]:
    kind = str(source.get("resource_kind") or "")
    payload = str(source.get("payload_text") or "")
    options = _parse_options(source.get("options_json"))

    if kind in {"http", "magnet"}:
        return await task_service.create_task(
            user_id=user_id,
            quota_bytes=quota_bytes,
            uri=payload,
            options=options,
        )

    if kind == "torrent":
        torrent = payload.removeprefix("base64:")
        indexes = _parse_selection_indexes(source.get("selection_json"))
        return await task_service.create_torrent_task(
            user_id=user_id,
            quota_bytes=quota_bytes,
            torrent=torrent,
            selected_file_indexes=indexes,
            options=options,
        )

    raise _incomplete()


async def retry_task(
    *,
    user_id: int,
    user_task_id: int,
    quota_bytes: int,
) -> dict[str, Any]:
    """Retry a terminal user task → new pid via existing create path."""
    existing = await get_user_task_by_id(user_id, user_task_id)
    if existing is None:
        raise NotFoundError(MSG_NOT_FOUND)

    if existing.get("history_expired_at_ms") is not None:
        raise BadRequestError(MSG_HISTORY_EXPIRED)

    status = str(existing.get("status") or "")
    if status == "completed":
        raise BadRequestError(MSG_COMPLETED)
    if status in ACTIVE_USER_TASK_STATUSES:
        raise BadRequestError(MSG_LIVE)
    if status not in RETRYABLE_STATUSES:
        # Unknown / unexpected terminal-ish states
        if status in TERMINAL_USER_TASK_STATUSES:
            raise BadRequestError(MSG_CANCELLED_OR_FAILED_ONLY)
        raise BadRequestError(MSG_LIVE)

    tid = int(existing["global_download_id"])
    global_download = await get_global_download_by_id(tid)
    if global_download is None:
        raise NotFoundError(MSG_NOT_FOUND)

    source = await _resolve_source(tid=tid, global_download=global_download)
    payload = await _rebuild_from_source(
        user_id=user_id,
        quota_bytes=quota_bytes,
        source=source,
        global_download=global_download,
    )
    logger.info(
        "任务重试成功 user_id=%s old_pid=%s new_pid=%s old_tid=%s",
        user_id,
        user_task_id,
        payload.get("id"),
        tid,
    )
    return payload
