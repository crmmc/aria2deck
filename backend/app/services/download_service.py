from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any, Protocol

from sqlalchemy.exc import IntegrityError

from app.repositories.downloads import (
    create_user_task,
    get_or_create_global_download,
    get_user_task,
    update_global_download,
    update_user_task,
)
from app.services.usage_service import release_reserved, reserve_bytes

logger = logging.getLogger(__name__)
RETRYABLE_DOWNLOAD_STATUSES = {"failed", "cancelled"}
RETRYABLE_TASK_STATUSES = {"failed", "cancelled"}


class Aria2SubmitClient(Protocol):
    async def add_uri(
        self, uris: list[str], options: Mapping[str, Any] | None = None
    ) -> str: ...

    async def force_remove(self, gid: str) -> str: ...


_download_locks: dict[int, asyncio.Lock] = {}
_download_locks_guard = asyncio.Lock()
_user_task_locks: dict[tuple[int, int], asyncio.Lock] = {}
_user_task_locks_guard = asyncio.Lock()


async def _get_download_lock(download_id: int) -> asyncio.Lock:
    async with _download_locks_guard:
        lock = _download_locks.get(download_id)
        if lock is None:
            lock = asyncio.Lock()
            _download_locks[download_id] = lock
        return lock


async def _get_user_task_lock(user_id: int, global_download_id: int) -> asyncio.Lock:
    key = (user_id, global_download_id)
    async with _user_task_locks_guard:
        lock = _user_task_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _user_task_locks[key] = lock
        return lock


async def _release_task_reservation(
    *, task: dict[str, Any], user_id: int, quota_bytes: int
) -> None:
    reserved_bytes = int(task.get("reserved_bytes") or 0)
    if reserved_bytes <= 0:
        return
    await release_reserved(user_id, reserved_bytes, quota_bytes=quota_bytes)
    await update_user_task(task["id"], {"reserved_bytes": 0})


async def _remove_submitted_gid(aria2_client: Aria2SubmitClient, gid: str) -> None:
    try:
        await aria2_client.force_remove(gid)
    except Exception:
        logger.exception("Failed to remove orphan aria2 download gid=%s", gid)


async def create_user_download(
    *,
    user_id: int,
    quota_bytes: int,
    uri: str,
    resource_key: str,
    resource_kind: str,
    display_name: str | None,
    total_bytes: int,
    aria2_client: Aria2SubmitClient,
    options: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    global_download = await get_or_create_global_download(
        {
            "resource_key": resource_key,
            "resource_kind": resource_kind,
            "source_uri": uri,
            "display_name": display_name,
            "total_bytes": max(0, int(total_bytes)),
        }
    )

    existing_task = await get_user_task(user_id, global_download["id"])
    if existing_task and existing_task["status"] not in RETRYABLE_TASK_STATUSES:
        return existing_task

    lock = await _get_user_task_lock(user_id, global_download["id"])
    async with lock:
        existing_task = await get_user_task(user_id, global_download["id"])
        if existing_task and existing_task["status"] not in RETRYABLE_TASK_STATUSES:
            return existing_task

        if existing_task and global_download["status"] in RETRYABLE_DOWNLOAD_STATUSES:
            updated_global = await update_global_download(
                global_download["id"],
                {
                    "aria2_gid": None,
                    "status": "queued",
                    "error_code": None,
                    "error_message": None,
                    "completed_at_ms": None,
                },
            )
            if updated_global is None:
                raise LookupError("global download not found")
            global_download = updated_global

        reserved_bytes = max(0, int(total_bytes))
        reservation_made = False
        task: dict[str, Any] | None = existing_task
        if reserved_bytes > 0:
            await reserve_bytes(user_id, reserved_bytes, quota_bytes=quota_bytes)
            reservation_made = True

        try:
            task_values = {
                "status": "queued",
                "reserved_bytes": reserved_bytes,
                "display_name": display_name,
                "error_message": None,
                "finished_at_ms": None,
            }
            if task:
                updated_task = await update_user_task(task["id"], task_values)
                if updated_task is None:
                    raise LookupError("user task not found")
                task = updated_task
            else:
                task = await create_user_task(
                    {
                        "user_id": user_id,
                        "global_download_id": global_download["id"],
                        **task_values,
                    }
                )
        except IntegrityError:
            if reservation_made:
                await release_reserved(user_id, reserved_bytes, quota_bytes=quota_bytes)
            existing_task = await get_user_task(user_id, global_download["id"])
            if existing_task:
                return existing_task
            raise
        except Exception:
            if reservation_made:
                await release_reserved(user_id, reserved_bytes, quota_bytes=quota_bytes)
            raise

    try:
        global_download = await _ensure_download_submitted(
            global_download=global_download,
            uri=uri,
            options=options,
            aria2_client=aria2_client,
        )
    except Exception as exc:
        await _release_task_reservation(
            task=task, user_id=user_id, quota_bytes=quota_bytes
        )
        await update_user_task(
            task["id"],
            {
                "status": "failed",
                "error_message": str(exc),
            },
        )
        raise

    if global_download["status"] == "active":
        updated_task = await update_user_task(task["id"], {"status": "active"})
        if updated_task:
            task = updated_task

    return task


async def _ensure_download_submitted(
    *,
    global_download: dict[str, Any],
    uri: str,
    options: Mapping[str, Any] | None,
    aria2_client: Aria2SubmitClient,
) -> dict[str, Any]:
    if global_download.get("aria2_gid") or global_download["status"] != "queued":
        return global_download

    lock = await _get_download_lock(global_download["id"])
    async with lock:
        current = await get_or_create_global_download(
            {
                "resource_key": global_download["resource_key"],
                "resource_kind": global_download["resource_kind"],
                "source_uri": global_download["source_uri"],
                "display_name": global_download["display_name"],
                "total_bytes": global_download["total_bytes"],
            }
        )
        if current.get("aria2_gid") or current["status"] != "queued":
            return current

        gid = await aria2_client.add_uri([uri], options or {})
        try:
            updated = await update_global_download(
                current["id"],
                {
                    "aria2_gid": gid,
                    "status": "active",
                },
            )
            if updated is None:
                raise RuntimeError("failed to persist submitted download")
            return updated
        except Exception:
            await _remove_submitted_gid(aria2_client, gid)
            raise
