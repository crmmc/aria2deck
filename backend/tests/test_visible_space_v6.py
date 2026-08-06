from __future__ import annotations

from app.domain.quota import (
    machine_share_percent,
    usage_percent,
    visible_space_from_quota,
)
from app.services.usage_service import machine_headroom_bytes


def test_visible_space_aligns_with_global_commitment() -> None:
    # Machine free 16G, min free 1G, A reserved 15G commitment → headroom 0
    headroom = machine_headroom_bytes(
        disk_free=16 * 1024**3,
        global_physical_commitment=15 * 1024**3,
        min_free_disk=1 * 1024**3,
    )
    assert headroom == 0

    space = visible_space_from_quota(
        quota_bytes=100 * 1024**3,
        used_bytes=0,
        reserved_bytes=0,
        machine_headroom=headroom,
    )
    assert space["available"] == 0
    assert space["limited"] is True


def test_visible_space_reflects_remaining_machine_headroom() -> None:
    headroom = machine_headroom_bytes(
        disk_free=16 * 1024**3,
        global_physical_commitment=15 * 1024**3,
        min_free_disk=0,
    )
    assert headroom == 1 * 1024**3

    space = visible_space_from_quota(
        quota_bytes=100 * 1024**3,
        used_bytes=10 * 1024**3,
        reserved_bytes=0,
        machine_headroom=headroom,
    )
    assert space["available"] == 1 * 1024**3
    assert space["limited"] is True
    assert space["total"] == 11 * 1024**3


def test_visible_space_uses_quota_when_machine_has_room() -> None:
    space = visible_space_from_quota(
        quota_bytes=100,
        used_bytes=20,
        reserved_bytes=10,
        machine_headroom=1000,
    )
    assert space["available"] == 70
    assert space["limited"] is False
    assert space["total"] == 100


def test_usage_and_machine_share_percent() -> None:
    assert usage_percent(used_bytes=50, quota_bytes=100) == 50.0
    assert usage_percent(used_bytes=0, quota_bytes=0) == 0.0
    assert machine_share_percent(used_bytes=25, total_used_bytes=100) == 25.0
    assert machine_share_percent(used_bytes=10, total_used_bytes=0) == 0.0
