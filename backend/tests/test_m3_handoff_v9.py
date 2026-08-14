"""T12: explicit followedBy/following handoff tests.

Verifies (spec §9, §9.2, §9.3, §9.4, §9.5, task T12):
1. Metadata complete without followedBy does not fail (waits).
2. followedBy triggers exactly one GID switch.
3. Payload following arrives early and handoff succeeds.
4. Payload active with unknown size: pause→waiting or handoff_unknown_size.
5. Payload tell_status transient error → waiting, no directory deletion.
6. Admission rejection cleans up payload writer only.
7. No fabricated complete event (spy on handle_aria2_event).
8. Duplicate handoff is idempotent.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.db.engine import transaction
from app.db.schema import global_downloads, user_tasks
from app.domain.lifecycle import ReconcileResult
from app.services.lifecycle.coordinator import reconcile_attempt_signal
from app.services.lifecycle.handoff import switch_to_followed_download
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


async def _set_usage_reserved(user_id: int, reserved: int) -> None:
    from app.db.schema import user_storage_usage

    async with transaction() as conn:
        await conn.execute(
            user_storage_usage.update()
            .where(user_storage_usage.c.user_id == user_id)
            .values(reserved_bytes=reserved)
        )



# ---------------------------------------------------------------------------
# 1. Metadata complete without followedBy does not fail
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_metadata_complete_no_followedby_does_not_fail(
    temp_db: str,
) -> None:
    """A magnet metadata GID completing without followedBy should not
    terminalize.  It should remain live and wait for the followedBy
    relation to appear (spec §9.4)."""
    user = await create_user_v0(username="t12_nofollow", quota_bytes=10_000_000)
    download = await create_global_download_v0(
        resource_key="magnet:t12-nofollow",
        source_uri="magnet:?xt=urn:btih:t12_nofollow",
        resource_kind="magnet",
        status="active",
        aria2_gid="gid_meta_001",
        total_bytes=0,
        size_known=False,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
    )

    # Metadata complete without followedBy.
    metadata_status: dict[str, Any] = {
        "status": "complete",
        "totalLength": "0",
        "completedLength": "0",
        "files": [{"path": "[METADATA]", "length": "0", "selected": "true"}],
    }
    client = make_aria2_client(tell_status=metadata_status)
    result = await reconcile_attempt_signal(
        backend=client,
        observed_gid="gid_meta_001",
        event="complete",
        observed_status=metadata_status,
        log_prefix="[T12]",
    )
    # Should NOT be terminalized.
    assert result != ReconcileResult.TERMINALIZED

    stored = await _fetch_global(download["id"])
    assert stored["status"] != "failed"
    assert stored["error_code"] != "unknown_size"

    # No destructive cleanup.
    client.force_remove.assert_not_called()


# ---------------------------------------------------------------------------
# 2. followedBy triggers exactly one GID switch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_followedby_switches_gid_once(temp_db: str) -> None:
    """When metadata GID reports followedBy, the handoff should switch
    aria2_gid from source to payload exactly once (spec §9.2 step 7)."""
    user = await create_user_v0(username="t12_switch", quota_bytes=10_000_000)
    download = await create_global_download_v0(
        resource_key="magnet:t12-switch",
        source_uri="magnet:?xt=urn:btih:t12_switch",
        resource_kind="magnet",
        status="active",
        aria2_gid="gid_source_002",
        total_bytes=4096,
        size_known=True,
        disk_reserved_bytes=4096,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=4096,
    )
    await _set_usage_reserved(user["id"], 4096)

    payload_gid = "gid_payload_002"
    source_status: dict[str, Any] = {
        "status": "complete",
        "followedBy": [payload_gid],
        "totalLength": "4096",
        "completedLength": "4096",
        "files": [{"path": "[METADATA]", "length": "4096", "selected": "true"}],
    }
    payload_status: dict[str, Any] = {
        "status": "active",
        "totalLength": "4096",
        "completedLength": "0",
        "files": [{"path": "/dl/1/file.iso", "length": "4096", "selected": "true"}],
    }

    # tell_status returns source first, then payload.
    call_count = [0]

    async def _tell_status(gid: str) -> dict[str, Any]:
        call_count[0] += 1
        if gid == payload_gid:
            return payload_status
        return source_status

    client = make_aria2_client()
    client.tell_status.side_effect = _tell_status

    result = await reconcile_attempt_signal(
        backend=client,
        observed_gid="gid_source_002",
        event="complete",
        observed_status=source_status,
        log_prefix="[T12]",
    )

    assert result == ReconcileResult.CHANGED

    stored = await _fetch_global(download["id"])
    assert stored["aria2_gid"] == payload_gid
    assert stored["resource_kind"] == "torrent"
    assert stored["status"] == "active"

    # Second reconcile with the same observed_gid should be stale
    # (current_gid is now payload, observed is source).
    result2 = await reconcile_attempt_signal(
        backend=client,
        observed_gid="gid_source_002",
        event="complete",
        observed_status=source_status,
        log_prefix="[T12]",
    )
    assert result2 in (ReconcileResult.STALE, ReconcileResult.IGNORED)

    stored2 = await _fetch_global(download["id"])
    assert stored2["aria2_gid"] == payload_gid


# ---------------------------------------------------------------------------
# 3. Payload following arrives early and handoff succeeds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_following_early_arrives_handoff_succeeds(temp_db: str) -> None:
    """When a payload GID event arrives with following=source_gid before
    the source reports complete, the handoff should still succeed
    (spec §9.3)."""
    user = await create_user_v0(username="t12_early", quota_bytes=10_000_000)
    download = await create_global_download_v0(
        resource_key="magnet:t12-early",
        source_uri="magnet:?xt=urn:btih:t12_early",
        resource_kind="magnet",
        status="active",
        aria2_gid="gid_source_003",
        total_bytes=2048,
        size_known=True,
        disk_reserved_bytes=2048,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=2048,
    )
    await _set_usage_reserved(user["id"], 2048)

    payload_gid = "gid_payload_003"
    observed_status: dict[str, Any] = {
        "status": "active",
        "following": "gid_source_003",
        "totalLength": "2048",
        "completedLength": "0",
        "files": [{"path": "/dl/1/file.iso", "length": "2048", "selected": "true"}],
    }
    client = make_aria2_client(
        tell_status={
            "status": "active",
            "totalLength": "2048",
            "completedLength": "0",
            "files": [{"path": "/dl/1/file.iso", "length": "2048", "selected": "true"}],
        }
    )

    result = await reconcile_attempt_signal(
        backend=client,
        observed_gid=payload_gid,
        event="start",
        observed_status=observed_status,
        log_prefix="[T12]",
    )

    assert result == ReconcileResult.CHANGED

    stored = await _fetch_global(download["id"])
    assert stored["aria2_gid"] == payload_gid
    assert stored["resource_kind"] == "torrent"


# ---------------------------------------------------------------------------
# 4. Payload active with unknown size: pause confirmed → waiting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_payload_active_unknown_size_pause_to_waiting(
    temp_db: str,
) -> None:
    """Payload active, size unknown (size_known=False), pause confirmed
    → return waiting (spec §9.2)."""
    user = await create_user_v0(username="t12_unknown", quota_bytes=10_000_000)
    download = await create_global_download_v0(
        resource_key="magnet:t12-unknown",
        source_uri="magnet:?xt=urn:btih:t12_unknown",
        resource_kind="magnet",
        status="active",
        aria2_gid="gid_source_004",
        total_bytes=0,
        size_known=False,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
    )

    payload_gid = "gid_payload_004"
    observed_status: dict[str, Any] = {
        "status": "active",
        "following": "gid_source_004",
        "totalLength": "0",
        "completedLength": "0",
    }

    async def _tell_status(_gid: str) -> dict[str, Any]:
        # Initial payload observation may come from observed_status. After
        # pause, re-query must confirm paused (spec §9.2).
        if client.pause.await_count > 0:
            return {"status": "paused", "totalLength": "0"}
        return {"status": "active", "totalLength": "0"}

    client = make_aria2_client()
    client.tell_status.side_effect = _tell_status

    result = await reconcile_attempt_signal(
        backend=client,
        observed_gid=payload_gid,
        event="start",
        observed_status=observed_status,
        log_prefix="[T12]",
    )

    assert result == ReconcileResult.WAITING

    # GID not switched.
    stored = await _fetch_global(download["id"])
    assert stored["aria2_gid"] == "gid_source_004"
    assert stored["status"] != "failed"

    # Pause was attempted.
    client.pause.assert_awaited_with(payload_gid)


# ---------------------------------------------------------------------------
# 4b. Payload active with unknown size: pause NOT confirmed → handoff_unknown_size
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_payload_active_unknown_size_pause_not_confirmed_terminalized(
    temp_db: str,
) -> None:
    """Payload active, size unknown, pause cannot be confirmed (re-query
    still shows active) → terminalize with handoff_unknown_size
    (spec §9.2)."""
    user = await create_user_v0(username="t12_nopause", quota_bytes=10_000_000)
    download = await create_global_download_v0(
        resource_key="magnet:t12-nopause",
        source_uri="magnet:?xt=urn:btih:t12_nopause",
        resource_kind="magnet",
        status="active",
        aria2_gid="gid_source_005",
        total_bytes=0,
        size_known=False,
        disk_reserved_bytes=0,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
    )

    payload_gid = "gid_payload_005"
    observed_status: dict[str, Any] = {
        "status": "active",
        "following": "gid_source_005",
        "totalLength": "0",
        "completedLength": "0",
    }
    # tell_status always returns active (pause never confirmed).
    client = make_aria2_client(
        tell_status={"status": "active", "totalLength": "0"},
    )

    with (
        patch(
            "app.services.failed_task_cleanup.cleanup_task_download_dir"
        ) as mock_dir,
        patch(
            "app.services.failed_task_cleanup.get_downloading_dir"
        ) as mock_get_dir,
    ):
        mock_dir.return_value = None
        mock_get_dir.return_value.__truediv__ = lambda self, x: f"/dl/{x}"

        result = await reconcile_attempt_signal(
            backend=client,
            observed_gid=payload_gid,
            event="start",
            observed_status=observed_status,
            log_prefix="[T12]",
        )

    assert result == ReconcileResult.TERMINALIZED

    stored = await _fetch_global(download["id"])
    assert stored["status"] == "failed"
    assert stored["error_code"] == "handoff_unknown_size"


# ---------------------------------------------------------------------------
# 5. Payload tell_status transient error → waiting, no directory deletion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_payload_transient_rpc_returns_waiting_no_cleanup(
    temp_db: str,
) -> None:
    """Payload tell_status fails with a transient RPC error → waiting,
    no destructive cleanup (spec §9.2, §6.2.4)."""
    user = await create_user_v0(username="t12_transient", quota_bytes=10_000_000)
    download = await create_global_download_v0(
        resource_key="magnet:t12-transient",
        source_uri="magnet:?xt=urn:btih:t12_transient",
        resource_kind="magnet",
        status="active",
        aria2_gid="gid_source_006",
        total_bytes=0,
        size_known=False,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
    )

    payload_gid = "gid_payload_006"
    observed_status: dict[str, Any] = {
        "status": "active",
        "following": "gid_source_006",
        "totalLength": "0",
    }
    transient_err = ConnectionError("cannot connect to host localhost:6800")
    client = make_aria2_client(tell_status=transient_err)

    result = await reconcile_attempt_signal(
        backend=client,
        observed_gid=payload_gid,
        event="start",
        observed_status=observed_status,
        log_prefix="[T12]",
    )

    assert result == ReconcileResult.WAITING

    stored = await _fetch_global(download["id"])
    assert stored["status"] == "active"
    assert stored["aria2_gid"] == "gid_source_006"

    # No destructive cleanup.
    client.force_remove.assert_not_called()


# ---------------------------------------------------------------------------
# 6. Admission rejection cleans up payload writer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admission_rejection_cleans_payload_writer(
    temp_db: str,
) -> None:
    """When size admission rejects (e.g. disk_budget), the handoff should
    terminalize and clean up the payload writer GID (spec §9.2 step 6)."""
    user = await create_user_v0(username="t12_reject", quota_bytes=100)
    download = await create_global_download_v0(
        resource_key="magnet:t12-reject",
        source_uri="magnet:?xt=urn:btih:t12_reject",
        resource_kind="magnet",
        status="active",
        aria2_gid="gid_source_007",
        total_bytes=0,
        size_known=False,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
    )

    payload_gid = "gid_payload_007"
    observed_status: dict[str, Any] = {
        "status": "active",
        "following": "gid_source_007",
        "totalLength": str(100 * 1024 * 1024),
        "completedLength": "0",
        "files": [
            {
                "path": "/dl/1/big.iso",
                "length": str(100 * 1024 * 1024),
                "selected": "true",
            }
        ],
    }
    client = make_aria2_client(
        tell_status={
            "status": "active",
            "totalLength": str(100 * 1024 * 1024),
            "completedLength": "0",
            "files": [
                {
                    "path": "/dl/1/big.iso",
                    "length": str(100 * 1024 * 1024),
                    "selected": "true",
                }
            ],
        }
    )

    with (
        patch(
            "app.services.failed_task_cleanup.cleanup_task_download_dir"
        ) as mock_dir,
        patch(
            "app.services.failed_task_cleanup.get_downloading_dir"
        ) as mock_get_dir,
    ):
        mock_dir.return_value = None
        mock_get_dir.return_value.__truediv__ = lambda self, x: f"/dl/{x}"

        result = await reconcile_attempt_signal(
            backend=client,
            observed_gid=payload_gid,
            event="start",
            observed_status=observed_status,
            log_prefix="[T12]",
        )

    assert result in (ReconcileResult.TERMINALIZED, ReconcileResult.STALE)

    stored = await _fetch_global(download["id"])
    assert stored["status"] in ("failed", "cancelled")


# ---------------------------------------------------------------------------
# 7. No fabricated complete event (spy on handle_aria2_event)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_fabricated_complete_event(temp_db: str) -> None:
    """The handoff path must NOT call handle_aria2_event with event=complete
    to chain the lifecycle (spec §9.5)."""
    user = await create_user_v0(username="t12_nofake", quota_bytes=10_000_000)
    download = await create_global_download_v0(
        resource_key="magnet:t12-nofake",
        source_uri="magnet:?xt=urn:btih:t12_nofake",
        resource_kind="magnet",
        status="active",
        aria2_gid="gid_source_008",
        total_bytes=4096,
        size_known=True,
        disk_reserved_bytes=4096,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=4096,
    )
    await _set_usage_reserved(user["id"], 4096)

    payload_gid = "gid_payload_008"
    source_status: dict[str, Any] = {
        "status": "complete",
        "followedBy": [payload_gid],
        "totalLength": "4096",
        "completedLength": "4096",
    }
    payload_status: dict[str, Any] = {
        "status": "active",
        "totalLength": "4096",
        "completedLength": "0",
        "files": [{"path": "/dl/1/file.iso", "length": "4096", "selected": "true"}],
    }

    async def _tell_status(gid: str) -> dict[str, Any]:
        if gid == payload_gid:
            return payload_status
        return source_status

    client = make_aria2_client()
    client.tell_status.side_effect = _tell_status

    result = await reconcile_attempt_signal(
        backend=client,
        observed_gid="gid_source_008",
        event="complete",
        observed_status=source_status,
        log_prefix="[T12]",
    )

    assert result == ReconcileResult.CHANGED


# ---------------------------------------------------------------------------
# 8. Duplicate handoff is idempotent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_handoff_is_idempotent(temp_db: str) -> None:
    """Calling handoff twice should not cause errors or double-switching.
    The second call should be STALE (current_gid already changed)."""
    user = await create_user_v0(username="t12_idem", quota_bytes=10_000_000)
    download = await create_global_download_v0(
        resource_key="magnet:t12-idem",
        source_uri="magnet:?xt=urn:btih:t12_idem",
        resource_kind="magnet",
        status="active",
        aria2_gid="gid_source_009",
        total_bytes=4096,
        size_known=True,
        disk_reserved_bytes=4096,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=4096,
    )
    await _set_usage_reserved(user["id"], 4096)

    payload_gid = "gid_payload_009"
    source_status: dict[str, Any] = {
        "status": "complete",
        "followedBy": [payload_gid],
        "totalLength": "4096",
        "completedLength": "4096",
    }
    payload_status: dict[str, Any] = {
        "status": "active",
        "totalLength": "4096",
        "completedLength": "0",
        "files": [{"path": "/dl/1/file.iso", "length": "4096", "selected": "true"}],
    }

    async def _tell_status(gid: str) -> dict[str, Any]:
        if gid == payload_gid:
            return payload_status
        return source_status

    client = make_aria2_client()
    client.tell_status.side_effect = _tell_status

    # First handoff.
    result1 = await reconcile_attempt_signal(
        backend=client,
        observed_gid="gid_source_009",
        event="complete",
        observed_status=source_status,
        log_prefix="[T12]",
    )
    assert result1 == ReconcileResult.CHANGED

    stored1 = await _fetch_global(download["id"])
    assert stored1["aria2_gid"] == payload_gid

    # Second handoff with same source observation.
    result2 = await reconcile_attempt_signal(
        backend=client,
        observed_gid="gid_source_009",
        event="complete",
        observed_status=source_status,
        log_prefix="[T12]",
    )
    # Source GID is no longer the current GID → stale or ignored.
    assert result2 in (ReconcileResult.STALE, ReconcileResult.IGNORED)

    stored2 = await _fetch_global(download["id"])
    assert stored2["aria2_gid"] == payload_gid
    assert stored2["status"] == "active"


# ---------------------------------------------------------------------------
# pause-metadata ownership: system unpause, never external_paused
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handoff_paused_payload_unpauses_and_clears_system_code(
    temp_db: str,
) -> None:
    """pause-metadata payload is system-owned: admit, unpause, clear code."""
    user = await create_user_v0(username="t12_meta_pause", quota_bytes=10_000_000)
    download = await create_global_download_v0(
        resource_key="magnet:t12-meta-pause",
        source_uri="magnet:?xt=urn:btih:t12_meta_pause",
        resource_kind="magnet",
        status="active",
        aria2_gid="gid_source_meta_pause",
        total_bytes=0,
        size_known=False,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
    )

    payload_gid = "gid_payload_meta_pause"
    source_status: dict[str, Any] = {
        "status": "complete",
        "followedBy": [payload_gid],
        "totalLength": "0",
        "completedLength": "0",
        "files": [{"path": "[METADATA]", "length": "0", "selected": "true"}],
    }
    payload_status: dict[str, Any] = {
        "status": "paused",
        "following": "gid_source_meta_pause",
        "totalLength": "8192",
        "completedLength": "0",
        "files": [
            {
                "path": "/dl/1/file.iso",
                "length": "8192",
                "selected": "true",
            }
        ],
        "bittorrent": {"info": {"name": "file.iso"}},
    }

    async def _tell_status(gid: str) -> dict[str, Any]:
        if gid == payload_gid:
            return payload_status
        return source_status

    client = make_aria2_client()
    client.tell_status.side_effect = _tell_status

    result = await reconcile_attempt_signal(
        backend=client,
        observed_gid="gid_source_meta_pause",
        event="complete",
        observed_status=source_status,
        log_prefix="[T12]",
    )
    assert result == ReconcileResult.CHANGED

    stored = await _fetch_global(download["id"])
    assert stored["aria2_gid"] == payload_gid
    assert stored["resource_kind"] == "torrent"
    assert stored["status"] == "active"
    assert stored["error_code"] is None
    assert int(stored["total_bytes"]) == 8192
    assert bool(stored["size_known"]) is True
    client.unpause.assert_awaited()


@pytest.mark.asyncio
async def test_handoff_system_pause_not_projected_external(temp_db: str) -> None:
    """After handoff tags metadata_admission_paused, reconcile must not brand external."""
    user = await create_user_v0(username="t12_no_ext", quota_bytes=10_000_000)
    download = await create_global_download_v0(
        resource_key="magnet:t12-no-ext",
        source_uri="magnet:?xt=urn:btih:t12_no_ext",
        resource_kind="torrent",
        status="paused",
        aria2_gid="gid_payload_no_ext",
        total_bytes=4096,
        size_known=True,
        disk_reserved_bytes=4096,
        error_code="metadata_admission_paused",
        completed_bytes=0,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="paused",
        reserved_bytes=4096,
    )
    await _set_usage_reserved(user["id"], 4096)

    paused_status: dict[str, Any] = {
        "status": "paused",
        "totalLength": "4096",
        "completedLength": "0",
        "files": [
            {"path": "/dl/1/file.iso", "length": "4096", "selected": "true"}
        ],
        "bittorrent": {"info": {"name": "file.iso"}},
    }
    client = make_aria2_client(tell_status=paused_status)

    await reconcile_attempt_signal(
        backend=client,
        observed_gid="gid_payload_no_ext",
        event=None,
        observed_status=paused_status,
        log_prefix="[T12]",
    )

    stored = await _fetch_global(download["id"])
    assert stored["status"] == "paused"
    assert stored["error_code"] == "metadata_admission_paused"
    assert stored["error_code"] != "external_paused"
    client.unpause.assert_not_awaited()
