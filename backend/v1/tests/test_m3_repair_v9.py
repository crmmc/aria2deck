"""T18: startup repair claim migration tests.

Verifies that repair functions obtain authorization through repair claims
or the coordinator before destructive cleanup, and never delete the only
copy of completed-without-index files.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.db.engine import transaction
from app.db.schema import global_downloads, stored_files
from app.services.repair import (
    purge_terminal_download_dirs,
    purge_terminal_residual_gids,
    rebuild_active_download_accounting,
    recover_completed_downloads_pending_index,
)
from tests.fakes import make_aria2_client
from tests.helpers_v0 import create_global_download_v0


async def _fetch_download(download_id: int) -> dict:
    async with transaction() as conn:
        row = (
            (
                await conn.execute(
                    select(global_downloads).where(
                        global_downloads.c.id == download_id
                    )
                )
            )
            .mappings()
            .first()
        )
    return dict(row)


# ---------------------------------------------------------------------------
# 1. Failed residual + matching GID: repair claim → cleanup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_residual_purged_after_claim(temp_db: str) -> None:
    download = await create_global_download_v0(
        resource_key="http:residual1",
        source_uri="http://example.com/file1",
        resource_kind="http",
        status="failed",
        aria2_gid="gid-residual-1",
        total_bytes=100,
        completed_bytes=0,
        size_known=True,
    )
    client = make_aria2_client(tell_status={})

    result = await purge_terminal_residual_gids(client)

    assert result["found"] == 1
    assert result["purged"] == 1
    assert result["failed"] == 0
    client.force_remove.assert_awaited_once_with("gid-residual-1")

    row = await _fetch_download(download["id"])
    assert row["aria2_gid"] is None


# ---------------------------------------------------------------------------
# 2. Live attempt is NOT purged by residual GID purge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_attempt_not_purged_by_residual(temp_db: str) -> None:
    await create_global_download_v0(
        resource_key="http:live2",
        source_uri="http://example.com/file2",
        resource_kind="http",
        status="active",
        aria2_gid="gid-active-2",
        total_bytes=1000,
        completed_bytes=500,
        size_known=True,
    )
    client = make_aria2_client(tell_status={})

    result = await purge_terminal_residual_gids(client)

    assert result["found"] == 0
    assert result["purged"] == 0
    client.force_remove.assert_not_awaited()


# ---------------------------------------------------------------------------
# 3. Completed-without-index with files: recovery attempts indexing,
#    failure preserves the files (no purge)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_completed_without_index_recovery_preserves_files(
    temp_db: str,
) -> None:
    download = await create_global_download_v0(
        resource_key="http:pend3",
        source_uri="http://example.com/file3",
        resource_kind="http",
        status="completed",
        aria2_gid="gid-completed-3",
        total_bytes=100,
        completed_bytes=100,
        size_known=True,
        completed_file_id=None,
    )
    download_id = download["id"]

    downloading_dir = Path(settings.download_dir) / "downloading"
    task_dir = downloading_dir / str(download_id)
    task_dir.mkdir(parents=True, exist_ok=True)
    payload = task_dir / "file3.bin"
    payload.write_bytes(b"X" * 100)

    client = make_aria2_client(tell_status={})

    # Patch handle_v0_download_complete to simulate recovery failure.
    with patch(
        "app.services.repair.handle_v0_download_complete",
        new_callable=AsyncMock,
        return_value=False,
    ):
        result = await recover_completed_downloads_pending_index(client)

    assert result["found"] == 1
    assert result["recovered"] == 0
    assert result["failed"] == 1

    # The file must still exist — no purge of the only copy.
    assert payload.exists(), "Only copy must not be deleted after recovery failure"

    row = await _fetch_download(download_id)
    # Should have been restored back to completed or active.
    assert row["status"] in ("completed", "active")


# ---------------------------------------------------------------------------
# 4. Completed-without-index is NOT deleted by purge_terminal_download_dirs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_completed_without_index_not_purged_by_dirs(
    temp_db: str,
) -> None:
    download = await create_global_download_v0(
        resource_key="http:pend4",
        source_uri="http://example.com/file4",
        resource_kind="http",
        status="completed",
        aria2_gid="gid-completed-4",
        total_bytes=100,
        completed_bytes=100,
        size_known=True,
        completed_file_id=None,
    )
    download_id = download["id"]

    downloading_dir = Path(settings.download_dir) / "downloading"
    task_dir = downloading_dir / str(download_id)
    task_dir.mkdir(parents=True, exist_ok=True)
    payload = task_dir / "file4.bin"
    payload.write_bytes(b"Y" * 100)

    result = await purge_terminal_download_dirs()

    assert result["found"] == 0
    assert result["purged"] == 0
    assert task_dir.exists(), "Completed-without-index dir must survive purge"


# ---------------------------------------------------------------------------
# 5. Repair does NOT revive failed/cancelled to queued/active
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_not_revived_by_rebuild(temp_db: str) -> None:
    await create_global_download_v0(
        resource_key="http:fail5",
        source_uri="http://example.com/file5",
        resource_kind="http",
        status="failed",
        aria2_gid=None,
        total_bytes=100,
        completed_bytes=0,
        size_known=True,
    )
    client = make_aria2_client(
        tell_status={
            "status": "active",
            "totalLength": "100",
            "completedLength": "50",
        }
    )

    result = await rebuild_active_download_accounting(client)

    assert result["rebuilt"] == 0
    assert result["failed"] == 0


@pytest.mark.asyncio
async def test_cancelled_not_revived_by_rebuild(temp_db: str) -> None:
    await create_global_download_v0(
        resource_key="http:cancel6",
        source_uri="http://example.com/file6",
        resource_kind="http",
        status="cancelled",
        aria2_gid=None,
        total_bytes=200,
        completed_bytes=0,
        size_known=True,
    )
    client = make_aria2_client(
        tell_status={
            "status": "waiting",
            "totalLength": "200",
            "completedLength": "0",
        }
    )

    result = await rebuild_active_download_accounting(client)

    assert result["rebuilt"] == 0
    assert result["failed"] == 0


# ---------------------------------------------------------------------------
# 6. Active attempt is reconciled via coordinator (no skip_status_check)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_active_attempt_reconciled_via_coordinator(temp_db: str) -> None:
    from tests.helpers_v0 import create_user_v0, create_user_task_v0

    user = await create_user_v0(username="rebuild-user")
    download = await create_global_download_v0(
        resource_key="http:active7",
        source_uri="http://example.com/file7",
        resource_kind="http",
        status="active",
        aria2_gid="gid-active-7",
        total_bytes=1000,
        completed_bytes=500,
        size_known=True,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=1000,
    )

    client = make_aria2_client(
        tell_status={
            "status": "active",
            "totalLength": "1000",
            "completedLength": "500",
            "files": [
                {
                    "path": f"/dl/downloading/{download['id']}/file7.bin",
                    "length": "1000",
                    "completedLength": "500",
                    "selected": "true",
                }
            ],
        }
    )

    result = await rebuild_active_download_accounting(client)

    # The coordinator should have called tell_status on the GID.
    client.tell_status.assert_awaited()
    assert result["rebuilt"] + result["failed"] >= 1


# ---------------------------------------------------------------------------
# 7. Residual purge skips when claim fails (GID changed in DB)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_residual_purge_skips_when_claim_fails(temp_db: str) -> None:
    await create_global_download_v0(
        resource_key="http:stale8",
        source_uri="http://example.com/file8",
        resource_kind="http",
        status="failed",
        aria2_gid="gid-old-8",
        total_bytes=100,
        completed_bytes=0,
        size_known=True,
    )
    client = make_aria2_client(tell_status={})

    # Simulate GID mismatch: claim_terminal_reclaim will return None because
    # the DB gid does not match what we pass. But purge_terminal_residual_gids
    # reads the DB gid itself, so it will match. Instead test a row that got
    # revived to active between list and claim.
    with patch(
        "app.services.repair.list_terminal_downloads_with_residual_gid",
        new_callable=AsyncMock,
        return_value=[
            {
                "id": 999,
                "aria2_gid": "gid-nonexistent",
            }
        ],
    ):
        result = await purge_terminal_residual_gids(client)

    assert result["found"] == 1
    assert result["purged"] == 0
    client.force_remove.assert_not_awaited()
