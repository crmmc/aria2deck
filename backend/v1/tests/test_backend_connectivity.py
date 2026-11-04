"""Tests for process-local download-backend connectivity state."""

from __future__ import annotations

import pytest

from app.services import backend_connectivity as connectivity


@pytest.fixture(autouse=True)
def _reset_connectivity_state() -> None:
    connectivity.reset_for_tests()
    yield
    connectivity.reset_for_tests()


@pytest.mark.asyncio
async def test_starts_connected_and_user_snapshot_has_fixed_message() -> None:
    assert connectivity.is_connected() is True
    payload = connectivity.snapshot(is_admin=False)
    assert payload == {
        "download_backend": {
            "status": "ok",
            "message": connectivity.USER_OK_MESSAGE,
        }
    }


@pytest.mark.asyncio
async def test_admin_snapshot_uses_admin_copy() -> None:
    payload = connectivity.snapshot(is_admin=True)
    assert payload["download_backend"]["status"] == "ok"
    assert payload["download_backend"]["message"] == connectivity.ADMIN_OK_MESSAGE
    assert set(payload["download_backend"].keys()) == {"status", "message"}


@pytest.mark.asyncio
async def test_single_failure_does_not_flip_to_degraded() -> None:
    await connectivity.mark_fail()
    assert connectivity.is_connected() is True
    assert connectivity.snapshot(is_admin=False)["download_backend"]["status"] == "ok"


@pytest.mark.asyncio
async def test_threshold_failures_flip_to_degraded() -> None:
    await connectivity.mark_fail()
    await connectivity.mark_fail()
    assert connectivity.is_connected() is False

    user_payload = connectivity.snapshot(is_admin=False)
    admin_payload = connectivity.snapshot(is_admin=True)

    assert user_payload["download_backend"] == {
        "status": "degraded",
        "message": connectivity.USER_DEGRADED_MESSAGE,
    }
    assert admin_payload["download_backend"] == {
        "status": "degraded",
        "message": connectivity.ADMIN_DEGRADED_MESSAGE,
    }
    # Public payload must never leak diagnostic fields.
    assert "detail" not in user_payload["download_backend"]
    assert "detail" not in admin_payload["download_backend"]
    assert "error" not in user_payload["download_backend"]
    assert "error" not in admin_payload["download_backend"]


@pytest.mark.asyncio
async def test_success_resets_failures_immediately() -> None:
    await connectivity.mark_fail()
    await connectivity.mark_fail()
    assert connectivity.is_connected() is False

    await connectivity.mark_ok()
    assert connectivity.is_connected() is True
    assert connectivity.snapshot(is_admin=True)["download_backend"]["status"] == "ok"

    # One subsequent failure after recovery should not degrade yet.
    await connectivity.mark_fail()
    assert connectivity.is_connected() is True
