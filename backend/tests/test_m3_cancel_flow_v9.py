"""T17: cancel_user_task service façade convergence (spec §13).

Verifies the service-level cancel flow:
  - shared attempt: cancelling one subscriber keeps global live, no force_remove
  - last subscriber: produces claim, triggers cleanup_with_claim / force_remove
  - idempotent: re-cancelling a terminal task returns current state
  - cleanup RPC failure does not block cancellation or subsequent create
  - wrong user: no claim, no state change
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.engine import transaction
from app.db.schema import global_downloads, user_storage_usage, user_tasks
from app.services.download_service import cancel_user_task, create_user_download
from tests.fakes import make_aria2_client
from tests.helpers_v0 import (
    create_global_download_v0,
    create_user_task_v0,
    create_user_v0,
)


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


async def _fetch_user_task(user_id: int, task_id: int) -> dict:
    async with transaction() as conn:
        row = (
            (
                await conn.execute(
                    select(user_tasks).where(
                        user_tasks.c.id == task_id,
                        user_tasks.c.user_id == user_id,
                    )
                )
            )
            .mappings()
            .one()
        )
    return dict(row)


async def _set_usage_reserved(user_id: int, reserved: int) -> None:
    async with transaction() as conn:
        await conn.execute(
            user_storage_usage.update()
            .where(user_storage_usage.c.user_id == user_id)
            .values(reserved_bytes=reserved)
        )


# ---------------------------------------------------------------------------
# 1. Shared attempt: cancel one subscriber, global stays live, no force_remove
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_shared_subscriber_keeps_global_live(temp_db: str) -> None:
    user_a = await create_user_v0(username="alice", quota_bytes=10_000)
    user_b = await create_user_v0(username="bob", quota_bytes=10_000)
    download = await create_global_download_v0(
        resource_key="http:shared-svc",
        resource_kind="http",
        source_uri="https://example.com/shared.bin",
        status="active",
        aria2_gid="gid-shared-svc",
        total_bytes=500,
        disk_reserved_bytes=500,
        display_name="shared.bin",
    )
    task_a = await create_user_task_v0(
        user_id=user_a["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=250,
        display_name="shared.bin",
    )
    await create_user_task_v0(
        user_id=user_b["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=250,
        display_name="shared.bin",
    )
    await _set_usage_reserved(user_a["id"], 250)
    await _set_usage_reserved(user_b["id"], 250)

    client = make_aria2_client()

    result = await cancel_user_task(
        user_id=user_a["id"],
        user_task_id=task_a["id"],
        quota_bytes=10_000,
        aria2_client=client,
    )

    assert result["status"] == "cancelled"

    # Global must stay active with its GID intact.
    stored = await _fetch_global(download["id"])
    assert stored["status"] == "active"
    assert stored["aria2_gid"] == "gid-shared-svc"

    # force_remove must not be called — global is still live.
    client.force_remove.assert_not_called()


# ---------------------------------------------------------------------------
# 2. Last subscriber: returns cancelled + triggers cleanup_with_claim
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_last_subscriber_triggers_cleanup(temp_db: str) -> None:
    user = await create_user_v0(username="solo", quota_bytes=10_000)
    download = await create_global_download_v0(
        resource_key="http:last-svc",
        resource_kind="http",
        source_uri="https://example.com/last.bin",
        status="active",
        aria2_gid="gid-last-svc",
        total_bytes=300,
        disk_reserved_bytes=300,
        display_name="last.bin",
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=300,
        display_name="last.bin",
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

    # Global should be terminalized.
    stored = await _fetch_global(download["id"])
    assert stored["status"] == "cancelled"

    # force_remove must be called for the writer GID.
    client.force_remove.assert_called_once_with("gid-last-svc")


# ---------------------------------------------------------------------------
# 3. Idempotent: re-cancelling a terminal task
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_idempotent_on_terminal(temp_db: str) -> None:
    user = await create_user_v0(username="idem", quota_bytes=10_000)
    download = await create_global_download_v0(
        resource_key="http:idem-svc",
        resource_kind="http",
        source_uri="https://example.com/idem.bin",
        status="active",
        aria2_gid="gid-idem-svc",
        total_bytes=200,
        disk_reserved_bytes=200,
        display_name="idem.bin",
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=200,
        display_name="idem.bin",
    )
    await _set_usage_reserved(user["id"], 200)

    client = make_aria2_client()

    first = await cancel_user_task(
        user_id=user["id"],
        user_task_id=task["id"],
        quota_bytes=10_000,
        aria2_client=client,
    )
    assert first["status"] == "cancelled"
    force_remove_count = client.force_remove.call_count

    # Second cancellation must be idempotent.
    second = await cancel_user_task(
        user_id=user["id"],
        user_task_id=task["id"],
        quota_bytes=10_000,
        aria2_client=client,
    )
    assert second["status"] == "cancelled"
    # No additional force_remove on the idempotent pass.
    assert client.force_remove.call_count == force_remove_count


# ---------------------------------------------------------------------------
# 4. cleanup RPC failure: task still cancelled, create not blocked
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_cleanup_failure_does_not_block(temp_db: str) -> None:
    user = await create_user_v0(username="rpc-fail", quota_bytes=10_000)
    download = await create_global_download_v0(
        resource_key="http:rpcfail-svc",
        resource_kind="http",
        source_uri="https://example.com/rpcfail.bin",
        status="active",
        aria2_gid="gid-rpcfail",
        total_bytes=150,
        disk_reserved_bytes=150,
        display_name="rpcfail.bin",
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=150,
        display_name="rpcfail.bin",
    )
    await _set_usage_reserved(user["id"], 150)

    client = make_aria2_client(force_remove=RuntimeError("aria2 unreachable"))

    result = await cancel_user_task(
        user_id=user["id"],
        user_task_id=task["id"],
        quota_bytes=10_000,
        aria2_client=client,
    )

    # Task must still be cancelled despite RPC failure.
    assert result["status"] == "cancelled"

    stored = await _fetch_global(download["id"])
    assert stored["status"] == "cancelled"

    # Creating a new download for a different resource must not raise
    # "旧下载任务尚未安全停止".
    client2 = make_aria2_client(add_uri="gid-new-after-fail")
    new_result = await create_user_download(
        user_id=user["id"],
        uri="https://example.com/new-resource.bin",
        resource_key="http:new-after-fail",
        resource_kind="http",
        display_name="new-resource.bin",
        total_bytes=100,
        quota_bytes=10_000,
        aria2_client=client2,
    )
    assert new_result["status"] in ("queued", "active")


# ---------------------------------------------------------------------------
# 5. Wrong user: no claim, no state change
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_wrong_user_no_change(temp_db: str) -> None:
    owner = await create_user_v0(username="owner-svc", quota_bytes=10_000)
    intruder = await create_user_v0(username="intruder-svc", quota_bytes=10_000)
    download = await create_global_download_v0(
        resource_key="http:wrong-user-svc",
        resource_kind="http",
        source_uri="https://example.com/wrong.bin",
        status="active",
        aria2_gid="gid-wu-svc",
        total_bytes=100,
        disk_reserved_bytes=100,
        display_name="wrong.bin",
    )
    task = await create_user_task_v0(
        user_id=owner["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=100,
        display_name="wrong.bin",
    )

    client = make_aria2_client()

    # The intruder has no task with this id — get_user_task_by_id returns None.
    with pytest.raises(LookupError):
        await cancel_user_task(
            user_id=intruder["id"],
            user_task_id=task["id"],
            quota_bytes=10_000,
            aria2_client=client,
        )

    # Global and owner task must remain unchanged.
    stored = await _fetch_global(download["id"])
    assert stored["status"] == "active"

    task_row = await _fetch_user_task(owner["id"], task["id"])
    assert task_row["status"] == "active"

    client.force_remove.assert_not_called()
