from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.db.engine import transaction
from app.db.schema import global_downloads, user_tasks
from app.repositories.downloads import get_global_by_resource_key, get_user_task
from app.services.download_service import cancel_user_task, create_user_download
from app.services.usage_service import get_usage
from tests.helpers_v0 import create_user_v0


async def _table_count(table) -> int:
    async with transaction() as conn:
        value = (
            await conn.execute(select(func.count()).select_from(table))
        ).scalar_one()
    return int(value or 0)


@pytest.mark.asyncio
async def test_concurrent_shared_download_create_keeps_one_global_download(
    temp_db: str,
) -> None:
    user_a = await create_user_v0(username="race_create_a", quota_bytes=1000)
    user_b = await create_user_v0(username="race_create_b", quota_bytes=1000)
    client = AsyncMock()
    client.add_uri.return_value = "gid-race-create"

    results = await asyncio.gather(
        create_user_download(
            user_id=user_a["id"],
            quota_bytes=user_a["quota_bytes"],
            uri="https://example.com/race-create.bin",
            resource_key="http:race-create",
            resource_kind="http",
            display_name="race-create.bin",
            total_bytes=300,
            aria2_client=client,
        ),
        create_user_download(
            user_id=user_b["id"],
            quota_bytes=user_b["quota_bytes"],
            uri="https://example.com/race-create.bin",
            resource_key="http:race-create",
            resource_kind="http",
            display_name="race-create.bin",
            total_bytes=300,
            aria2_client=client,
        ),
    )

    assert results[0]["global_download_id"] == results[1]["global_download_id"]
    assert await _table_count(global_downloads) == 1
    assert await _table_count(user_tasks) == 2
    client.add_uri.assert_awaited_once()


@pytest.mark.asyncio
async def test_last_subscriber_cancel_releases_reservation_once(temp_db: str) -> None:
    user = await create_user_v0(username="race_cancel_last", quota_bytes=1000)
    client = AsyncMock()
    client.add_uri.return_value = "gid-race-cancel"
    task = await create_user_download(
        user_id=user["id"],
        quota_bytes=user["quota_bytes"],
        uri="https://example.com/race-cancel.bin",
        resource_key="http:race-cancel",
        resource_kind="http",
        display_name="race-cancel.bin",
        total_bytes=500,
        aria2_client=client,
    )

    results = await asyncio.gather(
        cancel_user_task(
            user_id=user["id"],
            user_task_id=task["id"],
            quota_bytes=user["quota_bytes"],
            aria2_client=client,
        ),
        cancel_user_task(
            user_id=user["id"],
            user_task_id=task["id"],
            quota_bytes=user["quota_bytes"],
            aria2_client=client,
        ),
    )
    global_download = await get_global_by_resource_key("http:race-cancel")
    stored_task = await get_user_task(user["id"], task["global_download_id"])
    usage = await get_usage(user["id"], quota_bytes=user["quota_bytes"])

    assert {row["status"] for row in results} == {"cancelled"}
    assert stored_task is not None
    assert stored_task["status"] == "cancelled"
    assert stored_task["reserved_bytes"] == 0
    assert global_download is not None
    assert global_download["status"] == "cancelled"
    assert usage["reserved_bytes"] == 0
    client.force_remove.assert_awaited_once_with("gid-race-cancel")


@pytest.mark.asyncio
async def test_failed_submit_releases_user_reservation(temp_db: str) -> None:
    user = await create_user_v0(username="race_submit_fail", quota_bytes=1000)
    client = AsyncMock()
    client.add_uri.side_effect = RuntimeError("aria2 unavailable")

    with pytest.raises(RuntimeError, match="aria2 unavailable"):
        await create_user_download(
            user_id=user["id"],
            quota_bytes=user["quota_bytes"],
            uri="https://example.com/race-fail.bin",
            resource_key="http:race-fail",
            resource_kind="http",
            display_name="race-fail.bin",
            total_bytes=400,
            aria2_client=client,
        )

    global_download = await get_global_by_resource_key("http:race-fail")
    assert global_download is not None
    stored_task = await get_user_task(user["id"], global_download["id"])
    usage = await get_usage(user["id"], quota_bytes=user["quota_bytes"])

    assert stored_task is not None
    assert stored_task["status"] == "failed"
    assert stored_task["reserved_bytes"] == 0
    assert usage["reserved_bytes"] == 0
