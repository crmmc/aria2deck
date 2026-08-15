from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select, update

from app.aria2.listener import handle_aria2_event
from app.core.config import settings
from app.db.engine import transaction
from app.db.schema import global_downloads, stored_files, user_files, user_tasks
from app.repositories.task.user_tasks import mark_global_download_failed
from app.services.lifecycle import completion as completion_mod
from app.services.usage_service import get_usage, reserve_bytes
from app.services.storage import get_task_download_dir
from tests.fakes import make_aria2_client
from tests.helpers_v0 import (
    create_global_download_v0,
    create_user_file_v0,
    create_user_task_v0,
    create_user_v0,
)


def _patch_aria2_client(monkeypatch: pytest.MonkeyPatch, client: AsyncMock) -> None:
    def get_client(*args: object, **kwargs: object) -> AsyncMock:
        return client

    monkeypatch.setattr("app.aria2.listener.get_aria2_client", get_client)


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
async def test_listener_progress_only_size_waits_unknown_download(
    temp_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§3.3.1: live active + totalLength=0 waits; does not hard-kill."""
    user = await create_user_v0(username="listener_progress_only", quota_bytes=1000)
    download = await create_global_download_v0(
        resource_key="listener:progress-only", resource_kind="http",
        status="active", aria2_gid="gid-listener-progress-only",
        total_bytes=0, size_known=False,
    )
    task = await create_user_task_v0(
        user_id=user["id"], global_download_id=download["id"], status="active"
    )
    client = make_aria2_client(tell_status={
        "gid": "gid-listener-progress-only", "status": "active",
        "totalLength": "0", "completedLength": "123", "files": [],
    })
    _patch_aria2_client(monkeypatch, client)

    await handle_aria2_event("gid-listener-progress-only", "start")

    updated = await _fetch_global(download["id"])
    updated_task = await _fetch_user_task(task["id"])
    usage = await get_usage(user["id"], user["quota_bytes"])
    assert updated["status"] != "failed"
    assert updated["error_code"] != "unknown_size"
    assert updated["aria2_gid"] == "gid-listener-progress-only"
    assert updated["size_known"] == 0
    assert updated_task["status"] == "active"
    assert usage["reserved_bytes"] == 0
    client.pause.assert_not_awaited()
    client.unpause.assert_not_awaited()
    client.force_remove.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_completion_creates_one_stored_file_and_user_file_per_active_task(
    temp_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_a = await create_user_v0(username="listener_complete_a", quota_bytes=1000)
    user_b = await create_user_v0(username="listener_complete_b", quota_bytes=1000)
    download = await create_global_download_v0(
        resource_key="listener:complete",
        resource_kind="http",
        source_uri="https://example.com/payload.bin",
        status="active",
        aria2_gid="gid-listener-complete",
        display_name="payload.bin",
        total_bytes=7,
    )
    await create_user_task_v0(
        user_id=user_a["id"],
        global_download_id=download["id"],
        status="active",
        display_name="payload-a.bin",
    )
    await create_user_task_v0(
        user_id=user_b["id"],
        global_download_id=download["id"],
        status="active",
        display_name="payload-b.bin",
    )

    task_dir = get_task_download_dir(download["id"])
    task_dir.mkdir(parents=True, exist_ok=True)
    source_file = task_dir / "payload.bin"
    source_file.write_bytes(b"payload")

    client = make_aria2_client(tell_status={
        "gid": "gid-listener-complete",
        "status": "complete",
        "totalLength": "7",
        "completedLength": "7",
        "files": [{"path": str(source_file)}],
    })
    _patch_aria2_client(monkeypatch, client)

    await asyncio.gather(
        handle_aria2_event("gid-listener-complete", "complete"),
        handle_aria2_event("gid-listener-complete", "complete"),
    )

    updated = await _fetch_global(download["id"])
    async with transaction() as conn:
        stored_count = (
            await conn.execute(select(func.count()).select_from(stored_files))
        ).scalar_one()
        user_file_rows = (
            (
                await conn.execute(
                    select(user_files).where(
                        user_files.c.stored_file_id == updated["completed_file_id"]
                    )
                )
            )
            .mappings()
            .all()
        )

    assert updated["status"] == "completed"
    assert stored_count == 1
    assert len(user_file_rows) == 2
    assert {row["user_id"] for row in user_file_rows} == {user_a["id"], user_b["id"]}


@pytest.mark.asyncio
async def test_completion_with_followed_by_changes_gid_without_creating_files(
    temp_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await create_user_v0(username="listener_followed_by")
    download = await create_global_download_v0(
        resource_key="listener:followed-by",
        status="active",
        aria2_gid="gid-metadata",
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
    )
    client = make_aria2_client(tell_status=[
        {
            "gid": "gid-metadata",
            "status": "complete",
            "followedBy": ["gid-real"],
            "totalLength": "0",
            "completedLength": "0",
            "files": [],
        },
        {
            "gid": "gid-real",
            "status": "paused",
            "totalLength": "2048",
            "completedLength": "0",
            "files": [{"path": str(get_task_download_dir(download["id"]) / "file.bin"), "length": "2048"}],
            "bittorrent": {"info": {"name": "payload"}},
        },
    ])
    _patch_aria2_client(monkeypatch, client)

    await handle_aria2_event("gid-metadata", "complete")

    updated = await _fetch_global(download["id"])
    updated_task = await _fetch_user_task(task["id"])
    async with transaction() as conn:
        stored_count = (
            await conn.execute(select(func.count()).select_from(stored_files))
        ).scalar_one()
        user_file_count = (
            await conn.execute(select(func.count()).select_from(user_files))
        ).scalar_one()

    assert updated["aria2_gid"] == "gid-real"
    assert updated["status"] == "active"
    assert updated_task["status"] == "active"
    assert stored_count == 0
    assert user_file_count == 0


@pytest.mark.asyncio
async def test_event_for_followed_task_uses_following_to_update_original_gid(
    temp_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await create_user_v0(username="listener_following")
    download = await create_global_download_v0(
        resource_key="listener:following",
        resource_kind="magnet",
        source_uri="magnet:?xt=urn:btih:following",
        status="waiting",
        aria2_gid="gid-metadata",
        display_name="magnet:?xt=urn:btih:following",
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="waiting",
    )
    client = make_aria2_client(tell_status={
        "gid": "gid-real",
        "status": "active",
        "following": "gid-metadata",
        "totalLength": "4096",
        "completedLength": "1024",
        "bittorrent": {"info": {"name": "Real Torrent"}},
        "files": [{"path": str(get_task_download_dir(download["id"]) / "Real Torrent" / "file.bin"), "length": "4096"}],
    })
    _patch_aria2_client(monkeypatch, client)

    await handle_aria2_event("gid-real", "start")

    updated = await _fetch_global(download["id"])
    updated_task = await _fetch_user_task(task["id"])

    assert updated["aria2_gid"] == "gid-real"
    assert updated["status"] == "active"
    assert updated["display_name"] == "Real Torrent"
    assert updated["total_bytes"] == 4096
    assert updated["completed_bytes"] == 1024
    assert updated_task["status"] == "active"


@pytest.mark.asyncio
async def test_completion_with_followed_by_refreshes_real_task_name_and_size(
    temp_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await create_user_v0(username="listener_followed_refresh")
    download = await create_global_download_v0(
        resource_key="listener:followed-refresh",
        resource_kind="magnet",
        source_uri="magnet:?xt=urn:btih:followed",
        status="active",
        aria2_gid="gid-metadata",
        display_name="magnet:?xt=urn:btih:followed",
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
    )
    client = make_aria2_client(tell_status=[
        {
            "gid": "gid-metadata",
            "status": "complete",
            "followedBy": ["gid-real"],
            "totalLength": "0",
            "completedLength": "0",
            "files": [],
        },
        {
            "gid": "gid-real",
            "status": "active",
            "totalLength": "4096",
            "completedLength": "1024",
            "bittorrent": {"info": {"name": "Real Torrent"}},
            "files": [{"path": str(get_task_download_dir(download["id"]) / "Real Torrent" / "file.bin"), "length": "4096"}],
        },
    ])
    _patch_aria2_client(monkeypatch, client)

    await handle_aria2_event("gid-metadata", "complete")

    updated = await _fetch_global(download["id"])

    assert updated["aria2_gid"] == "gid-real"
    assert updated["display_name"] == "Real Torrent"
    assert updated["total_bytes"] == 4096
    assert updated["completed_bytes"] == 1024


@pytest.mark.asyncio
async def test_completion_with_followed_by_complete_real_status_indexes_payload(
    temp_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await create_user_v0(username="listener_followed_complete")
    await reserve_bytes(user["id"], 5, quota_bytes=user["quota_bytes"])
    download = await create_global_download_v0(
        resource_key="listener:followed-complete",
        resource_kind="magnet",
        source_uri="magnet:?xt=urn:btih:followed-complete",
        status="active",
        aria2_gid="gid-metadata-complete",
        display_name="magnet:?xt=urn:btih:followed-complete",
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=5,
    )
    task_dir = get_task_download_dir(download["id"])
    task_dir.mkdir(parents=True, exist_ok=True)
    source_file = task_dir / "payload.bin"
    source_file.write_bytes(b"abcde")

    client = make_aria2_client(tell_status=[
        {
            "gid": "gid-metadata-complete",
            "status": "complete",
            "followedBy": ["gid-real-complete"],
            "totalLength": "0",
            "completedLength": "0",
            "files": [],
        },
        {
            "gid": "gid-real-complete",
            "status": "complete",
            "following": "gid-metadata-complete",
            "totalLength": "5",
            "completedLength": "5",
            "bittorrent": {"info": {"name": "payload.bin"}},
            "files": [{"path": str(source_file), "length": "5"}],
        },
    ])
    _patch_aria2_client(monkeypatch, client)

    await handle_aria2_event("gid-metadata-complete", "complete")

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
async def test_start_event_replaces_exact_torrent_synthetic_task_name(
    temp_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await create_user_v0(username="listener_torrent_placeholder")
    resource_key = "1234567890abcdef"
    download = await create_global_download_v0(
        resource_key=resource_key,
        resource_kind="torrent",
        source_uri=f"magnet:?xt=urn:btih:{resource_key}",
        status="active",
        aria2_gid="gid-listener-torrent-placeholder",
        display_name=f"torrent-{resource_key[:12]}",
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        display_name=f"torrent-{resource_key[:12]}",
    )
    client = make_aria2_client(tell_status={
        "gid": "gid-listener-torrent-placeholder",
        "status": "active",
        "totalLength": "4096",
        "completedLength": "1024",
        "bittorrent": {"info": {"name": "Real Torrent"}},
        "files": [{"path": str(get_task_download_dir(download["id"]) / "Real Torrent" / "file.bin"), "length": "4096"}],
    })
    _patch_aria2_client(monkeypatch, client)

    await handle_aria2_event("gid-listener-torrent-placeholder", "start")

    updated_task = await _fetch_user_task(task["id"])

    assert updated_task["display_name"] == "Real Torrent"


@pytest.mark.asyncio
async def test_start_event_preserves_torrent_prefixed_http_user_task_name(
    temp_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await create_user_v0(username="listener_http_torrent_prefix")
    download = await create_global_download_v0(
        resource_key="listener:http-torrent-prefix",
        resource_kind="http",
        source_uri="https://example.com/torrent-release.iso",
        status="active",
        aria2_gid="gid-listener-http-torrent-prefix",
        display_name="torrent-release.iso",
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        display_name="torrent-release.iso",
    )
    client = make_aria2_client(tell_status={
        "gid": "gid-listener-http-torrent-prefix",
        "status": "active",
        "totalLength": "4096",
        "completedLength": "1024",
        "files": [{"path": "/downloads/renamed-by-server.iso", "length": "4096"}],
    })
    _patch_aria2_client(monkeypatch, client)

    await handle_aria2_event("gid-listener-http-torrent-prefix", "start")

    updated = await _fetch_global(download["id"])
    updated_task = await _fetch_user_task(task["id"])

    assert updated["display_name"] == "renamed-by-server.iso"
    assert updated_task["display_name"] == "torrent-release.iso"


@pytest.mark.asyncio
async def test_metadata_completion_retries_for_late_followed_by_before_file_validation(
    temp_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await create_user_v0(username="listener_late_followed_by")
    download = await create_global_download_v0(
        resource_key="listener:late-followed-by",
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
    )
    task_dir = get_task_download_dir(download["id"])
    task_dir.mkdir(parents=True, exist_ok=True)
    metadata_file = task_dir / "metadata"
    metadata_file.write_bytes(b"short")

    client = make_aria2_client(tell_status=[
        {
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
        {
            "gid": "gid-real",
            "status": "active",
            "totalLength": "4096",
            "completedLength": "512",
            "bittorrent": {"info": {"name": "Real Torrent"}},
            "files": [{"path": str(get_task_download_dir(download["id"]) / "Real Torrent" / "file.bin"), "length": "4096"}],
        },
    ])
    _patch_aria2_client(monkeypatch, client)

    await handle_aria2_event("gid-metadata", "complete")

    updated = await _fetch_global(download["id"])
    updated_task = await _fetch_user_task(task["id"])
    async with transaction() as conn:
        stored_count = (
            await conn.execute(select(func.count()).select_from(stored_files))
        ).scalar_one()
        user_file_count = (
            await conn.execute(select(func.count()).select_from(user_files))
        ).scalar_one()

    assert updated["aria2_gid"] == "gid-real"
    assert updated["status"] == "active"
    assert updated["error_code"] is None
    assert updated["display_name"] == "Real Torrent"
    assert updated["total_bytes"] == 4096
    assert updated["completed_bytes"] == 512
    assert updated_task["status"] == "active"
    assert stored_count == 0
    assert user_file_count == 0


@pytest.mark.asyncio
async def test_metadata_completion_discovers_followed_task_by_following_gid(
    temp_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await create_user_v0(username="listener_following_gid_discovery")
    download = await create_global_download_v0(
        resource_key="listener:following-gid-discovery",
        resource_kind="magnet",
        source_uri="magnet:?xt=urn:btih:following-gid",
        status="active",
        aria2_gid="gid-metadata",
        display_name="Real Torrent",
        total_bytes=9,
        completed_bytes=9,
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
    )
    task_dir = get_task_download_dir(download["id"])
    task_dir.mkdir(parents=True, exist_ok=True)
    metadata_file = task_dir / "movie.mkv"
    metadata_file.write_bytes(b"short")

    metadata_status = {
        "gid": "gid-metadata",
        "status": "complete",
        "totalLength": "9",
        "completedLength": "9",
        "bittorrent": {"info": {"name": "Real Torrent"}},
        "files": [
            {
                "path": str(metadata_file),
                "length": "9",
                "completedLength": "9",
            }
        ],
    }
    client = make_aria2_client(
        tell_status=[
            metadata_status,
            metadata_status,
            {
                "gid": "gid-real",
                "status": "active",
                "totalLength": "4096",
                "completedLength": "512",
                "bittorrent": {"info": {"name": "Real Torrent"}},
                "files": [
                    {"path": str(get_task_download_dir(download["id"]) / "Real Torrent" / "file.bin"), "length": "4096"}
                ],
            },
        ],
        tell_active=[
            {"gid": "gid-real", "status": "active", "followingGid": "gid-metadata"}
        ],
    )
    _patch_aria2_client(monkeypatch, client)

    await handle_aria2_event("gid-metadata", "complete")

    updated = await _fetch_global(download["id"])
    updated_task = await _fetch_user_task(task["id"])
    async with transaction() as conn:
        stored_count = (
            await conn.execute(select(func.count()).select_from(stored_files))
        ).scalar_one()
        user_file_count = (
            await conn.execute(select(func.count()).select_from(user_files))
        ).scalar_one()

    assert updated["aria2_gid"] == "gid-real"
    assert updated["resource_kind"] == "torrent"
    assert updated["status"] == "active"
    assert updated["error_code"] is None
    assert updated["display_name"] == "Real Torrent"
    assert updated["total_bytes"] == 4096
    assert updated["completed_bytes"] == 512
    assert updated_task["status"] == "active"
    assert stored_count == 0
    assert user_file_count == 0
    client.remove_download_result.assert_awaited_once_with("gid-metadata")


@pytest.mark.asyncio
async def test_metadata_completion_without_followed_by_does_not_index_metadata_file(
    temp_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await create_user_v0(username="listener_metadata_wait")
    download = await create_global_download_v0(
        resource_key="listener:metadata-wait",
        resource_kind="magnet",
        source_uri="magnet:?xt=urn:btih:metadatawait",
        status="active",
        aria2_gid="gid-metadata",
        display_name="magnet:?xt=urn:btih:metadatawait",
        total_bytes=8,
        completed_bytes=8,
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
    )
    task_dir = get_task_download_dir(download["id"])
    task_dir.mkdir(parents=True, exist_ok=True)
    metadata_file = task_dir / "metadata"
    metadata_file.write_bytes(b"metadata")

    client = make_aria2_client(tell_status={
        "gid": "gid-metadata",
        "status": "complete",
        "totalLength": "8",
        "completedLength": "8",
        "files": [
            {
                "path": str(metadata_file),
                "length": "8",
                "completedLength": "8",
            }
        ],
    })
    _patch_aria2_client(monkeypatch, client)

    await handle_aria2_event("gid-metadata", "complete")

    updated = await _fetch_global(download["id"])
    updated_task = await _fetch_user_task(task["id"])
    async with transaction() as conn:
        stored_count = (
            await conn.execute(select(func.count()).select_from(stored_files))
        ).scalar_one()
        user_file_count = (
            await conn.execute(select(func.count()).select_from(user_files))
        ).scalar_one()

    assert updated["aria2_gid"] == "gid-metadata"
    assert updated["status"] == "active"
    assert updated["completed_file_id"] is None
    assert updated_task["status"] == "active"
    assert stored_count == 0
    assert user_file_count == 0
    client.remove_download_result.assert_not_awaited()


@pytest.mark.asyncio
async def test_completed_download_missing_task_dir_uses_directory_error(
    temp_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await create_user_v0(username="listener_missing_dir")
    download = await create_global_download_v0(
        resource_key="listener:missing-dir",
        resource_kind="http",
        source_uri="https://example.com/payload.bin",
        status="active",
        aria2_gid="gid-missing-dir",
        display_name="payload.bin",
        total_bytes=7,
        completed_bytes=7,
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        display_name="payload.bin",
    )

    client = make_aria2_client(tell_status={
        "gid": "gid-missing-dir",
        "status": "complete",
        "totalLength": "7",
        "completedLength": "7",
        "files": [],
    })
    _patch_aria2_client(monkeypatch, client)

    await handle_aria2_event("gid-missing-dir", "complete")

    updated = await _fetch_global(download["id"])
    updated_task = await _fetch_user_task(task["id"])

    assert updated["status"] == "failed"
    assert updated["error_code"] == "download_dir_not_found"
    assert updated["error_message"] == "下载完成但下载目录不存在"
    assert updated_task["status"] == "failed"


@pytest.mark.asyncio
async def test_completed_download_existing_task_dir_missing_file_uses_file_error(
    temp_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await create_user_v0(username="listener_missing_file")
    download = await create_global_download_v0(
        resource_key="listener:missing-file",
        resource_kind="http",
        source_uri="https://example.com/payload.bin",
        status="active",
        aria2_gid="gid-missing-file",
        display_name="payload.bin",
        total_bytes=7,
        completed_bytes=7,
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        display_name="payload.bin",
    )
    task_dir = get_task_download_dir(download["id"])
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "payload.bin.aria2").write_bytes(b"")

    client = make_aria2_client(tell_status={
        "gid": "gid-missing-file",
        "status": "complete",
        "totalLength": "7",
        "completedLength": "7",
        "files": [],
    })
    _patch_aria2_client(monkeypatch, client)

    await handle_aria2_event("gid-missing-file", "complete")

    updated = await _fetch_global(download["id"])
    updated_task = await _fetch_user_task(task["id"])

    assert updated["status"] == "failed"
    assert updated["error_code"] == "download_file_not_found"
    assert updated["error_message"] == "下载完成但下载文件未找到"
    assert updated_task["status"] == "failed"


@pytest.mark.asyncio
async def test_completed_download_with_short_file_fails_size_validation(
    temp_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await create_user_v0(username="listener_size_mismatch")
    download = await create_global_download_v0(
        resource_key="listener:size-mismatch",
        resource_kind="http",
        source_uri="https://example.com/payload.bin",
        status="active",
        aria2_gid="gid-size-mismatch",
        display_name="payload.bin",
        total_bytes=9,
        completed_bytes=9,
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        display_name="payload.bin",
    )
    task_dir = get_task_download_dir(download["id"])
    task_dir.mkdir(parents=True, exist_ok=True)
    source_file = task_dir / "payload.bin"
    source_file.write_bytes(b"short")

    client = make_aria2_client(tell_status={
        "gid": "gid-size-mismatch",
        "status": "complete",
        "totalLength": "9",
        "completedLength": "9",
        "files": [
            {
                "path": str(source_file),
                "length": "9",
                "completedLength": "9",
            }
        ],
    })
    _patch_aria2_client(monkeypatch, client)

    await handle_aria2_event("gid-size-mismatch", "complete")

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
    assert updated["error_code"] == "completed_size_mismatch"
    assert updated["error_message"] == "下载完成但文件大小不匹配"
    assert updated_task["status"] == "failed"
    assert stored_count == 0
    assert user_file_count == 0
    assert not task_dir.exists()


@pytest.mark.asyncio
async def test_completed_download_over_actual_size_limit_fails_and_cleans_up(
    temp_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await create_user_v0(username="listener_actual_size", quota_bytes=100)
    await reserve_bytes(user["id"], 4, quota_bytes=user["quota_bytes"])
    download = await create_global_download_v0(
        resource_key="listener:actual-size",
        resource_kind="http",
        source_uri="https://example.com/payload.bin",
        status="active",
        aria2_gid="gid-actual-size",
        display_name="payload.bin",
        total_bytes=4,
        completed_bytes=4,
        size_limit_bytes=4,
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=4,
        display_name="payload.bin",
    )
    task_dir = Path(settings.download_dir) / "downloading" / str(download["id"])
    task_dir.mkdir(parents=True)
    source_file = task_dir / "payload.bin"
    source_file.write_bytes(b"12345")

    client = make_aria2_client(tell_status={
        "gid": "gid-actual-size",
        "status": "complete",
        "totalLength": "4",
        "completedLength": "4",
        "files": [{"path": str(source_file), "length": "4"}],
    })
    _patch_aria2_client(monkeypatch, client)

    await handle_aria2_event("gid-actual-size", "complete")

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

    assert updated["status"] == "failed"
    assert updated["error_code"] == "max_task_size_exceeded"
    assert updated["error_message"] == "任务大小超过系统限制"
    assert updated_task["status"] == "failed"
    assert updated_task["reserved_bytes"] == 0
    assert usage["used_bytes"] == 0
    assert usage["reserved_bytes"] == 0
    assert stored_count == 0
    assert user_file_count == 0
    assert not task_dir.exists()


@pytest.mark.asyncio
async def test_completed_download_over_only_user_quota_leaves_no_stored_payload(
    temp_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await create_user_v0(username="listener_actual_quota", quota_bytes=5)
    await reserve_bytes(user["id"], 4, quota_bytes=user["quota_bytes"])
    download = await create_global_download_v0(
        resource_key="listener:actual-quota",
        resource_kind="http",
        source_uri="https://example.com/quota.bin",
        status="active",
        aria2_gid="gid-actual-quota",
        display_name="quota.bin",
        total_bytes=4,
        completed_bytes=4,
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=4,
        display_name="quota.bin",
    )
    task_dir = Path(settings.download_dir) / "downloading" / str(download["id"])
    task_dir.mkdir(parents=True)
    source_file = task_dir / "quota.bin"
    source_file.write_bytes(b"123456")

    client = make_aria2_client(tell_status={
        "gid": "gid-actual-quota",
        "status": "complete",
        "totalLength": "4",
        "completedLength": "4",
        "files": [{"path": str(source_file), "length": "4"}],
    })
    _patch_aria2_client(monkeypatch, client)

    await handle_aria2_event("gid-actual-quota", "complete")

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

    assert updated["status"] == "cancelled"
    assert updated["completed_file_id"] is None
    assert updated["error_code"] == "no_eligible_subscribers"
    assert updated["error_message"] == "没有满足配额要求的订阅用户"
    assert updated_task["status"] == "failed"
    assert updated_task["reserved_bytes"] == 0
    assert usage["used_bytes"] == 0
    assert usage["reserved_bytes"] == 0
    assert stored_count == 0
    assert user_file_count == 0
    assert not task_dir.exists()


@pytest.mark.asyncio
async def test_complete_source_resolution_probes_four_times_every_half_second(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleep_intervals: list[float] = []

    async def fake_sleep(interval: float) -> None:
        sleep_intervals.append(interval)

    monkeypatch.setattr(completion_mod.asyncio, "sleep", fake_sleep)
    source = await completion_mod.resolve_complete_source_with_retry(
        completion_gid=None,
        task_dir=tmp_path / "missing",
        files=[],
        task_name=None,
        backend=None,
    )

    assert source is None
    assert sleep_intervals == [0.5, 0.5, 0.5]


@pytest.mark.asyncio
async def test_error_event_marks_global_and_user_tasks_failed_and_releases_reserved(
    temp_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await create_user_v0(username="listener_error", quota_bytes=1000)
    await reserve_bytes(user["id"], 300, quota_bytes=user["quota_bytes"])
    download = await create_global_download_v0(
        resource_key="listener:error",
        status="active",
        aria2_gid="gid-error",
        total_bytes=300,
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=300,
    )
    task_dir = get_task_download_dir(download["id"])
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "partial.bin").write_bytes(b"x")

    client = make_aria2_client(tell_status={
        "gid": "gid-error",
        "status": "error",
        "errorCode": "3",
        "errorMessage": "disk full",
        "totalLength": "300",
        "completedLength": "10",
        "files": [],
    })
    _patch_aria2_client(monkeypatch, client)
    broadcast = AsyncMock()
    monkeypatch.setattr(
        "app.services.lifecycle._shared.broadcast_task_update_to_subscribers",
        broadcast,
    )

    await handle_aria2_event("gid-error", "error")

    updated = await _fetch_global(download["id"])
    updated_task = await _fetch_user_task(task["id"])
    usage = await get_usage(user["id"], quota_bytes=user["quota_bytes"])

    assert updated["status"] == "failed"
    assert updated["aria2_gid"] is None
    assert updated["error_code"] == "3"
    assert updated["error_message"] == "aria2: disk full"
    assert updated_task["status"] == "failed"
    assert updated_task["reserved_bytes"] == 0
    assert usage["reserved_bytes"] == 0
    broadcast.assert_awaited_once_with(download["id"])


@pytest.mark.asyncio
async def test_mark_failed_noops_for_completed_download_with_completed_file_id(
    temp_db: str,
) -> None:
    user = await create_user_v0(username="listener_terminal_noop", quota_bytes=1000)
    stored_path = Path(settings.download_dir) / "store" / "terminal-noop.bin"
    stored_path.parent.mkdir(parents=True, exist_ok=True)
    stored_path.write_bytes(b"done")
    user_file = await create_user_file_v0(
        user_id=user["id"],
        real_path=stored_path,
        content_hash="terminal-noop-hash",
        display_name="terminal-noop.bin",
        size_bytes=4,
    )
    await reserve_bytes(user["id"], 200, quota_bytes=user["quota_bytes"])
    download = await create_global_download_v0(
        resource_key="listener:terminal-noop",
        status="completed",
        aria2_gid="gid-terminal-noop",
        completed_file_id=user_file["stored_file_id"],
        completed_bytes=4,
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=200,
    )

    returned = await mark_global_download_failed(
        download["id"],
        expected_gid="gid-terminal-noop",
        message="late error",
        error_code="late_error",
    )

    updated = await _fetch_global(download["id"])
    updated_task = await _fetch_user_task(task["id"])
    usage = await get_usage(user["id"], quota_bytes=user["quota_bytes"])

    assert returned is None
    assert updated["status"] == "completed"
    assert updated["completed_file_id"] == user_file["stored_file_id"]
    assert updated["aria2_gid"] == "gid-terminal-noop"
    assert updated_task["status"] == "active"
    assert updated_task["reserved_bytes"] == 200
    assert usage["reserved_bytes"] == 200


@pytest.mark.asyncio
async def test_late_completion_does_not_overwrite_failed_download(
    temp_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await create_user_v0(username="listener_late_complete")
    download = await create_global_download_v0(
        resource_key="listener:late-complete",
        status="failed",
        aria2_gid="gid-late-complete",
        total_bytes=4,
        completed_bytes=1,
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="failed",
        error_message="already failed",
    )
    task_dir = get_task_download_dir(download["id"])
    task_dir.mkdir(parents=True, exist_ok=True)
    source_file = task_dir / "late.bin"
    source_file.write_bytes(b"late")

    client = make_aria2_client(tell_status={
        "gid": "gid-late-complete",
        "status": "complete",
        "totalLength": "4",
        "completedLength": "4",
        "files": [{"path": str(source_file)}],
    })
    _patch_aria2_client(monkeypatch, client)

    await handle_aria2_event("gid-late-complete", "complete")

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
    assert updated["completed_file_id"] is None
    assert updated["completed_bytes"] == 1
    assert updated_task["status"] == "failed"
    assert stored_count == 0
    assert user_file_count == 0
    assert source_file.exists()


@pytest.mark.asyncio
async def test_error_event_serializes_with_inflight_completion(
    temp_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Complete and error events on the same attempt serialize on attempt lock.

    M3 removed the dedicated completion lock. Completion holds the lifecycle
    lock while writing; a concurrent error event must wait and then observe
    the completed terminal state without force_remove.
    """
    user = await create_user_v0(username="listener_complete_error_race")
    stored_path = Path(settings.download_dir) / "store" / "race-complete.bin"
    stored_path.parent.mkdir(parents=True, exist_ok=True)
    stored_path.write_bytes(b"done")
    user_file = await create_user_file_v0(
        user_id=user["id"],
        real_path=stored_path,
        content_hash="race-complete-hash",
        display_name="race-complete.bin",
        size_bytes=4,
    )
    download = await create_global_download_v0(
        resource_key="listener:complete-error-race",
        resource_kind="http",
        source_uri="https://example.com/race.bin",
        status="active",
        aria2_gid="gid-race-complete",
        total_bytes=4,
        completed_bytes=0,
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
    )
    task_dir = get_task_download_dir(download["id"])
    task_dir.mkdir(parents=True, exist_ok=True)
    source_file = task_dir / "race.bin"
    source_file.write_bytes(b"done")

    completion_started = asyncio.Event()
    allow_completion = asyncio.Event()

    async def fake_complete_global_download_locked(
        *,
        global_download_id: int,
        expected_gid: str,
        source_path: Path,
        original_name: str,
        expected_size: int | None = None,
    ) -> dict[str, int | str]:
        completion_started.set()
        assert expected_gid == "gid-race-complete"
        assert global_download_id == download["id"]
        assert source_path.resolve() == source_file.resolve()
        assert original_name == "race.bin"
        await allow_completion.wait()
        timestamp = 123456789
        async with transaction() as conn:
            await conn.execute(
                update(global_downloads)
                .where(global_downloads.c.id == download["id"])
                .values(
                    status="completed",
                    completed_file_id=user_file["stored_file_id"],
                    completed_bytes=4,
                    completed_at_ms=timestamp,
                    updated_at_ms=timestamp,
                )
            )
            await conn.execute(
                update(user_tasks)
                .where(user_tasks.c.id == task["id"])
                .values(
                    status="completed",
                    reserved_bytes=0,
                    updated_at_ms=timestamp,
                    finished_at_ms=timestamp,
                )
            )
        return {"status": "completed", "entries_created": 0, "user_files_created": 1}

    monkeypatch.setattr(
        "app.services.lifecycle.completion.complete_global_download_locked",
        fake_complete_global_download_locked,
    )

    client = make_aria2_client(tell_status=[
        {
            "gid": "gid-race-complete",
            "status": "complete",
            "totalLength": "4",
            "completedLength": "4",
            "files": [{"path": str(source_file)}],
        },
        {
            "gid": "gid-race-complete",
            "status": "error",
            "errorCode": "9",
            "errorMessage": "late failure",
            "totalLength": "4",
            "completedLength": "1",
            "files": [],
        },
    ])
    _patch_aria2_client(monkeypatch, client)

    completion_task = asyncio.create_task(
        handle_aria2_event("gid-race-complete", "complete")
    )
    await completion_started.wait()
    error_task = asyncio.create_task(
        handle_aria2_event("gid-race-complete", "error")
    )
    await asyncio.sleep(0.05)

    in_flight = await _fetch_global(download["id"])
    assert in_flight["status"] == "active"
    client.force_remove.assert_not_awaited()

    allow_completion.set()
    await asyncio.gather(completion_task, error_task)

    updated = await _fetch_global(download["id"])
    updated_task = await _fetch_user_task(task["id"])

    assert updated["status"] == "completed"
    assert updated["completed_file_id"] == user_file["stored_file_id"]
    assert updated["completed_bytes"] == 4
    assert updated_task["status"] == "completed"
    client.force_remove.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("event", ["start", "pause", "error", "complete"])
async def test_late_g1_event_does_not_mutate_g2_or_delete_directory(
    temp_db: str,
    monkeypatch: pytest.MonkeyPatch,
    event: str,
) -> None:
    user = await create_user_v0(username=f"late_g1_{event}")
    download = await create_global_download_v0(
        resource_key=f"listener:late-g1-{event}",
        resource_kind="http",
        source_uri="https://example.com/payload.bin",
        status="active",
        aria2_gid="gid-g2",
        total_bytes=7,
        completed_bytes=0,
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
    )
    task_dir = get_task_download_dir(download["id"])
    task_dir.mkdir(parents=True, exist_ok=True)
    source = task_dir / "payload.bin"
    source.write_bytes(b"payload")

    client = make_aria2_client(tell_status={
        "gid": "gid-g1",
        "status": "error" if event == "error" else event,
        "errorCode": "3",
        "errorMessage": "late error",
        "totalLength": "7",
        "completedLength": "7" if event == "complete" else "1",
        "files": [{"path": str(source), "length": "7"}],
    })
    _patch_aria2_client(monkeypatch, client)
    broadcast = AsyncMock()
    monkeypatch.setattr(
        "app.services.lifecycle._shared.broadcast_task_update_to_subscribers",
        broadcast,
    )

    await handle_aria2_event("gid-g1", event)

    updated = await _fetch_global(download["id"])
    updated_task = await _fetch_user_task(task["id"])
    assert updated["aria2_gid"] == "gid-g2"
    assert updated["status"] == "active"
    assert updated["completed_file_id"] is None
    assert updated_task["status"] == "active"
    assert source.read_bytes() == b"payload"
    client.force_remove.assert_not_awaited()
    client.remove_download_result.assert_not_awaited()
    broadcast.assert_not_awaited()
