"""Magnet tasks that fail on insufficient space keep their resolved name+size.

Covers the fix in .trellis/tasks/08-29-magnet-fail-keep-meta:
- handoff admission (disk_budget / max_task_size) terminal write backfills
  display_name in the same transaction as total_bytes/size_known.
- aria2 error terminal path (errorCode=9 disk-full) backfills display_name.
- [METADATA] placeholder names are never persisted.
- An absent name (extract returns None) never overwrites an existing one.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.engine import transaction
from app.db.schema import global_downloads, user_tasks
from app.domain.status import ACTIVE_GLOBAL_DOWNLOAD_STATUSES
from app.repositories.task.downloads import (
    claim_attempt_terminal,
    reconcile_download_size,
)
from app.services.lifecycle.cleanup import fail_download_and_reclaim
from app.services.lifecycle.handoff import coordinate_reported_size
from tests.fakes import make_aria2_client
from tests.helpers_v0 import (
    create_global_download_v0,
    create_user_task_v0,
    create_user_v0,
)

TORRENT_NAME = "Ubuntu-24.04.iso"
METADATA_TORRENT_NAME = "[METADATA]Ubuntu-24.04.iso"


async def _set_usage_reserved(user_id: int, reserved: int) -> None:
    from app.db.schema import user_storage_usage

    async with transaction() as conn:
        await conn.execute(
            user_storage_usage.update()
            .where(user_storage_usage.c.user_id == user_id)
            .values(reserved_bytes=reserved)
        )


async def _fetch_global(download_id: int) -> dict:
    async with transaction() as conn:
        row = (
            (
                await conn.execute(
                    select(global_downloads).where(
                        global_downloads.c.id == download_id
                    )
                )
            )
            .mappings()
            .one()
        )
    return dict(row)


async def _fetch_user_tasks(download_id: int) -> list[dict]:
    async with transaction() as conn:
        rows = (
            (
                await conn.execute(
                    select(user_tasks).where(
                        user_tasks.c.global_download_id == download_id
                    )
                )
            )
            .mappings()
            .all()
        )
    return [dict(r) for r in rows]


def _magnet_payload_status(
    *, name: str | None = TORRENT_NAME, total: int = 5_000_000
) -> dict:
    info: dict = {} if name is None else {"name": name}
    return {
        "status": "active",
        "totalLength": str(total),
        "completedLength": "0",
        "bittorrent": {"info": info},
    }


# ---------------------------------------------------------------------------
# (a) disk_budget admission failure backfills display_name + total_bytes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disk_budget_fail_keeps_resolved_name(temp_db: str) -> None:
    user = await create_user_v0(username="mag_budget", quota_bytes=10**12)
    download = await create_global_download_v0(
        resource_key="magnet:budget",
        source_uri="magnet:?xt=urn:btih:budget",
        resource_kind="magnet",
        status="active",
        aria2_gid="gid-budget",
        display_name=None,
        total_bytes=0,
        size_known=False,
        size_limit_bytes=10**12,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=0,
    )

    result = await reconcile_download_size(
        download_id=download["id"],
        expected_gid="gid-budget",
        candidate_bytes=5_000_000,
        completed_bytes=0,
        size_limit_bytes=10**12,
        disk_available_bytes=1,  # almost no disk left
        display_name=TORRENT_NAME,
    )
    assert result["outcome"] == "disk_budget"

    stored = await _fetch_global(download["id"])
    assert stored["status"] == "failed"
    assert stored["error_code"] == "disk_budget_exceeded"
    assert stored["display_name"] == TORRENT_NAME
    assert stored["total_bytes"] == 5_000_000
    assert stored["size_known"] == 1

    tasks = await _fetch_user_tasks(download["id"])
    assert len(tasks) == 1
    assert tasks[0]["status"] == "failed"
    assert tasks[0]["display_name"] == TORRENT_NAME


@pytest.mark.asyncio
async def test_max_task_size_fail_keeps_resolved_name(temp_db: str) -> None:
    user = await create_user_v0(username="mag_maxsize", quota_bytes=10**12)
    download = await create_global_download_v0(
        resource_key="magnet:maxsize",
        source_uri="magnet:?xt=urn:btih:maxsize",
        resource_kind="magnet",
        status="active",
        aria2_gid="gid-maxsize",
        display_name=None,
        total_bytes=0,
        size_known=False,
        size_limit_bytes=1024,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=0,
    )

    result = await reconcile_download_size(
        download_id=download["id"],
        expected_gid="gid-maxsize",
        candidate_bytes=5_000_000,
        completed_bytes=0,
        size_limit_bytes=1024,
        disk_available_bytes=10**12,
        display_name=TORRENT_NAME,
    )
    assert result["outcome"] == "max_task_size"

    stored = await _fetch_global(download["id"])
    assert stored["status"] == "failed"
    assert stored["error_code"] == "max_task_size_exceeded"
    assert stored["display_name"] == TORRENT_NAME
    assert stored["total_bytes"] == 5_000_000


@pytest.mark.asyncio
async def test_coordinate_reported_size_extracts_name_on_reject(
    temp_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: coordinate_reported_size pulls the name out of aria2
    tell_status and persists it when the disk budget rejects admission."""
    monkeypatch.setattr(
        "app.services.lifecycle.handoff.get_disk_available_bytes",
        lambda *a, **k: 1,
    )
    user = await create_user_v0(username="mag_coord", quota_bytes=10**12)
    download = await create_global_download_v0(
        resource_key="magnet:coord",
        source_uri="magnet:?xt=urn:btih:coord",
        resource_kind="magnet",
        status="active",
        aria2_gid="gid-coord",
        display_name=None,
        total_bytes=0,
        size_known=False,
        size_limit_bytes=10**12,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=0,
    )

    result = await coordinate_reported_size(
        backend=make_aria2_client(),
        download=download,
        expected_gid="gid-coord",
        control_gid="gid-coord",
        status=_magnet_payload_status(),
        acquire_lifecycle_lock=False,
    )
    assert result["outcome"] == "disk_budget"

    stored = await _fetch_global(download["id"])
    assert stored["status"] == "failed"
    assert stored["display_name"] == TORRENT_NAME
    assert stored["total_bytes"] == 5_000_000
    assert stored["size_known"] == 1

    tasks = await _fetch_user_tasks(download["id"])
    assert tasks[0]["display_name"] == TORRENT_NAME


