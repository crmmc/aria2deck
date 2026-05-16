from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

import app.services.download_service as download_service
from app.repositories.downloads import get_global_by_resource_key, get_user_task
from app.services.download_service import cancel_user_task, create_user_download
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
    temp_db: str,
) -> None:
    user = await create_user_v0(username="same_user_race_down", quota_bytes=500)
    client = AsyncMock()
    client.add_uri.return_value = "gid-same-user"

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


@pytest.mark.asyncio
async def test_cancel_one_user_keeps_shared_download_active(temp_db: str) -> None:
    user_a = await create_user_v0(username="cancel_shared_a", quota_bytes=1000)
    user_b = await create_user_v0(username="cancel_shared_b", quota_bytes=1000)
    client = AsyncMock()
    client.add_uri.return_value = "gid-shared-cancel"

    first = await create_user_download(
        user_id=user_a["id"],
        quota_bytes=user_a["quota_bytes"],
        uri="https://example.com/shared-cancel.bin",
        resource_key="http:shared-cancel",
        resource_kind="http",
        display_name="shared-cancel.bin",
        total_bytes=300,
        aria2_client=client,
    )
    second = await create_user_download(
        user_id=user_b["id"],
        quota_bytes=user_b["quota_bytes"],
        uri="https://example.com/shared-cancel.bin",
        resource_key="http:shared-cancel",
        resource_kind="http",
        display_name="shared-cancel.bin",
        total_bytes=300,
        aria2_client=client,
    )

    cancelled = await cancel_user_task(
        user_id=user_a["id"],
        user_task_id=first["id"],
        quota_bytes=user_a["quota_bytes"],
        aria2_client=client,
    )
    global_download = await get_global_by_resource_key("http:shared-cancel")
    first_task = await get_user_task(user_a["id"], first["global_download_id"])
    second_task = await get_user_task(user_b["id"], second["global_download_id"])
    first_usage = await get_usage(user_a["id"], quota_bytes=user_a["quota_bytes"])
    second_usage = await get_usage(user_b["id"], quota_bytes=user_b["quota_bytes"])

    assert cancelled["status"] == "cancelled"
    assert cancelled["reserved_bytes"] == 0
    assert first_task is not None
    assert first_task["status"] == "cancelled"
    assert first_task["finished_at_ms"] is not None
    assert second_task is not None
    assert second_task["status"] == "active"
    assert second_task["reserved_bytes"] == 300
    assert first_usage["reserved_bytes"] == 0
    assert second_usage["reserved_bytes"] == 300
    assert global_download is not None
    assert global_download["status"] == "active"
    assert global_download["aria2_gid"] == "gid-shared-cancel"
    client.force_remove.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_last_user_removes_global_download_and_releases_space(
    temp_db: str,
) -> None:
    user = await create_user_v0(username="cancel_last", quota_bytes=1000)
    client = AsyncMock()
    client.add_uri.return_value = "gid-last-cancel"

    task = await create_user_download(
        user_id=user["id"],
        quota_bytes=user["quota_bytes"],
        uri="https://example.com/last-cancel.bin",
        resource_key="http:last-cancel",
        resource_kind="http",
        display_name="last-cancel.bin",
        total_bytes=500,
        aria2_client=client,
    )

    cancelled = await cancel_user_task(
        user_id=user["id"],
        user_task_id=task["id"],
        quota_bytes=user["quota_bytes"],
        aria2_client=client,
    )
    global_download = await get_global_by_resource_key("http:last-cancel")
    usage = await get_usage(user["id"], quota_bytes=user["quota_bytes"])

    assert cancelled["status"] == "cancelled"
    assert cancelled["reserved_bytes"] == 0
    assert cancelled["finished_at_ms"] is not None
    assert usage["reserved_bytes"] == 0
    assert global_download is not None
    assert global_download["status"] == "cancelled"
    assert global_download["aria2_gid"] is None
    client.force_remove.assert_awaited_once_with("gid-last-cancel")


