"""T16: create / attach / Aria2 submit convergence (spec §12).

Verifies the unified creation flow:
  resource resolve → live attach / fresh INSERT → attempt lock → admit → submit
"""

from __future__ import annotations

import pytest

from app.db.engine import transaction
from app.db.schema import global_downloads, user_tasks
from app.repositories.downloads import (
    get_global_by_resource_key,
    get_global_download_by_id,
    get_user_task,
)
from app.services.download_service import (
    DuplicateTaskError,
    create_user_download,
    create_user_torrent_download,
)
from app.services.storage import get_store_path_for_hash
from tests.fakes import make_aria2_client
from tests.helpers_v0 import (
    create_global_download_v0,
    create_user_file_v0,
    create_user_task_v0,
    create_user_v0,
    now_ms,
)


@pytest.mark.asyncio
async def test_terminal_row_not_resurrected(temp_db: str) -> None:
    """A new request for a resource with a terminal row creates a fresh attempt."""
    user = await create_user_v0(username="terminal_user", quota_bytes=10_000)

    terminal = await create_global_download_v0(
        resource_key="http:terminal-resurrect",
        resource_kind="http",
        source_uri="https://example.com/terminal-resurrect.bin",
        status="failed",
        aria2_gid=None,
        display_name="terminal-resurrect.bin",
        total_bytes=100,
        completed_bytes=0,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=terminal["id"],
        status="failed",
        reserved_bytes=0,
        display_name="terminal-resurrect.bin",
    )

    client = make_aria2_client(add_uri="gid-terminal-new")

    task = await create_user_download(
        user_id=user["id"],
        quota_bytes=user["quota_bytes"],
        uri="https://example.com/terminal-resurrect.bin",
        resource_key="http:terminal-resurrect",
        resource_kind="http",
        display_name="terminal-resurrect.bin",
        total_bytes=100,
        aria2_client=client,
    )

    stored_terminal = await get_global_download_by_id(int(terminal["id"]))
    created = await get_global_download_by_id(int(task["global_download_id"]))
    assert stored_terminal is not None and created is not None
    assert stored_terminal["status"] == "failed"
    assert created["id"] != terminal["id"]
    assert created["status"] == "active"
    assert created["aria2_gid"] == "gid-terminal-new"


@pytest.mark.asyncio
async def test_live_concurrent_creates_one_attempt(temp_db: str) -> None:
    """Two users requesting the same live resource share one attempt."""
    user_a = await create_user_v0(username="live_a", quota_bytes=10_000)
    user_b = await create_user_v0(username="live_b", quota_bytes=10_000)

    client = make_aria2_client(add_uri="gid-live-shared")

    task_a = await create_user_download(
        user_id=user_a["id"],
        quota_bytes=user_a["quota_bytes"],
        uri="https://example.com/shared.bin",
        resource_key="http:shared",
        resource_kind="http",
        display_name="shared.bin",
        total_bytes=100,
        aria2_client=client,
    )
    task_b = await create_user_download(
        user_id=user_b["id"],
        quota_bytes=user_b["quota_bytes"],
        uri="https://example.com/shared.bin",
        resource_key="http:shared",
        resource_kind="http",
        display_name="shared.bin",
        total_bytes=100,
        aria2_client=client,
    )

    assert task_a["global_download_id"] == task_b["global_download_id"]
    assert client.add_uri.await_count == 1


@pytest.mark.asyncio
async def test_completed_store_attach_no_add_uri(temp_db: str) -> None:
    """Attaching a completed download with a stored file does not call addUri."""
    owner = await create_user_v0(username="store_owner_attach", quota_bytes=10_000)
    other = await create_user_v0(username="store_attach_user", quota_bytes=10_000)

    store_path = get_store_path_for_hash("attach_store_hash_16")
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_bytes(b"completed-data")
    user_file = await create_user_file_v0(
        user_id=owner["id"],
        real_path=store_path,
        content_hash="attach_store_hash_16",
        display_name="real.bin",
        size_bytes=14,
    )
    completed = await create_global_download_v0(
        resource_key="http:completed-attach-16",
        resource_kind="http",
        source_uri="https://example.com/completed-attach-16.bin",
        status="completed",
        aria2_gid=None,
        display_name="real.bin",
        total_bytes=14,
        completed_bytes=14,
        completed_file_id=user_file["stored_file_id"],
    )

    client = make_aria2_client()

    task = await create_user_download(
        user_id=other["id"],
        quota_bytes=other["quota_bytes"],
        uri="https://example.com/completed-attach-16.bin",
        resource_key="http:completed-attach-16",
        resource_kind="http",
        display_name="alias.bin",
        total_bytes=0,
        aria2_client=client,
    )

    assert task["global_download_id"] == completed["id"]
    assert task["status"] == "completed"
    client.add_uri.assert_not_awaited()