# ---------------------------------------------------------------------------
# (b) aria2 error terminal path (errorCode=9) backfills display_name
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_error_terminal_keeps_resolved_name(temp_db: str) -> None:
    user = await create_user_v0(username="mag_err9", quota_bytes=10**12)
    download = await create_global_download_v0(
        resource_key="magnet:err9",
        source_uri="magnet:?xt=urn:btih:err9",
        resource_kind="magnet",
        status="active",
        aria2_gid="gid-err9",
        display_name=None,
        total_bytes=5_000_000,
        size_known=True,
        disk_reserved_bytes=5_000_000,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=5_000_000,
    )
    await _set_usage_reserved(user["id"], 5_000_000)

    client = make_aria2_client(tell_status={})
    changed = await fail_download_and_reclaim(
        backend=client,
        download_id=download["id"],
        message="磁盘空间不足",
        error_code="9",
        expected_gid="gid-err9",
        writer_gid="gid-err9",
        log_prefix="[T-MAG]",
        display_name=TORRENT_NAME,
    )
    assert changed is True

    stored = await _fetch_global(download["id"])
    assert stored["status"] == "failed"
    assert stored["error_code"] == "9"
    assert stored["display_name"] == TORRENT_NAME
    # Existing total preserved (error path does not touch total_bytes).
    assert stored["total_bytes"] == 5_000_000
    assert stored["size_known"] == 1

    tasks = await _fetch_user_tasks(download["id"])
    assert tasks[0]["status"] == "failed"
    assert tasks[0]["display_name"] == TORRENT_NAME


