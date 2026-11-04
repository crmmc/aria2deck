from __future__ import annotations

from app.domain.pack import (
    PACK_ACTIVE_STATUSES,
    PACK_TERMINAL_STATUSES,
    is_pack_active_status,
    is_pack_terminal_status,
)


def test_pack_status_sets_capture_current_language() -> None:
    assert PACK_ACTIVE_STATUSES == ("pending", "packing")
    assert PACK_TERMINAL_STATUSES == ("completed", "failed", "cancelled")


def test_pack_status_predicates() -> None:
    assert is_pack_active_status("pending") is True
    assert is_pack_active_status("packing") is True
    assert is_pack_active_status("completed") is False

    assert is_pack_terminal_status("completed") is True
    assert is_pack_terminal_status("failed") is True
    assert is_pack_terminal_status("cancelled") is True
    assert is_pack_terminal_status("packing") is False
