"""Direct unit tests for four M2 refactored functions with zero direct coverage."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.domain import quota as domain_quota
from app.repositories.task.downloads import (
    create_global_download_attempt,
    find_latest_completed_global_download_by_resource_key,
    find_live_global_download_by_resource_key,
)
from tests.helpers_v0 import create_global_download_v0, create_user_file_v0, create_user_v0


# --------------------------------------------------------------------------- #
# create_global_download_attempt
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_create_attempt_inserts_live_row(temp_db: str) -> None:
    row = await create_global_download_attempt(
        {
            "resource_key": "http:attempt-basic",
            "resource_kind": "http",
            "source_uri": "https://example.com/basic.bin",
        }
    )
    assert row["resource_key"] == "http:attempt-basic"
    assert row["status"] == "queued"
    assert row["aria2_gid"] is None
    assert int(row["total_bytes"]) == 0
    assert int(row["completed_bytes"]) == 0


@pytest.mark.asyncio
async def test_create_attempt_with_all_fields(temp_db: str) -> None:
    row = await create_global_download_attempt(
        {
            "resource_key": "http:attempt-fields",
            "resource_kind": "http",
            "source_uri": "https://example.com/fields.bin",
            "display_name": "fields.bin",
            "total_bytes": 4096,
            "completed_bytes": 0,
            "size_known": 1,
            "size_limit_bytes": 8192,
        }
    )
    assert row["resource_key"] == "http:attempt-fields"
    assert row["display_name"] == "fields.bin"
    assert int(row["total_bytes"]) == 4096
    assert int(row["size_known"]) == 1
    assert int(row["size_limit_bytes"]) == 8192


# --------------------------------------------------------------------------- #
# find_live_global_download_by_resource_key
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_find_live_returns_active_row(temp_db: str) -> None:
    created = await create_global_download_v0(
        resource_key="http:live-active",
        status="active",
        aria2_gid="gid-live-active",
    )
    found = await find_live_global_download_by_resource_key("http:live-active")
    assert found is not None
    assert found["id"] == created["id"]


@pytest.mark.asyncio
async def test_find_live_returns_none_for_terminal(temp_db: str) -> None:
    await create_global_download_v0(
        resource_key="http:live-failed",
        status="failed",
    )
    found = await find_live_global_download_by_resource_key("http:live-failed")
    assert found is None


@pytest.mark.asyncio
async def test_find_live_returns_none_for_completed(temp_db: str) -> None:
    user = await create_user_v0(username="completed_file_owner", quota_bytes=1000)
    store_path = Path(settings.download_dir) / "store" / "completed_live_hash"
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_bytes(b"done")
    user_file = await create_user_file_v0(
        user_id=user["id"],
        real_path=store_path,
        content_hash="completed_live_hash",
        display_name="done.bin",
        size_bytes=4,
    )
    await create_global_download_v0(
        resource_key="http:live-completed",
        status="completed",
        completed_file_id=user_file["stored_file_id"],
    )
    found = await find_live_global_download_by_resource_key("http:live-completed")
    assert found is None


@pytest.mark.asyncio
async def test_find_live_returns_correct_row_when_multiple_exist(
    temp_db: str,
) -> None:
    failed = await create_global_download_v0(
        resource_key="http:live-mixed",
        status="failed",
    )
    active = await create_global_download_v0(
        resource_key="http:live-mixed",
        status="active",
        aria2_gid="gid-mixed-active",
    )
    found = await find_live_global_download_by_resource_key("http:live-mixed")
    assert found is not None
    assert found["id"] == active["id"]
    assert found["id"] != failed["id"]


# --------------------------------------------------------------------------- #
# find_latest_completed_global_download_by_resource_key
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_find_completed_returns_latest(temp_db: str) -> None:
    user = await create_user_v0(username="completed_latest_owner", quota_bytes=1000)
    store_path = Path(settings.download_dir) / "store" / "latest_hash"
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_bytes(b"data")
    user_file = await create_user_file_v0(
        user_id=user["id"],
        real_path=store_path,
        content_hash="latest_hash",
        display_name="latest.bin",
        size_bytes=4,
    )

    first = await create_global_download_v0(
        resource_key="http:completed-latest",
        status="completed",
        completed_file_id=user_file["stored_file_id"],
    )
    second = await create_global_download_v0(
        resource_key="http:completed-latest",
        status="completed",
        completed_file_id=user_file["stored_file_id"],
    )
    found = await find_latest_completed_global_download_by_resource_key(
        "http:completed-latest"
    )
    assert found is not None
    assert found["id"] == second["id"]
    assert found["id"] > first["id"]


@pytest.mark.asyncio
async def test_find_completed_returns_none_without_file(temp_db: str) -> None:
    await create_global_download_v0(
        resource_key="http:completed-no-file",
        status="completed",
        completed_file_id=None,
    )
    found = await find_latest_completed_global_download_by_resource_key(
        "http:completed-no-file"
    )
    assert found is None


@pytest.mark.asyncio
async def test_find_completed_returns_none_for_active(temp_db: str) -> None:
    await create_global_download_v0(
        resource_key="http:completed-active-row",
        status="active",
        aria2_gid="gid-active-only",
    )
    found = await find_latest_completed_global_download_by_resource_key(
        "http:completed-active-row"
    )
    assert found is None


# --------------------------------------------------------------------------- #
# get_disk_available_bytes
# --------------------------------------------------------------------------- #


def test_disk_available_returns_free_minus_reserve(
    monkeypatch: pytest.MonkeyPatch, temp_db: str
) -> None:
    fake_usage = SimpleNamespace(free=5 * 1024 * 1024 * 1024)
    monkeypatch.setattr(domain_quota.shutil, "disk_usage", lambda _p: fake_usage)
    result = domain_quota.get_disk_available_bytes(
        settings.download_dir, min_free_disk=1024
    )
    assert result == 5 * 1024 * 1024 * 1024 - 1024


def test_disk_available_returns_zero_when_full(
    monkeypatch: pytest.MonkeyPatch, temp_db: str
) -> None:
    fake_usage = SimpleNamespace(free=512)
    monkeypatch.setattr(domain_quota.shutil, "disk_usage", lambda _p: fake_usage)
    result = domain_quota.get_disk_available_bytes(
        settings.download_dir, min_free_disk=1024
    )
    assert result == 0
