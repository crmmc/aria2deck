from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update

from app.aria2.sync import (
    STALE_QUEUED_GRACE_SECONDS,
    _cleanup_stale_queued_downloads_v0,
    _fail_v0_download_and_cleanup,
    _repair_inconsistent_completed_downloads_v0,
    sync_tasks,
    _update_v0_download_from_aria2,
)
from app.core.config import settings
from app.core.state import AppState, get_task_complete_lock
from app.db.engine import transaction
from app.db.schema import global_downloads, user_files, user_tasks
from app.repositories.downloads import now_ms
from app.services.usage_service import get_usage, reserve_bytes
from tests.helpers_v0 import (
    create_global_download_v0,
    create_user_file_v0,
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


@pytest.mark.asyncio
async def test_stale_queued_download_without_gid_becomes_failed_after_grace_period(
    temp_db: str,
) -> None:
    user = await create_user_v0(username="sync_stale")
    download = await create_global_download_v0(
        resource_key="sync:stale",
        status="queued",
        aria2_gid=None,
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="queued",
    )
    old_timestamp = now_ms() - int((STALE_QUEUED_GRACE_SECONDS + 1) * 1000)
    async with transaction() as conn:
        await conn.execute(
            update(global_downloads)
            .where(global_downloads.c.id == download["id"])
            .values(updated_at_ms=old_timestamp)
        )

    await _cleanup_stale_queued_downloads_v0(AppState())

    updated = await _fetch_global(download["id"])
    updated_task = await _fetch_user_task(task["id"])

    assert updated["status"] == "failed"
    assert updated["error_code"] == "submit_timeout"
    assert updated_task["status"] == "failed"


@pytest.mark.asyncio
async def test_active_aria2_status_updates_global_bytes_and_active_user_task_status(
    temp_db: str,
) -> None:
    user = await create_user_v0(username="sync_active")
    download = await create_global_download_v0(
        resource_key="sync:active",
        status="queued",
        aria2_gid="gid-sync-active",
        total_bytes=0,
        completed_bytes=0,
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="queued",
    )
    client = AsyncMock()

    await _update_v0_download_from_aria2(
        state=AppState(),
        client=client,
        download=download,
        status={
            "gid": "gid-sync-active",
            "status": "active",
            "totalLength": "1000",
            "completedLength": "250",
            "downloadSpeed": "10",
            "files": [{"path": "/tmp/sync-active.bin"}],
        },
    )

    updated = await _fetch_global(download["id"])
    updated_task = await _fetch_user_task(task["id"])

    assert updated["status"] == "active"
    assert updated["total_bytes"] == 1000
    assert updated["completed_bytes"] == 250
    assert updated_task["status"] == "active"


@pytest.mark.asyncio
async def test_error_aria2_result_marks_active_user_tasks_failed(
    temp_db: str,
) -> None:
    user = await create_user_v0(username="sync_error", quota_bytes=1000)
    await reserve_bytes(user["id"], 400, quota_bytes=user["quota_bytes"])
    download = await create_global_download_v0(
        resource_key="sync:error",
        status="active",
        aria2_gid="gid-sync-error",
        total_bytes=400,
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=400,
    )
    client = AsyncMock()
    client.force_remove.return_value = "OK"
    client.remove_download_result.return_value = "OK"

    await _update_v0_download_from_aria2(
        state=AppState(),
        client=client,
        download=download,
        status={
            "gid": "gid-sync-error",
            "status": "error",
            "errorCode": "7",
            "errorMessage": "network error",
            "totalLength": "400",
            "completedLength": "11",
            "files": [],
        },
    )

    updated = await _fetch_global(download["id"])
    updated_task = await _fetch_user_task(task["id"])
    usage = await get_usage(user["id"], quota_bytes=user["quota_bytes"])

    assert updated["status"] == "failed"
    assert updated["aria2_gid"] is None
    assert updated["error_code"] == "7"
    assert updated_task["status"] == "failed"
    assert updated_task["reserved_bytes"] == 0
    assert usage["reserved_bytes"] == 0


@pytest.mark.asyncio
async def test_error_aria2_result_preserves_specific_aria2_error_message(
    temp_db: str,
) -> None:
    user = await create_user_v0(username="sync_specific_error", quota_bytes=1000)
    await reserve_bytes(user["id"], 100, quota_bytes=user["quota_bytes"])
    download = await create_global_download_v0(
        resource_key="sync:specific-error",
        status="active",
        aria2_gid="gid-sync-specific-error",
        total_bytes=100,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=100,
    )
    client = AsyncMock()
    client.force_remove.return_value = "OK"
    raw_error = "CUID#12 - Download aborted. URI=https://example.com/file.iso"

    await _update_v0_download_from_aria2(
        state=AppState(),
        client=client,
        download=download,
        status={
            "gid": "gid-sync-specific-error",
            "status": "error",
            "errorCode": "7",
            "errorMessage": raw_error,
            "totalLength": "100",
            "completedLength": "1",
            "files": [],
        },
    )

    updated = await _fetch_global(download["id"])

    assert updated["status"] == "failed"
    assert updated["error_message"] == raw_error


@pytest.mark.asyncio
async def test_metadata_followed_by_refreshes_real_task_progress_and_name(
    temp_db: str,
) -> None:
    user = await create_user_v0(username="sync_followed_refresh")
    download = await create_global_download_v0(
        resource_key="sync:followed-refresh",
        resource_kind="magnet",
        source_uri="magnet:?xt=urn:btih:followed",
        status="active",
        aria2_gid="gid-metadata",
        display_name="magnet:?xt=urn:btih:followed",
        total_bytes=0,
        completed_bytes=0,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
    )
    client = AsyncMock()
    client.tell_status.return_value = {
        "gid": "gid-real",
        "status": "active",
        "totalLength": "2048",
        "completedLength": "512",
        "bittorrent": {"info": {"name": "Real Torrent"}},
        "files": [{"path": "/downloads/Real Torrent/file.bin", "length": "2048"}],
    }
    client.remove_download_result.return_value = "OK"

    await _update_v0_download_from_aria2(
        state=AppState(),
        client=client,
        download=download,
        status={
            "gid": "gid-metadata",
            "status": "complete",
            "followedBy": ["gid-real"],
            "totalLength": "0",
            "completedLength": "0",
            "files": [],
        },
    )

    updated = await _fetch_global(download["id"])

    assert updated["aria2_gid"] == "gid-real"
    assert updated["status"] == "active"
    assert updated["display_name"] == "Real Torrent"
    assert updated["total_bytes"] == 2048
    assert updated["completed_bytes"] == 512
    client.tell_status.assert_awaited_once_with("gid-real")


@pytest.mark.asyncio
async def test_active_aria2_status_does_not_overwrite_completed_download(
    temp_db: str,
) -> None:
    user = await create_user_v0(username="sync_terminal_guard", quota_bytes=1000)
    stored_path = Path(settings.download_dir) / "store" / "sync-terminal.bin"
    stored_path.parent.mkdir(parents=True, exist_ok=True)
    stored_path.write_bytes(b"done")
    user_file = await create_user_file_v0(
        user_id=user["id"],
        real_path=stored_path,
        content_hash="sync-terminal-hash",
        display_name="sync-terminal.bin",
        size_bytes=4,
    )
    download = await create_global_download_v0(
        resource_key="sync:terminal-guard",
        status="completed",
        aria2_gid="gid-sync-terminal",
        total_bytes=4,
        completed_bytes=4,
        completed_file_id=user_file["stored_file_id"],
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="completed",
    )
    client = AsyncMock()

    await _update_v0_download_from_aria2(
        state=AppState(),
        client=client,
        download=download,
        status={
            "gid": "gid-sync-terminal",
            "status": "active",
            "totalLength": "1000",
            "completedLength": "250",
            "files": [{"path": "/tmp/stale-active.bin"}],
        },
    )

    updated = await _fetch_global(download["id"])
    updated_task = await _fetch_user_task(task["id"])

    assert updated["status"] == "completed"
    assert updated["total_bytes"] == 4
    assert updated["completed_bytes"] == 4
    assert updated["completed_file_id"] == user_file["stored_file_id"]
    assert updated_task["status"] == "completed"


@pytest.mark.asyncio
async def test_active_bt_status_with_full_bytes_completes_v0_download(
    temp_db: str,
) -> None:
    user = await create_user_v0(username="sync_active_full_bt", quota_bytes=1000)
    await reserve_bytes(user["id"], 7, quota_bytes=user["quota_bytes"])
    download = await create_global_download_v0(
        resource_key="sync:active-full-bt",
        resource_kind="magnet",
        source_uri="magnet:?xt=urn:btih:full",
        status="active",
        aria2_gid="gid-sync-active-full-bt",
        total_bytes=7,
        completed_bytes=6,
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=7,
    )
    task_dir = Path(settings.download_dir) / "downloading" / str(download["id"])
    task_dir.mkdir(parents=True)
    source_file = task_dir / "payload.bin"
    source_file.write_bytes(b"payload")
    client = AsyncMock()
    client.remove_download_result.return_value = "OK"

    await _update_v0_download_from_aria2(
        state=AppState(),
        client=client,
        download=download,
        status={
            "gid": "gid-sync-active-full-bt",
            "status": "active",
            "totalLength": "7",
            "completedLength": "7",
            "infoHash": "abc123",
            "bittorrent": {"info": {"name": "payload.bin"}},
            "files": [
                {"path": str(source_file), "length": "7", "completedLength": "7"}
            ],
        },
    )

    updated = await _fetch_global(download["id"])
    updated_task = await _fetch_user_task(task["id"])
    usage = await get_usage(user["id"], quota_bytes=user["quota_bytes"])
    async with transaction() as conn:
        user_file_count = (
            await conn.execute(
                select(user_files).where(user_files.c.user_id == user["id"])
            )
        ).all()

    assert updated["status"] == "completed"
    assert updated["completed_file_id"] is not None
    assert updated["completed_bytes"] == 7
    assert updated_task["status"] == "completed"
    assert updated_task["reserved_bytes"] == 0
    assert usage["reserved_bytes"] == 0
    assert len(user_file_count) == 1
    client.remove_download_result.assert_awaited_once_with("gid-sync-active-full-bt")


@pytest.mark.asyncio
async def test_active_full_bytes_without_real_file_name_stays_active(
    temp_db: str,
) -> None:
    user = await create_user_v0(username="sync_active_full_no_name", quota_bytes=1000)
    await reserve_bytes(user["id"], 7, quota_bytes=user["quota_bytes"])
    download = await create_global_download_v0(
        resource_key="sync:active-full-no-name",
        resource_kind="magnet",
        source_uri="magnet:?xt=urn:btih:no-name",
        status="active",
        aria2_gid="gid-sync-active-full-no-name",
        total_bytes=7,
        completed_bytes=6,
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=7,
    )
    client = AsyncMock()

    await _update_v0_download_from_aria2(
        state=AppState(),
        client=client,
        download=download,
        status={
            "gid": "gid-sync-active-full-no-name",
            "status": "active",
            "totalLength": "7",
            "completedLength": "7",
            "infoHash": "abc123",
            "files": [
                {
                    "path": "magnet:?xt=urn:btih:no-name",
                    "length": "7",
                    "completedLength": "7",
                }
            ],
        },
    )

    updated = await _fetch_global(download["id"])
    updated_task = await _fetch_user_task(task["id"])
    usage = await get_usage(user["id"], quota_bytes=user["quota_bytes"])

    assert updated["status"] == "active"
    assert updated["completed_file_id"] is None
    assert updated["completed_bytes"] == 7
    assert updated_task["status"] == "active"
    assert updated_task["reserved_bytes"] == 7
    assert usage["reserved_bytes"] == 7
    client.remove_download_result.assert_not_awaited()


@pytest.mark.asyncio
async def test_active_full_bytes_with_verify_integrity_pending_stays_active(
    temp_db: str,
) -> None:
    user = await create_user_v0(username="sync_active_full_verify", quota_bytes=1000)
    await reserve_bytes(user["id"], 7, quota_bytes=user["quota_bytes"])
    download = await create_global_download_v0(
        resource_key="sync:active-full-verify",
        resource_kind="magnet",
        source_uri="magnet:?xt=urn:btih:verify",
        status="active",
        aria2_gid="gid-sync-active-full-verify",
        total_bytes=7,
        completed_bytes=6,
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=7,
    )
    task_dir = Path(settings.download_dir) / "downloading" / str(download["id"])
    task_dir.mkdir(parents=True)
    source_file = task_dir / "payload.bin"
    source_file.write_bytes(b"payload")
    client = AsyncMock()

    await _update_v0_download_from_aria2(
        state=AppState(),
        client=client,
        download=download,
        status={
            "gid": "gid-sync-active-full-verify",
            "status": "active",
            "totalLength": "7",
            "completedLength": "7",
            "infoHash": "abc123",
            "verifyIntegrityPending": "true",
            "bittorrent": {"info": {"name": "payload.bin"}},
            "files": [
                {"path": str(source_file), "length": "7", "completedLength": "7"}
            ],
        },
    )

    updated = await _fetch_global(download["id"])
    updated_task = await _fetch_user_task(task["id"])
    usage = await get_usage(user["id"], quota_bytes=user["quota_bytes"])

    assert updated["status"] == "active"
    assert updated["completed_file_id"] is None
    assert updated["completed_bytes"] == 7
    assert updated_task["status"] == "active"
    assert updated_task["reserved_bytes"] == 7
    assert usage["reserved_bytes"] == 7
    client.remove_download_result.assert_not_awaited()


@pytest.mark.asyncio
async def test_repair_inconsistent_completed_download_marks_failed_and_releases_reserved(
    temp_db: str,
) -> None:
    user = await create_user_v0(username="sync_repair_completed", quota_bytes=1000)
    await reserve_bytes(user["id"], 200, quota_bytes=user["quota_bytes"])
    download = await create_global_download_v0(
        resource_key="sync:repair-completed",
        status="completed",
        aria2_gid="gid-repair-completed",
        total_bytes=200,
        completed_bytes=200,
        completed_file_id=None,
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=200,
    )
    old_timestamp = now_ms() - 31_000
    async with transaction() as conn:
        await conn.execute(
            update(global_downloads)
            .where(global_downloads.c.id == download["id"])
            .values(updated_at_ms=old_timestamp)
        )

    await _repair_inconsistent_completed_downloads_v0()

    updated = await _fetch_global(download["id"])
    updated_task = await _fetch_user_task(task["id"])
    usage = await get_usage(user["id"], quota_bytes=user["quota_bytes"])

    assert updated["status"] == "failed"
    assert updated["error_code"] == "completion_not_indexed"
    assert updated["completed_file_id"] is None
    assert updated_task["status"] == "failed"
    assert updated_task["reserved_bytes"] == 0
    assert usage["reserved_bytes"] == 0


@pytest.mark.asyncio
async def test_sync_failure_cleanup_waits_for_completion_lock(
    temp_db: str,
) -> None:
    user = await create_user_v0(username="sync_failure_lock", quota_bytes=1000)
    await reserve_bytes(user["id"], 100, quota_bytes=user["quota_bytes"])
    download = await create_global_download_v0(
        resource_key="sync:failure-lock",
        status="active",
        aria2_gid="gid-sync-failure-lock",
        total_bytes=100,
        completed_bytes=10,
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=100,
    )
    state = AppState()
    completion_lock = await get_task_complete_lock(state, download["id"])
    await completion_lock.acquire()
    client = AsyncMock()
    client.force_remove.return_value = "OK"
    client.remove_download_result.return_value = "OK"

    failure_task = asyncio.create_task(
        _fail_v0_download_and_cleanup(
            state=state,
            client=client,
            download_id=download["id"],
            gid="gid-sync-failure-lock",
            message="sync failure",
            error_code="sync_failure",
            log_prefix="[Test]",
        )
    )
    try:
        await asyncio.sleep(0.05)
        in_flight = await _fetch_global(download["id"])
        in_flight_task = await _fetch_user_task(task["id"])

        assert in_flight["status"] == "active"
        assert in_flight_task["status"] == "active"
        client.force_remove.assert_not_awaited()
    finally:
        completion_lock.release()

    await failure_task

    updated = await _fetch_global(download["id"])
    updated_task = await _fetch_user_task(task["id"])
    usage = await get_usage(user["id"], quota_bytes=user["quota_bytes"])

    assert updated["status"] == "failed"
    assert updated["error_code"] == "sync_failure"
    assert updated_task["status"] == "failed"
    assert usage["reserved_bytes"] == 0
    client.force_remove.assert_awaited_once_with("gid-sync-failure-lock")


@pytest.mark.asyncio
async def test_sync_missing_gid_recovers_completed_file_from_disk(
    temp_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await create_user_v0(username="sync_missing_gid_recover", quota_bytes=1000)
    await reserve_bytes(user["id"], 7, quota_bytes=user["quota_bytes"])
    download = await create_global_download_v0(
        resource_key="sync:missing-gid-recover",
        status="active",
        aria2_gid="gid-sync-missing-recover",
        total_bytes=7,
        completed_bytes=7,
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=7,
    )
    task_dir = Path(settings.download_dir) / "downloading" / str(download["id"])
    task_dir.mkdir(parents=True)
    (task_dir / "payload.bin").write_bytes(b"payload")

    client = AsyncMock()
    client.tell_status.side_effect = RuntimeError(
        "GID gid-sync-missing-recover is not found"
    )
    client.tell_active.return_value = []
    client.tell_waiting.return_value = []
    client.tell_stopped.return_value = []
    client.force_remove.return_value = "OK"
    client.remove_download_result.return_value = "OK"

    def get_client(*args: object, **kwargs: object) -> AsyncMock:
        return client

    monkeypatch.setattr("app.core.state.get_aria2_client", get_client)

    async def stop_after_first_sleep(_interval: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr("app.aria2.sync.asyncio.sleep", stop_after_first_sleep)

    with pytest.raises(asyncio.CancelledError):
        await sync_tasks(AppState(), interval=0.01)

    updated = await _fetch_global(download["id"])
    updated_task = await _fetch_user_task(task["id"])
    usage = await get_usage(user["id"], quota_bytes=user["quota_bytes"])
    async with transaction() as conn:
        user_file_count = (
            await conn.execute(
                select(user_files).where(user_files.c.user_id == user["id"])
            )
        ).all()

    assert updated["status"] == "completed"
    assert updated["completed_file_id"] is not None
    assert updated_task["status"] == "completed"
    assert updated_task["reserved_bytes"] == 0
    assert usage["reserved_bytes"] == 0
    assert len(user_file_count) == 1
    client.remove_download_result.assert_awaited_once_with("gid-sync-missing-recover")
