"""T20: coordinator boundary cleanup tests.

Verifies that the lifecycle coordinator module contains exactly one normal
lifecycle arbitration boundary (``reconcile_attempt_signal``) and that old
bypass paths have been removed or downgraded to thin wrappers (spec §16-17,
§9.5, task T20).

Test matrix:
1. Static: no ``handle_aria2_event(..., event="complete")`` fabrication.
2. Static: ``resolve_download_for_gid`` is pure (no guarded_update / gid write).
3. ``fail_download_and_reclaim`` uses claim_attempt_terminal + cleanup_with_claim.
4. Old GID / unrelated signal → stale / ignored, no DB destruction.
5. Handoff / complete does not fabricate events to chain lifecycle.
6. Static: no ``skip_status_check=True`` in lifecycle source.
7. Old entry points are thin wrappers delegating to reconcile_attempt_signal.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.db.engine import transaction
from tests.fakes import make_aria2_client
from app.db.schema import global_downloads, user_tasks
from app.domain.lifecycle import ReconcileResult
from app.services.lifecycle.coordinator import reconcile_attempt_signal
from app.services.lifecycle.handoff import resolve_download_for_gid
from tests.helpers_v0 import (
    create_global_download_v0,
    create_user_task_v0,
    create_user_v0,
)

_LIFECYCLE_SRC = Path(__file__).resolve().parent.parent / "app" / "services" / "lifecycle" / "coordinator.py"
_LIFECYCLE_CLEANUP_SRC = Path(__file__).resolve().parent.parent / "app" / "services" / "lifecycle" / "cleanup.py"
_LIFECYCLE_HANDOFF_SRC = Path(__file__).resolve().parent.parent / "app" / "services" / "lifecycle" / "handoff.py"


def _source_text() -> str:
    return _LIFECYCLE_SRC.read_text(encoding="utf-8")


def _cleanup_source_text() -> str:
    return _LIFECYCLE_CLEANUP_SRC.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. Static: no handle_aria2_event(..., event="complete") fabrication
# ---------------------------------------------------------------------------


def test_no_fabricated_complete_event_call() -> None:
    """The lifecycle source must not contain any
    ``handle_aria2_event(..., event="complete")`` call (spec §9.5)."""
    source = _source_text()
    pattern = r'handle_aria2_event\s*\([^)]*event\s*=\s*["\']complete["\']'
    matches = re.findall(pattern, source, re.DOTALL)
    assert matches == [], (
        f"Found fabricated complete event calls: {matches}"
    )


# ---------------------------------------------------------------------------
# 2. Static: resolve_download_for_gid is pure (no guarded_update / gid assign)
# ---------------------------------------------------------------------------


def test_resolve_download_for_gid_is_pure() -> None:
    """``resolve_download_for_gid`` must not call guarded_update,
    assign_submitted_gid, or any DB write function (spec §6.3, §16.2)."""
    source = _LIFECYCLE_HANDOFF_SRC.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(_LIFECYCLE_HANDOFF_SRC))

    resolve_func: ast.AsyncFunctionDef | None = None
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AsyncFunctionDef)
            and node.name == "resolve_download_for_gid"
        ):
            resolve_func = node
            break

    assert resolve_func is not None, "resolve_download_for_gid not found"

    forbidden_calls = {
        "guarded_update_global_download",
        "guarded_update_download_and_active_user_tasks",
        "assign_submitted_gid",
        "update_global_download",
        "mark_global_download_failed",
        "claim_attempt_terminal",
        "reconcile_download_size",
    }

    for child in ast.walk(resolve_func):
        if isinstance(child, ast.Call):
            func = child.func
            name: str | None = None
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name in forbidden_calls:
                pytest.fail(
                    f"resolve_download_for_gid calls forbidden write function: {name}"
                )


# ---------------------------------------------------------------------------
# 3. fail_download_and_reclaim uses claim_attempt_terminal + cleanup_with_claim
# ---------------------------------------------------------------------------


def test_fail_download_and_reclaim_uses_claim_and_cleanup() -> None:
    """``_fail_download_and_reclaim_operation`` must call
    ``claim_attempt_terminal`` then ``cleanup_with_claim`` (spec §10.1-10.3)."""
    source = _cleanup_source_text()
    tree = ast.parse(source, filename=str(_LIFECYCLE_CLEANUP_SRC))

    operation_func: ast.AsyncFunctionDef | None = None
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AsyncFunctionDef)
            and node.name == "_fail_download_and_reclaim_operation"
        ):
            operation_func = node
            break

    assert operation_func is not None

    called_names: set[str] = set()
    for child in ast.walk(operation_func):
        if isinstance(child, ast.Call):
            func = child.func
            if isinstance(func, ast.Name):
                called_names.add(func.id)
            elif isinstance(func, ast.Attribute):
                called_names.add(func.attr)

    assert "claim_attempt_terminal" in called_names, (
        "fail path must call claim_attempt_terminal for authorization"
    )
    assert "cleanup_with_claim" in called_names, (
        "fail path must call cleanup_with_claim with the claim"
    )


# ---------------------------------------------------------------------------
# 4. Old GID / unrelated signal → stale/ignored, no destruction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_old_gid_returns_stale(temp_db: str) -> None:
    """An event for a GID that no longer matches the current attempt
    returns STALE and does not modify the database (spec §6.2, §8.1)."""
    user = await create_user_v0(username="t20_stale", quota_bytes=10_000_000)
    download = await create_global_download_v0(
        resource_key="magnet:t20_stale",
        source_uri="magnet:?xt=urn:btih:t20_stale",
        resource_kind="magnet",
        status="active",
        aria2_gid="gid_new",
        total_bytes=1024,
        size_known=True,
    )
    await create_user_task_v0(user_id=user["id"], global_download_id=download["id"])

    client = make_aria2_client(tell_status={"status": "active", "totalLength": "1024"})

    # Send an event with the OLD gid — coordinator should see stale.
    result = await reconcile_attempt_signal(
        backend=client,
        observed_gid="gid_old",
        event="start",
        observed_status={"status": "active", "totalLength": "512"},
        log_prefix="[T20]",
    )
    assert result in (ReconcileResult.STALE, ReconcileResult.IGNORED), (
        f"Expected stale/ignored for old gid, got {result}"
    )

    # DB should still have the current GID, no destructive changes.
    async with transaction() as conn:
        row = (
            (
                await conn.execute(
                    select(global_downloads).where(
                        global_downloads.c.id == download["id"]
                    )
                )
            )
            .mappings()
            .first()
        )
    assert row is not None
    assert row["aria2_gid"] == "gid_new"
    assert row["status"] == "active"

    # No force_remove should have been called.
    client.force_remove.assert_not_called()


@pytest.mark.asyncio
async def test_unrelated_gid_returns_ignored(temp_db: str) -> None:
    """A GID that doesn't match any attempt returns IGNORED."""
    client = make_aria2_client()

    result = await reconcile_attempt_signal(
        backend=client,
        observed_gid="totally_unknown_gid",
        event="start",
        observed_status={"status": "active"},
        log_prefix="[T20]",
    )
    assert result == ReconcileResult.IGNORED
    client.force_remove.assert_not_called()


