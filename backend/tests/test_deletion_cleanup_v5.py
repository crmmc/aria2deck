from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from sqlalchemy import select, update

from app.db.engine import transaction
from app.domain.errors import NotFoundError
from app.db.schema import stored_files, users
from app.repositories import files as files_repo
from app.services import deletion_cleanup
from app.services.deletion_cleanup import DeletionCleanupManager
from app.services.file_service import (
    delete_user_file_reference_v0_result,
    resolve_download_target_with_read_lease,
)
from tests.helpers_v0 import now_ms


async def _stored(file_id: int) -> dict | None:
    async with transaction() as conn:
        row = (
            await conn.execute(
                select(stored_files).where(stored_files.c.id == file_id)
            )
        ).mappings().first()
    return dict(row) if row else None


@pytest.mark.asyncio
async def test_file_delete_retries_then_finalizes_missing_path(
    user_file: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = deletion_cleanup._delete_stored_path

    def fail_delete(*_: object) -> None:
        raise OSError("disk unavailable")

    result = await delete_user_file_reference_v0_result(1, user_file["id"])
    assert result.state == "pending"
    assert result.accepted is True

    monkeypatch.setattr(deletion_cleanup, "_delete_stored_path", fail_delete)
    await DeletionCleanupManager.run_once()
    pending = await _stored(user_file["stored_file_id"])
    assert pending is not None
    assert pending["delete_attempts"] == 1
    assert pending["delete_lease_token"] is None
    assert pending["delete_error"] == "物理清理失败：OSError"

    Path(user_file["real_path"]).unlink()
    async with transaction() as conn:
        await conn.execute(
            update(stored_files)
            .where(stored_files.c.id == user_file["stored_file_id"])
            .values(delete_next_retry_at_ms=0)
        )
    monkeypatch.setattr(deletion_cleanup, "_delete_stored_path", original)
    await DeletionCleanupManager.run_once()
    assert await _stored(user_file["stored_file_id"]) is None


@pytest.mark.asyncio
async def test_expired_claim_replays_existing_tombstone(user_file: dict) -> None:
    result = await delete_user_file_reference_v0_result(1, user_file["id"])
    assert result.accepted is True
    timestamp = now_ms()
    claimed = await files_repo.claim_due_stored_files(
        lease_token="crashed-worker",
        timestamp_ms=timestamp,
        lease_expires_at_ms=timestamp + 60_000,
        limit=1,
    )
    assert [row["id"] for row in claimed] == [user_file["stored_file_id"]]

    path = Path(user_file["real_path"])
    tombstone = path.parent / (
        f".{path.name}.aria2deck-delete-{user_file['stored_file_id']}"
    )
    os.replace(path, tombstone)
    async with transaction() as conn:
        await conn.execute(
            update(stored_files)
            .where(stored_files.c.id == user_file["stored_file_id"])
            .values(delete_lease_expires_at_ms=0)
        )

    await DeletionCleanupManager.run_once()
    assert not path.exists()
    assert not tombstone.exists()
    assert await _stored(user_file["stored_file_id"]) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("pending_table", [users, stored_files])
async def test_pending_record_rejects_new_read_after_existing_lease(
    test_user: dict,
    user_file: dict,
    pending_table,
) -> None:
    _, _, existing_lease = await resolve_download_target_with_read_lease(
        test_user["id"], user_file["content_hash"]
    )
    try:
        async with transaction() as conn:
            if pending_table is users:
                await conn.execute(
                    update(users)
                    .where(users.c.id == test_user["id"])
                    .values(pending_delete=1)
                )
            else:
                await conn.execute(
                    update(stored_files)
                    .where(stored_files.c.id == user_file["stored_file_id"])
                    .values(pending_delete=1)
                )
        with pytest.raises(NotFoundError, match="文件不存在"):
            await resolve_download_target_with_read_lease(
                test_user["id"], user_file["content_hash"]
            )
    finally:
        await existing_lease.release()


@pytest.mark.asyncio
async def test_file_cleanup_waits_for_existing_read_lease(user_file: dict) -> None:
    _, _, read_lease = await resolve_download_target_with_read_lease(
        1, user_file["content_hash"]
    )
    result = await delete_user_file_reference_v0_result(1, user_file["id"])
    assert result.accepted is True

    worker = asyncio.create_task(DeletionCleanupManager.run_once())
    await asyncio.sleep(0.05)
    assert not worker.done()

    await read_lease.release()
    await asyncio.wait_for(worker, timeout=1)
    assert await _stored(user_file["stored_file_id"]) is None
