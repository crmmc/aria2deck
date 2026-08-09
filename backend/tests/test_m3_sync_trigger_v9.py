"""T15: Sync trigger-only and tightened stopped-result cleanup tests.

Verifies (spec §7.2, §7.3, task T15):
1. Each live attempt submits exactly one ``reconcile_attempt_signal``.
2. Sync does not use the old enumeration snapshot to write DB directly.
3. Stopped cleanup does not remove GIDs still referenced by live attempts.
4. Unknown / unauthorized GIDs are never ``remove_download_result``-ed.
5. Handoff pending / waiting results do not mistakenly delete source result.
"""

from __future__ import annotations

import inspect
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.aria2 import sync as sync_mod
from app.aria2.sync import _sync_tasks_once
from app.db.engine import transaction
from app.db.schema import global_downloads
from app.domain.lifecycle import ReconcileResult
from app.services.lifecycle import coordinator, repair
from tests.fakes import make_aria2_client
from tests.helpers_v0 import (
    create_global_download_v0,
    create_user_task_v0,
    create_user_v0,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _mock_client(
    *,
    tell_status_map: dict[str, Any] | None = None,
    tell_stopped_return: list[dict[str, Any]] | None = None,
    tell_status_side_effect: Any = None,
) -> AsyncMock:
    client = make_aria2_client()
    if tell_status_side_effect is not None:
        client.tell_status.side_effect = tell_status_side_effect
    elif tell_status_map is not None:
        async def _tell_status(gid: str) -> dict[str, Any]:
            return tell_status_map.get(gid, {"status": "active", "totalLength": "0"})
        client.tell_status.side_effect = _tell_status
    client.tell_stopped.return_value = tell_stopped_return or []
    return client


# ---------------------------------------------------------------------------
# test 1: each live attempt submits one reconcile_attempt_signal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_each_live_attempt_submits_one_reconcile(temp_db: str) -> None:
    """Every tracked download triggers exactly one reconcile call."""
    user = await create_user_v0(username="t1")
    d1 = await create_global_download_v0(
        resource_key="rk-1", aria2_gid="g1", status="active",
        total_bytes=100, size_known=True,
    )
    d2 = await create_global_download_v0(
        resource_key="rk-2", aria2_gid="g2", status="active",
        total_bytes=100, size_known=True,
    )
    await create_user_task_v0(
        user_id=user["id"], global_download_id=d1["id"],
        status="active", reserved_bytes=100,
    )
    await create_user_task_v0(
        user_id=user["id"], global_download_id=d2["id"],
        status="active", reserved_bytes=100,
    )

    client = _mock_client(
        tell_status_map={
            "g1": {"status": "active", "totalLength": "100"},
            "g2": {"status": "active", "totalLength": "100"},
        }
    )

    with (
        patch("app.aria2.sync.get_aria2_client", return_value=client),
        patch.object(coordinator, "reconcile_attempt_signal", new_callable=AsyncMock) as mock_reconcile,
        patch.object(sync_mod, "list_v0_tracked_downloads", new_callable=AsyncMock) as mock_list,
        patch.object(repair, "repair_inconsistent_completed_downloads_v0", new_callable=AsyncMock),
        patch.object(repair, "cleanup_stale_queued_downloads_v0", new_callable=AsyncMock),
        patch("app.aria2.sync.backend_connectivity.mark_ok", new_callable=AsyncMock),
    ):
        mock_list.return_value = [
            {"id": d1["id"], "aria2_gid": "g1"},
            {"id": d2["id"], "aria2_gid": "g2"},
        ]
        mock_reconcile.return_value = ReconcileResult.CHANGED

        await _sync_tasks_once()

    assert mock_reconcile.call_count == 2

    observed_gids = sorted(
        str(call.kwargs.get("observed_gid") or "")
        for call in mock_reconcile.call_args_list
    )
    assert observed_gids == ["g1", "g2"]

    for call in mock_reconcile.call_args_list:
        assert call.kwargs.get("log_prefix") == "[Sync]"
        assert call.kwargs.get("event") is None


# ---------------------------------------------------------------------------
# test 2: old enumeration snapshot does not directly write DB
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_old_snapshot_not_used_for_direct_db_write(temp_db: str) -> None:
    """Sync source must not reference update_v0_download_from_aria2 / handle_missing_gid."""
    user = await create_user_v0(username="t2")
    d = await create_global_download_v0(
        resource_key="rk-2a", aria2_gid="g2a", status="active",
        total_bytes=100, size_known=True,
    )
    await create_user_task_v0(
        user_id=user["id"], global_download_id=d["id"],
        status="active", reserved_bytes=100,
    )

    client = _mock_client(
        tell_status_map={"g2a": {"status": "active", "totalLength": "100"}}
    )

    with (
        patch("app.aria2.sync.get_aria2_client", return_value=client),
        patch.object(coordinator, "reconcile_attempt_signal", new_callable=AsyncMock) as mock_reconcile,
        patch.object(sync_mod, "list_v0_tracked_downloads", new_callable=AsyncMock) as mock_list,
        patch.object(repair, "repair_inconsistent_completed_downloads_v0", new_callable=AsyncMock),
        patch.object(repair, "cleanup_stale_queued_downloads_v0", new_callable=AsyncMock),
        patch("app.aria2.sync.backend_connectivity.mark_ok", new_callable=AsyncMock),
    ):
        mock_list.return_value = [{"id": d["id"], "aria2_gid": "g2a"}]
        mock_reconcile.return_value = ReconcileResult.CHANGED

        await _sync_tasks_once()

    mock_reconcile.assert_called_once()

    source = inspect.getsource(sync_mod)
    assert "update_v0_download_from_aria2" not in source, (
        "sync.py must not reference update_v0_download_from_aria2"
    )
    assert "handle_missing_gid" not in source, (
        "sync.py must not reference handle_missing_gid"
    )


# ---------------------------------------------------------------------------
# test 3: stopped cleanup does not remove GIDs referenced by live attempts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stopped_cleanup_preserves_live_attempt_gid(temp_db: str) -> None:
    """A GID that reconcile left CHANGED (still live) must not be removed."""
    user = await create_user_v0(username="t3")
    d_live = await create_global_download_v0(
        resource_key="rk-3a", aria2_gid="live_gid", status="active",
        total_bytes=100, size_known=True,
    )
    d_done = await create_global_download_v0(
        resource_key="rk-3b", aria2_gid="done_gid", status="active",
        total_bytes=100, size_known=True,
    )
    await create_user_task_v0(
        user_id=user["id"], global_download_id=d_live["id"],
        status="active", reserved_bytes=100,
    )
    await create_user_task_v0(
        user_id=user["id"], global_download_id=d_done["id"],
        status="active", reserved_bytes=100,
    )

    client = _mock_client(
        tell_status_map={
            "live_gid": {"status": "active", "totalLength": "100"},
            "done_gid": {"status": "removed", "totalLength": "100"},
        },
        tell_stopped_return=[
            {"gid": "live_gid"},
            {"gid": "done_gid"},
        ],
    )

    reconcile_results: dict[str, ReconcileResult] = {
        "live_gid": ReconcileResult.CHANGED,
        "done_gid": ReconcileResult.TERMINALIZED,
    }

    async def _fake_reconcile(**kwargs: Any) -> ReconcileResult:
        return reconcile_results.get(
            str(kwargs.get("observed_gid") or ""), ReconcileResult.IGNORED
        )

    with (
        patch("app.aria2.sync.get_aria2_client", return_value=client),
        patch.object(coordinator, "reconcile_attempt_signal", side_effect=_fake_reconcile),
        patch.object(sync_mod, "list_v0_tracked_downloads", new_callable=AsyncMock) as mock_list,
        patch.object(repair, "repair_inconsistent_completed_downloads_v0", new_callable=AsyncMock),
        patch.object(repair, "cleanup_stale_queued_downloads_v0", new_callable=AsyncMock),
        patch("app.aria2.sync.backend_connectivity.mark_ok", new_callable=AsyncMock),
    ):
        mock_list.return_value = [
            {"id": d_live["id"], "aria2_gid": "live_gid"},
            {"id": d_done["id"], "aria2_gid": "done_gid"},
        ]

        await _sync_tasks_once()

    removed_gids = [
        call.args[0] if call.args else call.kwargs.get("gid", "")
        for call in client.remove_download_result.call_args_list
    ]
    assert "live_gid" not in removed_gids, (
        "must not remove result for GID still referenced by a live attempt"
    )
    assert "done_gid" in removed_gids


# ---------------------------------------------------------------------------
# test 4: unknown / unauthorized GID is not remove_download_result-ed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_gid_not_removed(temp_db: str) -> None:
    """A GID not in the removable set (never reconcile-confirmed) is untouched."""
    user = await create_user_v0(username="t4")
    d = await create_global_download_v0(
        resource_key="rk-4", aria2_gid="known_gid", status="active",
        total_bytes=100, size_known=True,
    )
    await create_user_task_v0(
        user_id=user["id"], global_download_id=d["id"],
        status="active", reserved_bytes=100,
    )

    client = _mock_client(
        tell_status_map={"known_gid": {"status": "active", "totalLength": "100"}},
        tell_stopped_return=[
            {"gid": "unknown_gid"},
            {"gid": "known_gid"},
        ],
    )

    with (
        patch("app.aria2.sync.get_aria2_client", return_value=client),
        patch.object(coordinator, "reconcile_attempt_signal", new_callable=AsyncMock) as mock_reconcile,
        patch.object(sync_mod, "list_v0_tracked_downloads", new_callable=AsyncMock) as mock_list,
        patch.object(repair, "repair_inconsistent_completed_downloads_v0", new_callable=AsyncMock),
        patch.object(repair, "cleanup_stale_queued_downloads_v0", new_callable=AsyncMock),
        patch("app.aria2.sync.backend_connectivity.mark_ok", new_callable=AsyncMock),
    ):
        mock_list.return_value = [{"id": d["id"], "aria2_gid": "known_gid"}]
        mock_reconcile.return_value = ReconcileResult.CHANGED

        await _sync_tasks_once()

    client.remove_download_result.assert_not_called()


# ---------------------------------------------------------------------------
# test 5: handoff pending / waiting result not deleted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handoff_waiting_source_not_deleted(temp_db: str) -> None:
    """When reconcile returns WAITING (handoff pending), source GID is kept."""
    user = await create_user_v0(username="t5")
    d = await create_global_download_v0(
        resource_key="rk-5", aria2_gid="source_gid", status="active",
        total_bytes=100, size_known=True, resource_kind="magnet",
    )
    await create_user_task_v0(
        user_id=user["id"], global_download_id=d["id"],
        status="active", reserved_bytes=100,
    )

    client = _mock_client(
        tell_status_map={
            "source_gid": {
                "status": "complete",
                "totalLength": "100",
                "followedBy": ["payload_gid"],
            },
        },
        tell_stopped_return=[
            {"gid": "source_gid"},
        ],
    )

    with (
        patch("app.aria2.sync.get_aria2_client", return_value=client),
        patch.object(coordinator, "reconcile_attempt_signal", new_callable=AsyncMock) as mock_reconcile,
        patch.object(sync_mod, "list_v0_tracked_downloads", new_callable=AsyncMock) as mock_list,
        patch.object(repair, "repair_inconsistent_completed_downloads_v0", new_callable=AsyncMock),
        patch.object(repair, "cleanup_stale_queued_downloads_v0", new_callable=AsyncMock),
        patch("app.aria2.sync.backend_connectivity.mark_ok", new_callable=AsyncMock),
    ):
        mock_list.return_value = [{"id": d["id"], "aria2_gid": "source_gid"}]
        mock_reconcile.return_value = ReconcileResult.WAITING

        await _sync_tasks_once()

    client.remove_download_result.assert_not_called()
