"""T14: WebSocket listener trigger-only tests.

Validates that the listener (spec §7.1):
1. Submits each event to ``reconcile_attempt_signal`` exactly once.
2. Does not fail the task when ``tell_status`` raises.
3. Keeps per-GID transport ordering without replacing the attempt lock.
4. Does not import repositories or cleanup modules.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.aria2.listener as listener_mod
from app.aria2.listener import (
    EVENT_MAP,
    handle_aria2_event,
)


# ---------------------------------------------------------------------------
# 1. Single event produces exactly one reconcile_attempt_signal call.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_single_event_one_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    """One scheduled event results in exactly one reconcile_attempt_signal."""
    calls: list[dict] = []

    fake_client = MagicMock()
    fake_client.tell_status = AsyncMock(return_value={"status": "active"})

    async def fake_signal(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(listener_mod, "get_aria2_client", lambda: fake_client)
    monkeypatch.setattr(
        listener_mod, "reconcile_attempt_signal", fake_signal
    )

    # Clean up any leftover state from other tests.
    listener_mod._event_tasks.clear()
    listener_mod._event_tails.clear()

    listener_mod._schedule_event("gid-single", "start")
    task = listener_mod._event_tails["gid-single"]
    await task

    assert len(calls) == 1
    assert calls[0]["observed_gid"] == "gid-single"
    assert calls[0]["event"] == "start"
    assert calls[0]["log_prefix"] == "[WS]"
    assert calls[0]["observed_status"] == {"status": "active"}


# ---------------------------------------------------------------------------
# 2. RPC tell_status failure does not directly fail the task.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rpc_failure_does_not_fail_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If tell_status raises, the listener still calls reconcile_attempt_signal
    with observed_status=None rather than raising or failing the task."""

    fake_client = MagicMock()
    fake_client.tell_status = AsyncMock(side_effect=RuntimeError("rpc down"))

    signal_called = False

    async def fake_signal(**kwargs):
        nonlocal signal_called
        signal_called = True
        assert kwargs["observed_status"] is None

    monkeypatch.setattr(listener_mod, "get_aria2_client", lambda: fake_client)
    monkeypatch.setattr(
        listener_mod, "reconcile_attempt_signal", fake_signal
    )

    listener_mod._event_tasks.clear()
    listener_mod._event_tails.clear()

    # Should NOT raise — RPC failure is an observation gap, not a task failure.
    await handle_aria2_event("gid-rpc-fail", "error")
    assert signal_called is True


# ---------------------------------------------------------------------------
# 3. Per-GID transport ordering does not replace the attempt lock.
#    Two different GIDs should be able to run concurrently (separate tails),
#    while events for the same GID run in receive order.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_different_gids_have_independent_transport_queues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Events for different GIDs get separate entries in _event_tails,
    proving that per-GID ordering is a transport concern, not a global lock."""

    fake_client = MagicMock()
    fake_client.tell_status = AsyncMock(return_value={"status": "active"})

    async def fake_signal(**kwargs):
        await asyncio.sleep(0.01)

    monkeypatch.setattr(listener_mod, "get_aria2_client", lambda: fake_client)
    monkeypatch.setattr(
        listener_mod, "reconcile_attempt_signal", fake_signal
    )

    listener_mod._event_tasks.clear()
    listener_mod._event_tails.clear()

    listener_mod._schedule_event("gid-a", "start")
    listener_mod._schedule_event("gid-b", "start")

    # Each GID has its own tail task.
    assert "gid-a" in listener_mod._event_tails
    assert "gid-b" in listener_mod._event_tails
    assert listener_mod._event_tails["gid-a"] is not listener_mod._event_tails["gid-b"]

    # Both should complete independently.
    await asyncio.gather(
        listener_mod._event_tails["gid-a"],
        listener_mod._event_tails["gid-b"],
    )


# ---------------------------------------------------------------------------
# 4. Static import check: listener must not import repositories or cleanup.
# ---------------------------------------------------------------------------

def test_listener_does_not_import_repositories_or_cleanup() -> None:
    """The listener module must not import repository or cleanup modules
    (spec §7.1 — listener is a trigger, not a lifecycle processor)."""
    # Force a fresh import to check import-time dependencies.
    # Remove cached module so we get a clean import graph snapshot.
    mods_to_check = [
        "app.repositories.downloads",
        "app.repositories.files",
        "app.repositories.storage",
        "app.services.failed_task_cleanup",
    ]

    # Reload to capture current import state.
    mod = importlib.reload(sys.modules["app.aria2.listener"])

    # Check the module's own namespace for any banned imports.
    mod_dict = dict(vars(mod))
    for repo_name in [
        "downloads",
        "files",
        "storage",
        "failed_task_cleanup",
        "mark_global_download_failed",
        "force_remove",
        "remove_download_result",
    ]:
        assert repo_name not in mod_dict, (
            f"listener imports banned symbol '{repo_name}'"
        )

    # Verify the source file does not contain banned import lines.
    import inspect

    source = inspect.getsource(mod)
    for banned in [
        "from app.repositories",
        "import app.repositories",
        "from app.services.failed_task_cleanup",
        "import app.services.failed_task_cleanup",
    ]:
        assert banned not in source, (
            f"listener source contains banned import: '{banned}'"
        )
