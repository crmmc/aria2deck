"""T27: restart recovery and repair isolation.

Covers spec §15 (startup repair), §22.4/22.5 recovery items:

1. completed-without-index with files / without files
2. active GID present / missing
3. terminal residual GID purged only with repair claim
4. failed / cancelled directories
5. active / paused directories and store contents untouched
6. recovery failure preserves the only file copy
7. physical reclaim only for repair-claim-authorized residuals
8. startup order never treats live or pending-index as anomalous residual
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.db.engine import transaction
from app.db.schema import global_downloads, stored_files, user_tasks
from app.services.repair import (
    purge_terminal_download_dirs,
    purge_terminal_residual_gids,
    rebuild_active_download_accounting,
    recover_completed_downloads_pending_index,
)
from tests.fakes import make_aria2_client
from tests.helpers_v0 import (
    create_global_download_v0,
    create_user_task_v0,
    create_user_v0,
)

# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _task_dir(download_id: int) -> Path:
    return Path(settings.download_dir) / "downloading" / str(download_id)


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


# --------------------------------------------------------------------------- #
# Scenario 1: completed-without-index — with files and without files          #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_completed_without_index_with_files_attempts_recovery(
    temp_db: str,
) -> None:
    """completed + completed_file_id IS NULL with files triggers re-index."""
    download = await create_global_download_v0(
        resource_key="http:pidx-files",
        source_uri="http://example.com/file-a",
        resource_kind="http",
        status="completed",
        aria2_gid="gid-pidx-a",
        total_bytes=200,
        completed_bytes=200,
        size_known=True,
        completed_file_id=None,
    )
    download_id = download["id"]
    task_dir = _task_dir(download_id)
    task_dir.mkdir(parents=True, exist_ok=True)
    payload = task_dir / "file-a.bin"
    payload.write_bytes(b"A" * 200)

    client = make_aria2_client(tell_status={})

    with (
        patch(
            "app.services.aria2_lifecycle_service.handle_v0_download_complete",
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_complete,
        patch(
            "app.services.repair.get_global_download_by_id",
            new_callable=AsyncMock,
            return_value={
                "id": download_id,
                "status": "completed",
                "completed_file_id": 999,
            },
        ),
    ):
        result = await recover_completed_downloads_pending_index(client)

    assert result["found"] == 1
    assert result["recovered"] == 1
    mock_complete.assert_awaited_once()


@pytest.mark.asyncio
async def test_completed_without_index_no_files_skipped(temp_db: str) -> None:
    """completed + completed_file_id IS NULL with empty dir is skipped."""
    download = await create_global_download_v0(
        resource_key="http:pidx-empty",
        source_uri="http://example.com/file-b",
        resource_kind="http",
        status="completed",
        aria2_gid="gid-pidx-b",
        total_bytes=100,
        completed_bytes=100,
        size_known=True,
        completed_file_id=None,
    )
    download_id = download["id"]
    _task_dir(download_id).mkdir(parents=True, exist_ok=True)

    client = make_aria2_client(tell_status={})

    with patch(
        "app.services.aria2_lifecycle_service.handle_v0_download_complete",
        new_callable=AsyncMock,
    ) as mock_complete:
        result = await recover_completed_downloads_pending_index(client)

    assert result["found"] == 1
    assert result["recovered"] == 0
    assert result["skipped"] == 1
    mock_complete.assert_not_awaited()


# --------------------------------------------------------------------------- #
# Scenario 2: active GID present / missing                                     #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_active_gid_present_reconciled(temp_db: str) -> None:
    """Active attempt with existing GID is reconciled via coordinator."""
    user = await create_user_v0(username="rebuild-user-present")
    download = await create_global_download_v0(
        resource_key="http:active-present",
        source_uri="http://example.com/file-c",
        resource_kind="http",
        status="active",
        aria2_gid="gid-active-present",
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
                    "path": f"/dl/downloading/{download['id']}/file-c.bin",
                    "length": "1000",
                    "completedLength": "500",
                    "selected": "true",
                }
            ],
        }
    )
    result = await rebuild_active_download_accounting(client)
    client.tell_status.assert_awaited()
    assert result["rebuilt"] + result["failed"] >= 1


@pytest.mark.asyncio
async def test_active_gid_missing_terminalized(temp_db: str) -> None:
    """Active attempt whose GID is missing from Aria2 gets terminalized."""
    from app.domain.lifecycle import ReconcileResult
    from app.services.aria2_lifecycle_service import reconcile_attempt_signal

    user = await create_user_v0(username="rebuild-user-missing")
    download = await create_global_download_v0(
        resource_key="http:active-missing",
        source_uri="http://example.com/file-d",
        resource_kind="http",
        status="active",
        aria2_gid="gid-active-missing",
        total_bytes=1000,
        completed_bytes=500,
        size_known=True,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=0,
    )

    client = make_aria2_client(tell_status=RuntimeError("gid not found"))

    result = await reconcile_attempt_signal(
        client=client,
        observed_gid="gid-active-missing",
        event="startup",
        observed_status=None,
        log_prefix="[Startup]",
    )
    assert result == ReconcileResult.TERMINALIZED

    row = await _fetch_download(download["id"])
    assert row["status"] == "failed"


# --------------------------------------------------------------------------- #
# Scenario 3: terminal residual GID purged only via repair claim              #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_terminal_residual_purged_with_claim(temp_db: str) -> None:
    """failed attempt with residual GID gets purged through repair claim."""
    download = await create_global_download_v0(
        resource_key="http:residual-fail",
        source_uri="http://example.com/file-e",
        resource_kind="http",
        status="failed",
        aria2_gid="gid-residual-fail",
        total_bytes=100,
        completed_bytes=0,
        size_known=True,
    )
    client = make_aria2_client(tell_status={})

    result = await purge_terminal_residual_gids(client)

    assert result["found"] == 1
    assert result["purged"] == 1
    client.force_remove.assert_awaited_once_with("gid-residual-fail")

    row = await _fetch_download(download["id"])
    assert row["aria2_gid"] is None


@pytest.mark.asyncio
async def test_terminal_residual_claim_fails_no_cleanup(temp_db: str) -> None:
    """If repair claim returns None (status changed), no force_remove."""
    await create_global_download_v0(
        resource_key="http:residual-active",
        source_uri="http://example.com/file-f",
        resource_kind="http",
        status="active",
        aria2_gid="gid-residual-active",
        total_bytes=100,
        completed_bytes=50,
        size_known=True,
    )
    client = make_aria2_client(tell_status={})

    result = await purge_terminal_residual_gids(client)

    assert result["found"] == 0
    assert result["purged"] == 0
    client.force_remove.assert_not_awaited()


# --------------------------------------------------------------------------- #
# Scenario 4: failed / cancelled directories are purged                        #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_failed_directory_purged(temp_db: str) -> None:
    """failed attempt directory is safe to purge."""
    download = await create_global_download_v0(
        resource_key="http:dir-fail",
        source_uri="http://example.com/file-g",
        resource_kind="http",
        status="failed",
        aria2_gid=None,
        total_bytes=100,
        completed_bytes=0,
        size_known=True,
    )
    task_dir = _task_dir(download["id"])
    task_dir.mkdir(parents=True, exist_ok=True)

    result = await purge_terminal_download_dirs()

    assert result["found"] == 1
    assert result["purged"] == 1
    assert not task_dir.exists()


@pytest.mark.asyncio
async def test_cancelled_directory_purged(temp_db: str) -> None:
    """cancelled attempt directory is safe to purge."""
    download = await create_global_download_v0(
        resource_key="http:dir-cancel",
        source_uri="http://example.com/file-h",
        resource_kind="http",
        status="cancelled",
        aria2_gid=None,
        total_bytes=100,
        completed_bytes=0,
        size_known=True,
    )
    task_dir = _task_dir(download["id"])
    task_dir.mkdir(parents=True, exist_ok=True)

    result = await purge_terminal_download_dirs()

    assert result["found"] == 1
    assert result["purged"] == 1
    assert not task_dir.exists()


# --------------------------------------------------------------------------- #
# Scenario 5: active / paused directories and store contents untouched        #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_active_directory_not_purged(temp_db: str) -> None:
    """active attempt directory must survive startup purge."""
    download = await create_global_download_v0(
        resource_key="http:dir-active",
        source_uri="http://example.com/file-i",
        resource_kind="http",
        status="active",
        aria2_gid="gid-active-dir",
        total_bytes=1000,
        completed_bytes=500,
        size_known=True,
    )
    task_dir = _task_dir(download["id"])
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "partial.bin").write_bytes(b"X" * 500)

    result = await purge_terminal_download_dirs()

    assert result["found"] == 0
    assert task_dir.exists(), "Active dir must not be purged"


@pytest.mark.asyncio
async def test_paused_directory_not_purged(temp_db: str) -> None:
    """paused attempt directory must survive startup purge."""
    download = await create_global_download_v0(
        resource_key="http:dir-paused",
        source_uri="http://example.com/file-j",
        resource_kind="http",
        status="paused",
        aria2_gid="gid-paused-dir",
        total_bytes=1000,
        completed_bytes=300,
        size_known=True,
    )
    task_dir = _task_dir(download["id"])
    task_dir.mkdir(parents=True, exist_ok=True)

    result = await purge_terminal_download_dirs()

    assert result["found"] == 0
    assert task_dir.exists()


@pytest.mark.asyncio
async def test_store_contents_not_touched_by_purge(temp_db: str) -> None:
    """Store directory is never touched by terminal dir purge."""
    from tests.helpers_v0 import create_user_file_v0

    user = await create_user_v0(username="store-user")
    store_dir = Path(settings.download_dir) / "store"
    store_dir.mkdir(parents=True, exist_ok=True)
    real_path = store_dir / "store-file.bin"
    real_path.write_bytes(b"S" * 256)

    await create_user_file_v0(
        user_id=user["id"],
        real_path=real_path,
        content_hash="hash-store-file",
        display_name="store-file.bin",
        size_bytes=256,
    )

    result = await purge_terminal_download_dirs()

    assert result["found"] == 0
    assert real_path.exists(), "Store content must not be deleted"


# --------------------------------------------------------------------------- #
# Scenario 6: recovery failure preserves the only file copy                   #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_recovery_failure_preserves_only_copy(temp_db: str) -> None:
    """completed-without-index recovery failure must not delete the file."""
    download = await create_global_download_v0(
        resource_key="http:recover-fail",
        source_uri="http://example.com/file-k",
        resource_kind="http",
        status="completed",
        aria2_gid="gid-recover-fail",
        total_bytes=300,
        completed_bytes=300,
        size_known=True,
        completed_file_id=None,
    )
    download_id = download["id"]
    task_dir = _task_dir(download_id)
    task_dir.mkdir(parents=True, exist_ok=True)
    payload = task_dir / "file-k.bin"
    payload.write_bytes(b"K" * 300)

    client = make_aria2_client(tell_status={})

    with patch(
        "app.services.aria2_lifecycle_service.handle_v0_download_complete",
        new_callable=AsyncMock,
        return_value=False,
    ):
        result = await recover_completed_downloads_pending_index(client)

    assert result["found"] == 1
    assert result["recovered"] == 0
    assert result["failed"] == 1
    assert payload.exists(), "Only copy must survive recovery failure"


@pytest.mark.asyncio
async def test_recovery_failure_not_purged_by_dir_purge(temp_db: str) -> None:
    """After recovery failure, completed-without-index dir survives purge."""
    download = await create_global_download_v0(
        resource_key="http:recover-fail-2",
        source_uri="http://example.com/file-l",
        resource_kind="http",
        status="completed",
        aria2_gid="gid-recover-fail-2",
        total_bytes=100,
        completed_bytes=100,
        size_known=True,
        completed_file_id=None,
    )
    download_id = download["id"]
    task_dir = _task_dir(download_id)
    task_dir.mkdir(parents=True, exist_ok=True)
    payload = task_dir / "file-l.bin"
    payload.write_bytes(b"L" * 100)

    result = await purge_terminal_download_dirs()

    assert result["found"] == 0
    assert task_dir.exists(), "Pending-index dir must survive dir purge"
    assert payload.exists()


# --------------------------------------------------------------------------- #
# Scenario 7: physical reclaim only for repair-claim-authorized residuals     #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_force_remove_only_for_terminal_residual(temp_db: str) -> None:
    """force_remove called only for failed/cancelled with residual GID."""
    await create_global_download_v0(
        resource_key="http:live-no-purge",
        source_uri="http://example.com/file-m",
        resource_kind="http",
        status="active",
        aria2_gid="gid-live-no-purge",
        total_bytes=500,
        completed_bytes=100,
        size_known=True,
    )
    await create_global_download_v0(
        resource_key="http:terminal-purge",
        source_uri="http://example.com/file-n",
        resource_kind="http",
        status="cancelled",
        aria2_gid="gid-terminal-purge",
        total_bytes=200,
        completed_bytes=0,
        size_known=True,
    )
    client = make_aria2_client(tell_status={})

    result = await purge_terminal_residual_gids(client)

    assert result["found"] == 1
    assert result["purged"] == 1
    client.force_remove.assert_awaited_once_with("gid-terminal-purge")


@pytest.mark.asyncio
async def test_cleanup_with_claim_no_writer_gids(temp_db: str) -> None:
    """Repair claim with no writer_gids does not call force_remove."""
    from app.domain.lifecycle import RepairClaim
    from app.services.failed_task_cleanup import cleanup_with_claim

    download = await create_global_download_v0(
        resource_key="http:no-writer",
        source_uri="http://example.com/file-o",
        resource_kind="http",
        status="failed",
        aria2_gid=None,
        total_bytes=100,
        completed_bytes=0,
        size_known=True,
    )
    download_id = download["id"]
    task_dir = _task_dir(download_id)
    task_dir.mkdir(parents=True, exist_ok=True)

    client = make_aria2_client(tell_status={})
    claim = RepairClaim(
        attempt_id=download_id,
        expected_current_gid=None,
        writer_gids=(),
        result_gids=(),
        terminal_status="failed",
        claim_timestamp=0,
    )

    result = await cleanup_with_claim(client, claim, log_prefix="[Test]")

    assert result.writer_stopped is True
    assert result.directory_cleaned is True
    client.force_remove.assert_not_awaited()


# --------------------------------------------------------------------------- #
# Scenario 8: startup order never treats live / pending-index as residual     #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_live_attempt_not_in_residual_list(temp_db: str) -> None:
    """Live (active/waiting/paused) attempts are never returned as residual."""
    from app.repositories.downloads import (
        list_terminal_downloads_with_residual_gid,
    )

    for idx, status_val in enumerate(("active", "waiting", "paused")):
        await create_global_download_v0(
            resource_key=f"http:live-{idx}",
            source_uri=f"http://example.com/live-{idx}",
            resource_kind="http",
            status=status_val,
            aria2_gid=f"gid-live-{idx}",
            total_bytes=500,
            completed_bytes=100,
            size_known=True,
        )

    residuals = await list_terminal_downloads_with_residual_gid()
    assert len(residuals) == 0


@pytest.mark.asyncio
async def test_pending_index_not_in_residual_list(temp_db: str) -> None:
    """completed + completed_file_id IS NULL is never treated as residual."""
    from app.repositories.downloads import (
        list_terminal_downloads_with_residual_gid,
    )

    await create_global_download_v0(
        resource_key="http:pidx-residual",
        source_uri="http://example.com/file-p",
        resource_kind="http",
        status="completed",
        aria2_gid="gid-pidx-residual",
        total_bytes=100,
        completed_bytes=100,
        size_known=True,
        completed_file_id=None,
    )

    residuals = await list_terminal_downloads_with_residual_gid()
    assert len(residuals) == 0


@pytest.mark.asyncio
async def test_live_and_pending_not_in_terminal_dir_ids(temp_db: str) -> None:
    """Neither live nor pending-index ids appear in terminal dir id list."""
    from app.repositories.downloads import list_terminal_download_ids

    live = await create_global_download_v0(
        resource_key="http:live-ids",
        source_uri="http://example.com/file-q",
        resource_kind="http",
        status="active",
        aria2_gid="gid-live-ids",
        total_bytes=500,
        completed_bytes=100,
        size_known=True,
    )
    pending = await create_global_download_v0(
        resource_key="http:pidx-ids",
        source_uri="http://example.com/file-r",
        resource_kind="http",
        status="completed",
        aria2_gid="gid-pidx-ids",
        total_bytes=100,
        completed_bytes=100,
        size_known=True,
        completed_file_id=None,
    )

    terminal_ids = await list_terminal_download_ids()
    assert live["id"] not in terminal_ids
    assert pending["id"] not in terminal_ids


@pytest.mark.asyncio
async def test_failed_cancelled_in_terminal_dir_ids(temp_db: str) -> None:
    """failed and cancelled ids do appear in terminal dir id list."""
    from app.repositories.downloads import list_terminal_download_ids

    failed = await create_global_download_v0(
        resource_key="http:failed-ids",
        source_uri="http://example.com/file-s",
        resource_kind="http",
        status="failed",
        aria2_gid=None,
        total_bytes=100,
        completed_bytes=0,
        size_known=True,
    )
    cancelled = await create_global_download_v0(
        resource_key="http:cancelled-ids",
        source_uri="http://example.com/file-t",
        resource_kind="http",
        status="cancelled",
        aria2_gid=None,
        total_bytes=100,
        completed_bytes=0,
        size_known=True,
    )

    terminal_ids = await list_terminal_download_ids()
    assert failed["id"] in terminal_ids
    assert cancelled["id"] in terminal_ids
