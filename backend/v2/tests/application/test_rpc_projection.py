from __future__ import annotations

import pytest

from aria2deck.application.downloads.models import ObservedFile, TaskProjection
from aria2deck.application.downloads.rpc_projection import (
    VirtualGidSpace,
    aria2_status,
    basename_of,
    global_stat_payload,
    option_payload,
    slice_window,
    tell_status_payload,
    uris_payload,
    version_payload,
)
from aria2deck.domain.downloads import SubscriptionStatus

TASK = TaskProjection(
    id=7,
    status=SubscriptionStatus.ACTIVE,
    status_label="下载中",
    name="movie.mkv",
    display_uri="https://example.com/展示链接",
    total_bytes=1000,
    completed_bytes=400,
    download_speed=128,
    upload_speed=16,
    peer_count=3,
    connection_count=5,
    files=(ObservedFile("folder/movie.mkv", 1000, 400, True),),
    observation_fresh=True,
    reserved_bytes=0,
    error_code=None,
    retryable=False,
    created_at_ms=1,
    updated_at_ms=2,
    finished_at_ms=None,
)


def test_virtual_gid_is_deterministic_and_prefix_isolated() -> None:
    space_a = VirtualGidSpace()
    again = VirtualGidSpace(space_a.prefix)
    assert space_a.virtual_gid(7) == again.virtual_gid(7)
    assert len(space_a.virtual_gid(7)) == 16
    assert space_a.virtual_gid(7) != space_a.virtual_gid(8)
    space_b = VirtualGidSpace()
    assert space_a.virtual_gid(7) != space_b.virtual_gid(7), "重启前缀不同，新旧不碰撞"


@pytest.mark.parametrize(
    "status,expected",
    [
        (SubscriptionStatus.QUEUED, "queued"),
        (SubscriptionStatus.ACTIVE, "active"),
        (SubscriptionStatus.WAITING, "waiting"),
        (SubscriptionStatus.PAUSED, "paused"),
        (SubscriptionStatus.COMPLETED, "complete"),
        (SubscriptionStatus.FAILED, "error"),
        (SubscriptionStatus.CANCELLED, "removed"),
        (SubscriptionStatus.EXPIRED, "removed"),
    ],
)
def test_status_mapping_table(status: SubscriptionStatus, expected: str) -> None:
    assert aria2_status(status) == expected


def test_tell_status_payload_stringifies_and_filters_keys() -> None:
    payload = tell_status_payload(TASK, "abcd1234abcd1234")
    assert payload["gid"] == "abcd1234abcd1234"
    assert payload["status"] == "active"
    for key in ("totalLength", "completedLength", "downloadSpeed", "uploadSpeed", "connections"):
        assert isinstance(payload[key], str)
    assert payload["totalLength"] == "1000"
    assert payload["connections"] == "5"
    files = payload["files"]
    assert isinstance(files, list)
    entry = files[0]
    assert isinstance(entry, dict)
    assert entry["path"] == "movie.mkv", "path 仅 basename"
    assert entry["length"] == "1000"
    assert entry["selected"] == "true"

    filtered = tell_status_payload(TASK, "g", keys=["gid", "status"])
    assert set(filtered) == {"gid", "status"}


def test_uris_payload_only_exposes_display_uri() -> None:
    payload = uris_payload(TASK)
    assert payload == [{"uri": "https://example.com/展示链接", "status": "used"}]
    import dataclasses

    assert uris_payload(dataclasses.replace(TASK, display_uri=None)) == []


def test_global_stat_and_version_and_option_payloads() -> None:
    counts = {status: 0 for status in SubscriptionStatus}
    counts[SubscriptionStatus.ACTIVE] = 2
    counts[SubscriptionStatus.WAITING] = 3
    counts[SubscriptionStatus.CANCELLED] = 1
    stat = global_stat_payload(counts, 6, 111, 22)
    assert stat == {
        "downloadSpeed": "111",
        "uploadSpeed": "22",
        "numActive": "2",
        "numWaiting": "3",
        "numStopped": "1",
        "numStoppedTotal": "6",
    }
    version = version_payload()
    assert version["version"] and "aria2.rpc.multiCall" in version["enabledFeatures"]
    options = option_payload()
    assert "dir" in options and "secret" not in " ".join(options).lower()


def test_basename_handles_nested_and_windows_separators() -> None:
    assert basename_of("a/b/c.bin") == "c.bin"
    assert basename_of("c.bin") == "c.bin"
    assert basename_of("a\\b\\c.bin") == "c.bin"


def test_slice_window_supports_negative_offset() -> None:
    items = list(range(5))
    assert slice_window(items, -2, 5) == [3, 4]
    assert slice_window(items, 1, 2) == [1, 2]
    assert slice_window(items, 99, 5) == []
    assert slice_window(items, 0, 0) == []  # num=0 → 空（aria2 语义）
    assert slice_window(items, 0, 99) == items
