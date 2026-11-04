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

    # Default tell_status is active → unpause re-query success (new semantic).
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
    # Always re-query after unpause before clearing ownership.
    client.tell_status.assert_awaited()

    stored = await _fetch_global(download["id"])
    # Success only after re-query active|waiting — then clear ownership.
    assert stored["error_code"] is None
    assert int(stored["total_bytes"]) == 2048


@pytest.mark.asyncio
async def test_grow_unpause_rpc_ok_still_paused_keeps_code(temp_db: str) -> None:
    """T8 / AC-6: unpause RPC ok but re-query still paused → keep growth code."""
    user = await create_user_v0(username="t11_t8_fake", quota_bytes=10_000_000)
    download = await create_global_download_v0(
        resource_key="http:t11-t8-fake",
        source_uri="https://example.com/file.zip",
        resource_kind="http",
        status="active",
        aria2_gid="gid_t8_fake",
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
        pause="OK",
        unpause="OK",
        tell_status={
            "status": "paused",
            "totalLength": "2048",
            "completedLength": "512",
        },
    )
    result = await coordinate_reported_size(
        backend=client,
        download=download,
        expected_gid="gid_t8_fake",
        control_gid="gid_t8_fake",
        status={
            "status": "active",
            "totalLength": "2048",
            "completedLength": "512",
        },
        acquire_lifecycle_lock=False,
    )
    assert result["outcome"] == "admitted"
    assert result["paused_by_us"] is True
    assert result.get("unpause_soft_failed") is True
    client.unpause.assert_called_once_with("gid_t8_fake")

    stored = await _fetch_global(download["id"])
    assert stored["status"] == "paused"
    assert stored["error_code"] in {"admission_paused", "growth_unpause_failed"}
    assert stored["error_code"] != "external_paused"
    assert int(stored["total_bytes"]) == 2048


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
    # pause re-query (exception path) + unpause re-query (always).
    assert client.tell_status.await_count == 2
    client.tell_status.assert_awaited_with("gid_pexcp_003")
    client.unpause.assert_called_once_with("gid_pexcp_003")

    stored = await _fetch_global(download["id"])
    # unpause RPC may return OK, but re-query still paused → soft_failed.
    assert stored["status"] == "paused"
    assert stored["error_code"] in {"admission_paused", "growth_unpause_failed"}
    assert result.get("unpause_soft_failed") is True


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
#    soft system mark (M6): keep live, do not reclaim
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
        disk_reserved_bytes=1024,
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

    # Size was admitted; unpause soft-fails without killing the task.
    assert result["outcome"] == "admitted"
    assert result.get("unpause_soft_failed") is True

    stored = await _fetch_global(download["id"])
    assert stored["status"] == "paused"
    assert stored["aria2_gid"] == "gid_ufail_005"
    assert stored["error_code"] in {"admission_paused", "growth_unpause_failed"}
    assert int(stored["total_bytes"]) == 2048
    assert int(stored["disk_reserved_bytes"]) == 2048
    mock_dir.assert_not_called()
    client.force_remove.assert_not_awaited()

    tasks = await _fetch_user_tasks(download["id"])
    assert len(tasks) == 1
    assert tasks[0]["status"] == "paused"
    assert int(tasks[0]["reserved_bytes"]) == 2048


@pytest.mark.asyncio
async def test_size_known_does_not_shrink_reserved(temp_db: str) -> None:
    """AC-1: trusted size_known floor must not shrink on smaller live candidate."""
    user = await create_user_v0(username="t11_noshrink", quota_bytes=10_000_000)
    download = await create_global_download_v0(
        resource_key="torrent:t11-noshrink",
        source_uri="magnet:?xt=urn:btih:t11noshrink",
        resource_kind="torrent",
        status="active",
        aria2_gid="gid_noshrink_010",
        total_bytes=500_000,
        size_known=True,
        completed_bytes=0,
        disk_reserved_bytes=500_000,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=500_000,
    )
    await _set_usage_reserved(user["id"], 500_000)

    client = make_aria2_client()
    result = await coordinate_reported_size(
        backend=client,
        download=download,
        expected_gid="gid_noshrink_010",
        control_gid="gid_noshrink_010",
        status={
            "status": "active",
            "totalLength": "1000",
            "completedLength": "0",
            "files": [
                {
                    "path": "/dl/partial.bin",
                    "length": "1000",
                    "selected": "true",
                }
            ],
        },
        acquire_lifecycle_lock=False,
    )

    assert result["outcome"] == "admitted"
    client.pause.assert_not_awaited()
    client.unpause.assert_not_awaited()

    stored = await _fetch_global(download["id"])
    assert int(stored["total_bytes"]) == 500_000
    assert int(stored["disk_reserved_bytes"]) == 500_000
    assert stored["status"] == "active"

    tasks = await _fetch_user_tasks(download["id"])
    assert int(tasks[0]["reserved_bytes"]) == 500_000


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