@pytest.mark.asyncio
async def test_concurrent_cancel_same_task_releases_reservation_once(
    temp_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await create_user_v0(username="cancel_same_race", quota_bytes=2000)
    client = AsyncMock()
    client.add_uri.side_effect = ["gid-cancel-race-1", "gid-cancel-race-2"]

    task_to_cancel = await create_user_download(
        user_id=user["id"],
        quota_bytes=user["quota_bytes"],
        uri="https://example.com/cancel-race-1.bin",
        resource_key="http:cancel-race-1",
        resource_kind="http",
        display_name="cancel-race-1.bin",
        total_bytes=300,
        aria2_client=client,
    )
    await create_user_download(
        user_id=user["id"],
        quota_bytes=user["quota_bytes"],
        uri="https://example.com/cancel-race-2.bin",
        resource_key="http:cancel-race-2",
        resource_kind="http",
        display_name="cancel-race-2.bin",
        total_bytes=400,
        aria2_client=client,
    )

    original_get_user_task_by_id = download_service.get_user_task_by_id
    first_reads = 0
    both_read = asyncio.Event()
    read_lock = asyncio.Lock()

    async def synchronized_get_user_task_by_id(user_id: int, user_task_id: int):
        nonlocal first_reads
        row = await original_get_user_task_by_id(user_id, user_task_id)
        if user_task_id != task_to_cancel["id"] or row is None:
            return row
        async with read_lock:
            if first_reads < 2:
                first_reads += 1
                if first_reads == 2:
                    both_read.set()
            else:
                return row
        await both_read.wait()
        return row

    monkeypatch.setattr(
        download_service, "get_user_task_by_id", synchronized_get_user_task_by_id
    )

    results = await asyncio.gather(
        cancel_user_task(
            user_id=user["id"],
            user_task_id=task_to_cancel["id"],
            quota_bytes=user["quota_bytes"],
            aria2_client=client,
        ),
        cancel_user_task(
            user_id=user["id"],
            user_task_id=task_to_cancel["id"],
            quota_bytes=user["quota_bytes"],
            aria2_client=client,
        ),
    )
    usage = await get_usage(user["id"], quota_bytes=user["quota_bytes"])

    assert results[0]["status"] == "cancelled"
    assert results[1]["status"] == "cancelled"
    assert usage["reserved_bytes"] == 400
    client.force_remove.assert_any_await("gid-cancel-race-1")
    assert client.force_remove.await_count == 1


@pytest.mark.asyncio
async def test_cancel_last_user_force_remove_failure_keeps_task_retryable(
    temp_db: str,
) -> None:
    user = await create_user_v0(username="cancel_retry", quota_bytes=1000)
    client = AsyncMock()
    client.add_uri.return_value = "gid-cancel-retry"
    task = await create_user_download(
        user_id=user["id"],
        quota_bytes=user["quota_bytes"],
        uri="https://example.com/cancel-retry.bin",
        resource_key="http:cancel-retry",
        resource_kind="http",
        display_name="cancel-retry.bin",
        total_bytes=500,
        aria2_client=client,
    )

    client.force_remove.side_effect = [OSError("aria2 timeout"), "gid-cancel-retry"]

    with pytest.raises(OSError, match="aria2 timeout"):
        await cancel_user_task(
            user_id=user["id"],
            user_task_id=task["id"],
            quota_bytes=user["quota_bytes"],
            aria2_client=client,
        )

    task_after_failure = await get_user_task(user["id"], task["global_download_id"])
    global_after_failure = await get_global_by_resource_key("http:cancel-retry")
    usage_after_failure = await get_usage(user["id"], quota_bytes=user["quota_bytes"])

    assert task_after_failure is not None
    assert task_after_failure["status"] == "active"
    assert task_after_failure["reserved_bytes"] == 500
    assert global_after_failure is not None
    assert global_after_failure["status"] == "active"
    assert global_after_failure["aria2_gid"] == "gid-cancel-retry"
    assert usage_after_failure["reserved_bytes"] == 500

    cancelled = await cancel_user_task(
        user_id=user["id"],
        user_task_id=task["id"],
        quota_bytes=user["quota_bytes"],
        aria2_client=client,
    )
    usage_after_retry = await get_usage(user["id"], quota_bytes=user["quota_bytes"])

    assert cancelled["status"] == "cancelled"
    assert usage_after_retry["reserved_bytes"] == 0
    assert client.force_remove.await_count == 2


@pytest.mark.asyncio
async def test_cancel_missing_user_task_raises_lookup_error(temp_db: str) -> None:
    user = await create_user_v0(username="cancel_missing")
    client = AsyncMock()

    with pytest.raises(LookupError, match="task not found"):
        await cancel_user_task(
            user_id=user["id"],
            user_task_id=99999,
            quota_bytes=user["quota_bytes"],
            aria2_client=client,
        )

    client.force_remove.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_terminal_user_task_is_noop(temp_db: str) -> None:
    user = await create_user_v0(username="cancel_terminal", quota_bytes=1000)
    client = AsyncMock()
    client.add_uri.return_value = "gid-cancel-terminal"
    task = await create_user_download(
        user_id=user["id"],
        quota_bytes=user["quota_bytes"],
        uri="https://example.com/cancel-terminal.bin",
        resource_key="http:cancel-terminal",
        resource_kind="http",
        display_name="cancel-terminal.bin",
        total_bytes=300,
        aria2_client=client,
    )

    await cancel_user_task(
        user_id=user["id"],
        user_task_id=task["id"],
        quota_bytes=user["quota_bytes"],
        aria2_client=client,
    )
    first_usage = await get_usage(user["id"], quota_bytes=user["quota_bytes"])

    terminal = await cancel_user_task(
        user_id=user["id"],
        user_task_id=task["id"],
        quota_bytes=user["quota_bytes"],
        aria2_client=client,
    )
    second_usage = await get_usage(user["id"], quota_bytes=user["quota_bytes"])

    assert terminal["status"] == "cancelled"
    assert first_usage["reserved_bytes"] == 0
    assert second_usage["reserved_bytes"] == 0
    assert client.force_remove.await_count == 1
