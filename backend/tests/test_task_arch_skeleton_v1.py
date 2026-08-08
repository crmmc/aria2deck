"""Task architecture skeleton v1 acceptance tests.

Verifies that the new three-layer module skeleton exists:
- ``app.modules.task_core.states``: TidState / PidState / error codes / to_db_status
- ``app.modules.backend.port``: BackendPort protocol + Snapshot types
- ``app.modules.user_ref.projection``: user-visible label mapping

These tests do not touch aria2 or the database; they only assert that the
new contracts are importable and behave according to the spec.
"""

from __future__ import annotations

from typing import get_type_hints

import pytest


class TestTaskCoreStates:
    """Task Core internal state model."""

    def test_tid_state_values(self):
        from app.modules.task_core import states

        # TidState mirrors existing DB strings (no DB migration in v1).
        assert states.TidState.ACTIVE.value == "active"
        assert states.TidState.WAITING.value == "waiting"
        assert states.TidState.PAUSED.value == "paused"
        assert states.TidState.QUEUED.value == "queued"
        assert states.TidState.COMPLETED.value == "completed"
        assert states.TidState.FAILED.value == "failed"
        assert states.TidState.CANCELLED.value == "cancelled"

    def test_pid_state_values(self):
        from app.modules.task_core import states

        # PidState is what the user sees; it can collapse TidState nuances.
        assert states.PidState.DOWNLOADING.value == "downloading"
        assert states.PidState.QUEUED.value == "queued"
        assert states.PidState.PAUSED.value == "paused"
        assert states.PidState.COMPLETED.value == "completed"
        assert states.PidState.FAILED.value == "failed"
        assert states.PidState.CANCELLED.value == "cancelled"

    def test_error_code_constants(self):
        from app.modules.task_core import states

        # Minimal error-code set required by PRD v1.
        assert states.ERROR_QUOTA_QUEUED == "quota_queued"
        assert states.ERROR_DISK_QUEUED == "disk_queued"
        assert states.ERROR_QUOTA_EXCEEDED == "quota_exceeded"
        assert states.ERROR_EXTERNAL_PAUSED == "external_paused"
        assert states.ERROR_MAX_TASK_SIZE_EXCEEDED == "max_task_size_exceeded"

    def test_to_db_status_round_trip(self):
        from app.modules.task_core import states

        # TidState -> DB string must be identity (v1 keeps DB strings).
        for tid in states.TidState:
            assert states.to_db_status(tid) == tid.value

        # Also accept raw strings for defensive interop.
        assert states.to_db_status("active") == "active"

    def test_tid_to_pid_projection(self):
        from app.modules.task_core import states

        # Running-ish internal states project to user-visible DOWNLOADING.
        assert states.tid_to_pid(states.TidState.ACTIVE) == states.PidState.DOWNLOADING
        assert states.tid_to_pid(states.TidState.WAITING) == states.PidState.DOWNLOADING
        # Queued (either by platform or by us) projects to QUEUED.
        assert states.tid_to_pid(states.TidState.QUEUED) == states.PidState.QUEUED
        # Paused projects to PAUSED regardless of who paused it; the
        # error_code carries the "external" nuance for the UI layer.
        assert states.tid_to_pid(states.TidState.PAUSED) == states.PidState.PAUSED
        # Terminal states map 1:1.
        assert states.tid_to_pid(states.TidState.COMPLETED) == states.PidState.COMPLETED
        assert states.tid_to_pid(states.TidState.FAILED) == states.PidState.FAILED
        assert states.tid_to_pid(states.TidState.CANCELLED) == states.PidState.CANCELLED


class TestBackendPort:
    """Backend adapter Protocol surface."""

    def test_backend_port_is_protocol(self):
        from app.modules.backend import port

        # BackendPort must be a typing.Protocol so adapters can duck-type.
        assert issubclass(port.BackendPort, object)
        assert getattr(port.BackendPort, "_is_protocol", False) is True

    def test_backend_port_method_names(self):
        from app.modules.backend import port

        expected = {"submit", "tell_many", "pause", "unpause", "remove"}
        actual = {
            name
            for name in dir(port.BackendPort)
            if not name.startswith("_")
        }
        # Required v1 logical ports must exist; extra helpers are OK.
        assert expected <= actual

    def test_snapshot_types_exist(self):
        from app.modules.backend import port

        # Snapshot is the read-model the Task Core consumes.
        assert hasattr(port, "Snapshot")
        hints = get_type_hints(port.Snapshot)
        # Must expose at least tid + status so Task Core can reason.
        assert "tid" in hints
        assert "status" in hints


class TestUserRefProjection:
    """User-visible label projection."""

    @pytest.mark.parametrize(
        ("status", "error_code", "expected"),
        [
            # No progress visible -> queued.
            ("queued", None, "排队中"),
            ("queued", "quota_queued", "排队中"),
            ("queued", "disk_queued", "排队中"),
            # Running -> downloading.
            ("active", None, "下载中"),
            ("waiting", None, "下载中"),
            # External pause -> paused label.
            ("paused", "external_paused", "已暂停"),
            ("paused", None, "已暂停"),
            # Terminal.
            ("completed", None, "已完成"),
            ("failed", None, "已失败"),
            ("cancelled", None, "已取消"),
        ],
    )
    def test_user_visible_label(self, status, error_code, expected):
        from app.modules.user_ref import projection

        assert projection.user_visible_label(status, error_code) == expected

    def test_user_visible_label_quota_exceeded(self):
        from app.modules.user_ref import projection

        # Quota-exceeded failure surfaces as failed (specific message is a
        # UI concern; v1 only guarantees the state label).
        label = projection.user_visible_label("failed", "quota_exceeded")
        assert label == "已失败"
