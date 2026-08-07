"""T25: completion and cleanup authorization race tests.

Verifies spec §22.4 invariants for the completion/fail/cancel race and
cleanup claim authorization:

1. Completion first → fail is stale, store file not deleted.
2. Fail first → completion cannot create stored file association.
3. Writer not stopped (non-not-found RPC error) → directory retained.
4. Writer stopped but directory deletion fails → GID and dir preserved.
5. GID not found → repeated cleanup is idempotent.
6. completed-without-index recovery failure → only file copy preserved.
7. failed/cancelled terminal residual → disk_reserved_bytes zeroed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.db.engine import transaction
from app.db.schema import (
    global_downloads,
    stored_files,
    user_storage_usage,
    user_tasks,
)
from app.domain.lifecycle import make_terminalization_claim
from app.domain.status import ACTIVE_GLOBAL_DOWNLOAD_STATUSES
from app.repositories.downloads import claim_attempt_terminal
from app.services.aria2_lifecycle_service import (
    fail_download_and_reclaim,
    handle_v0_download_complete,
)
from app.services.failed_task_cleanup import cleanup_with_claim
from app.services.storage import get_downloading_dir
from tests.fakes import make_aria2_client
from tests.helpers_v0 import (
    create_global_download_v0,
    create_user_task_v0,
    create_user_v0,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _fetch_global(download_id: int) -> dict:
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
            .one()
        )
    return dict(row)


async def _fetch_stored_files() -> list[dict]:
    async with transaction() as conn:
        rows = (
            (await conn.execute(select(stored_files))).mappings().all()
        )
    return [dict(r) for r in rows]


async def _set_usage_reserved(user_id: int, reserved: int) -> None:
    async with transaction() as conn:
        await conn.execute(
            user_storage_usage.update()
            .where(user_storage_usage.c.user_id == user_id)
            .values(reserved_bytes=reserved)
        )


def _create_download_file(download_id: int, name: str, data: bytes) -> Path:
    task_dir = get_downloading_dir() / str(download_id)
    task_dir.mkdir(parents=True, exist_ok=True)
    file_path = task_dir / name
    file_path.write_bytes(data)
    return file_path


def _complete_aria2_status(*, file_path: str, size: int) -> dict[str, Any]:
    return {
        "status": "complete",
        "totalLength": str(size),
        "completedLength": str(size),
        "files": [
            {"path": file_path, "length": str(size), "selected": "true"},
        ],
    }


async def _seed_active_download(
    *,
    user: dict,
    resource_key: str,
    gid: str,
    total_bytes: int = 100,
    reserved: int | None = None,
) -> dict:
    res = reserved if reserved is not None else total_bytes
    download = await create_global_download_v0(
        resource_key=resource_key,
        source_uri=f"https://example.com/{resource_key}",
        resource_kind="http",
        status="active",
        aria2_gid=gid,
        total_bytes=total_bytes,
        completed_bytes=total_bytes,
        size_known=True,
        disk_reserved_bytes=res,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=res,
    )
    await _set_usage_reserved(user["id"], res)
    return download


# ---------------------------------------------------------------------------
# 1. Completion first → fail stale, store file NOT deleted (§22.4-1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_first_then_fail_does_not_delete_store(temp_db: str) -> None:
    user = await create_user_v0(username="t25_c1", quota_bytes=10_000_000)
    dl = await _seed_active_download(
        user=user, resource_key="http:t25-c1", gid="gid-c1"
    )

    source = _create_download_file(dl["id"], "c1.bin", b"completion-data-c1")
    aria2_status = _complete_aria2_status(
        file_path=str(source), size=len(b"completion-data-c1")
    )

    changed = await handle_v0_download_complete(
        client=make_aria2_client(tell_status=aria2_status),
        download=dl,
        aria2_status=aria2_status,
        completion_gid="gid-c1",
        log_prefix="[T25-1]",
        allow_metadata_handoff_defer=False,
    )
    assert changed is True

    row = await _fetch_global(dl["id"])
    assert row["status"] == "completed"
    assert row["completed_file_id"] is not None

    sfs = await _fetch_stored_files()
    assert len(sfs) == 1
    store_path = Path(str(sfs[0]["real_path"]))
    assert store_path.exists()

    # Fail arrives after completion → stale
    failed = await fail_download_and_reclaim(
        client=make_aria2_client(tell_status={}),
        download_id=dl["id"],
        message="late failure",
        error_code="error",
        expected_gid="gid-c1",
        writer_gid="gid-c1",
        log_prefix="[T25-1]",
    )
    assert failed is False

    row2 = await _fetch_global(dl["id"])
    assert row2["status"] == "completed"
    assert row2["completed_file_id"] == row["completed_file_id"]
    assert store_path.exists(), "store file must survive stale fail"


# ---------------------------------------------------------------------------
# 2. Fail first → completion cannot create stored file (§22.4-2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fail_first_then_complete_cannot_index(temp_db: str) -> None:
    user = await create_user_v0(username="t25_f2", quota_bytes=10_000_000)
    dl = await _seed_active_download(
        user=user, resource_key="http:t25-f2", gid="gid-f2"
    )

    failed = await fail_download_and_reclaim(
        client=make_aria2_client(tell_status={}),
        download_id=dl["id"],
        message="prior failure",
        error_code="error",
        expected_gid="gid-f2",
        writer_gid="gid-f2",
        log_prefix="[T25-2]",
    )
    assert failed is True

    row = await _fetch_global(dl["id"])
    assert row["status"] == "failed"
    assert row["completed_file_id"] is None

    source = _create_download_file(dl["id"], "f2.bin", b"fail-first-data")
    aria2_status = _complete_aria2_status(
        file_path=str(source), size=len(b"fail-first-data")
    )

    changed = await handle_v0_download_complete(
        client=make_aria2_client(tell_status=aria2_status),
        download=dl,
        aria2_status=aria2_status,
        completion_gid="gid-f2",
        log_prefix="[T25-2]",
        allow_metadata_handoff_defer=False,
    )
    assert changed is False

    row2 = await _fetch_global(dl["id"])
    assert row2["status"] == "failed"
    assert row2["completed_file_id"] is None
    assert len(await _fetch_stored_files()) == 0


# ---------------------------------------------------------------------------
# 3. Writer not stopped (network error) → directory retained (§22.4-3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_writer_not_stopped_retains_directory(temp_db: str) -> None:
    download = await create_global_download_v0(
        resource_key="http:t25-w3",
        status="failed",
        aria2_gid="gid-w3",
        total_bytes=100,
        completed_bytes=0,
    )
    task_dir = get_downloading_dir() / str(download["id"])
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "partial.bin").write_bytes(b"partial")

    client = make_aria2_client(tell_status={})
    client.force_remove.side_effect = ConnectionError("network unreachable")
    claim = make_terminalization_claim(
        attempt_id=download["id"],
        expected_current_gid="gid-w3",
        writer_gids=("gid-w3",),
        result_gids=("gid-w3",),
        terminal_status="failed",
        claim_timestamp=1,
    )

    with patch(
        "app.services.failed_task_cleanup.cleanup_task_download_dir"
    ) as mock_dir:
        result = await cleanup_with_claim(client, claim, log_prefix="[T25-3]")

    assert result.writer_stopped is False
    assert result.directory_cleaned is False
    mock_dir.assert_not_called()
    assert task_dir.exists(), "directory must survive when writer not stopped"

    row = await _fetch_global(download["id"])
    assert row["aria2_gid"] == "gid-w3", "GID must be retained for fencing"


# ---------------------------------------------------------------------------
# 4. Writer stopped but dir delete fails → keep GID and dir (§22.4-4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dir_delete_failure_preserves_gid_and_dir(temp_db: str) -> None:
    download = await create_global_download_v0(
        resource_key="http:t25-d4",
        status="failed",
        aria2_gid="gid-d4",
        total_bytes=100,
        completed_bytes=0,
    )
    task_dir = get_downloading_dir() / str(download["id"])
    task_dir.mkdir(parents=True, exist_ok=True)

    client = make_aria2_client(tell_status={})
    claim = make_terminalization_claim(
        attempt_id=download["id"],
        expected_current_gid="gid-d4",
        writer_gids=("gid-d4",),
        result_gids=("gid-d4",),
        terminal_status="failed",
        claim_timestamp=1,
    )

    with patch(
        "app.services.failed_task_cleanup.cleanup_task_download_dir",
        side_effect=OSError("permission denied"),
    ):
        result = await cleanup_with_claim(client, claim, log_prefix="[T25-4]")

    assert result.writer_stopped is True
    assert result.directory_cleaned is False
    client.force_remove.assert_awaited_once_with("gid-d4")
    assert task_dir.exists(), "directory must survive when dir delete fails"


# ---------------------------------------------------------------------------
# 5. GID not found → repeated cleanup is idempotent (§22.4-5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gid_not_found_idempotent_repeated_cleanup(temp_db: str) -> None:
    download = await create_global_download_v0(
        resource_key="http:t25-i5",
        status="failed",
        aria2_gid="gid-i5",
        total_bytes=100,
        completed_bytes=0,
    )
    task_dir = get_downloading_dir() / str(download["id"])
    task_dir.mkdir(parents=True, exist_ok=True)

    def _make_client() -> AsyncMock:
        c = make_aria2_client(tell_status={})
        c.force_remove.side_effect = Exception("GID not found")
        return c

    def _make_claim():
        return make_terminalization_claim(
            attempt_id=download["id"],
            expected_current_gid="gid-i5",
            writer_gids=("gid-i5",),
            result_gids=("gid-i5",),
            terminal_status="failed",
            claim_timestamp=1,
        )

    # First cleanup round
    with patch(
        "app.services.failed_task_cleanup.cleanup_task_download_dir"
    ) as mock_dir:
        mock_dir.return_value = None
        r1 = await cleanup_with_claim(_make_client(), _make_claim(), log_prefix="[T25-5a]")
    assert r1.writer_stopped is True
    assert r1.directory_cleaned is True

    row1 = await _fetch_global(download["id"])
    assert row1["aria2_gid"] is None, "GID cleared after first cleanup"

    # Second cleanup round — repair claim with no GID (already cleared)
    from app.domain.lifecycle import make_repair_claim

    client2 = make_aria2_client(tell_status={})
    client2.force_remove.side_effect = Exception("GID not found")
    repair_claim = make_repair_claim(
        attempt_id=download["id"],
        expected_current_gid=None,
        writer_gids=(),
        result_gids=(),
        terminal_status="failed",
        claim_timestamp=2,
    )
    with patch(
        "app.services.failed_task_cleanup.cleanup_task_download_dir"
    ) as mock_dir2:
        mock_dir2.return_value = None
        r2 = await cleanup_with_claim(client2, repair_claim, log_prefix="[T25-5b]")
    assert r2.writer_stopped is True
    assert r2.directory_cleaned is True

    # State unchanged after second pass
    row2 = await _fetch_global(download["id"])
    assert row2["aria2_gid"] is None


# ---------------------------------------------------------------------------
# 6. completed-without-index recovery failure → only copy preserved (§22.4-6)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pending_index_recovery_failure_preserves_only_copy(
    temp_db: str,
) -> None:
    download = await create_global_download_v0(
        resource_key="http:t25-p6",
        source_uri="http://example.com/p6",
        resource_kind="http",
        status="completed",
        aria2_gid="gid-p6",
        total_bytes=100,
        completed_bytes=100,
        size_known=True,
        completed_file_id=None,
    )

    task_dir = get_downloading_dir() / str(download["id"])
    task_dir.mkdir(parents=True, exist_ok=True)
    payload = task_dir / "p6.bin"
    payload.write_bytes(b"X" * 100)

    from app.services.repair import recover_completed_downloads_pending_index

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
    assert payload.exists(), "only file copy must survive recovery failure"


# ---------------------------------------------------------------------------
# 7. failed/cancelled terminal residual → no disk promise (§22.4-7)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_terminal_zeroes_disk_reserved(temp_db: str) -> None:
    user = await create_user_v0(username="t25-r7", quota_bytes=10_000_000)
    dl = await _seed_active_download(
        user=user, resource_key="http:t25-r7f", gid="gid-r7f"
    )

    claim = await claim_attempt_terminal(
        attempt_id=dl["id"],
        expected_gid="gid-r7f",
        terminal_status="failed",
        error_code="test",
        error_message="failed residual",
        expected_statuses=ACTIVE_GLOBAL_DOWNLOAD_STATUSES,
    )
    assert claim is not None

    row = await _fetch_global(dl["id"])
    assert row["status"] == "failed"
    assert row["disk_reserved_bytes"] == 0

    async with transaction() as conn:
        usage = (
            (
                await conn.execute(
                    select(user_storage_usage).where(
                        user_storage_usage.c.user_id == user["id"]
                    )
                )
            )
            .mappings()
            .one()
        )
    assert usage["reserved_bytes"] == 0

    # Verify user tasks also terminal and reservation released
    async with transaction() as conn:
        ut_rows = (
            (
                await conn.execute(
                    select(user_tasks).where(
                        user_tasks.c.global_download_id == dl["id"]
                    )
                )
            )
            .mappings()
            .all()
        )
    for ut in ut_rows:
        assert ut["status"] == "failed"
        assert ut["reserved_bytes"] == 0


@pytest.mark.asyncio
async def test_cancelled_terminal_zeroes_disk_reserved(temp_db: str) -> None:
    user = await create_user_v0(username="t25-r7c", quota_bytes=10_000_000)
    dl = await _seed_active_download(
        user=user, resource_key="http:t25-r7c", gid="gid-r7c"
    )

    claim = await claim_attempt_terminal(
        attempt_id=dl["id"],
        expected_gid="gid-r7c",
        terminal_status="cancelled",
        error_code="user_cancelled",
        error_message="cancelled",
        expected_statuses=ACTIVE_GLOBAL_DOWNLOAD_STATUSES,
    )
    assert claim is not None

    row = await _fetch_global(dl["id"])
    assert row["status"] == "cancelled"
    assert row["disk_reserved_bytes"] == 0
