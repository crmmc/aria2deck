from __future__ import annotations

import time
from datetime import datetime, timezone


def now_ms() -> int:
    return int(time.time() * 1000)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ms_to_iso(timestamp_ms: int | None) -> str | None:
    if timestamp_ms is None:
        return None
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).isoformat()
