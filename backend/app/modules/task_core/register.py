"""Blocking register(): admission gate for Task Core v1.

Responsibilities:
- Find existing live tid by resource_key → join if quota allows.
- Find latest completed tid with stored file → attach if quota allows (no oversell).
- Otherwise create a new tid + pid.
- Enforce known-size > quota_bytes → quota_exceeded.
- Enforce used+reserved+size <= quota_bytes for attach (CAS already handles this).
- Enforce same-user same-tid active pid → duplicate_task.

This module does NOT submit to aria2 (submit is stubbed / left for Task 3).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from app.domain.status import ACTIVE_USER_TASK_STATUSES
from app.modules.task_core.states import ERROR_QUOTA_EXCEEDED
from app.repositories.task.user_tasks import (
    attach_completed_file_to_user,
    create_user_task,
    get_user_task,
)
from app.repositories.task.downloads import (
    create_global_download_attempt,
    find_latest_completed_global_download_by_resource_key,
    find_live_global_download_by_resource_key,
)
from app.repositories.task.sources import (
    content_digest_for_payload,
    create_download_source,
    encode_options_json,
    encode_selection_json,
)
from app.repositories.errors import RepositoryConflictError
from app.services.usage_service import get_usage, release_reserved, reserve_bytes

RegisterOutcome = Literal["created", "joined_live", "attached_completed"]

DUPLICATE_TASK_MESSAGE = "任务已存在"


class RegisterError(Exception):
    """Structured register failure with a stable error code."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class ResourceSpec:
    resource_key: str
    source_uri: str
    resource_kind: str  # http | magnet | torrent | other
    display_name: str | None = None
    size_bytes: int = 0
    size_known: bool = False
    display_uri: str | None = None  # override ``uri`` in REST response
    # S layer (download_sources). payload defaults to source_uri when omitted.
    source_payload: str | None = None
    # Partial torrent selection only; full selection leaves this None.
    selection_indexes: tuple[int, ...] | None = None
    # User options before select-file injection; filtered by G1 whitelist on write.
    source_options: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class RegisterResult:
    pid: int  # user_tasks.id
    tid: int  # global_downloads.id
    outcome: RegisterOutcome
    status: str


def _size_known(resource: ResourceSpec) -> bool:
    return resource.size_known or resource.size_bytes > 0


async def register(
    *,
    user_id: int,
    quota_bytes: int,
    resource: ResourceSpec,
) -> RegisterResult:
    """Blocking admission gate. Returns pid/tid/outcome or raises RegisterError."""
    size = int(resource.size_bytes or 0)
    known = _size_known(resource)

    # AC-6: known size over total quota → reject immediately.
    if known and size > int(quota_bytes):
        raise RegisterError(ERROR_QUOTA_EXCEEDED, "任务大小超过用户配额")

    # 1. Attach to completed + store (instant transfer, no oversell).
    completed = await find_latest_completed_global_download_by_resource_key(
        resource.resource_key
    )
    if completed is not None and completed.get("completed_file_id") is not None:
        return await _register_attach(
            user_id=user_id,
            quota_bytes=quota_bytes,
            resource=resource,
            completed=completed,
            size=size,
        )

    # 2. Join existing live tid.
    live = await find_live_global_download_by_resource_key(resource.resource_key)
    if live is not None:
        return await _register_join_live(
            user_id=user_id,
            quota_bytes=quota_bytes,
            resource=resource,
            live=live,
            size=size,
            known=known,
        )

    # 3. Create new tid + pid.
    return await _register_create(
        user_id=user_id,
        quota_bytes=quota_bytes,
        resource=resource,
        size=size,
        known=known,
    )


async def _register_attach(
    *,
    user_id: int,
    quota_bytes: int,
    resource: ResourceSpec,
    completed: dict,
    size: int,
) -> RegisterResult:
    tid = int(completed["id"])
    # Duplicate check: same user already has a task for this tid.
    existing = await get_user_task(user_id, tid)
    if existing is not None:
        raise RegisterError("duplicate_task", DUPLICATE_TASK_MESSAGE)

    # AC-4: attach must be strict; attach_completed_file_to_user uses CAS
    # used+reserved+size <= quota. We pre-check to give a stable error code.
    effective_size = int(completed.get("completed_bytes") or size or 0)
    if effective_size > int(quota_bytes):
        raise RegisterError(ERROR_QUOTA_EXCEEDED, "任务大小超过用户配额")
    usage = await get_usage(user_id, quota_bytes)
    if usage["used_bytes"] + usage["reserved_bytes"] + effective_size > int(quota_bytes):
        raise RegisterError(ERROR_QUOTA_EXCEEDED, "用户配额不足，无法秒传")

    try:
        task = await attach_completed_file_to_user(
            user_id=user_id,
            quota_bytes=quota_bytes,
            global_download_id=tid,
            stored_file_id=int(completed["completed_file_id"]),
            size_bytes=effective_size,
            display_name=str(
                resource.display_name
                or completed.get("display_name")
                or resource.source_uri
            ),
            finished_at_ms=int(completed.get("completed_at_ms") or 0),
        )
    except ValueError as exc:
        if "quota" in str(exc).lower():
            raise RegisterError(ERROR_QUOTA_EXCEEDED, "用户配额不足，无法秒传") from exc
        raise
    except RepositoryConflictError as exc:
        raise RegisterError("conflict", str(exc)) from exc

    return RegisterResult(
        pid=int(task["id"]),
        tid=tid,
        outcome="attached_completed",
        status=str(task["status"]),
    )


