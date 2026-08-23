"""Coverage tests for health/stats/singleton-lease services."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.services import health_service, stats_service
from app.services.singleton_lease import ApplicationSingletonLease


# ---------------------------------------------------------------------------
# health_service
# ---------------------------------------------------------------------------


class _App:
    def __init__(self, workers=None, database_ready=None) -> None:
        self.state = SimpleNamespace(
            background_workers=workers, database_ready=database_ready
        )


async def _db_ok() -> bool:
    return True


def _running_workers() -> dict:
    return {
        name: {"task": SimpleNamespace(done=lambda: False), "error": None}
        for name in health_service.REQUIRED_WORKERS
    }


def test_worker_done_probe_raises() -> None:
    def boom() -> bool:
        raise RuntimeError("probe failed")

    app = _App(
        workers={"sync": {"task": SimpleNamespace(done=boom), "error": None}},
        database_ready=_db_ok,
    )
    errors = asyncio.run(health_service.readiness_errors(app))
    assert "sync 状态异常" in errors


def test_worker_done_returns_non_bool() -> None:
    app = _App(
        workers={"sync": {"task": SimpleNamespace(done=lambda: "nope")}},
        database_ready=_db_ok,
    )
    errors = asyncio.run(health_service.readiness_errors(app))
    assert "sync 状态异常" in errors


def test_worker_done_reports_error_message() -> None:
    app = _App(
        workers={
            "sync": {
                "task": SimpleNamespace(done=lambda: True),
                "error": "崩溃:boom",
            },
        },
        database_ready=_db_ok,
    )
    errors = asyncio.run(health_service.readiness_errors(app))
    assert "崩溃:boom" in errors


def test_worker_done_without_error_message() -> None:
    app = _App(
        workers={"sync": {"task": SimpleNamespace(done=lambda: True), "error": None}}
    )
    errors = asyncio.run(health_service.readiness_errors(app))
    assert "sync 已退出" in errors


def test_database_probe_non_awaitable() -> None:
    app = _App(workers=_running_workers(), database_ready=lambda: "not-awaitable")
    errors = asyncio.run(health_service.readiness_errors(app))
    assert "数据库不可用" in errors


def test_database_probe_raises() -> None:
    def boom():
        raise RuntimeError("db down")

    app = _App(workers=_running_workers(), database_ready=boom)
    errors = asyncio.run(health_service.readiness_errors(app))
    assert "数据库不可用" in errors


def test_download_directory_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "download_dir", "/nonexistent-aria2deck-dir")
    app = _App(workers=_running_workers(), database_ready=_db_ok)
    errors = asyncio.run(health_service.readiness_errors(app))
    assert errors == ["下载目录不可用"]


def test_readiness_ok_reports_running_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(settings, "download_dir", str(tmp_path))
    workers = _running_workers()
    app = _App(workers=workers, database_ready=_db_ok)
    errors = asyncio.run(health_service.readiness_errors(app))
    assert errors == []
    assert workers["sync"]["status"] == "running"
    assert "last_observed_at" in workers["sync"]


def test_worker_entry_not_mapping_treated_as_not_started() -> None:
    app = _App(workers={"sync": "broken"}, database_ready=_db_ok)
    errors = asyncio.run(health_service.readiness_errors(app))
    assert "sync 未启动" in errors


# ---------------------------------------------------------------------------
# stats_service
# ---------------------------------------------------------------------------


def test_get_directory_size_bytes_counts_files(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "a.bin").write_bytes(b"x" * 100)
    (tmp_path / "sub" / "b.bin").write_bytes(b"y" * 50)
    assert stats_service.get_directory_size_bytes(tmp_path) == 150
    assert stats_service.get_directory_size_bytes(tmp_path / "missing") == 0


def test_get_directory_size_bytes_survives_stat_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "a.bin"
    target.write_bytes(b"x" * 10)
    real_stat = Path.stat

    calls: dict[str, int] = {}

    def failing_stat(self, **kwargs):
        if self.name == "a.bin":
            calls["a.bin"] = calls.get("a.bin", 0) + 1
            if calls["a.bin"] > 1:  # first call feeds is_file(), second feeds stat()
                raise FileNotFoundError("gone")
        return real_stat(self, **kwargs)

    monkeypatch.setattr(Path, "stat", failing_stat)
    stats_service._machine_size_cache = None
    assert stats_service.get_directory_size_bytes(tmp_path) == 0


def test_get_user_and_machine_stats(
    test_user: dict, temp_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(stats_service, "_machine_size_lock", asyncio.Lock())
    monkeypatch.setattr(stats_service, "_machine_size_cache", None)
    result = asyncio.run(
        stats_service.get_user_stats(user_id=test_user["id"], quota_bytes=1000)
    )
    assert result["disk_total_space"] == 1000
    assert result["active_task_count"] == 0
    assert result["download_speed"] == 0

    machine = asyncio.run(stats_service.get_machine_stats(admin_id=1))
    assert machine["disk_total"] > 0
    assert machine["system_used"] >= 0
    # cached second pass returns identical cached directory size
    again = asyncio.run(stats_service.get_machine_stats(admin_id=1))
    assert again["download_used"] == machine["download_used"]
    assert again["disk_total"] == machine["disk_total"]


# ---------------------------------------------------------------------------
# singleton_lease
# ---------------------------------------------------------------------------


def test_singleton_lease_rejects_second_holder(temp_db: str) -> None:
    lease = ApplicationSingletonLease.acquire()
    try:
        with pytest.raises(RuntimeError, match="单 worker"):
            ApplicationSingletonLease.acquire()
    finally:
        lease.release()
    # after release a new holder can acquire
    second = ApplicationSingletonLease.acquire()
    second.release()


def test_singleton_lease_release_is_idempotent(temp_db: str) -> None:
    lease = ApplicationSingletonLease.acquire()
    lease.release()
    lease.release()


def test_singleton_lease_write_failure_closes_fd(
    temp_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing_write(fd: int, data: bytes) -> int:
        raise OSError("disk full")

    monkeypatch.setattr("app.services.singleton_lease.os.write", failing_write)
    with pytest.raises(OSError, match="disk full"):
        ApplicationSingletonLease.acquire()


def test_worker_done_not_callable() -> None:
    app = _App(workers={"sync": {"task": object()}}, database_ready=_db_ok)
    errors = asyncio.run(health_service.readiness_errors(app))
    assert "sync 未启动" in errors


def test_machine_size_cache_double_checked_under_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from time import monotonic

    monkeypatch.setattr(stats_service, "_machine_size_lock", asyncio.Lock())
    monkeypatch.setattr(stats_service, "_machine_size_cache", None)

    async def scenario() -> int:
        download_path = Path(settings.download_dir)
        async with stats_service._machine_size_lock:
            waiter = asyncio.create_task(
                stats_service._get_cached_directory_size(download_path)
            )
            await asyncio.sleep(0.05)
            stats_service._machine_size_cache = (
                str(download_path),
                monotonic() + 60,
                4242,
            )
        return await waiter

    assert asyncio.run(scenario()) == 4242
