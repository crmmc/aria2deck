"""Task Core sync policy integration tests.

Verifies that sync_once / apply_queue_policy correctly wires
decide_on_snapshot + apply_decision into the batch sync path:

- quota_queued resumes when headroom recovers (AC-5).
- external pause is recorded, never auto-unpaused (AC-6).
- disk_queued resumes when disk is available (AC-5).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.db.engine import transaction
from app.db.schema import global_downloads
from app.modules.backend.port import BackendPort, Snapshot
from app.modules.task_core.states import (
    ERROR_DISK_QUEUED,
    ERROR_EXTERNAL_PAUSED,
    ERROR_METADATA_ADMISSION_PAUSED,
    ERROR_QUOTA_QUEUED,
)
from app.modules.task_core.sync import apply_queue_policy, sync_once
from tests.helpers_v0 import (
    create_global_download_v0,
    create_user_task_v0,
    create_user_v0,
)


async def _get_global(tid: int) -> dict | None:
    async with transaction() as conn:
        row = (
            await conn.execute(
                select(global_downloads).where(global_downloads.c.id == tid)
            )
        ).mappings().first()
    return dict(row) if row else None


@pytest.mark.asyncio
async def test_quota_queued_resumes_on_sufficient_headroom(temp_db: str) -> None:
    """quota_queued + enough quota headroom + snap paused -> unpause, clear error."""
    user = await create_user_v0(username="qp1", quota_bytes=10**9)
    gd = await create_global_download_v0(
        resource_key="http://example.com/q1.bin",
        source_uri="http://example.com/q1.bin",
        resource_kind="http",
        status="paused",
        aria2_gid="gid-q1",
        total_bytes=100,
        size_known=True,
        error_code=ERROR_QUOTA_QUEUED,
        error_message="配额不足，排队中",
    )
    await create_user_task_v0(
        user_id=user["id"], global_download_id=gd["id"], status="paused"
    )
    backend = AsyncMock(spec=BackendPort)
    backend.tell_many = AsyncMock(
        return_value=[
            Snapshot(
                tid=gd["id"],
                status="paused",
                raw={"completedLength": "0", "totalLength": "100"},
            )
        ]
    )
    backend.tell_status = AsyncMock(return_value={"status": "active"})

    report = await sync_once(backend)

    assert report.updated == 1
    backend.unpause.assert_awaited_once_with(gd["id"])
    row = await _get_global(gd["id"])
    assert row is not None
    assert row["error_code"] is None
    assert row["error_message"] is None


@pytest.mark.asyncio
async def test_external_pause_marked_without_unpause(temp_db: str) -> None:
    """paused without system error_code and without admitted size -> external pause."""
    user = await create_user_v0(username="qp2", quota_bytes=10**9)
    gd = await create_global_download_v0(
        resource_key="http://example.com/q2.bin",
        source_uri="http://example.com/q2.bin",
        resource_kind="http",
        status="paused",
        aria2_gid="gid-q2",
        total_bytes=0,
        size_known=False,
    )
    await create_user_task_v0(
        user_id=user["id"], global_download_id=gd["id"], status="paused"
    )
    backend = AsyncMock(spec=BackendPort)
    backend.tell_many = AsyncMock(
        return_value=[
            Snapshot(
                tid=gd["id"],
                status="paused",
                raw={"completedLength": "0", "totalLength": "100"},
            )
        ]
    )

    await sync_once(backend)

    backend.unpause.assert_not_called()
    row = await _get_global(gd["id"])
    assert row is not None
    assert row["error_code"] == ERROR_EXTERNAL_PAUSED


@pytest.mark.asyncio
async def test_disk_queued_resumes_when_disk_available(temp_db: str) -> None:
    """disk_queued + disk available -> resume."""
    user = await create_user_v0(username="qp3", quota_bytes=10**9)
    gd = await create_global_download_v0(
        resource_key="http://example.com/q3.bin",
        source_uri="http://example.com/q3.bin",
        resource_kind="http",
        status="paused",
        aria2_gid="gid-q3",
        total_bytes=100,
        size_known=True,
        error_code=ERROR_DISK_QUEUED,
    )
    await create_user_task_v0(
        user_id=user["id"], global_download_id=gd["id"], status="paused"
    )
    backend = AsyncMock(spec=BackendPort)
    backend.tell_many = AsyncMock(
        return_value=[
            Snapshot(
                tid=gd["id"],
                status="paused",
                raw={"completedLength": "0", "totalLength": "100"},
            )
        ]
    )
    backend.tell_status = AsyncMock(return_value={"status": "active"})

    await sync_once(backend)

    backend.unpause.assert_awaited_once_with(gd["id"])
    row = await _get_global(gd["id"])
    assert row is not None
    assert row["error_code"] is None


@pytest.mark.asyncio
async def test_apply_queue_policy_resumes_quota_queued(temp_db: str) -> None:
    """apply_queue_policy (production path) resumes quota_queued on headroom recovery."""
    user = await create_user_v0(username="qp4", quota_bytes=10**9)
    gd = await create_global_download_v0(
        resource_key="http://example.com/q4.bin",
        source_uri="http://example.com/q4.bin",
        resource_kind="http",
        status="paused",
        aria2_gid="gid-q4",
        total_bytes=100,
        size_known=True,
        error_code=ERROR_QUOTA_QUEUED,
    )
    await create_user_task_v0(
        user_id=user["id"], global_download_id=gd["id"], status="paused"
    )
    backend = AsyncMock(spec=BackendPort)
    backend.tell_many = AsyncMock(
        return_value=[
            Snapshot(
                tid=gd["id"],
                status="paused",
                raw={"completedLength": "0", "totalLength": "100"},
            )
        ]
    )
    backend.tell_status = AsyncMock(return_value={"status": "active"})

    report = await apply_queue_policy(backend)

    assert report.updated == 1
    backend.unpause.assert_awaited_once_with(gd["id"])
    row = await _get_global(gd["id"])
    assert row is not None
    assert row["error_code"] is None


@pytest.mark.asyncio
async def test_sync_metadata_admission_paused_active_does_not_clear(
    temp_db: str,
) -> None:
    """T9c policy/sync: metadata phase active keeps metadata_admission_paused."""
    user = await create_user_v0(username="qp-t9c", quota_bytes=10**9)
    gd = await create_global_download_v0(
        resource_key="magnet:?xt=urn:btih:t9c",
        source_uri="magnet:?xt=urn:btih:t9c",
        resource_kind="magnet",
        status="active",
        aria2_gid="gid-t9c",
        total_bytes=0,
        size_known=False,
        error_code=ERROR_METADATA_ADMISSION_PAUSED,
    )
    await create_user_task_v0(
        user_id=user["id"], global_download_id=gd["id"], status="active"
    )
    backend = AsyncMock(spec=BackendPort)
    backend.tell_many = AsyncMock(
        return_value=[
            Snapshot(
                tid=gd["id"],
                status="active",
                raw={"completedLength": "0", "totalLength": "0"},
            )
        ]
    )

    await sync_once(backend)

    backend.unpause.assert_not_called()
    row = await _get_global(gd["id"])
    assert row is not None
    assert row["error_code"] == ERROR_METADATA_ADMISSION_PAUSED
    assert row["error_code"] != ERROR_EXTERNAL_PAUSED


@pytest.mark.asyncio
async def test_sync_resume_still_paused_keeps_pending_code(temp_db: str) -> None:
    """T10b via sync: unpause returns but re-query paused keeps ownership code."""
    user = await create_user_v0(username="qp-t10b", quota_bytes=10**9)
    gd = await create_global_download_v0(
        resource_key="http://example.com/sync-t10b.bin",
        source_uri="http://example.com/sync-t10b.bin",
        resource_kind="http",
        status="paused",
        aria2_gid="gid-sync-t10b",
        total_bytes=100,
        size_known=True,
        error_code="admission_paused",
    )
    await create_user_task_v0(
        user_id=user["id"], global_download_id=gd["id"], status="paused"
    )
    backend = AsyncMock(spec=BackendPort)
    backend.tell_many = AsyncMock(
        return_value=[
            Snapshot(
                tid=gd["id"],
                status="paused",
                raw={"completedLength": "0", "totalLength": "100"},
            )
        ]
    )
    backend.tell_status = AsyncMock(return_value={"status": "paused"})

    await sync_once(backend)

    backend.unpause.assert_awaited_once_with(gd["id"])
    row = await _get_global(gd["id"])
    assert row is not None
    assert row["error_code"] is not None
    assert row["error_code"] in {"admission_paused", "unpause_failed"}