# ---------------------------------------------------------------------------
# 5. Handoff / complete does not fabricate events
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handoff_does_not_fabricate_complete_event(temp_db: str) -> None:
    """``switch_to_followed_download`` must not call ``handle_aria2_event``
    with event="complete" to chain the lifecycle (spec §9.5)."""
    user = await create_user_v0(username="t20_nofake", quota_bytes=10_000_000)
    download = await create_global_download_v0(
        resource_key="magnet:t20_nofake",
        source_uri="magnet:?xt=urn:btih:t20_nofake",
        resource_kind="magnet",
        status="active",
        aria2_gid="metadata_gid",
        total_bytes=0,
        size_known=False,
    )
    await create_user_task_v0(user_id=user["id"], global_download_id=download["id"])

    client = make_aria2_client(tell_status={})

    payload_status = {
        "status": "active",
        "totalLength": "2048",
        "completedLength": "0",
        "following": "metadata_gid",
        "files": [{"path": "/tmp/payload.bin", "length": "2048", "selected": "true"}],
    }

    async def _tell_status(gid: str) -> dict[str, Any]:
        if gid == "payload_gid":
            return payload_status
        return {"status": "complete", "followedBy": ["payload_gid"], "totalLength": "0"}

    client.tell_status.side_effect = _tell_status

    result = await reconcile_attempt_signal(
        backend=client,
        observed_gid="payload_gid",
        event=None,
        observed_status=payload_status,
        log_prefix="[T20]",
    )

    # Result should be a normal reconcile outcome, not from a fabricated event.
    assert result in (
        ReconcileResult.CHANGED,
        ReconcileResult.STALE,
        ReconcileResult.WAITING,
        ReconcileResult.TERMINALIZED,
    )


# ---------------------------------------------------------------------------
# 6. Static: no skip_status_check=True in lifecycle source
# ---------------------------------------------------------------------------


def test_no_skip_status_check_in_lifecycle() -> None:
    """The lifecycle service must not use ``skip_status_check=True``
    to bypass terminal claim authorization (spec §10.5, task T20)."""
    source = _source_text()
    assert "skip_status_check=True" not in source, (
        "skip_status_check=True bypasses terminal claim authorization"
    )


# ---------------------------------------------------------------------------
# 7. Old entry points are thin wrappers delegating to reconcile_attempt_signal
# ---------------------------------------------------------------------------


def test_dead_bypass_functions_removed() -> None:
    """Deprecated thin wrappers must be removed from the lifecycle source."""
    source = _source_text()
    tree = ast.parse(source, filename=str(_LIFECYCLE_SRC))

    defined_funcs: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defined_funcs.add(node.name)

    removed = {
        "handle_aria2_event",
        "update_v0_download_from_aria2",
        "complete_v0_download_from_sync",
        "handle_missing_gid",
        "fail_v0_download_and_cleanup",
        "_fail_v0_download_and_cleanup_locked",
        "get_task_complete_lock",
        "cleanup_failed_download_artifacts",
        "_cleanup_handoff_rejection_operation",
        "_cleanup_handoff_rejection_safely",
        "_cleanup_terminal_generation_safely",
    }
    for name in removed:
        assert name not in defined_funcs, (
            f"Deprecated function {name} should have been removed"
        )