@pytest.mark.asyncio
async def test_submit_no_gid_claims_failed(temp_db: str) -> None:
    """When submit returns no GID, the attempt is claimed failed with no writer."""
    user = await create_user_v0(username="no_gid_user", quota_bytes=10_000)

    client = make_aria2_client(add_uri=RuntimeError("aria2 refused"))

    with pytest.raises(Exception, match="内部下载任务提交失败"):
        await create_user_download(
            user_id=user["id"],
            quota_bytes=user["quota_bytes"],
            uri="https://example.com/no-gid.bin",
            resource_key="http:no-gid",
            resource_kind="http",
            display_name="no-gid.bin",
            total_bytes=100,
            aria2_client=client,
        )

    global_row = await get_global_by_resource_key("http:no-gid")
    assert global_row is not None
    assert global_row["status"] == "failed"
    assert global_row["aria2_gid"] is None

    user_task = await get_user_task(user["id"], int(global_row["id"]))
    assert user_task is not None
    assert user_task["status"] == "failed"


@pytest.mark.asyncio
async def test_submit_gid_returned_but_assign_fails_gid_not_persisted(
    temp_db: str,
) -> None:
    """When assign_submitted_gid fails, the local GID is cleaned up, not persisted.

    We force the failure by making assign_submitted_gid return None.  This
    simulates the DB row having changed between the submit and the CAS write.
    The returned GID must not appear on any global_downloads row.
    """
    user = await create_user_v0(username="gid_not_persisted", quota_bytes=10_000)

    client = make_aria2_client(
        add_uri="gid-local-unpersisted",
        force_remove="gid-local-unpersisted",
    )

    import app.services.download_service as ds_module

    original_assign = ds_module.assign_submitted_gid

    async def _failing_assign(*, download_id, gid, status):
        return None

    ds_module.assign_submitted_gid = _failing_assign
    try:
        with pytest.raises(Exception):
            await create_user_download(
                user_id=user["id"],
                quota_bytes=user["quota_bytes"],
                uri="https://example.com/gid-not-persisted.bin",
                resource_key="http:gid-not-persisted",
                resource_kind="http",
                display_name="gid-not-persisted.bin",
                total_bytes=100,
                aria2_client=client,
            )
    finally:
        ds_module.assign_submitted_gid = original_assign

    global_row = await get_global_by_resource_key("http:gid-not-persisted")
    assert global_row is not None
    # The local GID must never appear in the DB row.
    assert global_row["aria2_gid"] != "gid-local-unpersisted"
    # The attempt should be in a terminal state (failed via cleanup).
    assert global_row["status"] == "failed"

    # The local aria2 GID should have been best-effort stopped.
    client.force_remove.assert_awaited()


