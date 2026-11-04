"""Observation store v1 — in-memory observed detail read-model unit tests.

Pure synchronous tests (no asyncio, no DB):

- record → get_observed_detail round-trip.
- terminal stamping on observed status entering the terminal set.
- write-time sweep of entries past TERMINAL_TTL_MS (terminal or not).
- evict / clear.
- matches_row_gid conservative equality.
"""

from __future__ import annotations

from app.modules.task_core import observation_store as store


def _sanitized(status: str = "active", gid: str | None = "g1") -> dict:
    data: dict = {"status": status, "totalLength": "100", "completedLength": "10"}
    if gid is not None:
        data["gid"] = gid
    return data


def test_record_get_roundtrip() -> None:
    store.clear()
    sanitized = {"status": "active", "gid": "g1", "downloadSpeed": "1024"}
    store.record_observed_detail(1, sanitized, 1_000)

    detail = store.get_observed_detail(1)
    assert detail is not None
    assert detail.sanitized == sanitized
    assert detail.sanitized["status"] == "active"
    assert detail.updated_at_ms == 1_000
    store.clear()


def test_terminal_stamp() -> None:
    store.clear()
    store.record_observed_detail(1, _sanitized("complete"), 2_000)
    detail = store.get_observed_detail(1)
    assert detail is not None
    assert detail.terminal_at_ms == 2_000

    store.record_observed_detail(2, _sanitized("active"), 2_000)
    detail = store.get_observed_detail(2)
    assert detail is not None
    assert detail.terminal_at_ms is None
    store.clear()


def test_write_time_sweep() -> None:
    store.clear()
    old_ts = 1_000
    # Terminal entry far past TTL at sweep time (sweep now = 601_001).
    store.record_observed_detail(1, _sanitized("error", "g-old"), old_ts)
    # Active entry far past TTL (terminal observation never arrived, e.g.
    # lost cancel event) — must be swept too, not just terminal-stamped ones.
    store.record_observed_detail(2, _sanitized("active", "g-stale"), old_ts)
    # Terminal entry still inside the TTL window at sweep time.
    store.record_observed_detail(3, _sanitized("removed", "g-kept"), 600_000)
    # Fresh write on another tid triggers the sweep (now = 601_001).
    store.record_observed_detail(4, _sanitized("active", "g-new"), 601_001)

    assert store.get_observed_detail(1) is None
    assert store.get_observed_detail(2) is None
    assert store.get_observed_detail(3) is not None
    assert store.get_observed_detail(4) is not None
    store.clear()


def test_evict_and_clear() -> None:
    store.clear()
    store.record_observed_detail(1, _sanitized(), 1_000)
    store.record_observed_detail(2, _sanitized(), 1_000)

    store.evict(1)
    assert store.get_observed_detail(1) is None
    assert store.get_observed_detail(2) is not None

    store.clear()
    assert store.get_observed_detail(2) is None


def test_matches_row_gid() -> None:
    store.clear()
    store.record_observed_detail(1, _sanitized(gid="g1"), 1_000)

    detail = store.get_observed_detail(1)
    assert store.matches_row_gid(detail, "g1") is True
    # row_gid None / empty -> conservative False.
    assert store.matches_row_gid(detail, None) is False
    assert store.matches_row_gid(detail, "") is False
    # Missing detail -> False.
    assert store.matches_row_gid(None, "g1") is False
    # gid mismatch -> False.
    assert store.matches_row_gid(detail, "g2") is False
    store.clear()


def test_matches_row_gid_missing_key() -> None:
    store.clear()
    store.record_observed_detail(1, _sanitized(gid=None), 1_000)

    detail = store.get_observed_detail(1)
    assert detail is not None
    assert "gid" not in detail.sanitized
    assert store.matches_row_gid(detail, "g1") is False
    store.clear()
