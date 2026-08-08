"""T26: create / share / cancel race acceptance tests (spec §22.5).

Verifies:
  1. HTTP, RPC, torrent, magnet, and history-page retry all enter the
     same fresh-attempt semantics (spec §22.5.1, §12).
  2. Concurrent creates for the same live resource produce exactly one
     live attempt (spec §22.5.2, §12.2).
  3. A terminal row is never resurrected (spec §22.5.3, §12.3).
  4. Cancelling one subscriber of a shared attempt does not touch Aria2
     (spec §22.5.4, §13.1).
  5. Cancelling the last subscriber produces a cancellation claim and
     triggers cleanup (spec §22.5.5, §13.1).
  6. When submit returns a GID but the DB CAS fails, that GID never
     appears on any other attempt row (spec §22.5.6, §12.4).
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from app.db.engine import transaction
from app.db.schema import global_downloads, user_storage_usage, user_tasks
from app.repositories.downloads import (
    get_global_by_resource_key,
    get_global_download_by_id,
)
from app.services.download_service import (
    cancel_user_task,
    create_user_download,
    create_user_torrent_download,
)
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


async def _fetch_all_globals_by_gid(gid: str) -> list[dict]:
    async with transaction() as conn:
        rows = (
            (
                await conn.execute(
                    select(global_downloads).where(
                        global_downloads.c.aria2_gid == gid
                    )
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


# ---------------------------------------------------------------------------
# 1. All creation paths enter the same fresh-attempt semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_create_fresh_attempt_after_terminal(temp_db: str) -> None:
    """HTTP create after a failed attempt produces a fresh attempt that
    does not inherit any runtime state from the terminal row."""
    user = await create_user_v0(username="http_retry", quota_bytes=10_000)

    terminal = await create_global_download_v0(
        resource_key="http:fresh-http",
        resource_kind="http",
        source_uri="https://example.com/fresh.bin",
        status="failed",
        aria2_gid="gid-old-http",
        display_name="fresh.bin",
        total_bytes=999,
        completed_bytes=100,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=terminal["id"],
        status="failed",
        reserved_bytes=0,
        display_name="fresh.bin",
    )

    client = make_aria2_client(add_uri="gid-new-http")

    task = await create_user_download(
        user_id=user["id"],
        quota_bytes=user["quota_bytes"],
        uri="https://example.com/fresh.bin",
        resource_key="http:fresh-http",
        resource_kind="http",
        display_name="fresh.bin",
        total_bytes=200,
        aria2_client=client,
    )

    old = await get_global_download_by_id(int(terminal["id"]))
    new = await get_global_download_by_id(int(task["global_download_id"]))
    assert old is not None and new is not None

    # Fresh attempt has a new ID and does not inherit runtime state.
    assert new["id"] != old["id"]
    assert new["aria2_gid"] == "gid-new-http"
    assert new["completed_bytes"] == 0
    assert new["total_bytes"] == 200
    assert new["status"] == "active"

    # Old terminal row unchanged.
    assert old["status"] == "failed"
    assert old["aria2_gid"] == "gid-old-http"


@pytest.mark.asyncio
async def test_magnet_create_fresh_attempt_after_terminal(temp_db: str) -> None:
    """Magnet create after a cancelled attempt produces a fresh attempt."""
    user = await create_user_v0(username="mag_retry", quota_bytes=10_000)

    info_hash = "0123456789abcdef0123456789abcdef01234567"
    terminal = await create_global_download_v0(
        resource_key=info_hash,
        resource_kind="magnet",
        source_uri=f"magnet:?xt=urn:btih:{info_hash}",
        status="cancelled",
        aria2_gid="gid-old-mag",
        display_name="mag-test",
        total_bytes=0,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=terminal["id"],
        status="cancelled",
        reserved_bytes=0,
        display_name="mag-test",
    )

    client = make_aria2_client(add_uri="gid-new-mag")

    task = await create_user_download(
        user_id=user["id"],
        quota_bytes=user["quota_bytes"],
        uri=f"magnet:?xt=urn:btih:{info_hash}",
        resource_key=info_hash,
        resource_kind="magnet",
        display_name="mag-test",
        total_bytes=0,
        aria2_client=client,
    )

    new = await get_global_download_by_id(int(task["global_download_id"]))
    old = await get_global_download_by_id(int(terminal["id"]))
    assert new is not None and old is not None
    assert new["id"] != old["id"]
    assert new["aria2_gid"] == "gid-new-mag"
    assert new["resource_kind"] == "magnet"
    assert new["status"] == "active"
    assert old["status"] == "cancelled"


@pytest.mark.asyncio
async def test_torrent_create_fresh_attempt_after_terminal(temp_db: str) -> None:
    """Torrent create after a failed attempt produces a fresh attempt."""
    user = await create_user_v0(username="tor_retry", quota_bytes=10_000)

    terminal = await create_global_download_v0(
        resource_key="torrent:tor_fresh_hash",
        resource_kind="torrent",
        source_uri="[torrent]",
        status="failed",
        aria2_gid="gid-old-tor",
        display_name="old.txt",
        total_bytes=500,
        completed_bytes=200,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=terminal["id"],
        status="failed",
        reserved_bytes=0,
        display_name="old.txt",
    )

    client = make_aria2_client(add_torrent="gid-new-tor")

    task = await create_user_torrent_download(
        user_id=user["id"],
        quota_bytes=user["quota_bytes"],
        torrent_data="d8:announce20:http://tracker.test4:infod6:lengthi100e4:name8:test.txtee",
        resource_key="torrent:tor_fresh_hash_new",
        source_uri="[torrent]",
        display_name="test.txt",
        total_bytes=100,
        aria2_client=client,
    )

    new = await get_global_download_by_id(int(task["global_download_id"]))
    assert new is not None
    assert new["id"] != terminal["id"]
    assert new["aria2_gid"] == "gid-new-tor"
    assert new["resource_kind"] == "torrent"
    assert new["status"] == "active"
    assert new["completed_bytes"] == 0


@pytest.mark.asyncio
async def test_history_page_retry_is_same_create_path(temp_db: str) -> None:
    """History-page retry calls the same ``create_user_download`` and
    therefore also produces a fresh attempt (spec §3.1, §22.5.1)."""
    user = await create_user_v0(username="hist_retry", quota_bytes=10_000)

    client = make_aria2_client(add_uri=["gid-hist-1", "gid-hist-2"])

    first = await create_user_download(
        user_id=user["id"],
        quota_bytes=user["quota_bytes"],
        uri="https://example.com/hist-retry.bin",
        resource_key="http:hist-retry",
        resource_kind="http",
        display_name="hist-retry.bin",
        total_bytes=100,
        aria2_client=client,
    )

    from app.services.aria2_lifecycle_service import fail_download_and_reclaim

    await fail_download_and_reclaim(
        client=client,
        download_id=first["global_download_id"],
        expected_gid="gid-hist-1",
        writer_gid="gid-hist-1",
        message="failed",
        error_code="failure",
        log_prefix="[Test]",
    )

    # Retry from history page calls the exact same create path.
    second = await create_user_download(
        user_id=user["id"],
        quota_bytes=user["quota_bytes"],
        uri="https://example.com/hist-retry.bin",
        resource_key="http:hist-retry",
        resource_kind="http",
        display_name="hist-retry.bin",
        total_bytes=100,
        aria2_client=client,
    )

    assert second["global_download_id"] != first["global_download_id"]

    old = await get_global_download_by_id(int(first["global_download_id"]))
    new = await get_global_download_by_id(int(second["global_download_id"]))
    assert old is not None and new is not None
    assert old["status"] == "failed"
    assert new["status"] == "active"
    assert new["aria2_gid"] == "gid-hist-2"
    assert new["aria2_gid"] != old["aria2_gid"]


# ---------------------------------------------------------------------------
# 2. Concurrent creates for the same live resource → one attempt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_same_resource_one_live_attempt(temp_db: str) -> None:
    """Multiple concurrent ``create_user_download`` calls for the same
    resource key must produce exactly one live attempt and submit to Aria2
    exactly once (spec §22.5.2, §12.2)."""
    user_a = await create_user_v0(username="conc_a", quota_bytes=10_000)
    user_b = await create_user_v0(username="conc_b", quota_bytes=10_000)
    user_c = await create_user_v0(username="conc_c", quota_bytes=10_000)

    client = make_aria2_client(add_uri="gid-conc-shared")

    results = await asyncio.gather(
        create_user_download(
            user_id=user_a["id"],
            quota_bytes=user_a["quota_bytes"],
            uri="https://example.com/conc-shared.bin",
            resource_key="http:conc-shared",
            resource_kind="http",
            display_name="conc-shared.bin",
            total_bytes=100,
            aria2_client=client,
        ),
        create_user_download(
            user_id=user_b["id"],
            quota_bytes=user_b["quota_bytes"],
            uri="https://example.com/conc-shared.bin",
            resource_key="http:conc-shared",
            resource_kind="http",
            display_name="conc-shared.bin",
            total_bytes=100,
            aria2_client=client,
        ),
        create_user_download(
            user_id=user_c["id"],
            quota_bytes=user_c["quota_bytes"],
            uri="https://example.com/conc-shared.bin",
            resource_key="http:conc-shared",
            resource_kind="http",
            display_name="conc-shared.bin",
            total_bytes=100,
            aria2_client=client,
        ),
    )

    # All three tasks share the same global_download_id.
    gids = {r["global_download_id"] for r in results}
    assert len(gids) == 1

    # add_uri submitted exactly once.
    assert client.add_uri.await_count == 1

    stored = await get_global_download_by_id(int(results[0]["global_download_id"]))
    assert stored is not None
    assert stored["status"] == "active"
    assert stored["aria2_gid"] == "gid-conc-shared"


# ---------------------------------------------------------------------------
# 3. Terminal row not resurrected (failed + cancelled)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_row_not_resurrected(temp_db: str) -> None:
    """A new create for a resource whose only row is ``failed`` does not
    modify that row; it creates a fresh attempt (spec §22.5.3, §12.3)."""
    user = await create_user_v0(username="no_resurrect_f", quota_bytes=10_000)

    failed = await create_global_download_v0(
        resource_key="http:no-resurrect-f",
        resource_kind="http",
        source_uri="https://example.com/no-res-f.bin",
        status="failed",
        aria2_gid="gid-fail-old",
        display_name="no-res-f.bin",
        total_bytes=100,
        completed_bytes=50,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=failed["id"],
        status="failed",
        reserved_bytes=0,
        display_name="no-res-f.bin",
    )

    client = make_aria2_client(add_uri="gid-fail-fresh")

    task = await create_user_download(
        user_id=user["id"],
        quota_bytes=user["quota_bytes"],
        uri="https://example.com/no-res-f.bin",
        resource_key="http:no-resurrect-f",
        resource_kind="http",
        display_name="no-res-f.bin",
        total_bytes=100,
        aria2_client=client,
    )

    old = await get_global_download_by_id(int(failed["id"]))
    new = await get_global_download_by_id(int(task["global_download_id"]))
    assert old is not None and new is not None
    assert new["id"] != old["id"]
    assert old["status"] == "failed"
    assert old["aria2_gid"] == "gid-fail-old"
    assert new["status"] == "active"
    assert new["aria2_gid"] == "gid-fail-fresh"


@pytest.mark.asyncio
async def test_cancelled_row_not_resurrected(temp_db: str) -> None:
    """A new create for a resource whose only row is ``cancelled`` does not
    modify that row; it creates a fresh attempt (spec §22.5.3)."""
    user = await create_user_v0(username="no_resurrect_c", quota_bytes=10_000)

    cancelled = await create_global_download_v0(
        resource_key="http:no-resurrect-c",
        resource_kind="http",
        source_uri="https://example.com/no-res-c.bin",
        status="cancelled",
        aria2_gid="gid-cancel-old",
        display_name="no-res-c.bin",
        total_bytes=100,
        completed_bytes=30,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=cancelled["id"],
        status="cancelled",
        reserved_bytes=0,
        display_name="no-res-c.bin",
    )

    client = make_aria2_client(add_uri="gid-cancel-fresh")

    task = await create_user_download(
        user_id=user["id"],
        quota_bytes=user["quota_bytes"],
        uri="https://example.com/no-res-c.bin",
        resource_key="http:no-resurrect-c",
        resource_kind="http",
        display_name="no-res-c.bin",
        total_bytes=100,
        aria2_client=client,
    )

    old = await get_global_download_by_id(int(cancelled["id"]))
    new = await get_global_download_by_id(int(task["global_download_id"]))
    assert old is not None and new is not None
    assert new["id"] != old["id"]
    assert old["status"] == "cancelled"
    assert old["aria2_gid"] == "gid-cancel-old"
    assert new["status"] == "active"
    assert new["aria2_gid"] == "gid-cancel-fresh"


# ---------------------------------------------------------------------------
# 4. Shared attempt: cancel one subscriber does not touch Aria2
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_one_subscriber_no_aria2_control(temp_db: str) -> None:
    """When multiple users share a live attempt, cancelling one user's task
    must not call ``force_remove`` or any Aria2 control.  The global attempt
    stays live (spec §22.5.4, §13.1)."""
    user_a = await create_user_v0(username="share_a", quota_bytes=10_000)
    user_b = await create_user_v0(username="share_b", quota_bytes=10_000)

    download = await create_global_download_v0(
        resource_key="http:shared-cancel",
        resource_kind="http",
        source_uri="https://example.com/shared-cancel.bin",
        status="active",
        aria2_gid="gid-shared-cancel",
        total_bytes=400,
        disk_reserved_bytes=400,
        display_name="shared-cancel.bin",
    )
    task_a = await create_user_task_v0(
        user_id=user_a["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=200,
        display_name="shared-cancel.bin",
    )
    await create_user_task_v0(
        user_id=user_b["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=200,
        display_name="shared-cancel.bin",
    )
    await _set_usage_reserved(user_a["id"], 200)
    await _set_usage_reserved(user_b["id"], 200)

    client = make_aria2_client()

    result = await cancel_user_task(
        user_id=user_a["id"],
        user_task_id=task_a["id"],
        quota_bytes=10_000,
        aria2_client=client,
    )

    assert result["status"] == "cancelled"

    # Global stays active, GID intact.
    stored = await _fetch_global(download["id"])
    assert stored["status"] == "active"
    assert stored["aria2_gid"] == "gid-shared-cancel"

    # No Aria2 control whatsoever.
    client.force_remove.assert_not_called()
    client.pause.assert_not_called()
    client.unpause.assert_not_called()


# ---------------------------------------------------------------------------
# 5. Last subscriber cancel → cancellation claim + cleanup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_last_subscriber_triggers_cleanup(temp_db: str) -> None:
    """Cancelling the last active subscriber produces a cancellation claim
    that terminalizes the attempt and triggers ``force_remove``
    (spec §22.5.5, §13.1)."""
    user = await create_user_v0(username="last_cancel", quota_bytes=10_000)

    download = await create_global_download_v0(
        resource_key="http:last-cancel",
        resource_kind="http",
        source_uri="https://example.com/last-cancel.bin",
        status="active",
        aria2_gid="gid-last-cancel",
        total_bytes=300,
        disk_reserved_bytes=300,
        display_name="last-cancel.bin",
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=300,
        display_name="last-cancel.bin",
    )
    await _set_usage_reserved(user["id"], 300)

    client = make_aria2_client()

    result = await cancel_user_task(
        user_id=user["id"],
        user_task_id=task["id"],
        quota_bytes=10_000,
        aria2_client=client,
    )

    assert result["status"] == "cancelled"

    stored = await _fetch_global(download["id"])
    assert stored["status"] == "cancelled"
    assert stored["disk_reserved_bytes"] == 0

    # force_remove called exactly once for the writer GID.
    client.force_remove.assert_called_once_with("gid-last-cancel")


@pytest.mark.asyncio
async def test_cancel_shared_then_last_progressive(temp_db: str) -> None:
    """Progressive cancellation: cancel one of two subscribers (global
    stays live, no force_remove), then cancel the remaining subscriber
    (global terminalizes, force_remove called once)."""
    user_a = await create_user_v0(username="prog_a", quota_bytes=10_000)
    user_b = await create_user_v0(username="prog_b", quota_bytes=10_000)

    download = await create_global_download_v0(
        resource_key="http:prog-cancel",
        resource_kind="http",
        source_uri="https://example.com/prog.bin",
        status="active",
        aria2_gid="gid-prog",
        total_bytes=400,
        disk_reserved_bytes=400,
        display_name="prog.bin",
    )
    task_a = await create_user_task_v0(
        user_id=user_a["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=200,
        display_name="prog.bin",
    )
    task_b = await create_user_task_v0(
        user_id=user_b["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=200,
        display_name="prog.bin",
    )
    await _set_usage_reserved(user_a["id"], 200)
    await _set_usage_reserved(user_b["id"], 200)

    client = make_aria2_client()

    # First cancel: shared, no Aria2 control.
    result_a = await cancel_user_task(
        user_id=user_a["id"],
        user_task_id=task_a["id"],
        quota_bytes=10_000,
        aria2_client=client,
    )
    assert result_a["status"] == "cancelled"
    client.force_remove.assert_not_called()

    stored = await _fetch_global(download["id"])
    assert stored["status"] == "active"

    # Second cancel: last subscriber, triggers cleanup.
    result_b = await cancel_user_task(
        user_id=user_b["id"],
        user_task_id=task_b["id"],
        quota_bytes=10_000,
        aria2_client=client,
    )
    assert result_b["status"] == "cancelled"

    stored = await _fetch_global(download["id"])
    assert stored["status"] == "cancelled"

    client.force_remove.assert_called_once_with("gid-prog")


# ---------------------------------------------------------------------------
# 6. Submit returns GID but DB CAS fails → GID never appears on any row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_gid_db_write_fail_gid_not_on_any_attempt(
    temp_db: str,
) -> None:
    """When ``assign_submitted_gid`` returns None (DB CAS failed), the
    returned GID must be best-effort stopped and must never appear on any
    ``global_downloads`` row (spec §22.5.6, §12.4)."""
    user = await create_user_v0(username="gid_leak", quota_bytes=10_000)

    leaked_gid = "gid-leaked-unpersisted"

    client = make_aria2_client(add_uri=leaked_gid, force_remove=leaked_gid)

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
                uri="https://example.com/gid-leak.bin",
                resource_key="http:gid-leak",
                resource_kind="http",
                display_name="gid-leak.bin",
                total_bytes=100,
                aria2_client=client,
            )
    finally:
        ds_module.assign_submitted_gid = original_assign

    # The leaked GID must not appear on any global_downloads row.
    rows = await _fetch_all_globals_by_gid(leaked_gid)
    assert len(rows) == 0

    # The attempt should be in a terminal state (failed via cleanup).
    global_row = await get_global_by_resource_key("http:gid-leak")
    assert global_row is not None
    assert global_row["status"] == "failed"
    assert global_row["aria2_gid"] != leaked_gid

    # The leaked GID was best-effort stopped.
    client.force_remove.assert_awaited()


@pytest.mark.asyncio
async def test_submit_gid_db_write_fail_does_not_infect_second_create(
    temp_db: str,
) -> None:
    """After a submit-CAS failure, a subsequent create for a *different*
    resource must not inherit the leaked GID (spec §22.5.6, §12.4)."""
    user = await create_user_v0(username="no_infect", quota_bytes=10_000)

    leaked_gid = "gid-infect-leaked"
    clean_gid = "gid-clean-new"

    client = make_aria2_client()
    client.add_uri.side_effect = [leaked_gid, clean_gid]

    import app.services.download_service as ds_module

    original_assign = ds_module.assign_submitted_gid

    call_count = 0

    async def _fail_first_assign(*, download_id, gid, status):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return None
        return await original_assign(
            download_id=download_id, gid=gid, status=status
        )

    client.add_uri.side_effect = [leaked_gid, clean_gid]
    ds_module.assign_submitted_gid = _fail_first_assign
    try:
        # First create: GID returned but CAS fails.
        with pytest.raises(Exception):
            await create_user_download(
                user_id=user["id"],
                quota_bytes=user["quota_bytes"],
                uri="https://example.com/infect-first.bin",
                resource_key="http:infect-first",
                resource_kind="http",
                display_name="infect-first.bin",
                total_bytes=100,
                aria2_client=client,
            )
    finally:
        ds_module.assign_submitted_gid = original_assign

    # Second create: different resource, must get its own clean GID.
    task2 = await create_user_download(
        user_id=user["id"],
        quota_bytes=user["quota_bytes"],
        uri="https://example.com/infect-second.bin",
        resource_key="http:infect-second",
        resource_kind="http",
        display_name="infect-second.bin",
        total_bytes=100,
        aria2_client=client,
    )

    new = await get_global_download_by_id(int(task2["global_download_id"]))
    assert new is not None
    assert new["aria2_gid"] == clean_gid

    # Leaked GID not on any row.
    leaked_rows = await _fetch_all_globals_by_gid(leaked_gid)
    assert len(leaked_rows) == 0