async def _register_join_live(
    *,
    user_id: int,
    quota_bytes: int,
    resource: ResourceSpec,
    live: dict,
    size: int,
    known: bool,
) -> RegisterResult:
    tid = int(live["id"])
    existing = await get_user_task(user_id, tid)
    if existing is not None and str(existing["status"]) in ACTIVE_USER_TASK_STATUSES:
        raise RegisterError("duplicate_task", DUPLICATE_TASK_MESSAGE)

    # If the live tid already knows its size, use it for headroom check.
    live_size = int(live.get("total_bytes") or 0)
    live_known = bool(live.get("size_known"))
    effective_size = live_size if live_known else size
    effective_known = live_known or known

    if effective_known and effective_size > int(quota_bytes):
        raise RegisterError(ERROR_QUOTA_EXCEEDED, "任务大小超过用户配额")

    if effective_known:
        usage = await get_usage(user_id, quota_bytes)
        if usage["used_bytes"] + usage["reserved_bytes"] + effective_size > int(
            quota_bytes
        ):
            raise RegisterError(ERROR_QUOTA_EXCEEDED, "用户配额不足，无法加入下载")
        try:
            await reserve_bytes(user_id, effective_size, quota_bytes=quota_bytes)
        except ValueError:
            raise RegisterError(ERROR_QUOTA_EXCEEDED, "用户配额不足，无法加入下载")

    # Minimal v1: create pid referencing the same tid.
    # For a fresh join we set status to match the global live status.
    task_status = str(live["status"]) if live["status"] in ACTIVE_USER_TASK_STATUSES else "queued"
    values = {
        "user_id": user_id,
        "global_download_id": tid,
        "status": task_status,
        "reserved_bytes": effective_size if effective_known else 0,
        "display_name": resource.display_name,
    }
    if existing is not None:
        # Re-activate a previously failed/cancelled pid by creating a new one is
        # not supported by the unique constraint; keep minimal and error.
        raise RegisterError("duplicate_task", DUPLICATE_TASK_MESSAGE)
    try:
        task = await create_user_task(values)
    except Exception:
        if effective_known:
            await release_reserved(user_id, effective_size, quota_bytes=quota_bytes)
        raise
    return RegisterResult(
        pid=int(task["id"]),
        tid=tid,
        outcome="joined_live",
        status=str(task["status"]),
    )


async def _register_create(
    *,
    user_id: int,
    quota_bytes: int,
    resource: ResourceSpec,
    size: int,
    known: bool,
) -> RegisterResult:
    if known:
        usage = await get_usage(user_id, quota_bytes)
        if usage["used_bytes"] + usage["reserved_bytes"] + size > int(quota_bytes):
            raise RegisterError(ERROR_QUOTA_EXCEEDED, "用户配额不足，无法创建任务")
        try:
            await reserve_bytes(user_id, size, quota_bytes=quota_bytes)
        except ValueError:
            raise RegisterError(ERROR_QUOTA_EXCEEDED, "用户配额不足，无法创建任务")

    payload_text = resource.source_payload or resource.source_uri
    source_row = await create_download_source(
        {
            "resource_kind": resource.resource_kind,
            "payload_text": payload_text,
            "selection_json": encode_selection_json(resource.selection_indexes),
            "options_json": encode_options_json(resource.source_options),
            "content_digest": content_digest_for_payload(payload_text),
            "resource_identity": resource.resource_key,
        }
    )

    global_values = {
        "resource_key": resource.resource_key,
        "resource_kind": resource.resource_kind,
        "source_uri": resource.source_uri,
        "source_id": int(source_row["id"]),
        "display_name": resource.display_name,
        "status": "queued",
        "total_bytes": size,
        "completed_bytes": 0,
        "size_known": 1 if known else 0,
        "disk_reserved_bytes": size if known else 0,
    }
    try:
        gd = await create_global_download_attempt(global_values)
    except RepositoryConflictError:
        # Race: another attempt created the live tid first.
        # Release our reservation; join will re-reserve.
        if known:
            await release_reserved(user_id, size, quota_bytes=quota_bytes)
        live = await find_live_global_download_by_resource_key(resource.resource_key)
        if live is None:
            raise RegisterError("stale", "资源状态已变更，请重试")
        return await _register_join_live(
            user_id=user_id,
            quota_bytes=quota_bytes,
            resource=resource,
            live=live,
            size=size,
            known=known,
        )

    try:
        task = await create_user_task(
            {
                "user_id": user_id,
                "global_download_id": int(gd["id"]),
                "status": "queued",
                "reserved_bytes": size if known else 0,
                "display_name": resource.display_name,
            }
        )
    except Exception:
        if known:
            await release_reserved(user_id, size, quota_bytes=quota_bytes)
        raise
    return RegisterResult(
        pid=int(task["id"]),
        tid=int(gd["id"]),
        outcome="created",
        status=str(task["status"]),
    )
