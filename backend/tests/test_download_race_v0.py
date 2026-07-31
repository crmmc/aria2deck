from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.db.engine import transaction
from app.db.schema import global_downloads, user_tasks
from app.repositories.downloads import (
    clear_terminal_download_gid,
    DownloadAdmissionError,
    get_global_by_resource_key,
    get_user_task,
)
from app.services import aria2_lifecycle_service
from app.services.failed_task_cleanup import CleanupResult
from app.services.aria2_lifecycle_service import (
    fail_v0_download_and_cleanup,
    switch_to_followed_download,
)
from app.services.download_service import cancel_user_task, create_user_download
from app.services.storage import get_task_download_dir
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

    with pytest.raises(RuntimeError, match="内部下载任务提交失败"):
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


@pytest.mark.asyncio
async def test_failure_cleanup_blocks_retry_until_cleanup_finishes(
    temp_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await create_user_v0(username="race_failure_retry", quota_bytes=1000)
    client = AsyncMock()
    client.add_uri.side_effect = ["gid-failure-g1", "gid-failure-g2"]
    task = await create_user_download(
        user_id=user["id"],
        quota_bytes=user["quota_bytes"],
        uri="https://example.com/failure-retry.bin",
        resource_key="http:failure-retry",
        resource_kind="http",
        display_name="failure-retry.bin",
        total_bytes=100,
        aria2_client=client,
    )
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()

    async def blocked_cleanup(**kwargs: object) -> CleanupResult:
        cleanup_started.set()
        await release_cleanup.wait()
        task_id = kwargs["task_id"]
        gid = kwargs["gid"]
        assert isinstance(task_id, int)
        assert isinstance(gid, str)
        await clear_terminal_download_gid(task_id, expected_gid=gid)
        return CleanupResult(True, True, True)

    monkeypatch.setattr(
        aria2_lifecycle_service,
        "cleanup_terminal_download_generation",
        blocked_cleanup,
    )
    failure_task = asyncio.create_task(
        fail_v0_download_and_cleanup(
            client=client,
            download_id=task["global_download_id"],
            gid="gid-failure-g1",
            message="failed",
            error_code="failure",
            log_prefix="[Test]",
        )
    )
    await cleanup_started.wait()

    retry_task = asyncio.create_task(
        create_user_download(
            user_id=user["id"],
            quota_bytes=user["quota_bytes"],
            uri="https://example.com/failure-retry.bin",
            resource_key="http:failure-retry",
            resource_kind="http",
            display_name="failure-retry.bin",
            total_bytes=100,
            aria2_client=client,
        )
    )
    await asyncio.sleep(0.05)
    assert not retry_task.done()
    assert client.add_uri.await_count == 1

    release_cleanup.set()
    failed, retried = await asyncio.gather(failure_task, retry_task)
    stored = await get_global_by_resource_key("http:failure-retry")
    assert failed is True
    assert retried["id"] == task["id"]
    assert stored is not None
    assert stored["aria2_gid"] == "gid-failure-g2"
    assert stored["status"] == "active"


@pytest.mark.asyncio
async def test_cancelled_failure_cleanup_finishes_before_propagating_cancel(
    temp_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await create_user_v0(username="race_failure_cancel", quota_bytes=1000)
    client = AsyncMock()
    client.add_uri.side_effect = ["gid-failure-cancel-g1", "gid-failure-cancel-g2"]
    task = await create_user_download(
        user_id=user["id"],
        quota_bytes=user["quota_bytes"],
        uri="https://example.com/failure-cancel.bin",
        resource_key="http:failure-cancel",
        resource_kind="http",
        display_name="failure-cancel.bin",
        total_bytes=100,
        aria2_client=client,
    )
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()

    async def delayed_cleanup(**kwargs: object) -> CleanupResult:
        cleanup_started.set()
        await release_cleanup.wait()
        task_id = kwargs["task_id"]
        gid = kwargs["gid"]
        assert isinstance(task_id, int)
        assert isinstance(gid, str)
        await clear_terminal_download_gid(task_id, expected_gid=gid)
        return CleanupResult(True, True, True)

    monkeypatch.setattr(
        aria2_lifecycle_service,
        "cleanup_terminal_download_generation",
        delayed_cleanup,
    )
    failure = asyncio.create_task(
        fail_v0_download_and_cleanup(
            client=client,
            download_id=task["global_download_id"],
            gid="gid-failure-cancel-g1",
            message="failed",
            error_code="failure",
            log_prefix="[Test]",
        )
    )
    await cleanup_started.wait()
    failure.cancel()
    release_cleanup.set()
    with pytest.raises(asyncio.CancelledError):
        await failure

    failed = await get_global_by_resource_key("http:failure-cancel")
    assert failed is not None
    assert failed["status"] == "failed"
    assert failed["aria2_gid"] is None
    retried = await create_user_download(
        user_id=user["id"],
        quota_bytes=user["quota_bytes"],
        uri="https://example.com/failure-cancel.bin",
        resource_key="http:failure-cancel",
        resource_kind="http",
        display_name="failure-cancel.bin",
        total_bytes=100,
        aria2_client=client,
    )
    assert retried["status"] == "active"


@pytest.mark.asyncio
async def test_successful_cleanup_clears_gid_before_retry_submission(
    temp_db: str,
) -> None:
    user = await create_user_v0(username="race_cleanup_success", quota_bytes=1000)
    client = AsyncMock()
    client.add_uri.side_effect = ["gid-cleanup-success-g1", "gid-cleanup-success-g2"]
    task = await create_user_download(
        user_id=user["id"],
        quota_bytes=user["quota_bytes"],
        uri="https://example.com/cleanup-success.bin",
        resource_key="http:cleanup-success",
        resource_kind="http",
        display_name="cleanup-success.bin",
        total_bytes=100,
        aria2_client=client,
    )
    await fail_v0_download_and_cleanup(
        client=client,
        download_id=task["global_download_id"],
        gid="gid-cleanup-success-g1",
        message="failed",
        error_code="failure",
        log_prefix="[Test]",
    )
    failed = await get_global_by_resource_key("http:cleanup-success")
    failed_usage = await get_usage(user["id"], quota_bytes=user["quota_bytes"])
    assert failed is not None
    assert failed["status"] == "failed"
    assert failed["aria2_gid"] is None
    assert failed["disk_reserved_bytes"] == 0
    assert failed_usage["reserved_bytes"] == 0

    retried = await create_user_download(
        user_id=user["id"],
        quota_bytes=user["quota_bytes"],
        uri="https://example.com/cleanup-success.bin",
        resource_key="http:cleanup-success",
        resource_kind="http",
        display_name="cleanup-success.bin",
        total_bytes=100,
        aria2_client=client,
    )
    retried_usage = await get_usage(user["id"], quota_bytes=user["quota_bytes"])
    assert retried["status"] == "active"
    assert retried_usage["reserved_bytes"] == 100
    assert client.add_uri.await_count == 2


@pytest.mark.asyncio
async def test_force_remove_failure_keeps_gid_and_blocks_retry(
    temp_db: str,
) -> None:
    user = await create_user_v0(username="race_force_remove_failure", quota_bytes=1000)
    client = AsyncMock()
    client.add_uri.return_value = "gid-force-remove-g1"
    task = await create_user_download(
        user_id=user["id"],
        quota_bytes=user["quota_bytes"],
        uri="https://example.com/force-remove.bin",
        resource_key="http:force-remove-failure",
        resource_kind="http",
        display_name="force-remove.bin",
        total_bytes=100,
        aria2_client=client,
    )
    client.force_remove.side_effect = OSError("aria2 unavailable")

    changed = await fail_v0_download_and_cleanup(
        client=client,
        download_id=task["global_download_id"],
        gid="gid-force-remove-g1",
        message="failed",
        error_code="failure",
        log_prefix="[Test]",
    )
    failed = await get_global_by_resource_key("http:force-remove-failure")
    usage = await get_usage(user["id"], quota_bytes=user["quota_bytes"])

    assert changed is True
    assert failed is not None
    assert failed["status"] == "failed"
    assert failed["aria2_gid"] == "gid-force-remove-g1"
    assert failed["disk_reserved_bytes"] == 0
    assert usage["reserved_bytes"] == 0
    with pytest.raises(DownloadAdmissionError, match="previous_cleanup_pending"):
        await create_user_download(
            user_id=user["id"],
            quota_bytes=user["quota_bytes"],
            uri="https://example.com/force-remove.bin",
            resource_key="http:force-remove-failure",
            resource_kind="http",
            display_name="force-remove.bin",
            total_bytes=100,
            aria2_client=client,
        )
    assert client.add_uri.await_count == 1


@pytest.mark.asyncio
async def test_directory_cleanup_failure_keeps_gid_and_blocks_retry(
    temp_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await create_user_v0(username="race_directory_failure", quota_bytes=1000)
    client = AsyncMock()
    client.add_uri.return_value = "gid-directory-g1"
    task = await create_user_download(
        user_id=user["id"],
        quota_bytes=user["quota_bytes"],
        uri="https://example.com/directory.bin",
        resource_key="http:directory-cleanup-failure",
        resource_kind="http",
        display_name="directory.bin",
        total_bytes=100,
        aria2_client=client,
    )
    cleanup_calls = 0

    async def fail_directory_cleanup(task_id: int) -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1
        raise RuntimeError(f"cannot delete {task_id}")

    monkeypatch.setattr(
        "app.services.failed_task_cleanup.cleanup_task_download_dir",
        fail_directory_cleanup,
    )
    await fail_v0_download_and_cleanup(
        client=client,
        download_id=task["global_download_id"],
        gid="gid-directory-g1",
        message="failed",
        error_code="failure",
        log_prefix="[Test]",
    )
    failed = await get_global_by_resource_key("http:directory-cleanup-failure")
    assert failed is not None
    assert failed["aria2_gid"] == "gid-directory-g1"
    assert failed["disk_reserved_bytes"] == 0

    with pytest.raises(DownloadAdmissionError, match="previous_cleanup_pending"):
        await create_user_download(
            user_id=user["id"],
            quota_bytes=user["quota_bytes"],
            uri="https://example.com/directory.bin",
            resource_key="http:directory-cleanup-failure",
            resource_kind="http",
            display_name="directory.bin",
            total_bytes=100,
            aria2_client=client,
        )
    assert cleanup_calls == 2
    assert client.add_uri.await_count == 1


@pytest.mark.asyncio
async def test_cancel_wins_handoff_and_removes_followed_gid(
    temp_db: str,
) -> None:
    user = await create_user_v0(username="race_cancel_handoff", quota_bytes=1000)
    client = AsyncMock()
    client.add_uri.return_value = "gid-handoff-g1"
    info_hash = "c" * 40
    task = await create_user_download(
        user_id=user["id"],
        quota_bytes=user["quota_bytes"],
        uri=f"magnet:?xt=urn:btih:{info_hash}",
        resource_key=info_hash,
        resource_kind="magnet",
        display_name="cancel-handoff",
        total_bytes=100,
        aria2_client=client,
    )
    tell_started = asyncio.Event()
    release_tell = asyncio.Event()

    async def delayed_followed_status(gid: str) -> dict[str, object]:
        assert gid == "gid-handoff-g2"
        tell_started.set()
        await release_tell.wait()
        return {
            "gid": gid,
            "status": "active",
            "totalLength": "100",
            "completedLength": "0",
            "files": [{"path": "/downloads/payload.bin", "length": "100"}],
        }

    client.tell_status.side_effect = delayed_followed_status
    handoff = asyncio.create_task(
        switch_to_followed_download(
            client=client,
            download={
                "id": task["global_download_id"],
                "aria2_gid": "gid-handoff-g1",
                "resource_kind": "magnet",
                "display_name": "cancel-handoff",
            },
            metadata_gid="gid-handoff-g1",
            followed_gid="gid-handoff-g2",
            display_name_fallback="cancel-handoff",
            log_prefix="[Test]",
        )
    )
    await tell_started.wait()
    cancelled = await cancel_user_task(
        user_id=user["id"],
        user_task_id=task["id"],
        quota_bytes=user["quota_bytes"],
        aria2_client=client,
    )
    release_tell.set()
    switched = await handoff

    stored = await get_global_by_resource_key(info_hash)
    assert cancelled["status"] == "cancelled"
    assert switched is False
    assert stored is not None
    assert stored["status"] == "cancelled"
    assert stored["aria2_gid"] is None
    client.force_remove.assert_any_await("gid-handoff-g1")
    client.force_remove.assert_any_await("gid-handoff-g2")


@pytest.mark.asyncio
async def test_stale_handoff_stops_followed_without_deleting_shared_directory(
    temp_db: str,
) -> None:
    user = await create_user_v0(username="stale_handoff_dir", quota_bytes=1000)
    client = AsyncMock()
    client.add_uri.return_value = "gid-current-generation"
    info_hash = "d" * 40
    task = await create_user_download(
        user_id=user["id"], quota_bytes=user["quota_bytes"],
        uri=f"magnet:?xt=urn:btih:{info_hash}",
        resource_key=info_hash, resource_kind="magnet",
        display_name="stale-handoff", total_bytes=100, aria2_client=client,
    )
    stored = await get_global_by_resource_key(info_hash)
    assert stored is not None
    task_dir = get_task_download_dir(task["global_download_id"])
    sentinel = task_dir / "current-generation.bin"
    sentinel.write_bytes(b"keep")
    client.tell_status.return_value = {
        "status": "active", "totalLength": "100", "completedLength": "0",
        "files": [{"selected": "true", "length": "100"}],
    }

    switched = await switch_to_followed_download(
        client=client, download=stored, metadata_gid="gid-stale-metadata",
        followed_gid="gid-untracked-followed", display_name_fallback=None,
        log_prefix="[Test]",
    )

    latest = await get_global_by_resource_key(info_hash)
    assert latest is not None
    assert switched is False
    assert latest["aria2_gid"] == "gid-current-generation"
    assert latest["status"] == "active"
    assert sentinel.read_bytes() == b"keep"
    client.force_remove.assert_awaited_once_with("gid-untracked-followed")
    client.remove_download_result.assert_awaited_once_with("gid-untracked-followed")
