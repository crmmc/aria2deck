"""T11: size coordination and pause/unpause idempotency tests.

Verifies (spec §8.2, §8.3, §8.4, §20, §22.3):
1. Size not growing → no pause/unpause.
2. Size growing → pause succeeds → budget admitted → unpause returns active.
3. pause RPC throws but re-query shows paused → idempotent success.
4. unpause RPC throws but re-query shows active → idempotent success,
   no growth_unpause_failed.
5. unpause fails and re-query still paused + fencing valid → growth_unpause_failed.
6. GID already changed or task already terminal → no growth failure written.
7. magnet metadata phase totalLength=0 → no unknown-size cleanup.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import select

from app.db.engine import transaction
from app.db.schema import global_downloads, user_tasks
from app.services.lifecycle.coordinator import reconcile_attempt_signal
from app.services.lifecycle.handoff import coordinate_reported_size
from app.domain.lifecycle import ReconcileResult
from tests.fakes import make_aria2_client
from tests.helpers_v0 import (
    create_global_download_v0,
    create_user_task_v0,
    create_user_v0,
)


async def _set_usage_reserved(user_id: int, reserved: int) -> None:
    from app.db.schema import user_storage_usage

    async with transaction() as conn:
        await conn.execute(
            user_storage_usage.update()
            .where(user_storage_usage.c.user_id == user_id)
            .values(reserved_bytes=reserved)
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


def _gid_error(gid: str = "gid_test") -> Exception:
    return Exception(f"gid {gid} not found (artificial)")


# ---------------------------------------------------------------------------
# 1. Size not growing → no pause/unpause
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_pause_when_size_not_growing(temp_db: str) -> None:
    user = await create_user_v0(username="t11_nogrow", quota_bytes=10_000_000)
    download = await create_global_download_v0(
        resource_key="http:t11-nogrow",
        source_uri="https://example.com/file.zip",
        resource_kind="http",
        status="active",
        aria2_gid="gid_nogrow_001",
        total_bytes=1024,
        size_known=True,
        completed_bytes=512,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=1024,
    )
    await _set_usage_reserved(user["id"], 1024)

    client = make_aria2_client()
    result = await coordinate_reported_size(
        backend=client,
        download=download,
        expected_gid="gid_nogrow_001",
        control_gid="gid_nogrow_001",
        status={
            "status": "active",
            "totalLength": "1024",
            "completedLength": "512",
        },
        acquire_lifecycle_lock=False,
    )
    assert result["outcome"] == "admitted"
    assert result["paused_by_us"] is False

    client.pause.assert_not_called()
    client.unpause.assert_not_called()


# ---------------------------------------------------------------------------
# 2. Size growing → pause succeeds → budget admitted → unpause returns active
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_grow_pause_admit_unpause(temp_db: str) -> None:
    user = await create_user_v0(username="t11_grow", quota_bytes=10_000_000)
    download = await create_global_download_v0(
        resource_key="http:t11-grow",
        source_uri="https://example.com/file.zip",
        resource_kind="http",
        status="active",
        aria2_gid="gid_grow_002",
        total_bytes=1024,
        size_known=True,
        completed_bytes=512,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=1024,
    )
    await _set_usage_reserved(user["id"], 1024)

    client = make_aria2_client()
    result = await coordinate_reported_size(
        backend=client,
        download=download,
        expected_gid="gid_grow_002",
        control_gid="gid_grow_002",
        status={
            "status": "active",
            "totalLength": "2048",
            "completedLength": "512",
        },
        acquire_lifecycle_lock=False,
    )
    assert result["outcome"] == "admitted"
    assert result["paused_by_us"] is True

    client.pause.assert_called_once_with("gid_grow_002")
    client.unpause.assert_called_once_with("gid_grow_002")


# ---------------------------------------------------------------------------
# 3. pause RPC throws but re-query shows paused → idempotent success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pause_exception_requery_paused(temp_db: str) -> None:
    user = await create_user_v0(username="t11_pexcp", quota_bytes=10_000_000)
    download = await create_global_download_v0(
        resource_key="http:t11-pexcp",
        source_uri="https://example.com/file.zip",
        resource_kind="http",
        status="active",
        aria2_gid="gid_pexcp_003",
        total_bytes=1024,
        size_known=True,
        completed_bytes=512,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=1024,
    )
    await _set_usage_reserved(user["id"], 1024)

    client = make_aria2_client(
        pause=Exception("artificial pause failure"),
        tell_status={
            "status": "paused",
            "totalLength": "2048",
            "completedLength": "512",
        },
    )
    result = await coordinate_reported_size(
        backend=client,
        download=download,
        expected_gid="gid_pexcp_003",
        control_gid="gid_pexcp_003",
        status={
            "status": "active",
            "totalLength": "2048",
            "completedLength": "512",
        },
        acquire_lifecycle_lock=False,
    )
    assert result["outcome"] == "admitted"
    assert result["paused_by_us"] is True

    client.pause.assert_called_once_with("gid_pexcp_003")
    client.tell_status.assert_called_once_with("gid_pexcp_003")
    client.unpause.assert_called_once_with("gid_pexcp_003")

    stored = await _fetch_global(download["id"])
    assert stored["status"] == "active"


# ---------------------------------------------------------------------------
# 4. unpause RPC throws but re-query shows active → idempotent success,
#    no growth_unpause_failed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unpause_exception_requery_active(temp_db: str) -> None:
    user = await create_user_v0(username="t11_uexcp", quota_bytes=10_000_000)
    download = await create_global_download_v0(
        resource_key="http:t11-uexcp",
        source_uri="https://example.com/file.zip",
        resource_kind="http",
        status="active",
        aria2_gid="gid_uexcp_004",
        total_bytes=1024,
        size_known=True,
        completed_bytes=512,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=1024,
    )
    await _set_usage_reserved(user["id"], 1024)

    # pause succeeds, unpause throws, but re-query shows active.
    client = make_aria2_client(
        unpause=Exception("artificial unpause failure"),
        tell_status={
            "status": "active",
            "totalLength": "2048",
            "completedLength": "512",
        },
    )
    result = await coordinate_reported_size(
        backend=client,
        download=download,
        expected_gid="gid_uexcp_004",
        control_gid="gid_uexcp_004",
        status={
            "status": "active",
            "totalLength": "2048",
            "completedLength": "512",
        },
        acquire_lifecycle_lock=False,
    )
    # Idempotent success: outcome stays admitted, no growth_unpause_failed.
    assert result["outcome"] == "admitted"

    stored = await _fetch_global(download["id"])
    assert stored["status"] == "active"
    assert stored["error_code"] != "growth_unpause_failed"


# ---------------------------------------------------------------------------
# 5. unpause fails and re-query still paused + fencing valid →
#    growth_unpause_failed terminal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unpause_real_failure_growth_unpause_failed(temp_db: str) -> None:
    user = await create_user_v0(username="t11_ufail", quota_bytes=10_000_000)
    download = await create_global_download_v0(
        resource_key="http:t11-ufail",
        source_uri="https://example.com/file.zip",
        resource_kind="http",
        status="active",
        aria2_gid="gid_ufail_005",
        total_bytes=1024,
        size_known=True,
        completed_bytes=512,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=1024,
    )
    await _set_usage_reserved(user["id"], 1024)

    # pause succeeds, unpause throws, re-query still shows paused.
    client = make_aria2_client(
        unpause=Exception("artificial unpause failure"),
        tell_status={
            "status": "paused",
            "totalLength": "2048",
            "completedLength": "512",
        },
    )
    with (
        patch("app.services.failed_task_cleanup.cleanup_task_download_dir") as mock_dir,
        patch(
            "app.services.failed_task_cleanup.get_downloading_dir"
        ) as mock_get_dir,
    ):
        mock_dir.return_value = None
        mock_get_dir.return_value.__truediv__ = lambda self, x: f"/dl/{x}"

        result = await coordinate_reported_size(
            backend=client,
            download=download,
            expected_gid="gid_ufail_005",
            control_gid="gid_ufail_005",
            status={
                "status": "active",
                "totalLength": "2048",
                "completedLength": "512",
            },
            acquire_lifecycle_lock=False,
        )

    assert result["outcome"] == "terminalized"

    stored = await _fetch_global(download["id"])
    assert stored["status"] == "failed"
    assert stored["error_code"] == "growth_unpause_failed"

    tasks = await _fetch_user_tasks(download["id"])
    assert len(tasks) == 1
    assert tasks[0]["status"] == "failed"


# ---------------------------------------------------------------------------
# 6. GID already changed or task already terminal → no growth failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gid_changed_no_growth_failure(temp_db: str) -> None:
    """When DB GID has already changed, coordinate_reported_size returns stale.

    No growth_pause_failed or growth_unpause_failed is written.
    """
    user = await create_user_v0(username="t11_gidchg", quota_bytes=10_000_000)
    download = await create_global_download_v0(
        resource_key="http:t11-gidchg",
        source_uri="https://example.com/file.zip",
        resource_kind="http",
        status="active",
        aria2_gid="gid_new_after_handoff",
        total_bytes=1024,
        size_known=True,
        completed_bytes=512,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=1024,
    )
    await _set_usage_reserved(user["id"], 1024)

    # Pass an expected_gid that no longer matches the DB row.
    client = make_aria2_client()
    result = await coordinate_reported_size(
        backend=client,
        download=download,
        expected_gid="gid_old_before_handoff",
        control_gid="gid_old_before_handoff",
        status={
            "status": "active",
            "totalLength": "2048",
            "completedLength": "512",
        },
        acquire_lifecycle_lock=False,
    )
    assert result["outcome"] == "stale"

    client.pause.assert_not_called()
    client.unpause.assert_not_called()

    stored = await _fetch_global(download["id"])
    assert stored["status"] == "active"
    assert stored["error_code"] != "growth_pause_failed"
    assert stored["error_code"] != "growth_unpause_failed"


@pytest.mark.asyncio
async def test_pause_missing_gid_db_still_points_no_growth_failure(
    temp_db: str,
) -> None:
    """GID gone from aria2 but DB still points to it.

    Should terminalize with gid_missing, NOT growth_pause_failed.
    """
    user = await create_user_v0(username="t11_pmissing", quota_bytes=10_000_000)
    download = await create_global_download_v0(
        resource_key="http:t11-pmissing",
        source_uri="https://example.com/file.zip",
        resource_kind="http",
        status="active",
        aria2_gid="gid_pmissing_006",
        total_bytes=1024,
        size_known=True,
        completed_bytes=512,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=1024,
    )
    await _set_usage_reserved(user["id"], 1024)

    # pause throws with missing GID error, re-query also missing GID.
    client = make_aria2_client(
        pause=_gid_error("gid_pmissing_006"),
        tell_status=_gid_error("gid_pmissing_006"),
    )
    with (
        patch("app.services.failed_task_cleanup.cleanup_task_download_dir") as mock_dir,
        patch(
            "app.services.failed_task_cleanup.get_downloading_dir"
        ) as mock_get_dir,
    ):
        mock_dir.return_value = None
        mock_get_dir.return_value.__truediv__ = lambda self, x: f"/dl/{x}"

        result = await coordinate_reported_size(
            backend=client,
            download=download,
            expected_gid="gid_pmissing_006",
            control_gid="gid_pmissing_006",
            status={
                "status": "active",
                "totalLength": "2048",
                "completedLength": "512",
            },
            acquire_lifecycle_lock=False,
        )

    assert result["outcome"] == "terminalized"

    stored = await _fetch_global(download["id"])
    assert stored["status"] == "failed"
    assert stored["error_code"] == "gid_missing"
    assert stored["error_code"] != "growth_pause_failed"


# ---------------------------------------------------------------------------
# 7. magnet metadata phase totalLength=0 → no unknown-size cleanup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_magnet_metadata_total_zero_no_cleanup(temp_db: str) -> None:
    """During magnet metadata phase, totalLength=0 should not trigger
    unknown-size terminalization.

    This tests reconcile_attempt_signal: the active/waiting/paused branch
    should skip size admission when is_metadata is True.
    """
    user = await create_user_v0(username="t11_meta", quota_bytes=10_000_000)
    download = await create_global_download_v0(
        resource_key="magnet:t11-meta",
        source_uri="magnet:?xt=urn:btih:t11_meta",
        resource_kind="magnet",
        status="active",
        aria2_gid="gid_meta_007",
        total_bytes=0,
        size_known=False,
        completed_bytes=0,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
    )

    metadata_status: dict[str, Any] = {
        "status": "active",
        "totalLength": "0",
        "completedLength": "0",
        "files": [
            {
                "path": "[METADATA]",
                "length": "0",
                "selected": "true",
            }
        ],
    }
    client = make_aria2_client(tell_status=metadata_status)
    result = await reconcile_attempt_signal(
        backend=client,
        observed_gid="gid_meta_007",
        event="start",
        observed_status=metadata_status,
        log_prefix="[T11]",
    )
    # Should NOT be terminalized; should be CHANGED (projected) or WAITING.
    assert result != ReconcileResult.TERMINALIZED

    stored = await _fetch_global(download["id"])
    assert stored["status"] != "failed"
    assert stored["error_code"] != "unknown_size"
