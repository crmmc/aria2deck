from __future__ import annotations


def backend_is_degraded(consecutive_failures: int) -> bool:
    return consecutive_failures >= 2