@pytest.mark.asyncio
async def test_claim_attempt_terminal_writes_display_name(temp_db: str) -> None:
    user = await create_user_v0(username="mag_claim", quota_bytes=10**12)
    download = await create_global_download_v0(
        resource_key="magnet:claim",
        source_uri="magnet:?xt=urn:btih:claim",
        resource_kind="magnet",
        status="active",
        aria2_gid="gid-claim-mag",
        display_name=None,
        total_bytes=0,
        size_known=False,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=0,
    )

    claim = await claim_attempt_terminal(
        attempt_id=download["id"],
        expected_gid="gid-claim-mag",
        terminal_status="failed",
        error_code="9",
        error_message="磁盘空间不足",
        expected_statuses=ACTIVE_GLOBAL_DOWNLOAD_STATUSES,
        display_name=TORRENT_NAME,
    )
    assert claim is not None

    stored = await _fetch_global(download["id"])
    assert stored["status"] == "failed"
    assert stored["display_name"] == TORRENT_NAME

    tasks = await _fetch_user_tasks(download["id"])
    assert tasks[0]["display_name"] == TORRENT_NAME


# ---------------------------------------------------------------------------
# (c) [METADATA] placeholder never persisted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_metadata_placeholder_name_not_written(
    temp_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """extract_display_name filters [METADATA] → None → nothing written."""
    monkeypatch.setattr(
        "app.services.lifecycle.handoff.get_disk_available_bytes",
        lambda *a, **k: 1,
    )
    user = await create_user_v0(username="mag_meta", quota_bytes=10**12)
    download = await create_global_download_v0(
        resource_key="magnet:meta",
        source_uri="magnet:?xt=urn:btih:meta",
        resource_kind="magnet",
        status="active",
        aria2_gid="gid-meta",
        display_name=None,
        total_bytes=0,
        size_known=False,
        size_limit_bytes=10**12,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=0,
    )

    # Metadata-phase status: only a [METADATA] placeholder is available.
    result = await coordinate_reported_size(
        backend=make_aria2_client(),
        download=download,
        expected_gid="gid-meta",
        control_gid="gid-meta",
        status=_magnet_payload_status(name=METADATA_TORRENT_NAME),
        acquire_lifecycle_lock=False,
    )
    assert result["outcome"] == "disk_budget"

    stored = await _fetch_global(download["id"])
    assert stored["status"] == "failed"
    assert stored["display_name"] is None
    assert stored["total_bytes"] == 5_000_000

    tasks = await _fetch_user_tasks(download["id"])
    assert tasks[0]["display_name"] is None


# ---------------------------------------------------------------------------
# (d) None display_name never overwrites an existing value
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_none_display_name_keeps_existing(temp_db: str) -> None:
    user = await create_user_v0(username="mag_keep", quota_bytes=10**12)
    download = await create_global_download_v0(
        resource_key="magnet:keep",
        source_uri="magnet:?xt=urn:btih:keep",
        resource_kind="magnet",
        status="active",
        aria2_gid="gid-keep",
        display_name="已存在的名字",
        total_bytes=0,
        size_known=False,
        size_limit_bytes=10**12,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=0,
        display_name="已存在的名字",
    )

    result = await reconcile_download_size(
        download_id=download["id"],
        expected_gid="gid-keep",
        candidate_bytes=5_000_000,
        completed_bytes=0,
        size_limit_bytes=10**12,
        disk_available_bytes=1,
        display_name=None,
    )
    assert result["outcome"] == "disk_budget"

    stored = await _fetch_global(download["id"])
    assert stored["status"] == "failed"
    assert stored["display_name"] == "已存在的名字"

    tasks = await _fetch_user_tasks(download["id"])
    assert tasks[0]["display_name"] == "已存在的名字"
