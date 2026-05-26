from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select, update

from app.aria2.listener import handle_aria2_event
from app.core.config import settings
from app.core.state import AppState
from app.db.engine import transaction
from app.db.schema import global_downloads, stored_files, user_files, user_tasks
from app.repositories.downloads import mark_global_download_failed
from app.services.usage_service import get_usage, reserve_bytes
from tests.helpers_v0 import (
    create_global_download_v0,
    create_user_file_v0,
    create_user_task_v0,
    create_user_v0,
)


def _patch_aria2_client(monkeypatch: pytest.MonkeyPatch, client: AsyncMock) -> None:
    def get_client(*args: object, **kwargs: object) -> AsyncMock:
        return client

    monkeypatch.setattr("app.core.state.get_aria2_client", get_client)


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
async def test_duplicate_completion_creates_one_stored_file_and_user_file_per_active_task(
    temp_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_a = await create_user_v0(username="listener_complete_a", quota_bytes=1000)
    user_b = await create_user_v0(username="listener_complete_b", quota_bytes=1000)
    download = await create_global_download_v0(
        resource_key="listener:complete",
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

    task_dir = Path(settings.download_dir) / "downloading" / str(download["id"])
    task_dir.mkdir(parents=True)
    source_file = task_dir / "payload.bin"
    source_file.write_bytes(b"payload")

    client = AsyncMock()
    client.tell_status.return_value = {
        "gid": "gid-listener-complete",
        "status": "complete",
        "totalLength": "7",
        "completedLength": "7",
        "files": [{"path": str(source_file)}],
    }
    client.remove_download_result.return_value = "OK"
    _patch_aria2_client(monkeypatch, client)

    state = AppState()
    await asyncio.gather(
        handle_aria2_event(state, "gid-listener-complete", "complete"),
        handle_aria2_event(state, "gid-listener-complete", "complete"),
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
    client = AsyncMock()
    client.tell_status.return_value = {
        "gid": "gid-metadata",
        "status": "complete",
        "followedBy": ["gid-real"],
        "totalLength": "0",
        "completedLength": "0",
        "files": [],
    }
    client.remove_download_result.return_value = "OK"
    _patch_aria2_client(monkeypatch, client)

    await handle_aria2_event(AppState(), "gid-metadata", "complete")

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
    client = AsyncMock()
    client.tell_status.return_value = {
        "gid": "gid-real",
        "status": "active",
        "following": "gid-metadata",
        "totalLength": "4096",
        "completedLength": "1024",
        "bittorrent": {"info": {"name": "Real Torrent"}},
        "files": [{"path": "/downloads/Real Torrent/file.bin", "length": "4096"}],
    }
    _patch_aria2_client(monkeypatch, client)

    await handle_aria2_event(AppState(), "gid-real", "start")

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
    client = AsyncMock()
    client.tell_status.side_effect = [
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
            "files": [{"path": "/downloads/Real Torrent/file.bin", "length": "4096"}],
        },
    ]
    client.remove_download_result.return_value = "OK"
    _patch_aria2_client(monkeypatch, client)

    await handle_aria2_event(AppState(), "gid-metadata", "complete")

    updated = await _fetch_global(download["id"])

    assert updated["aria2_gid"] == "gid-real"
    assert updated["display_name"] == "Real Torrent"
    assert updated["total_bytes"] == 4096
    assert updated["completed_bytes"] == 1024


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
    task_dir = Path(settings.download_dir) / "downloading" / str(download["id"])
    task_dir.mkdir(parents=True)
    metadata_file = task_dir / "metadata"
    metadata_file.write_bytes(b"short")

    client = AsyncMock()
    client.tell_status.side_effect = [
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
            "files": [{"path": "/downloads/Real Torrent/file.bin", "length": "4096"}],
        },
    ]
    client.remove_download_result.return_value = "OK"
    _patch_aria2_client(monkeypatch, client)

    await handle_aria2_event(AppState(), "gid-metadata", "complete")

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
    task_dir = Path(settings.download_dir) / "downloading" / str(download["id"])
    task_dir.mkdir(parents=True)
    metadata_file = task_dir / "metadata"
    metadata_file.write_bytes(b"metadata")

    client = AsyncMock()
    client.tell_status.return_value = {
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
    }
    client.remove_download_result.return_value = "OK"
    _patch_aria2_client(monkeypatch, client)

    await handle_aria2_event(AppState(), "gid-metadata", "complete")

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

    client = AsyncMock()
    client.tell_status.return_value = {
        "gid": "gid-missing-dir",
        "status": "complete",
        "totalLength": "7",
        "completedLength": "7",
        "files": [],
    }
    client.force_remove.return_value = "OK"
    client.remove_download_result.return_value = "OK"
    _patch_aria2_client(monkeypatch, client)

    await handle_aria2_event(AppState(), "gid-missing-dir", "complete")

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
    task_dir = Path(settings.download_dir) / "downloading" / str(download["id"])
    task_dir.mkdir(parents=True)
    (task_dir / "payload.bin.aria2").write_bytes(b"")

    client = AsyncMock()
    client.tell_status.return_value = {
        "gid": "gid-missing-file",
        "status": "complete",
        "totalLength": "7",
        "completedLength": "7",
        "files": [],
    }
    client.force_remove.return_value = "OK"
    client.remove_download_result.return_value = "OK"
    _patch_aria2_client(monkeypatch, client)

    await handle_aria2_event(AppState(), "gid-missing-file", "complete")

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
    task_dir = Path(settings.download_dir) / "downloading" / str(download["id"])
    task_dir.mkdir(parents=True)
    source_file = task_dir / "payload.bin"
    source_file.write_bytes(b"short")

    client = AsyncMock()
    client.tell_status.return_value = {
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
    }
    client.force_remove.return_value = "OK"
    client.remove_download_result.return_value = "OK"
    _patch_aria2_client(monkeypatch, client)

    await handle_aria2_event(AppState(), "gid-size-mismatch", "complete")

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
async def test_complete_source_resolution_probes_four_times_every_half_second(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.aria2 import listener

    sleep_intervals: list[float] = []

    async def fake_sleep(interval: float) -> None:
        sleep_intervals.append(interval)

    monkeypatch.setattr(listener.asyncio, "sleep", fake_sleep)
    source = await listener._resolve_complete_source_with_retry(
        completion_gid=None,
        task_dir=tmp_path / "missing",
        files=[],
        task_name=None,
        state=AppState(),
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
    task_dir = Path(settings.download_dir) / "downloading" / str(download["id"])
    task_dir.mkdir(parents=True)
    (task_dir / "partial.bin").write_bytes(b"x")

    client = AsyncMock()
    client.tell_status.return_value = {
        "gid": "gid-error",
        "status": "error",
        "errorCode": "3",
        "errorMessage": "disk full",
        "totalLength": "300",
        "completedLength": "10",
        "files": [],
    }
    client.force_remove.return_value = "OK"
    client.remove_download_result.return_value = "OK"
    _patch_aria2_client(monkeypatch, client)

    await handle_aria2_event(AppState(), "gid-error", "error")

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
        message="late error",
        error_code="late_error",
    )

    updated = await _fetch_global(download["id"])
    updated_task = await _fetch_user_task(task["id"])
    usage = await get_usage(user["id"], quota_bytes=user["quota_bytes"])

    assert returned is not None
    assert returned["status"] == "completed"
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
    task_dir = Path(settings.download_dir) / "downloading" / str(download["id"])
    task_dir.mkdir(parents=True)
    source_file = task_dir / "late.bin"
    source_file.write_bytes(b"late")

    client = AsyncMock()
    client.tell_status.return_value = {
        "gid": "gid-late-complete",
        "status": "complete",
        "totalLength": "4",
        "completedLength": "4",
        "files": [{"path": str(source_file)}],
    }
    _patch_aria2_client(monkeypatch, client)

    await handle_aria2_event(AppState(), "gid-late-complete", "complete")

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
async def test_error_event_waits_for_inflight_completion_lock(
    temp_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    task_dir = Path(settings.download_dir) / "downloading" / str(download["id"])
    task_dir.mkdir(parents=True)
    source_file = task_dir / "race.bin"
    source_file.write_bytes(b"done")

    completion_started = asyncio.Event()
    allow_completion = asyncio.Event()

    async def fake_complete_global_download(
        *,
        global_download_id: int,
        source_path: Path,
        original_name: str,
    ) -> dict[str, int | str]:
        completion_started.set()
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
        "app.aria2.listener.complete_global_download",
        fake_complete_global_download,
    )

    client = AsyncMock()
    client.tell_status.side_effect = [
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
    ]
    client.remove_download_result.return_value = "OK"
    client.force_remove.return_value = "OK"
    _patch_aria2_client(monkeypatch, client)
    state = AppState()

    completion_task = asyncio.create_task(
        handle_aria2_event(state, "gid-race-complete", "complete")
    )
    await completion_started.wait()
    error_task = asyncio.create_task(
        handle_aria2_event(state, "gid-race-complete", "error")
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