# ---------------------------------------------------------------------------
# 8. live unknown_size: waiting/active/paused + no trusted total → WAITING
#    (spec §3.3.1 / case T12; must not fail_download_and_reclaim)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("raw_status", ["waiting", "active", "paused"])
async def test_live_unknown_size_waits_without_terminalizing(
    temp_db: str,
    raw_status: str,
) -> None:
    """T12 / §3.3.1: live raw + totalLength=0 + pending code must WAIT,
    never terminalize with unknown_size.
    """
    from app.modules.task_core.states import (
        ERROR_ADMISSION_PAUSED,
        ERROR_EXTERNAL_PAUSED,
    )

    user = await create_user_v0(
        username=f"t12_unk_{raw_status}", quota_bytes=10_000_000
    )
    download = await create_global_download_v0(
        resource_key=f"http:t12-unknown-{raw_status}",
        source_uri="https://example.com/t12-unknown.bin",
        resource_kind="http",
        status=raw_status if raw_status != "waiting" else "waiting",
        aria2_gid=f"gid_t12_{raw_status}",
        total_bytes=0,
        size_known=False,
        completed_bytes=0,
        error_code=ERROR_ADMISSION_PAUSED,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status=raw_status if raw_status != "waiting" else "waiting",
    )

    live_status: dict[str, Any] = {
        "gid": f"gid_t12_{raw_status}",
        "status": raw_status,
        "totalLength": "0",
        "completedLength": "0",
        "files": [],
    }
    client = make_aria2_client(tell_status=live_status)
    result = await reconcile_attempt_signal(
        backend=client,
        observed_gid=f"gid_t12_{raw_status}",
        event=None,
        observed_status=live_status,
        log_prefix="[T12]",
    )

    assert result == ReconcileResult.WAITING
    assert result != ReconcileResult.TERMINALIZED

    stored = await _fetch_global(download["id"])
    assert stored["status"] != "failed"
    assert stored["error_code"] != "unknown_size"
    assert stored["error_code"] != ERROR_EXTERNAL_PAUSED
    assert stored["error_code"] == ERROR_ADMISSION_PAUSED
    assert stored["aria2_gid"] == f"gid_t12_{raw_status}"
    client.force_remove.assert_not_awaited()


@pytest.mark.asyncio
async def test_t15_size_known_floor_growth_keeps_system_code(temp_db: str) -> None:
    """T15 / AC-6 + §3.6.1: size_known floor + larger live total growth path.

    Selected floor stays (no shrink); growth pause/unpause soft path keeps a
    system ownership code and never brands external_paused.
    """
    user = await create_user_v0(username="t15_floor_growth", quota_bytes=50_000_000)
    # Create-time selected floor 1024; aria2 later reports 4096 (full torrent-ish).
    download = await create_global_download_v0(
        resource_key="torrent:t15-floor:files:abc",
        source_uri="base64:dGVzdA==",
        resource_kind="torrent",
        status="active",
        aria2_gid="gid_t15_floor",
        total_bytes=1024,
        size_known=True,
        completed_bytes=0,
        disk_reserved_bytes=1024,
        error_code=None,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=1024,
    )
    await _set_usage_reserved(user["id"], 1024)

    # Growth: active → pause for admit → unpause fails soft (still paused).
    client = make_aria2_client(
        pause="OK",
        unpause="OK",
        tell_status={
            "status": "paused",
            "totalLength": "4096",
            "completedLength": "0",
            "files": [
                {"path": "/tmp/sel.bin", "length": "1024", "selected": "true"},
                {"path": "/tmp/other.bin", "length": "3072", "selected": "false"},
            ],
        },
    )
    result = await coordinate_reported_size(
        backend=client,
        download=download,
        expected_gid="gid_t15_floor",
        control_gid="gid_t15_floor",
        status={
            "status": "active",
            "totalLength": "4096",
            "completedLength": "0",
            "files": [
                {"path": "/tmp/sel.bin", "length": "1024", "selected": "true"},
                {"path": "/tmp/other.bin", "length": "3072", "selected": "false"},
            ],
        },
        acquire_lifecycle_lock=False,
    )
    assert result["outcome"] in {"admitted", "pause_soft_failed", "rpc_unavailable"}
    stored = await _fetch_global(download["id"])
    assert stored["status"] != "failed"
    # Floor: total must not shrink below create-time 1024; growth may raise.
    assert int(stored["total_bytes"]) >= 1024
    if result.get("unpause_soft_failed") or result["outcome"] == "admitted":
        # System code path (admission or growth_unpause), never external.
        if stored["error_code"] is not None:
            assert stored["error_code"] != "external_paused"
            assert stored["error_code"] in {
                "admission_paused",
                "growth_unpause_failed",
                "growth_pause_failed",
            }
    # Never reclaim
    client.force_remove.assert_not_awaited()