@pytest.mark.asyncio
async def test_retry_creates_new_attempt(temp_db: str) -> None:
    """Retry after failure creates a new attempt with a new ID (same as create)."""
    user = await create_user_v0(username="retry_user", quota_bytes=10_000)

    client = make_aria2_client(add_uri=["gid-retry-first", "gid-retry-second"])

    first = await create_user_download(
        user_id=user["id"],
        quota_bytes=user["quota_bytes"],
        uri="https://example.com/retry-create.bin",
        resource_key="http:retry-create",
        resource_kind="http",
        display_name="retry-create.bin",
        total_bytes=100,
        aria2_client=client,
    )

    from app.services.aria2_lifecycle_service import fail_download_and_reclaim

    await fail_download_and_reclaim(
        client=client,
        download_id=first["global_download_id"],
        expected_gid="gid-retry-first",
        writer_gid="gid-retry-first",
        message="failed",
        error_code="failure",
        log_prefix="[Test]",
    )

    second = await create_user_download(
        user_id=user["id"],
        quota_bytes=user["quota_bytes"],
        uri="https://example.com/retry-create.bin",
        resource_key="http:retry-create",
        resource_kind="http",
        display_name="retry-create.bin",
        total_bytes=100,
        aria2_client=client,
    )

    assert second["global_download_id"] != first["global_download_id"]
    assert second["id"] != first["id"]

    old = await get_global_download_by_id(int(first["global_download_id"]))
    new = await get_global_download_by_id(int(second["global_download_id"]))
    assert old is not None and new is not None
    assert old["status"] == "failed"
    assert new["status"] == "active"
    assert new["aria2_gid"] == "gid-retry-second"


@pytest.mark.asyncio
async def test_magnet_create_uses_magnet_submit(temp_db: str) -> None:
    """Magnet resource kind submits via add_uri with the magnet URI."""
    user = await create_user_v0(username="magnet_user", quota_bytes=10_000)

    client = make_aria2_client(add_uri="gid-magnet-16")

    task = await create_user_download(
        user_id=user["id"],
        quota_bytes=user["quota_bytes"],
        uri="magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567",
        resource_key="0123456789abcdef0123456789abcdef01234567",
        resource_kind="magnet",
        display_name="magnet-test",
        total_bytes=0,
        aria2_client=client,
    )

    assert task["status"] in {"queued", "active"}
    client.add_uri.assert_awaited_once()

    global_row = await get_global_download_by_id(int(task["global_download_id"]))
    assert global_row is not None
    assert global_row["aria2_gid"] == "gid-magnet-16"
    assert global_row["resource_kind"] == "magnet"


@pytest.mark.asyncio
async def test_torrent_create_uses_add_torrent(temp_db: str) -> None:
    """Torrent resource kind submits via add_torrent."""
    user = await create_user_v0(username="torrent_user", quota_bytes=10_000)

    client = make_aria2_client(add_torrent="gid-torrent-16")

    task = await create_user_torrent_download(
        user_id=user["id"],
        quota_bytes=user["quota_bytes"],
        torrent_data="d8:announce20:http://tracker.test4:infod6:lengthi100e4:name8:test.txtee",
        resource_key="torrent:hash16",
        source_uri="[torrent]",
        display_name="test.txt",
        total_bytes=100,
        aria2_client=client,
    )

    assert task["status"] in {"queued", "active"}
    client.add_torrent.assert_awaited_once()
    client.add_uri.assert_not_awaited()

    global_row = await get_global_download_by_id(int(task["global_download_id"]))
    assert global_row is not None
    assert global_row["aria2_gid"] == "gid-torrent-16"
    assert global_row["resource_kind"] == "torrent"


@pytest.mark.asyncio
async def test_submit_ensure_only_submits_once(temp_db: str) -> None:
    """_ensure_download_submitted does not re-submit an already-submitted attempt."""
    user = await create_user_v0(username="once_user", quota_bytes=10_000)
    client = make_aria2_client(add_uri="gid-once")

    task = await create_user_download(
        user_id=user["id"],
        quota_bytes=user["quota_bytes"],
        uri="https://example.com/once.bin",
        resource_key="http:once",
        resource_kind="http",
        display_name="once.bin",
        total_bytes=100,
        aria2_client=client,
    )
    assert client.add_uri.await_count == 1

    # Second user attaching to the same live resource should NOT trigger another submit.
    user2 = await create_user_v0(username="once_user2", quota_bytes=10_000)
    task2 = await create_user_download(
        user_id=user2["id"],
        quota_bytes=user2["quota_bytes"],
        uri="https://example.com/once.bin",
        resource_key="http:once",
        resource_kind="http",
        display_name="once.bin",
        total_bytes=100,
        aria2_client=client,
    )
    assert client.add_uri.await_count == 1
    assert task2["global_download_id"] == task["global_download_id"]
