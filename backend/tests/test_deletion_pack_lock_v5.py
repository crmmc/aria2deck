from __future__ import annotations

import asyncio
import threading

import pytest

from app.services import deletion_cleanup
from app.services.deletion_cleanup import DeletionCleanupManager
from app.services.file_service import delete_user_file_reference_v0_result
from app.services.storage_locks import get_content_hash_lock


@pytest.mark.asyncio
async def test_file_worker_holds_content_lock_through_physical_delete(
    user_file: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    original = deletion_cleanup._delete_stored_path

    def blocked_delete(row: dict, cancel_event: threading.Event) -> None:
        entered.set()
        assert release.wait(2)
        original(row, cancel_event)

    result = await delete_user_file_reference_v0_result(1, user_file["id"])
    assert result.accepted is True
    monkeypatch.setattr(deletion_cleanup, "_delete_stored_path", blocked_delete)

    worker = asyncio.create_task(DeletionCleanupManager.run_once())
    assert await asyncio.to_thread(entered.wait, 2)
    lock = await get_content_hash_lock(user_file["content_hash"])
    acquired = asyncio.Event()

    async def compete() -> None:
        async with lock:
            acquired.set()

    contender = asyncio.create_task(compete())
    await asyncio.sleep(0)
    assert not acquired.is_set()

    release.set()
    await worker
    await contender
    assert acquired.is_set()
