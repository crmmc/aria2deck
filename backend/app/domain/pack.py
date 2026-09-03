from __future__ import annotations

PACK_ACTIVE_STATUSES = ("pending", "packing")
PACK_TERMINAL_STATUSES = ("completed", "failed", "cancelled")


def is_pack_active_status(status: str) -> bool:
    return status in PACK_ACTIVE_STATUSES


def is_pack_terminal_status(status: str) -> bool:
    return status in PACK_TERMINAL_STATUSES
