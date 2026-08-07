"""Phase 1: fail paths reclaim dirs; completed shells; startup terminal dir purge."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update

from app.aria2.sync import STALE_QUEUED_GRACE_SECONDS
from app.core.config import settings
from app.db.engine import transaction
from app.db.schema import global_downloads
from app.repositories.downloads import now_ms
from app.services.aria2_lifecycle_service import (
    cleanup_stale_queued_downloads_v0,
    coordinate_reported_size,
    handle_v0_download_complete,
    repair_inconsistent_completed_downloads_v0,
)
from app.services.download_service import complete_global_download, create_user_download
from app.services.repair import purge_terminal_download_dirs
from app.services.storage import get_downloading_dir
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


def _task_dir(download_id: int) -> Path:
    path = get_downloading_dir() / str(download_id)
    path.mkdir(parents=True, exist_ok=True)
    (path / "payload.bin").write_bytes(b"residual")
    return path


@pytest.mark.asyncio
async def test_growth_pause_failed_reclaims_download_dir(temp_db: str) -> None:
    user = await create_user_v0(username="growth_pause_reclaim", quota_bytes=1000)
    client = make_aria2_client(add_uri="gid-growth-pause", pause=OSError("pause failed"))
    task = await create_user_download(
        user_id=user["id"],
        quota_bytes=user["quota_bytes"],
        uri="https://example.com/grow.bin",
        resource_key="quota:growth-pause-reclaim",
        resource_kind="http",
        display_name="grow.bin",
        total_bytes=100,
        size_known=True,
        aria2_client=client,
    )
    download_id = int(task["global_download_id"])
    task_dir = _task_dir(download_id)

    result = await coordinate_reported_size(
        client=client,
        download=await _fetch_global(download_id),
        expected_gid="gid-growth-pause",
        control_gid="gid-growth-pause",
        status={
            "status": "active",
            "totalLength": "200",
            "completedLength": "0",
        },
    )

    stored = await _fetch_global(download_id)
    assert result["outcome"] == "terminalized"
    assert stored["status"] == "failed"
    assert stored["error_code"] == "growth_pause_failed"
    assert not task_dir.exists()
    client.force_remove.assert_awaited_once_with("gid-growth-pause")


@pytest.mark.asyncio
async def test_repair_inconsistent_completed_reclaims_download_dir(
    temp_db: str,
) -> None:
    from app.services.usage_service import get_usage, reserve_bytes

    user = await create_user_v0(username="repair_completed_reclaim", quota_bytes=1000)
    await reserve_bytes(user["id"], 200, quota_bytes=user["quota_bytes"])
    download = await create_global_download_v0(
        resource_key="sync:repair-completed-reclaim",
        status="completed",
        aria2_gid="gid-repair-completed-reclaim",
        total_bytes=200,
        completed_bytes=200,
        completed_file_id=None,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=200,
    )
    task_dir = _task_dir(download["id"])
    old_timestamp = now_ms() - 31_000
    async with transaction() as conn:
        await conn.execute(
            update(global_downloads)
            .where(global_downloads.c.id == download["id"])
            .values(updated_at_ms=old_timestamp)
        )

    client = make_aria2_client()
    await repair_inconsistent_completed_downloads_v0(client=client)

    updated = await _fetch_global(download["id"])
    usage = await get_usage(user["id"], quota_bytes=user["quota_bytes"])
    assert updated["status"] == "failed"
    assert updated["error_code"] == "completion_not_indexed"
    assert usage["reserved_bytes"] == 0
    assert not task_dir.exists()
    client.force_remove.assert_awaited_once_with("gid-repair-completed-reclaim")


@pytest.mark.asyncio
async def test_stale_queued_cleanup_reclaims_download_dir(temp_db: str) -> None:
    user = await create_user_v0(username="stale_queued_reclaim")
    download = await create_global_download_v0(
        resource_key="cleanup:stale-reclaim",
        status="queued",
        aria2_gid=None,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="queued",
    )
    task_dir = _task_dir(download["id"])
    old_timestamp = now_ms() - int((STALE_QUEUED_GRACE_SECONDS + 1) * 1000)
    async with transaction() as conn:
        await conn.execute(
            update(global_downloads)
            .where(global_downloads.c.id == download["id"])
            .values(updated_at_ms=old_timestamp)
        )

    client = make_aria2_client()
    await cleanup_stale_queued_downloads_v0(client=client)

    updated = await _fetch_global(download["id"])
    assert updated["status"] == "failed"
    assert updated["error_code"] == "submit_timeout"
    assert not task_dir.exists()
    client.force_remove.assert_not_awaited()


@pytest.mark.asyncio
async def test_completed_success_reclaims_download_dir_shell(temp_db: str) -> None:
    user = await create_user_v0(username="complete_shell_reclaim", quota_bytes=1000)
    client = make_aria2_client(add_uri="gid-complete-shell")
    payload = b"complete-shell-payload"
    task = await create_user_download(
        user_id=user["id"],
        quota_bytes=user["quota_bytes"],
        uri="https://example.com/shell.bin",
        resource_key="http:complete-shell",
        resource_kind="http",
        display_name="shell.bin",
        total_bytes=len(payload),
        aria2_client=client,
    )
    download_id = int(task["global_download_id"])
    task_dir = Path(settings.download_dir) / "downloading" / str(download_id)
    task_dir.mkdir(parents=True, exist_ok=True)
    source_file = task_dir / "shell.bin"
    source_file.write_bytes(payload)

    changed = await handle_v0_download_complete(
        client=client,
        download=await _fetch_global(download_id),
        aria2_status={
            "status": "complete",
            "totalLength": str(len(payload)),
            "completedLength": str(len(payload)),
            "files": [
                {
                    "path": str(source_file),
                    "length": str(len(payload)),
                    "completedLength": str(len(payload)),
                }
            ],
        },
        completion_gid="gid-complete-shell",
        log_prefix="[Test]",
        allow_metadata_handoff_defer=False,
    )

    stored = await _fetch_global(download_id)
    assert changed is True
    assert stored["status"] == "completed"
    assert not task_dir.exists()
    client.remove_download_result.assert_awaited()


@pytest.mark.asyncio
async def test_purge_terminal_download_dirs_only_touches_terminal(
    temp_db: str,
) -> None:
    user = await create_user_v0(username="purge_user", quota_bytes=10_000)
    failed = await create_global_download_v0(
        resource_key="purge:failed",
        status="failed",
        aria2_gid=None,
        total_bytes=10,
        completed_bytes=0,
    )
    pending_completed = await create_global_download_v0(
        resource_key="purge:completed-pending",
        status="completed",
        aria2_gid=None,
        total_bytes=10,
        completed_bytes=10,
        completed_file_id=None,
    )
    store_path = Path(settings.download_dir) / "store" / "indexed.bin"
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_bytes(b"indexed")
    user_file = await create_user_file_v0(
        user_id=user["id"],
        real_path=store_path,
        content_hash="a" * 64,
        display_name="indexed.bin",
        size_bytes=7,
    )
    indexed_completed = await create_global_download_v0(
        resource_key="purge:completed-indexed",
        status="completed",
        aria2_gid=None,
        total_bytes=7,
        completed_bytes=7,
        completed_file_id=user_file["stored_file_id"],
    )
    active = await create_global_download_v0(
        resource_key="purge:active",
        status="active",
        aria2_gid="gid-active",
        total_bytes=10,
        completed_bytes=1,
    )
    failed_dir = _task_dir(failed["id"])
    pending_dir = _task_dir(pending_completed["id"])
    indexed_dir = _task_dir(indexed_completed["id"])
    active_dir = _task_dir(active["id"])
    pack_dir = get_downloading_dir() / "pack_9"
    pack_dir.mkdir(parents=True, exist_ok=True)
    (pack_dir / "x.bin").write_bytes(b"pack")

    result = await purge_terminal_download_dirs()

    # failed + completed-with-index only.
    assert result["found"] == 2
    assert result["purged"] == 2
    assert result["failed"] == 0
    assert not failed_dir.exists()
    assert not indexed_dir.exists()
    assert pending_dir.exists()
    assert active_dir.exists()
    assert pack_dir.exists()


@pytest.mark.asyncio
async def test_purge_keeps_completed_without_index_and_recovery_indexes(
    temp_db: str,
) -> None:
    from app.services.repair import recover_completed_downloads_pending_index

    user = await create_user_v0(username="recover_completed", quota_bytes=10_000)
    payload = b"pending-index-payload"
    download = await create_global_download_v0(
        resource_key="recover:pending-index",
        status="completed",
        aria2_gid="gid-pending-index",
        total_bytes=len(payload),
        completed_bytes=len(payload),
        completed_file_id=None,
        display_name="pending.bin",
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="completed",
        reserved_bytes=0,
        display_name="pending.bin",
    )
    task_dir = Path(settings.download_dir) / "downloading" / str(download["id"])
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "pending.bin").write_bytes(payload)

    purge_before = await purge_terminal_download_dirs()
    assert task_dir.exists()
    assert purge_before["found"] == 0

    client = make_aria2_client()
    result = await recover_completed_downloads_pending_index(client)
    assert result["found"] == 1
    assert result["recovered"] == 1

    stored = await _fetch_global(download["id"])
    assert stored["status"] == "completed"
    assert stored["completed_file_id"] is not None
    assert not task_dir.exists()


@pytest.mark.asyncio
async def test_recover_failed_restore_keeps_only_copy(
    temp_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import repair as repair_module

    user = await create_user_v0(username="recover_fail_restore", quota_bytes=10_000)
    payload = b"only-copy-payload"
    download = await create_global_download_v0(
        resource_key="recover:fail-restore",
        status="completed",
        aria2_gid=None,
        total_bytes=len(payload),
        completed_bytes=len(payload),
        completed_file_id=None,
        display_name="only-copy.bin",
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="completed",
        reserved_bytes=0,
        display_name="only-copy.bin",
    )
    task_dir = Path(settings.download_dir) / "downloading" / str(download["id"])
    task_dir.mkdir(parents=True, exist_ok=True)
    payload_path = task_dir / "only-copy.bin"
    payload_path.write_bytes(payload)

    async def _boom(**_kwargs):
        raise RuntimeError("forced recovery failure")

    monkeypatch.setattr(
        "app.services.aria2_lifecycle_service.handle_v0_download_complete",
        _boom,
    )

    client = make_aria2_client()
    result = await repair_module.recover_completed_downloads_pending_index(client)
    assert result["found"] == 1
    assert result["failed"] == 1
    assert result["recovered"] == 0

    stored = await _fetch_global(download["id"])
    assert stored["status"] == "completed"
    assert stored["completed_file_id"] is None
    assert payload_path.exists()
    assert payload_path.read_bytes() == payload

    purge = await purge_terminal_download_dirs()
    assert purge["found"] == 0
    assert task_dir.exists()


@pytest.mark.asyncio
async def test_complete_global_download_still_indexes_after_shell_cleanup_helper(
    temp_db: str,
) -> None:
    """Sanity: complete_global_download itself still moves files into store."""
    user = await create_user_v0(username="complete_store_sanity", quota_bytes=1000)
    client = make_aria2_client(add_uri="gid-store-sanity")
    payload = b"store-sanity"
    task = await create_user_download(
        user_id=user["id"],
        quota_bytes=user["quota_bytes"],
        uri="https://example.com/store.bin",
        resource_key="http:store-sanity",
        resource_kind="http",
        display_name="store.bin",
        total_bytes=len(payload),
        aria2_client=client,
    )
    source_path = Path(settings.download_dir) / "downloading" / str(
        task["global_download_id"]
    )
    source_path.mkdir(parents=True, exist_ok=True)
    (source_path / "store.bin").write_bytes(payload)

    result = await complete_global_download(
        global_download_id=task["global_download_id"],
        expected_gid="gid-store-sanity",
        source_path=source_path,
        original_name="store.bin",
    )
    assert result is not None
    assert result["status"] == "completed"
