from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

import app.services.download_service as download_service
from app.repositories.downloads import get_global_by_resource_key, get_user_task
from app.services.download_service import create_user_download
from app.services.usage_service import get_usage
from tests.helpers_v0 import create_user_v0


@pytest.mark.asyncio
async def test_two_users_share_one_global_download(temp_db: str):
    user_a = await create_user_v0(username="down_a")
    user_b = await create_user_v0(username="down_b")
    client = AsyncMock()
    client.add_uri.return_value = "gid-shared"

    first = await create_user_download(
        user_id=user_a["id"],
        quota_bytes=user_a["quota_bytes"],
        uri="https://example.com/file.iso",
        resource_key="http:abc",
        resource_kind="http",
        display_name="file.iso",
        total_bytes=100,
        aria2_client=client,
    )
    second = await create_user_download(
        user_id=user_b["id"],
        quota_bytes=user_b["quota_bytes"],
        uri="https://example.com/file.iso",
        resource_key="http:abc",
        resource_kind="http",
        display_name="file.iso",
        total_bytes=100,
        aria2_client=client,
    )

    assert first["global_download_id"] == second["global_download_id"]
    assert first["id"] != second["id"]
    client.add_uri.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_download_reserves_user_space(temp_db: str):
    user = await create_user_v0(username="reserve_down", quota_bytes=1000)
    client = AsyncMock()
    client.add_uri.return_value = "gid-reserve"

    task = await create_user_download(
        user_id=user["id"],
        quota_bytes=user["quota_bytes"],
        uri="https://example.com/small.bin",
        resource_key="http:small",
        resource_kind="http",
        display_name="small.bin",
        total_bytes=400,
        aria2_client=client,
    )

    assert task["reserved_bytes"] == 400
    assert task["status"] == "active"


@pytest.mark.asyncio
async def test_concurrent_same_user_create_keeps_single_reservation(
    temp_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await create_user_v0(username="same_user_race_down", quota_bytes=500)
    client = AsyncMock()
    client.add_uri.return_value = "gid-same-user"
    original_get_user_task = download_service.get_user_task
    lock = asyncio.Lock()
    both_read = asyncio.Event()
    empty_read_count = 0

    async def synchronized_get_user_task(user_id: int, global_download_id: int):
        nonlocal empty_read_count
        task = await original_get_user_task(user_id, global_download_id)
        if task is not None:
            return task
        async with lock:
            empty_read_count += 1
            if empty_read_count == 2:
                both_read.set()
        await both_read.wait()
        return task

    monkeypatch.setattr(download_service, "get_user_task", synchronized_get_user_task)

    results = await asyncio.gather(
        create_user_download(
            user_id=user["id"],
            quota_bytes=user["quota_bytes"],
            uri="https://example.com/race.bin",
            resource_key="http:race",
            resource_kind="http",
            display_name="race.bin",
            total_bytes=500,
            aria2_client=client,
        ),
        create_user_download(
            user_id=user["id"],
            quota_bytes=user["quota_bytes"],
            uri="https://example.com/race.bin",
            resource_key="http:race",
            resource_kind="http",
            display_name="race.bin",
            total_bytes=500,
            aria2_client=client,
        ),
        return_exceptions=True,
    )
    usage = await get_usage(user["id"], quota_bytes=user["quota_bytes"])

    assert all(not isinstance(result, Exception) for result in results)
    assert results[0]["id"] == results[1]["id"]
    assert usage["reserved_bytes"] == 500
    client.add_uri.assert_awaited_once()


@pytest.mark.asyncio
async def test_add_uri_failure_releases_reservation_and_marks_task_failed(
    temp_db: str,
) -> None:
    user = await create_user_v0(username="submit_fail_down", quota_bytes=1000)
    client = AsyncMock()
    client.add_uri.side_effect = RuntimeError("aria2 unavailable")

    with pytest.raises(RuntimeError, match="aria2 unavailable"):
        await create_user_download(
            user_id=user["id"],
            quota_bytes=user["quota_bytes"],
            uri="https://example.com/fail.bin",
            resource_key="http:fail",
            resource_kind="http",
            display_name="fail.bin",
            total_bytes=300,
            aria2_client=client,
        )

    global_download = await get_global_by_resource_key("http:fail")
    assert global_download is not None
    task = await get_user_task(user["id"], global_download["id"])
    usage = await get_usage(user["id"], quota_bytes=user["quota_bytes"])

    assert task is not None
    assert task["status"] == "failed"
    assert task["reserved_bytes"] == 0
    assert usage["reserved_bytes"] == 0


@pytest.mark.asyncio
async def test_submit_persist_failure_removes_orphan_gid_and_releases_reservation(
    temp_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await create_user_v0(username="persist_fail_down", quota_bytes=1000)
    client = AsyncMock()
    client.add_uri.return_value = "gid-orphan"

    async def fail_global_update(download_id: int, values: dict):
        if "aria2_gid" in values:
            raise RuntimeError("db unavailable")
        return await original_update_global_download(download_id, values)

    original_update_global_download = download_service.update_global_download
    monkeypatch.setattr(download_service, "update_global_download", fail_global_update)

    with pytest.raises(RuntimeError, match="db unavailable"):
        await create_user_download(
            user_id=user["id"],
            quota_bytes=user["quota_bytes"],
            uri="https://example.com/orphan.bin",
            resource_key="http:orphan",
            resource_kind="http",
            display_name="orphan.bin",
            total_bytes=400,
            aria2_client=client,
        )

    global_download = await get_global_by_resource_key("http:orphan")
    assert global_download is not None
    task = await get_user_task(user["id"], global_download["id"])
    usage = await get_usage(user["id"], quota_bytes=user["quota_bytes"])

    client.force_remove.assert_awaited_once_with("gid-orphan")
    assert global_download["aria2_gid"] is None
    assert global_download["status"] == "queued"
    assert task is not None
    assert task["status"] == "failed"
    assert task["reserved_bytes"] == 0
    assert usage["reserved_bytes"] == 0


@pytest.mark.asyncio
async def test_retry_failed_submit_reactivates_user_task(temp_db: str) -> None:
    user = await create_user_v0(username="retry_fail_down", quota_bytes=1000)
    client = AsyncMock()
    client.add_uri.side_effect = [RuntimeError("temporary rpc failure"), "gid-retry"]

    with pytest.raises(RuntimeError, match="temporary rpc failure"):
        await create_user_download(
            user_id=user["id"],
            quota_bytes=user["quota_bytes"],
            uri="https://example.com/retry.bin",
            resource_key="http:retry",
            resource_kind="http",
            display_name="retry.bin",
            total_bytes=200,
            aria2_client=client,
        )

    task = await create_user_download(
        user_id=user["id"],
        quota_bytes=user["quota_bytes"],
        uri="https://example.com/retry.bin",
        resource_key="http:retry",
        resource_kind="http",
        display_name="retry.bin",
        total_bytes=200,
        aria2_client=client,
    )
    usage = await get_usage(user["id"], quota_bytes=user["quota_bytes"])

    assert task["status"] == "active"
    assert task["reserved_bytes"] == 200
    assert usage["reserved_bytes"] == 200
    assert client.add_uri.await_count == 2
