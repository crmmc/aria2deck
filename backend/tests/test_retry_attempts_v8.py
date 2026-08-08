"""M2 retry attempts: terminal rows are archived; retry creates new attempts."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.core.config import settings
from app.repositories.downloads import (
    get_global_by_resource_key,
    get_global_download_by_id,
)
from app.services.download_service import create_user_download
from app.services.storage import get_store_path_for_hash
from tests.fakes import make_aria2_client
from tests.helpers_v0 import (
    create_global_download_v0,
    create_user_file_v0,
    create_user_task_v0,
    create_user_v0,
)


@pytest.mark.asyncio
async def test_failed_retry_creates_new_attempt_ids(temp_db: str) -> None:
    user = await create_user_v0(username="retry_new_attempt", quota_bytes=1000)
    client = make_aria2_client(add_uri=["gid-retry-old", "gid-retry-new"])

    first = await create_user_download(
        user_id=user["id"],
        quota_bytes=user["quota_bytes"],
        uri="https://example.com/new-attempt.bin",
        resource_key="http:new-attempt",
        resource_kind="http",
        display_name="new-attempt.bin",
        total_bytes=100,
        aria2_client=client,
    )
    from app.services.aria2_lifecycle_service import fail_download_and_reclaim

    changed = await fail_download_and_reclaim(
        client=client,
        download_id=first["global_download_id"],
        expected_gid="gid-retry-old",
        writer_gid="gid-retry-old",
        message="failed",
        error_code="failure",
        log_prefix="[Test]",
    )
    assert changed is True

    second = await create_user_download(
        user_id=user["id"],
        quota_bytes=user["quota_bytes"],
        uri="https://example.com/new-attempt.bin",
        resource_key="http:new-attempt",
        resource_kind="http",
        display_name="new-attempt.bin",
        total_bytes=100,
        aria2_client=client,
    )

    old_global = await get_global_download_by_id(int(first["global_download_id"]))
    new_global = await get_global_download_by_id(int(second["global_download_id"]))
    assert old_global is not None and new_global is not None
    assert second["global_download_id"] != first["global_download_id"]
    assert second["id"] != first["id"]
    assert old_global["status"] == "failed"
    assert new_global["status"] == "active"
    assert new_global["aria2_gid"] == "gid-retry-new"


@pytest.mark.asyncio
async def test_terminal_global_download_never_resurrected(temp_db: str) -> None:
    terminal = await create_global_download_v0(
        resource_key="http:terminal-archived",
        resource_kind="http",
        source_uri="https://example.com/terminal.bin",
        status="failed",
        aria2_gid=None,
        display_name="terminal.bin",
        total_bytes=10,
        completed_bytes=0,
    )
    user = await create_user_v0(username="terminal_archive", quota_bytes=1000)
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=terminal["id"],
        status="failed",
        reserved_bytes=0,
        display_name="terminal.bin",
    )
    client = make_aria2_client(add_uri="gid-terminal-new")

    task = await create_user_download(
        user_id=user["id"],
        quota_bytes=user["quota_bytes"],
        uri="https://example.com/terminal.bin",
        resource_key="http:terminal-archived",
        resource_kind="http",
        display_name="terminal.bin",
        total_bytes=10,
        aria2_client=client,
    )

    stored_terminal = await get_global_download_by_id(int(terminal["id"]))
    created = await get_global_download_by_id(int(task["global_download_id"]))
    assert stored_terminal is not None
    assert created is not None
    assert stored_terminal["status"] == "failed"
    assert created["id"] != terminal["id"]
    assert created["status"] == "active"


@pytest.mark.asyncio
async def test_completed_same_user_duplicate_rejected_without_store_file(
    temp_db: str,
) -> None:
    user = await create_user_v0(username="completed_same_user", quota_bytes=1000)
    completed = await create_global_download_v0(
        resource_key="http:completed-duplicate",
        resource_kind="http",
        source_uri="https://example.com/completed-duplicate.bin",
        status="completed",
        aria2_gid=None,
        display_name="original.bin",
        total_bytes=10,
        completed_bytes=10,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=completed["id"],
        status="completed",
        display_name="original.bin",
    )
    client = make_aria2_client()

    from app.services.download_service import DuplicateTaskError

    with pytest.raises(DuplicateTaskError, match="任务已存在"):
        await create_user_download(
            user_id=user["id"],
            quota_bytes=user["quota_bytes"],
            uri="https://example.com/completed-duplicate.bin",
            resource_key="http:completed-duplicate",
            resource_kind="http",
            display_name="replacement.bin",
            total_bytes=10,
            aria2_client=client,
        )


@pytest.mark.asyncio
async def test_completed_other_user_attaches_store_file(temp_db: str) -> None:
    owner = await create_user_v0(username="store_owner", quota_bytes=1000)
    other = await create_user_v0(username="store_attach", quota_bytes=1000)
    store_path = get_store_path_for_hash("retry_attempt_store_hash")
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_bytes(b"completed")
    user_file = await create_user_file_v0(
        user_id=owner["id"],
        real_path=store_path,
        content_hash="retry_attempt_store_hash",
        display_name="real.bin",
        size_bytes=9,
    )
    completed = await create_global_download_v0(
        resource_key="http:completed-attach",
        resource_kind="http",
        source_uri="https://example.com/completed-attach.bin",
        status="completed",
        aria2_gid=None,
        display_name="real.bin",
        total_bytes=9,
        completed_bytes=9,
        completed_file_id=user_file["stored_file_id"],
    )
    client = make_aria2_client()

    task = await create_user_download(
        user_id=other["id"],
        quota_bytes=other["quota_bytes"],
        uri="https://example.com/completed-attach.bin",
        resource_key="http:completed-attach",
        resource_kind="http",
        display_name="alias.bin",
        total_bytes=0,
        aria2_client=client,
    )

    assert task["global_download_id"] == completed["id"]
    assert task["status"] == "completed"
    client.add_uri.assert_not_awaited()
