"""Tests for task_backend_snapshots repository (M3 T03)."""

from __future__ import annotations

import pytest
from sqlalchemy import insert

from app.db.engine import transaction
from app.db.schema import global_downloads
from app.repositories.backend_snapshots import (
    get_snapshot,
    get_snapshots_for_tids,
    upsert_snapshot,
)
from tests.helpers_v0 import now_ms


async def _create_global_download(resource_key: str) -> int:
    timestamp = now_ms()
    async with transaction() as conn:
        row = (
            (
                await conn.execute(
                    insert(global_downloads)
                    .values(
                        resource_key=resource_key,
                        resource_kind="http",
                        source_uri="https://example.com/file.bin",
                        status="active",
                        created_at_ms=timestamp,
                        updated_at_ms=timestamp,
                    )
                    .returning(global_downloads.c.id)
                )
            )
            .mappings()
            .one()
        )
    return row["id"]


@pytest.mark.asyncio
async def test_upsert_insert_and_update(temp_db: str) -> None:
    tid = await _create_global_download("http:repo_upsert")

    await upsert_snapshot(
        global_download_id=tid,
        download_speed=100,
        upload_speed=10,
        total_length=1000,
        completed_length=400,
        status="active",
        files_json='[{"path": "a.bin"}]',
        raw_json='{"gid": "g1"}',
        updated_at_ms=111,
    )
    row = await get_snapshot(tid)
    assert row is not None
    assert row["global_download_id"] == tid
    assert row["download_speed"] == 100
    assert row["upload_speed"] == 10
    assert row["total_length"] == 1000
    assert row["completed_length"] == 400
    assert row["status"] == "active"
    assert row["files_json"] == '[{"path": "a.bin"}]'
    assert row["raw_json"] == '{"gid": "g1"}'
    assert row["updated_at_ms"] == 111

    await upsert_snapshot(
        global_download_id=tid,
        download_speed=200,
        upload_speed=20,
        total_length=1000,
        completed_length=900,
        status="active",
        files_json="[]",
        raw_json="{}",
        updated_at_ms=222,
    )
    row = await get_snapshot(tid)
    assert row is not None
    assert row["download_speed"] == 200
    assert row["upload_speed"] == 20
    assert row["completed_length"] == 900
    assert row["files_json"] == "[]"
    assert row["raw_json"] == "{}"
    assert row["updated_at_ms"] == 222


@pytest.mark.asyncio
async def test_get_snapshot_missing_returns_none(temp_db: str) -> None:
    tid = await _create_global_download("http:repo_missing")
    assert await get_snapshot(tid) is None


@pytest.mark.asyncio
async def test_get_snapshots_for_tids_returns_mapping(temp_db: str) -> None:
    tid_a = await _create_global_download("http:repo_batch_a")
    tid_b = await _create_global_download("http:repo_batch_b")
    tid_c = await _create_global_download("http:repo_batch_c")

    await upsert_snapshot(
        global_download_id=tid_a,
        download_speed=1,
        upload_speed=0,
        total_length=10,
        completed_length=5,
        status="active",
        files_json="[]",
        raw_json="{}",
        updated_at_ms=1,
    )
    await upsert_snapshot(
        global_download_id=tid_b,
        download_speed=2,
        upload_speed=0,
        total_length=20,
        completed_length=8,
        status="waiting",
        files_json="[]",
        raw_json="{}",
        updated_at_ms=2,
    )

    result = await get_snapshots_for_tids([tid_a, tid_b, tid_c])
    assert set(result) == {tid_a, tid_b}
    assert result[tid_a]["status"] == "active"
    assert result[tid_b]["status"] == "waiting"

    assert await get_snapshots_for_tids([]) == {}
