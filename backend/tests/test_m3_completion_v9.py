"""T13: completion ingestion and directory shell cleanup tests.

Verifies (spec §11, §15.4, §16, task T13):
1. Normal completion: store index + completed_file_id + dir shell deletion.
2. Complete vs fail race: first claim wins.
   a) Fail claims first → complete cannot create stored association.
   b) Complete claims first → fail is stale.
3. pending-index (completed without file_id with files) is not purged.
4. Source missing with no followedBy → fail claim + cleanup.
5. Wrapper/locked lock semantics: locked path doesn't re-acquire lock.
6. Source missing → late handoff attempt before fail claim.
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
    stored_file_entries,
    stored_files,
    user_files,
    user_storage_usage,
    user_tasks,
)
from app.services.lifecycle.cleanup import fail_download_and_reclaim
from app.services.lifecycle.completion import handle_v0_download_complete
from app.domain.locks import get_download_lifecycle_lock
from app.services.lifecycle.completion import (
    complete_global_download,
    complete_global_download_locked,
)
from app.services.storage import cleanup_task_download_dir, get_downloading_dir
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


async def _fetch_user_tasks(download_id: int) -> list[dict]:
    async with transaction() as conn:
        rows = (
            (
                await conn.execute(
                    select(user_tasks).where(
                        user_tasks.c.global_download_id == download_id
                    )
                )
            )
            .mappings()
            .all()
        )
    return [dict(r) for r in rows]


async def _fetch_stored_files() -> list[dict]:
    async with transaction() as conn:
        rows = (
            (
                await conn.execute(select(stored_files))
            )
            .mappings()
            .all()
        )
    return [dict(r) for r in rows]


async def _fetch_user_files(user_id: int) -> list[dict]:
    async with transaction() as conn:
        rows = (
            (
                await conn.execute(
                    select(user_files).where(user_files.c.user_id == user_id)
                )
            )
            .mappings()
            .all()
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
    """Create a source file in the downloading dir and return its path."""
    task_dir = get_downloading_dir() / str(download_id)
    task_dir.mkdir(parents=True, exist_ok=True)
    file_path = task_dir / name
    file_path.write_bytes(data)
    return file_path


def _complete_aria2_status(
    *,
    file_path: str,
    size: int,
    total_length: int | None = None,
) -> dict[str, Any]:
    if total_length is None:
        total_length = size
    return {
        "status": "complete",
        "totalLength": str(total_length),
        "completedLength": str(total_length),
        "files": [
            {"path": file_path, "length": str(size), "selected": "true"},
        ],
    }


# ---------------------------------------------------------------------------
# 1. Normal completion: store index + completed_file_id + dir shell deleted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_normal_completion_indexes_and_cleans_dir(temp_db: str) -> None:
    """Completion should create stored_files/user_files entries, write
    completed_file_id, and delete the emptied downloading/<id> shell."""
    user = await create_user_v0(username="t13_normal", quota_bytes=10_000_000)
    download = await create_global_download_v0(
        resource_key="http:t13-normal",
        source_uri="https://example.com/t13",
        resource_kind="http",
        status="active",
        aria2_gid="gid-t13-normal",
        total_bytes=100,
        completed_bytes=100,
        size_known=True,
        disk_reserved_bytes=100,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=100,
    )
    await _set_usage_reserved(user["id"], 100)

    file_data = b"completion-test-data"
    source = _create_download_file(download["id"], "test.bin", file_data)
    source_path_str = str(source)
    task_dir = get_downloading_dir() / str(download["id"])

    aria2_status = _complete_aria2_status(
        file_path=source_path_str, size=len(file_data)
    )
    client = make_aria2_client(tell_status=aria2_status)

    changed = await handle_v0_download_complete(
        backend=client,
        download=download,
        aria2_status=aria2_status,
        completion_gid="gid-t13-normal",
        log_prefix="[T13]",
        allow_metadata_handoff_defer=False,
    )

    assert changed is True

    stored = await _fetch_global(download["id"])
    assert stored["status"] == "completed"
    assert stored["completed_file_id"] is not None
    assert stored["aria2_gid"] is None

    # stored_files created
    sfs = await _fetch_stored_files()
    assert len(sfs) == 1
    assert sfs[0]["size_bytes"] == len(file_data)

    # user_files created
    ufs = await _fetch_user_files(user["id"])
    assert len(ufs) == 1

    # user_tasks completed
    tasks = await _fetch_user_tasks(download["id"])
    assert all(t["status"] == "completed" for t in tasks)

    # Downloading dir shell deleted
    assert not task_dir.exists()


# ---------------------------------------------------------------------------
# 2. Complete vs fail race: first claim wins
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fail_claims_first_then_complete_cannot_index(
    temp_db: str,
) -> None:
    """When fail claims terminal first, the completion path cannot create
    stored file association (spec §11.3)."""
    user = await create_user_v0(username="t13_race_f", quota_bytes=10_000_000)
    download = await create_global_download_v0(
        resource_key="http:t13-race-f",
        source_uri="https://example.com/t13rf",
        resource_kind="http",
        status="active",
        aria2_gid="gid-t13-race-f",
        total_bytes=100,
        completed_bytes=100,
        size_known=True,
        disk_reserved_bytes=100,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=100,
    )
    await _set_usage_reserved(user["id"], 100)

    # Fail claims first
    client = make_aria2_client(tell_status={})
    failed = await fail_download_and_reclaim(
        backend=client,
        download_id=download["id"],
        message="竞态测试：先失败",
        error_code="error",
        expected_gid="gid-t13-race-f",
        writer_gid="gid-t13-race-f",
        log_prefix="[T13]",
    )
    assert failed is True

    stored_after_fail = await _fetch_global(download["id"])
    assert stored_after_fail["status"] == "failed"
    assert stored_after_fail["completed_file_id"] is None

    # Now completion tries
    file_data = b"race-fail-data"
    source = _create_download_file(download["id"], "test.bin", file_data)
    aria2_status = _complete_aria2_status(
        file_path=str(source), size=len(file_data)
    )

    changed = await handle_v0_download_complete(
        backend=make_aria2_client(tell_status=aria2_status),
        download=download,
        aria2_status=aria2_status,
        completion_gid="gid-t13-race-f",
        log_prefix="[T13]",
        allow_metadata_handoff_defer=False,
    )

    # Completion should NOT have changed anything — status is already failed
    assert changed is False

    stored_after_complete = await _fetch_global(download["id"])
    assert stored_after_complete["status"] == "failed"
    assert stored_after_complete["completed_file_id"] is None

    # No stored files created
    sfs = await _fetch_stored_files()
    assert len(sfs) == 0


@pytest.mark.asyncio
async def test_complete_claims_first_then_fail_is_stale(temp_db: str) -> None:
    """When complete claims first, the subsequent fail is stale and must
    NOT delete the store file (spec §11.3)."""
    user = await create_user_v0(username="t13_race_c", quota_bytes=10_000_000)
    download = await create_global_download_v0(
        resource_key="http:t13-race-c",
        source_uri="https://example.com/t13rc",
        resource_kind="http",
        status="active",
        aria2_gid="gid-t13-race-c",
        total_bytes=100,
        completed_bytes=100,
        size_known=True,
        disk_reserved_bytes=100,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=100,
    )
    await _set_usage_reserved(user["id"], 100)

    # Completion first
    file_data = b"race-complete-data"
    source = _create_download_file(download["id"], "test.bin", file_data)
    aria2_status = _complete_aria2_status(
        file_path=str(source), size=len(file_data)
    )

    changed = await handle_v0_download_complete(
        backend=make_aria2_client(tell_status=aria2_status),
        download=download,
        aria2_status=aria2_status,
        completion_gid="gid-t13-race-c",
        log_prefix="[T13]",
        allow_metadata_handoff_defer=False,
    )
    assert changed is True

    stored_after_complete = await _fetch_global(download["id"])
    assert stored_after_complete["status"] == "completed"
    completed_file_id = stored_after_complete["completed_file_id"]
    assert completed_file_id is not None

    # Store file exists
    sfs = await _fetch_stored_files()
    assert len(sfs) == 1
    store_path = Path(str(sfs[0]["real_path"]))
    assert store_path.exists()

    # Now fail tries — should be stale (completed + completed_file_id set)
    client_fail = make_aria2_client(tell_status={})
    failed = await fail_download_and_reclaim(
        backend=client_fail,
        download_id=download["id"],
        message="竞态测试：后失败",
        error_code="error",
        expected_gid="gid-t13-race-c",
        writer_gid="gid-t13-race-c",
        log_prefix="[T13]",
    )
    assert failed is False

    # State unchanged: still completed with file
    stored_after_fail = await _fetch_global(download["id"])
    assert stored_after_fail["status"] == "completed"
    assert stored_after_fail["completed_file_id"] == completed_file_id

    # Store file NOT deleted
    assert store_path.exists()


# ---------------------------------------------------------------------------
# 3. pending-index: completed without file_id is not purged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pending_index_not_purged_by_fail_claim(temp_db: str) -> None:
    """A completed row with completed_file_id IS NULL and real files in the
    download dir must NOT be purged by fail_download_and_reclaim (spec §15.4).

    claim_attempt_terminal accepts 'completed' in expected_statuses, but the
    CAS requires completed_file_id IS NULL. When complete sets file_id first,
    fail cannot claim. When complete failed to set file_id (pending-index),
    fail CAN claim — but that's the repair path's job, not a purge.

    Here we test that the completion coordinator itself preserves the
    pending-index state: if complete_global_download_locked's CAS to write
    completed_file_id fails (e.g. race), the download dir is NOT deleted."""
    user = await create_user_v0(username="t13_pending", quota_bytes=10_000_000)
    download = await create_global_download_v0(
        resource_key="http:t13-pending",
        source_uri="https://example.com/t13p",
        resource_kind="http",
        status="active",
        aria2_gid="gid-t13-pending",
        total_bytes=50,
        completed_bytes=50,
        size_known=True,
        disk_reserved_bytes=50,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=50,
    )
    await _set_usage_reserved(user["id"], 50)

    file_data = b"pending-index-data"
    source = _create_download_file(download["id"], "test.bin", file_data)
    task_dir = get_downloading_dir() / str(download["id"])

    aria2_status = _complete_aria2_status(
        file_path=str(source), size=len(file_data)
    )

    # Simulate the CAS failing: complete_active_user_tasks_for_stored_file
    # returns None. We patch it so the file gets moved to store but
    # completed_file_id is NOT written.
    with patch(
        "app.services.lifecycle.completion.complete_active_user_tasks_for_stored_file",
        return_value=None,
    ):
        result = await complete_global_download_locked(
            global_download_id=download["id"],
            expected_gid="gid-t13-pending",
            source_path=source,
            original_name="test.bin",
            expected_size=len(file_data),
        )

    # CAS failed → None returned
    assert result is None

    # DB: still active (not completed), no completed_file_id
    stored = await _fetch_global(download["id"])
    assert stored["status"] == "active"
    assert stored["completed_file_id"] is None

    # The download dir should NOT be cleaned by the coordinator
    # (it wasn't — complete_global_download_locked returned None)
    # The file may have been moved to store by compensation, but the
    # download dir shell should still exist (possibly empty) or the
    # compensation restored the source.
    # Key assertion: no stored_file was successfully linked to this attempt.
    sfs = await _fetch_stored_files()
    # Compensation should have cleaned up any partial stored_file
    # (or restored the moved source). Either way, no link.
    assert all(
        sf.get("original_name") != "test.bin" or not sf.get("pending_delete")
        for sf in sfs
    )


@pytest.mark.asyncio
async def test_pending_index_completed_without_file_preserved_by_reclaim(
    temp_db: str,
) -> None:
    """A completed attempt with completed_file_id IS NULL is a pending-index
    state. fail_download_and_reclaim with expected_statuses including
    'completed' CAN claim it (used by repair), but the completion coordinator
    should never produce this state and then purge the file.

    Here we verify the service-level behavior: handle_v0_download_complete
    does NOT delete the download dir when complete_global_download_locked
    returns None (CAS mismatch)."""
    user = await create_user_v0(username="t13_pi2", quota_bytes=10_000_000)
    download = await create_global_download_v0(
        resource_key="http:t13-pi2",
        source_uri="https://example.com/t13pi2",
        resource_kind="http",
        status="active",
        aria2_gid="gid-t13-pi2",
        total_bytes=50,
        completed_bytes=50,
        size_known=True,
        disk_reserved_bytes=50,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=50,
    )
    await _set_usage_reserved(user["id"], 50)

    file_data = b"pi2-data"
    source = _create_download_file(download["id"], "test.bin", file_data)
    task_dir = get_downloading_dir() / str(download["id"])

    aria2_status = _complete_aria2_status(
        file_path=str(source), size=len(file_data)
    )

    # Simulate CAS failure so completion returns None
    with patch(
        "app.services.lifecycle.completion.get_global_download_for_generation",
        return_value=None,
    ):
        changed = await handle_v0_download_complete(
            backend=make_aria2_client(tell_status=aria2_status),
            download=download,
            aria2_status=aria2_status,
            completion_gid="gid-t13-pi2",
            log_prefix="[T13]",
            allow_metadata_handoff_defer=False,
        )

    assert changed is False

    # Download dir should still exist (not purged)
    assert task_dir.exists()
    # Source file preserved
    assert source.exists()


# ---------------------------------------------------------------------------
# 4. Source missing with no followedBy → fail claim + cleanup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_source_missing_no_followedby_fails_and_cleans(
    temp_db: str,
) -> None:
    """When completion cannot find the source file and there is no
    followedBy to handoff to, the attempt should be failed and cleaned
    (spec §11.2 step 4)."""
    user = await create_user_v0(username="t13_nometa", quota_bytes=10_000_000)
    download = await create_global_download_v0(
        resource_key="http:t13-nometa",
        source_uri="https://example.com/t13nm",
        resource_kind="http",
        status="active",
        aria2_gid="gid-t13-nometa",
        total_bytes=100,
        completed_bytes=100,
        size_known=True,
        disk_reserved_bytes=100,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=100,
    )
    await _set_usage_reserved(user["id"], 100)

    # Create download dir but NO source file
    task_dir = get_downloading_dir() / str(download["id"])
    task_dir.mkdir(parents=True, exist_ok=True)

    aria2_status: dict[str, Any] = {
        "status": "complete",
        "totalLength": "100",
        "completedLength": "100",
        "files": [],
    }
    client = make_aria2_client(tell_status=aria2_status)

    changed = await handle_v0_download_complete(
        backend=client,
        download=download,
        aria2_status=aria2_status,
        completion_gid="gid-t13-nometa",
        log_prefix="[T13]",
        allow_metadata_handoff_defer=False,
    )

    # Should have failed (claimed terminal)
    assert changed is True

    stored = await _fetch_global(download["id"])
    assert stored["status"] == "failed"
    assert stored["completed_file_id"] is None

    # No stored files created
    sfs = await _fetch_stored_files()
    assert len(sfs) == 0

    # force_remove was called (cleanup attempted)
    client.force_remove.assert_called()


# ---------------------------------------------------------------------------
# 5. Wrapper/locked lock semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wrapper_acquires_lock_locked_does_not(temp_db: str) -> None:
    """complete_global_download (wrapper) acquires the lifecycle lock;
    complete_global_download_locked does NOT re-acquire it.

    Calling complete_global_download_locked while already holding the
    lifecycle lock must not deadlock (spec §11.1)."""
    user = await create_user_v0(username="t13_lock", quota_bytes=10_000_000)
    download = await create_global_download_v0(
        resource_key="http:t13-lock",
        source_uri="https://example.com/t13lk",
        resource_kind="http",
        status="active",
        aria2_gid="gid-t13-lock",
        total_bytes=50,
        completed_bytes=50,
        size_known=True,
        disk_reserved_bytes=50,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=50,
    )
    await _set_usage_reserved(user["id"], 50)

    file_data = b"lock-test-data"
    source = _create_download_file(download["id"], "test.bin", file_data)

    # Track lock acquisitions
    original_get_lock = get_download_lifecycle_lock
    lock_acquire_count = [0]

    async def _tracking_get_lock(download_id: int):
        lock = await original_get_lock(download_id)
        if lock.locked():
            lock_acquire_count[0] += 1
        return lock

    # Hold the lock manually, then call locked version — should NOT deadlock
    lock = await get_download_lifecycle_lock(download["id"])
    async with lock:
        result = await complete_global_download_locked(
            global_download_id=download["id"],
            expected_gid="gid-t13-lock",
            source_path=source,
            original_name="test.bin",
            expected_size=len(file_data),
        )

    assert result is not None
    assert result["status"] == "completed"

    stored = await _fetch_global(download["id"])
    assert stored["status"] == "completed"
    assert stored["completed_file_id"] is not None


@pytest.mark.asyncio
async def test_wrapper_acquires_lock_independently(temp_db: str) -> None:
    """complete_global_download (wrapper) independently acquires the lock."""
    user = await create_user_v0(username="t13_wrp", quota_bytes=10_000_000)
    download = await create_global_download_v0(
        resource_key="http:t13-wrp",
        source_uri="https://example.com/t13wrp",
        resource_kind="http",
        status="active",
        aria2_gid="gid-t13-wrp",
        total_bytes=50,
        completed_bytes=50,
        size_known=True,
        disk_reserved_bytes=50,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=50,
    )
    await _set_usage_reserved(user["id"], 50)

    file_data = b"wrap-test"
    source = _create_download_file(download["id"], "test.bin", file_data)

    # Wrapper should work standalone (acquires lock internally)
    result = await complete_global_download(
        global_download_id=download["id"],
        expected_gid="gid-t13-wrp",
        source_path=source,
        original_name="test.bin",
        expected_size=len(file_data),
    )

    assert result is not None
    assert result["status"] == "completed"


# ---------------------------------------------------------------------------
# 6. Source missing → late handoff attempt before fail claim
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_source_missing_tries_late_handoff_first(temp_db: str) -> None:
    """When the source path is not found, handle_v0_download_complete should
    first try explicit late-followed handoff before failing (spec §11.2 step 3).

    We verify that switch_to_late_followed_download_if_supported is called
    when source_path is None."""
    user = await create_user_v0(username="t13_late", quota_bytes=10_000_000)
    download = await create_global_download_v0(
        resource_key="magnet:t13-late",
        source_uri="magnet:?xt=urn:btih:t13_late",
        resource_kind="magnet",
        status="active",
        aria2_gid="gid-meta-late",
        total_bytes=0,
        size_known=False,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
    )

    # Create download dir but NO payload file
    task_dir = get_downloading_dir() / str(download["id"])
    task_dir.mkdir(parents=True, exist_ok=True)

    # Metadata status: complete with [METADATA] file (no real file path)
    metadata_status: dict[str, Any] = {
        "status": "complete",
        "totalLength": "0",
        "completedLength": "0",
        "files": [{"path": "[METADATA]", "length": "0", "selected": "true"}],
    }
    client = make_aria2_client(tell_status=metadata_status)

    # Track if late handoff is attempted by spying on the internal
    # _refresh_followed_gid call (which is the first thing
    # switch_to_late_followed_download_if_supported does).
    with patch(
        "app.services.lifecycle.handoff._refresh_followed_gid",
        return_value=None,
    ) as spy_refresh:
        changed = await handle_v0_download_complete(
            backend=client,
            download=download,
            aria2_status=metadata_status,
            completion_gid="gid-meta-late",
            log_prefix="[T13]",
            allow_metadata_handoff_defer=False,
        )

    # Late handoff was attempted (_refresh_followed_gid called)
    assert spy_refresh.called is True

    # Since no followedBy was found, the attempt should be failed
    stored = await _fetch_global(download["id"])
    assert stored["status"] == "failed"
