"""Tests for v0 stale queued download cleanup helpers."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update

from app.aria2.sync import STALE_QUEUED_GRACE_SECONDS
from app.services.aria2_lifecycle_service import cleanup_stale_queued_downloads_v0
from app.services.task_broadcast import clear_connections, set_connections_for_user
from app.db.engine import transaction
from app.db.schema import global_downloads, user_tasks
from app.repositories.downloads import now_ms
from tests.helpers_v0 import (
    create_global_download_v0,
    create_user_task_v0,
    create_user_v0,
)


async def _fetch_global(download_id: int) -> dict:
    async with transaction() as conn:
        row = (
            (
                await conn.execute(
                    select(global_downloads).where(global_downloads.c.id == download_id)
                )
            )
            .mappings()
            .one()
        )
    return dict(row)


async def _fetch_user_task(task_id: int) -> dict:
    async with transaction() as conn:
        row = (
            (await conn.execute(select(user_tasks).where(user_tasks.c.id == task_id)))
            .mappings()
            .one()
        )
    return dict(row)


async def _age_download(download_id: int, *, seconds: float) -> None:
    old_timestamp = now_ms() - int(seconds * 1000)
    async with transaction() as conn:
        await conn.execute(
            update(global_downloads)
            .where(global_downloads.c.id == download_id)
            .values(updated_at_ms=old_timestamp)
        )


@pytest.mark.asyncio
async def test_stale_queued_download_without_gid_becomes_failed_after_grace_period(
    temp_db: str,
) -> None:
    user = await create_user_v0(username="cleanup_lock")
    download = await create_global_download_v0(
        resource_key="cleanup:lock",
        status="queued",
        aria2_gid=None,
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="queued",
    )
    await _age_download(download["id"], seconds=STALE_QUEUED_GRACE_SECONDS + 1)

    await cleanup_stale_queued_downloads_v0()

    updated = await _fetch_global(download["id"])
    updated_task = await _fetch_user_task(task["id"])

    assert updated["status"] == "failed"
    assert updated["error_code"] == "submit_timeout"
    assert updated_task["status"] == "failed"


@pytest.mark.asyncio
async def test_stale_queued_download_cleanup_broadcasts_after_failure(
    temp_db: str,
) -> None:
    user = await create_user_v0(username="cleanup_broadcast")
    download = await create_global_download_v0(
        resource_key="cleanup:broadcast",
        status="queued",
        aria2_gid=None,
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="queued",
    )
    await _age_download(download["id"], seconds=STALE_QUEUED_GRACE_SECONDS + 1)
    ws = AsyncMock()
    await clear_connections()
    await set_connections_for_user(user["id"], {ws})

    await cleanup_stale_queued_downloads_v0()

    updated = await _fetch_global(download["id"])
    assert updated["status"] == "failed"
    ws.send_json.assert_awaited_once()
    payload = ws.send_json.await_args.args[0]
    assert payload["type"] == "task_update"
    assert payload["task"]["id"] == task["id"]
    assert payload["task"]["status"] == "error"


@pytest.mark.asyncio
async def test_recent_queued_download_without_gid_remains_queued(temp_db: str) -> None:
    user = await create_user_v0(username="cleanup_recent")
    download = await create_global_download_v0(
        resource_key="cleanup:recent",
        status="queued",
        aria2_gid=None,
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="queued",
    )
    await _age_download(download["id"], seconds=STALE_QUEUED_GRACE_SECONDS - 1)

    await cleanup_stale_queued_downloads_v0()

    updated = await _fetch_global(download["id"])
    updated_task = await _fetch_user_task(task["id"])

    assert updated["status"] == "queued"
    assert updated_task["status"] == "queued"
