from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select, update

from app.aria2.client import Aria2Client
from app.aria2.sync import (
    STALE_QUEUED_GRACE_SECONDS,
    _cleanup_owned_stopped_results,
    sync_tasks,
)
from app.domain.lifecycle import ReconcileResult
from app.services.lifecycle.cleanup import fail_download_and_reclaim
from app.services.lifecycle.completion import expected_completed_size
from app.services.lifecycle.coordinator import reconcile_attempt_signal
from app.services.lifecycle.repair import (
    cleanup_stale_queued_downloads_v0,
    repair_inconsistent_completed_downloads_v0,
)
from app.core.config import settings
from app.db.engine import transaction
from app.db.schema import global_downloads, stored_files, user_files, user_tasks
from app.core.time_utils import now_ms
from app.services.usage_service import get_usage, reserve_bytes
from app.services.storage import get_task_download_dir
from tests.fakes import make_aria2_client
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
async def test_unknown_aria2_gids_are_ignored_by_cleanup(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = make_aria2_client()
    client.tell_active.return_value = [{"gid": "foreign-active"}]
    client.tell_waiting.return_value = [{"gid": "foreign-waiting"}]
    client.tell_stopped.return_value = [{"gid": "foreign-stopped"}]

    await _cleanup_owned_stopped_results(
        client=client,
        removable_gids={"owned-stopped"},
        max_actions=10,
    )

    client.force_remove.assert_not_awaited()
    client.remove_download_result.assert_not_awaited()
    assert "foreign-" not in caplog.text


@pytest.mark.asyncio
async def test_owned_stopped_aria2_gid_result_is_removed() -> None:
    client = make_aria2_client()
    client.tell_stopped.return_value = [{"gid": "owned-stopped"}]

    await _cleanup_owned_stopped_results(
        client=client,
        removable_gids={"owned-stopped"},
        max_actions=10,
    )

    client.force_remove.assert_not_awaited()
    client.remove_download_result.assert_awaited_once_with("owned-stopped")


@pytest.mark.asyncio
async def test_tracked_stopped_gid_is_not_removed_without_cleanup_eligibility() -> None:
    client = make_aria2_client()
    client.tell_stopped.return_value = [{"gid": "owned-active"}]

    await _cleanup_owned_stopped_results(
        client=client,
        removable_gids=set(),
        max_actions=10,
    )

    client.remove_download_result.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_does_not_remove_metadata_result_while_handoff_pending(
    temp_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await create_user_v0(username="sync_metadata_cleanup_guard")
    download = await create_global_download_v0(
        resource_key="sync:metadata-cleanup-guard",
        resource_kind="magnet",
        source_uri="magnet:?xt=urn:btih:cleanupguard",
        status="active",
        aria2_gid="gid-metadata",
        display_name="Real Torrent",
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
    )
    client = make_aria2_client(
        tell_status={
            "gid": "gid-metadata",
            "status": "complete",
            "totalLength": "9",
            "completedLength": "9",
            "bittorrent": {"info": {"name": "Real Torrent"}},
            "files": [{"path": "/downloads/Real Torrent/movie.mkv", "length": "9"}],
        },
    )
    client.tell_stopped.return_value = [{"gid": "gid-metadata"}]

    def get_client(*args: object, **kwargs: object) -> AsyncMock:
        return client

    monkeypatch.setattr("app.aria2.sync.get_aria2_client", get_client)
    monkeypatch.setattr(
        "app.services.lifecycle.handoff.COMPLETE_SOURCE_RETRY_COUNT", 1
    )

    async def stop_after_first_sleep(_interval: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr("app.aria2.sync.asyncio.sleep", stop_after_first_sleep)

    with pytest.raises(asyncio.CancelledError):
        await sync_tasks(interval=0.01)

    updated = await _fetch_global(download["id"])
    updated_task = await _fetch_user_task(task["id"])

    assert updated["aria2_gid"] == "gid-metadata"
    assert updated["status"] == "active"
    assert updated["completed_file_id"] is None
    assert updated_task["status"] == "active"
    # M3: reconcile may return COMPLETED for a deferred metadata handoff,
    # which adds the GID to removable_gids.  The key invariant is that
    # no metadata file was indexed and the download stays live.
    async with transaction() as conn:
        stored_count = (
            await conn.execute(select(func.count()).select_from(stored_files))
        ).scalar_one()
    assert stored_count == 0


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

    await cleanup_stale_queued_downloads_v0()

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
    client = make_aria2_client()

    changed = await reconcile_attempt_signal(
        backend=client,
        observed_gid=str(download["aria2_gid"]),
        event=None,
        observed_status={
            "gid": "gid-sync-active",
            "status": "active",
            "totalLength": "1000",
            "completedLength": "250",
            "downloadSpeed": "10",
            "files": [{"path": "/tmp/sync-active.bin"}],
        },
        log_prefix="[Sync]",
    )

    updated = await _fetch_global(download["id"])
    updated_task = await _fetch_user_task(task["id"])

    assert updated["status"] == "active"
    assert updated["total_bytes"] == 1000
    assert updated["completed_bytes"] == 250
    assert updated_task["status"] == "active"


@pytest.mark.asyncio
async def test_sync_progress_only_size_waits_unknown_download(temp_db: str) -> None:
    """§3.3.1: live active + totalLength=0 waits; does not hard-kill."""
    user = await create_user_v0(username="sync_progress_only", quota_bytes=1000)
    download = await create_global_download_v0(
        resource_key="sync:progress-only", resource_kind="http",
        status="active", aria2_gid="gid-sync-progress-only",
        total_bytes=0, size_known=False,
    )
    task = await create_user_task_v0(
        user_id=user["id"], global_download_id=download["id"], status="active"
    )
    client = make_aria2_client()

    changed = await reconcile_attempt_signal(
        backend=client,
        observed_gid=str(download["aria2_gid"]),
        event=None,
        observed_status={
            "gid": "gid-sync-progress-only", "status": "active",
            "totalLength": "0", "completedLength": "123", "files": [],
        },
        log_prefix="[Sync]",
    )

    updated = await _fetch_global(download["id"])
    updated_task = await _fetch_user_task(task["id"])
    usage = await get_usage(user["id"], user["quota_bytes"])
    assert changed == ReconcileResult.WAITING
    assert updated["status"] != "failed"
    assert updated["error_code"] != "unknown_size"
    assert updated["aria2_gid"] == "gid-sync-progress-only"
    assert updated["size_known"] == 0
    assert updated_task["status"] == "active"
    client.pause.assert_not_awaited()
    client.unpause.assert_not_awaited()
    client.force_remove.assert_not_awaited()
    assert usage["reserved_bytes"] == 0


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
    client = make_aria2_client()

    await reconcile_attempt_signal(
        backend=client,
        observed_gid=str(download["aria2_gid"]),
        event=None,
        observed_status={
            "gid": "gid-sync-error",
            "status": "error",
            "errorCode": "7",
            "errorMessage": "network error",
            "totalLength": "400",
            "completedLength": "11",
            "files": [],
        },
        log_prefix="[Sync]",
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
    client = make_aria2_client()
    raw_error = "CUID#12 - Download aborted. URI=https://example.com/file.iso"

    await reconcile_attempt_signal(
        backend=client,
        observed_gid=str(download["aria2_gid"]),
        event=None,
        observed_status={
            "gid": "gid-sync-specific-error",
            "status": "error",
            "errorCode": "7",
            "errorMessage": raw_error,
            "totalLength": "100",
            "completedLength": "1",
            "files": [],
        },
        log_prefix="[Sync]",
    )

    updated = await _fetch_global(download["id"])

    assert updated["status"] == "failed"
    assert updated["error_message"] == f"aria2: {raw_error}"


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
    client = make_aria2_client(
        tell_status={
            "gid": "gid-real",
            "status": "active",
            "totalLength": "2048",
            "completedLength": "512",
            "bittorrent": {"info": {"name": "Real Torrent"}},
            "files": [{"path": str(get_task_download_dir(download["id"]) / "Real Torrent" / "file.bin"), "length": "2048"}],
        },
    )

    await reconcile_attempt_signal(
        backend=client,
        observed_gid=str(download["aria2_gid"]),
        event=None,
        observed_status={
            "gid": "gid-metadata",
            "status": "complete",
            "followedBy": ["gid-real"],
            "totalLength": "0",
            "completedLength": "0",
            "files": [],
        },
        log_prefix="[Sync]",
    )

    updated = await _fetch_global(download["id"])

    assert updated["aria2_gid"] == "gid-real"
    assert updated["status"] == "active"
    assert updated["error_code"] is None
    assert updated["display_name"] == "Real Torrent"
    assert updated["total_bytes"] == 2048
    assert updated["completed_bytes"] == 512
    # Handoff fetch + M9 unpause re-query may both tell_status payload gid.
    assert client.tell_status.await_count >= 1
    assert all(
        call.args[0] == "gid-real" for call in client.tell_status.await_args_list
    )


@pytest.mark.asyncio
async def test_metadata_followed_by_preserves_real_waiting_status(
    temp_db: str,
) -> None:
    user = await create_user_v0(username="sync_followed_waiting")
    download = await create_global_download_v0(
        resource_key="sync:followed-waiting",
        resource_kind="magnet",
        source_uri="magnet:?xt=urn:btih:followedwaiting",
        status="active",
        aria2_gid="gid-metadata-waiting",
        display_name="magnet:?xt=urn:btih:followedwaiting",
        total_bytes=0,
        completed_bytes=0,
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        display_name="magnet:?xt=urn:btih:followedwaiting",
    )
    client = make_aria2_client(
        tell_status={
            "gid": "gid-real-waiting",
            "status": "waiting",
            "totalLength": "2048",
            "completedLength": "0",
            "bittorrent": {"info": {"name": "Waiting Torrent"}},
            "files": [{"path": str(get_task_download_dir(download["id"]) / "Waiting Torrent" / "file.bin"), "length": "2048"}],
        },
    )

    await reconcile_attempt_signal(
        backend=client,
        observed_gid=str(download["aria2_gid"]),
        event=None,
        observed_status={
            "gid": "gid-metadata-waiting",
            "status": "complete",
            "followedBy": ["gid-real-waiting"],
            "totalLength": "0",
            "completedLength": "0",
            "files": [],
        },
        log_prefix="[Sync]",
    )

    updated = await _fetch_global(download["id"])
    updated_task = await _fetch_user_task(task["id"])

    assert updated["aria2_gid"] == "gid-real-waiting"
    assert updated["status"] == "waiting"
    assert updated_task["status"] == "waiting"


@pytest.mark.asyncio
async def test_metadata_followed_by_complete_real_status_is_indexed(
    temp_db: str,
) -> None:
    user = await create_user_v0(username="sync_followed_complete")
    await reserve_bytes(user["id"], 5, quota_bytes=user["quota_bytes"])
    download = await create_global_download_v0(
        resource_key="sync:followed-complete",
        resource_kind="magnet",
        source_uri="magnet:?xt=urn:btih:followedcomplete",
        status="active",
        aria2_gid="gid-metadata-complete",
        display_name="magnet:?xt=urn:btih:followedcomplete",
        total_bytes=0,
        completed_bytes=0,
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=5,
        display_name="magnet:?xt=urn:btih:followedcomplete",
    )
    task_dir = get_task_download_dir(download["id"])
    task_dir.mkdir(parents=True, exist_ok=True)
    source_file = task_dir / "payload.bin"
    source_file.write_bytes(b"abcde")

    client = make_aria2_client(
        tell_status={
            "gid": "gid-real-complete",
            "status": "complete",
            "following": "gid-metadata-complete",
            "totalLength": "5",
            "completedLength": "5",
            "bittorrent": {"info": {"name": "payload.bin"}},
            "files": [{"path": str(source_file), "length": "5"}],
        },
    )

    await reconcile_attempt_signal(
        backend=client,
        observed_gid=str(download["aria2_gid"]),
        event=None,
        observed_status={
            "gid": "gid-metadata-complete",
            "status": "complete",
            "followedBy": ["gid-real-complete"],
            "totalLength": "0",
            "completedLength": "0",
            "files": [],
        },
        log_prefix="[Sync]",
    )

    updated = await _fetch_global(download["id"])
    updated_task = await _fetch_user_task(task["id"])
    usage = await get_usage(user["id"], quota_bytes=user["quota_bytes"])
    async with transaction() as conn:
        stored_count = (
            await conn.execute(select(func.count()).select_from(stored_files))
        ).scalar_one()
        user_file_count = (
            await conn.execute(select(func.count()).select_from(user_files))
        ).scalar_one()

    assert updated["status"] == "completed"
    assert updated["completed_file_id"] is not None
    assert updated["aria2_gid"] is None
    assert updated_task["status"] == "completed"
    assert updated_task["reserved_bytes"] == 0
    assert usage["reserved_bytes"] == 0
    assert stored_count == 1
    assert user_file_count == 1


@pytest.mark.asyncio
async def test_torrent_synthetic_task_name_is_replaced_with_real_name(
    temp_db: str,
) -> None:
    user = await create_user_v0(username="sync_torrent_placeholder")
    resource_key = "1234567890abcdef"
    download = await create_global_download_v0(
        resource_key=resource_key,
        resource_kind="torrent",
        source_uri=f"magnet:?xt=urn:btih:{resource_key}",
        status="active",
        aria2_gid="gid-torrent-placeholder",
        display_name=f"torrent-{resource_key[:12]}",
        total_bytes=0,
        completed_bytes=0,
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        display_name=f"torrent-{resource_key[:12]}",
    )
    client = make_aria2_client()

    await reconcile_attempt_signal(
        backend=client,
        observed_gid=str(download["aria2_gid"]),
        event=None,
        observed_status={
            "gid": "gid-torrent-placeholder",
            "status": "active",
            "totalLength": "2048",
            "completedLength": "512",
            "bittorrent": {"info": {"name": "Real Torrent"}},
            "files": [{"path": str(get_task_download_dir(download["id"]) / "Real Torrent" / "file.bin"), "length": "2048"}],
        },
        log_prefix="[Sync]",
    )

    updated_task = await _fetch_user_task(task["id"])

    assert updated_task["display_name"] == "Real Torrent"


@pytest.mark.asyncio
async def test_torrent_prefixed_user_task_name_is_not_overwritten_for_http_download(
    temp_db: str,
) -> None:
    user = await create_user_v0(username="sync_torrent_prefix_http")
    download = await create_global_download_v0(
        resource_key="sync:http-torrent-prefix",
        resource_kind="http",
        source_uri="https://example.com/torrent-release.iso",
        status="active",
        aria2_gid="gid-http-torrent-prefix",
        display_name="torrent-release.iso",
        total_bytes=0,
        completed_bytes=0,
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        display_name="torrent-release.iso",
    )
    client = make_aria2_client()

    await reconcile_attempt_signal(
        backend=client,
        observed_gid=str(download["aria2_gid"]),
        event=None,
        observed_status={
            "gid": "gid-http-torrent-prefix",
            "status": "active",
            "totalLength": "2048",
            "completedLength": "512",
            "files": [{"path": "/downloads/renamed-by-server.iso", "length": "2048"}],
        },
        log_prefix="[Sync]",
    )

    updated = await _fetch_global(download["id"])
    updated_task = await _fetch_user_task(task["id"])

    assert updated["display_name"] == "renamed-by-server.iso"
    assert updated_task["display_name"] == "torrent-release.iso"


@pytest.mark.asyncio
async def test_http_download_ignores_noisy_bittorrent_name_without_live_evidence(
    temp_db: str,
) -> None:
    user = await create_user_v0(username="sync_http_bt_noise")
    download = await create_global_download_v0(
        resource_key="sync:http-bt-noise",
        resource_kind="http",
        source_uri="https://example.com/plain.bin",
        status="active",
        aria2_gid="gid-http-bt-noise",
        display_name="plain.bin",
        total_bytes=0,
        completed_bytes=0,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        display_name="plain.bin",
    )
    client = make_aria2_client()

    await reconcile_attempt_signal(
        backend=client,
        observed_gid=str(download["aria2_gid"]),
        event=None,
        observed_status={
            "gid": "gid-http-bt-noise",
            "status": "active",
            "totalLength": "2048",
            "completedLength": "512",
            "bittorrent": {"info": {"name": "Noisy Torrent Name"}},
            "files": [{"path": "/downloads/plain.bin", "length": "2048"}],
        },
        log_prefix="[Sync]",
    )

    updated = await _fetch_global(download["id"])

    assert updated["resource_kind"] == "http"
    assert updated["display_name"] == "plain.bin"
    assert updated["total_bytes"] == 2048
    assert updated["completed_bytes"] == 512


@pytest.mark.asyncio
async def test_http_torrent_live_infohash_stays_http_without_followed_by(
    temp_db: str,
) -> None:
    """Without explicit followedBy, infoHash alone does not upgrade resource_kind."""
    user = await create_user_v0(username="sync_http_torrent_upgrade")
    info_hash = "0123456789abcdef0123456789abcdef01234567"
    download = await create_global_download_v0(
        resource_key="sync:http-torrent-upgrade",
        resource_kind="http",
        source_uri="https://example.com/payload.torrent",
        status="active",
        aria2_gid="gid-http-torrent-upgrade",
        display_name="payload.torrent",
        total_bytes=0,
        completed_bytes=0,
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        display_name="payload.torrent",
    )
    client = make_aria2_client()

    await reconcile_attempt_signal(
        backend=client,
        observed_gid=str(download["aria2_gid"]),
        event=None,
        observed_status={
            "gid": "gid-http-torrent-upgrade",
            "status": "active",
            "totalLength": "4096",
            "completedLength": "1024",
            "infoHash": info_hash,
            "bittorrent": {"info": {"name": "Real Torrent"}},
            "files": [{"path": str(get_task_download_dir(download["id"]) / "Real Torrent" / "file.bin")}],
        },
        log_prefix="[Sync]",
    )

    updated = await _fetch_global(download["id"])
    updated_task = await _fetch_user_task(task["id"])

    # M3: without followedBy, infoHash alone does not upgrade resource_kind.
    assert updated["resource_kind"] == "http"
    assert updated["display_name"] == "Real Torrent"
    assert updated["total_bytes"] == 4096
    assert updated["completed_bytes"] == 1024
    assert updated_task["display_name"] == "payload.torrent"


@pytest.mark.asyncio
async def test_http_torrent_followed_by_handoff_upgrades_resource_kind(
    temp_db: str,
) -> None:
    user = await create_user_v0(username="sync_http_followed_upgrade")
    info_hash = "abcdefabcdefabcdefabcdefabcdefabcdefabcd"
    download = await create_global_download_v0(
        resource_key="sync:http-followed-upgrade",
        resource_kind="http",
        source_uri="https://example.com/payload.torrent",
        status="active",
        aria2_gid="gid-http-metadata",
        display_name="payload.torrent",
        total_bytes=0,
        completed_bytes=0,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        display_name="payload.torrent",
    )
    client = make_aria2_client(
        tell_status={
            "gid": "gid-real-bt",
            "status": "active",
            "totalLength": "8192",
            "completedLength": "2048",
            "infoHash": info_hash,
            "bittorrent": {"info": {"name": "Real Followed Torrent"}},
            "files": [{"path": "/downloads/Real Followed Torrent/file.bin"}],
        },
    )

    await reconcile_attempt_signal(
        backend=client,
        observed_gid=str(download["aria2_gid"]),
        event=None,
        observed_status={
            "gid": "gid-http-metadata",
            "status": "complete",
            "followedBy": ["gid-real-bt"],
            "totalLength": "0",
            "completedLength": "0",
            "files": [],
        },
        log_prefix="[Sync]",
    )

    updated = await _fetch_global(download["id"])

    assert updated["aria2_gid"] == "gid-real-bt"
    assert updated["resource_kind"] == "torrent"
    assert updated["resource_key"] == "sync:http-followed-upgrade"
    assert updated["bt_info_hash"] == info_hash
    assert updated["display_name"] == "Real Followed Torrent"
    assert updated["total_bytes"] == 8192
    assert updated["completed_bytes"] == 2048


@pytest.mark.asyncio
async def test_metadata_completion_retries_for_late_followed_by_before_file_validation(
    temp_db: str,
) -> None:
    user = await create_user_v0(username="sync_late_followed_by", quota_bytes=5000)
    await reserve_bytes(user["id"], 9, quota_bytes=user["quota_bytes"])
    download = await create_global_download_v0(
        resource_key="sync:late-followed-by",
        resource_kind="magnet",
        source_uri="magnet:?xt=urn:btih:late-followed",
        status="active",
        aria2_gid="gid-metadata",
        display_name="magnet:?xt=urn:btih:late-followed",
        total_bytes=9,
        completed_bytes=9,
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=9,
    )
    task_dir = get_task_download_dir(download["id"])
    task_dir.mkdir(parents=True, exist_ok=True)
    metadata_file = task_dir / "metadata"
    metadata_file.write_bytes(b"short")
    payload_active = {
        "gid": "gid-real",
        "status": "active",
        "totalLength": "2048",
        "completedLength": "256",
        "bittorrent": {"info": {"name": "Real Torrent"}},
        "files": [
            {
                "path": str(
                    get_task_download_dir(download["id"])
                    / "Real Torrent"
                    / "file.bin"
                ),
                "length": "2048",
            }
        ],
    }
    client = make_aria2_client(
        tell_status=[
            {
                "gid": "gid-metadata",
                "status": "complete",
                "followedBy": ["gid-real"],
                "totalLength": "9",
                "completedLength": "9",
                "files": [
                    {
                        "path": str(metadata_file),
                        "length": "9",
                        "completedLength": "9",
                    }
                ],
            },
            payload_active,
            payload_active,
            payload_active,
        ],
    )

    await reconcile_attempt_signal(
        backend=client,
        observed_gid=str(download["aria2_gid"]),
        event=None,
        observed_status={
            "gid": "gid-metadata",
            "status": "complete",
            "totalLength": "9",
            "completedLength": "9",
            "files": [
                {
                    "path": str(metadata_file),
                    "length": "9",
                    "completedLength": "9",
                }
            ],
        },
        log_prefix="[Sync]",
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

    assert updated["aria2_gid"] == "gid-real"
    assert updated["status"] == "active"
    assert updated["error_code"] is None
    assert updated["display_name"] == "Real Torrent"
    assert updated["total_bytes"] == 2048
    assert updated["completed_bytes"] == 256
    assert updated_task["status"] == "active"
    assert updated_task["reserved_bytes"] == 2048
    assert usage["reserved_bytes"] == 2048
    assert len(user_file_count) == 0


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
    client = make_aria2_client()

    await reconcile_attempt_signal(
        backend=client,
        observed_gid=str(download["aria2_gid"]),
        event=None,
        observed_status={
            "gid": "gid-sync-terminal",
            "status": "active",
            "totalLength": "1000",
            "completedLength": "250",
            "files": [{"path": "/tmp/stale-active.bin"}],
        },
        log_prefix="[Sync]",
    )

    updated = await _fetch_global(download["id"])
    updated_task = await _fetch_user_task(task["id"])

    assert updated["status"] == "completed"
    assert updated["total_bytes"] == 4
    assert updated["completed_bytes"] == 4
    assert updated["completed_file_id"] == user_file["stored_file_id"]
    assert updated_task["status"] == "completed"


@pytest.mark.asyncio
async def test_active_bt_status_with_full_bytes_stays_active(
    temp_db: str,
) -> None:
    """Active+full bytes does NOT complete; only explicit complete status does."""
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
    task_dir = get_task_download_dir(download["id"])
    task_dir.mkdir(parents=True, exist_ok=True)
    source_file = task_dir / "payload.bin"
    source_file.write_bytes(b"payload")
    client = make_aria2_client()

    await reconcile_attempt_signal(
        backend=client,
        observed_gid=str(download["aria2_gid"]),
        event=None,
        observed_status={
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
        log_prefix="[Sync]",
    )

    updated = await _fetch_global(download["id"])
    updated_task = await _fetch_user_task(task["id"])
    usage = await get_usage(user["id"], quota_bytes=user["quota_bytes"])

    # M3: active+full bytes stays active, no indexing.
    assert updated["status"] == "active"
    assert updated["completed_file_id"] is None
    assert updated["completed_bytes"] == 7
    assert updated_task["status"] == "active"
    assert updated_task["reserved_bytes"] == 7
    assert usage["reserved_bytes"] == 7
    client.remove_download_result.assert_not_awaited()


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
    client = make_aria2_client()

    await reconcile_attempt_signal(
        backend=client,
        observed_gid=str(download["aria2_gid"]),
        event=None,
        observed_status={
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
        log_prefix="[Sync]",
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
    task_dir = get_task_download_dir(download["id"])
    task_dir.mkdir(parents=True, exist_ok=True)
    source_file = task_dir / "payload.bin"
    source_file.write_bytes(b"payload")
    client = make_aria2_client()

    await reconcile_attempt_signal(
        backend=client,
        observed_gid=str(download["aria2_gid"]),
        event=None,
        observed_status={
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
        log_prefix="[Sync]",
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_client = AsyncMock(spec=Aria2Client)
    raw_client.force_remove.return_value = "OK"
    raw_client.remove_download_result.return_value = "OK"
    monkeypatch.setattr("app.aria2.gateway.get_aria2_client", lambda: raw_client)

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

    await repair_inconsistent_completed_downloads_v0()

    updated = await _fetch_global(download["id"])
    updated_task = await _fetch_user_task(task["id"])
    usage = await get_usage(user["id"], quota_bytes=user["quota_bytes"])

    assert updated["status"] == "failed"
    assert updated["error_code"] == "completion_not_indexed"
    assert updated["completed_file_id"] is None
    assert updated_task["status"] == "failed"
    assert updated_task["reserved_bytes"] == 0
    assert usage["reserved_bytes"] == 0
    raw_client.force_remove.assert_awaited_once_with("gid-repair-completed")
    raw_client.remove_download_result.assert_awaited_once_with("gid-repair-completed")
    assert not hasattr(raw_client, "force_remove_gid")


@pytest.mark.asyncio
async def test_sync_failure_cleanup_fails_immediately(
    temp_db: str,
) -> None:
    """Completion lock is deprecated; failure proceeds immediately."""
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
    client = make_aria2_client()

    changed = await fail_download_and_reclaim(
        backend=client,
        download_id=download["id"],
        expected_gid="gid-sync-failure-lock",
        writer_gid="gid-sync-failure-lock",
        message="sync failure",
        error_code="sync_failure",
        log_prefix="[Test]",
    )

    updated = await _fetch_global(download["id"])
    updated_task = await _fetch_user_task(task["id"])
    usage = await get_usage(user["id"], quota_bytes=user["quota_bytes"])

    assert changed is True
    assert updated["status"] == "failed"
    assert updated["error_code"] == "sync_failure"
    assert updated_task["status"] == "failed"
    assert usage["reserved_bytes"] == 0
    client.force_remove.assert_awaited_once_with("gid-sync-failure-lock")


@pytest.mark.asyncio
async def test_sync_missing_gid_known_size_fails_with_gid_missing(
    temp_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sync no longer recovers from disk; known-size missing GID → failed."""
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
    task_dir = get_task_download_dir(download["id"])
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "payload.bin").write_bytes(b"payload")

    client = make_aria2_client(
        tell_status=RuntimeError(
            "GID gid-sync-missing-recover is not found"
        ),
    )

    def get_client(*args: object, **kwargs: object) -> AsyncMock:
        return client

    monkeypatch.setattr("app.aria2.sync.get_aria2_client", get_client)

    async def stop_after_first_sleep(_interval: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr("app.aria2.sync.asyncio.sleep", stop_after_first_sleep)

    with pytest.raises(asyncio.CancelledError):
        await sync_tasks(interval=0.01)

    updated = await _fetch_global(download["id"])
    updated_task = await _fetch_user_task(task["id"])
    usage = await get_usage(user["id"], quota_bytes=user["quota_bytes"])

    assert updated["status"] == "failed"
    assert updated["error_code"] == "gid_missing"
    assert updated["completed_file_id"] is None
    assert updated_task["status"] == "failed"
    assert usage["reserved_bytes"] == 0


@pytest.mark.asyncio
async def test_missing_gid_with_unknown_size_fails_without_indexing(
    temp_db: str,
) -> None:
    user = await create_user_v0(username="sync_missing_gid_unknown", quota_bytes=1000)
    download = await create_global_download_v0(
        resource_key="sync:missing-gid-unknown",
        status="active",
        aria2_gid="gid-sync-missing-unknown",
        total_bytes=0,
        completed_bytes=0,
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
    )
    task_dir = get_task_download_dir(download["id"])
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "unknown.bin").write_bytes(b"unknown payload")
    client = make_aria2_client()

    await reconcile_attempt_signal(
        backend=client,
        observed_gid="gid-sync-missing-unknown",
        event=None,
        observed_status=None,
        observed_error=Exception("Active Resource not found: gid#gid-sync-missing-unknown"),
        log_prefix="[Sync]",
    )

    updated = await _fetch_global(download["id"])
    updated_task = await _fetch_user_task(task["id"])
    async with transaction() as conn:
        stored_count = (
            await conn.execute(select(func.count()).select_from(stored_files))
        ).scalar_one()
        user_file_count = (
            await conn.execute(select(func.count()).select_from(user_files))
        ).scalar_one()
    assert updated["status"] == "failed"
    assert updated["error_code"] == "unknown_size"
    assert updated["completed_file_id"] is None
    assert updated_task["status"] == "failed"
    assert stored_count == 0
    assert user_file_count == 0


def test_expected_completed_size_ignores_unselected_torrent_files(
    tmp_path: Path,
) -> None:
    status = {
        "totalLength": "104",
        "files": [
            {"path": "selected.bin", "length": "4", "selected": "true"},
            {"path": "ignored.bin", "length": "100", "selected": False},
        ],
    }

    assert expected_completed_size(status, tmp_path) == 4
    assert expected_completed_size(
        {
            "totalLength": "100",
            "files": [
                {"path": "ignored.bin", "length": "100", "selected": "false"}
            ],
        },
        tmp_path,
    ) == 0


@pytest.mark.asyncio
async def test_sync_recovers_after_first_round_exception(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    repair = AsyncMock(side_effect=[RuntimeError("first round failed"), None])
    list_downloads = AsyncMock(return_value=[])
    cleanup_stale = AsyncMock()
    client = make_aria2_client()
    monkeypatch.setattr(
        "app.aria2.sync.repair.repair_inconsistent_completed_downloads_v0",
        repair,
    )
    monkeypatch.setattr(
        "app.aria2.sync.list_v0_tracked_downloads",
        list_downloads,
    )
    monkeypatch.setattr(
        "app.aria2.sync.repair.cleanup_stale_queued_downloads_v0",
        cleanup_stale,
    )
    monkeypatch.setattr("app.aria2.sync.get_aria2_client", lambda: client)
    sleeps = 0

    async def stop_after_second_round(_interval: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr("app.aria2.sync.asyncio.sleep", stop_after_second_round)
    with caplog.at_level(logging.ERROR, logger="app.aria2.sync"):
        with pytest.raises(asyncio.CancelledError):
            await sync_tasks(interval=0.01)

    assert repair.await_count == 2
    list_downloads.assert_awaited_once()
    cleanup_stale.assert_awaited_once()
    assert "Synchronization round failed" in caplog.text


@pytest.mark.asyncio
async def test_sync_round_writes_live_snapshot_for_active_download(
    temp_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """速度回归：sync 轮询必须刷新观测仓（速度/进度的读模型）。"""
    from app.modules.task_core import observation_store

    user = await create_user_v0(username="sync-snap-user")
    download = await create_global_download_v0(
        resource_key="rk-sync-snap",
        source_uri="http://example.com/snap.bin",
        resource_kind="http",
        status="active",
        aria2_gid="gid-snap",
        total_bytes=4096,
        completed_bytes=0,
        size_known=True,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=int(download["id"]),
        status="active",
        reserved_bytes=4096,
    )

    client = make_aria2_client(
        tell_status={
            "gid": "gid-snap",
            "status": "active",
            "totalLength": "4096",
            "completedLength": "1024",
            "downloadSpeed": "234567",
            "uploadSpeed": "0",
            "files": [],
        }
    )
    client.tell_stopped.return_value = []

    def get_client(*args: object, **kwargs: object) -> object:
        return client

    monkeypatch.setattr("app.aria2.sync.get_aria2_client", get_client)

    async def stop_after_first_sleep(_interval: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr("app.aria2.sync.asyncio.sleep", stop_after_first_sleep)

    with pytest.raises(asyncio.CancelledError):
        await sync_tasks(interval=0.01)

    entry = observation_store.get_observed_detail(int(download["id"]))
    assert entry is not None, "sync 轮询未写入 observation_store"
    assert entry.sanitized["downloadSpeed"] == "234567"
    assert entry.sanitized["totalLength"] == "4096"
    assert entry.sanitized["completedLength"] == "1024"
