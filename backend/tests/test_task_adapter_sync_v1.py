"""Task 4 — Backend adapter + submit/sync via Task Core.

Covers AC-1 (boundary: BackendPort adapter wraps Aria2Client) and
AC-8 (batch sync path exists and writes back to DB).

Tests use AsyncMock for the aria2 client so no real aria2 RPC happens.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.db.engine import transaction
from app.db.schema import global_downloads
from app.modules.backend.aria2_adapter import Aria2BackendAdapter
from app.modules.backend.port import BackendPort, Snapshot
from app.modules.task_core.submit import submit_tid
from app.modules.task_core.sync import sync_once
from tests.helpers_v0 import (
    create_global_download_v0,
    create_user_task_v0,
    create_user_v0,
)


async def _get_global(tid: int) -> dict | None:
    async with transaction() as conn:
        row = (
            await conn.execute(
                select(global_downloads).where(global_downloads.c.id == tid)
            )
        ).mappings().first()
    return dict(row) if row else None


def _make_aria2_client() -> AsyncMock:
    client = AsyncMock()
    client.add_uri = AsyncMock(return_value="gid-abc")
    client.add_torrent = AsyncMock(return_value="gid-tor")
    client.tell_status = AsyncMock(
        return_value={
            "gid": "gid-abc",
            "status": "active",
            "totalLength": "1024",
            "completedLength": "512",
        }
    )
    client.pause = AsyncMock(return_value="gid-abc")
    client.unpause = AsyncMock(return_value="gid-abc")
    client.remove = AsyncMock(return_value="gid-abc")
    client.remove_download_result = AsyncMock(return_value="OK")
    return client


@pytest.mark.asyncio
async def test_adapter_submit_calls_add_uri_and_persists_gid(temp_db: str) -> None:
    """AC-1: adapter.submit calls add_uri with base options and stores gid in DB."""
    user = await create_user_v0(username="s1")
    gd = await create_global_download_v0(
        resource_key="magnet:?xt=urn:btih:abc",
        source_uri="magnet:?xt=urn:btih:abc",
        resource_kind="magnet",
        status="queued",
        total_bytes=0,
        size_known=False,
    )
    await create_user_task_v0(
        user_id=user["id"], global_download_id=gd["id"], status="queued"
    )
    client = _make_aria2_client()
    adapter = Aria2BackendAdapter(client)

    gid = await adapter.submit(
        tid=gd["id"], uri="magnet:?xt=urn:btih:abc", options={}
    )

    assert gid == "gid-abc"
    client.add_uri.assert_awaited_once()
    uris, opts = client.add_uri.await_args.args
    assert uris == ["magnet:?xt=urn:btih:abc"]
    assert opts["seed-time"] == "0"
    assert opts["pause-metadata"] == "true"
    row = await _get_global(gd["id"])
    assert row is not None
    assert row["aria2_gid"] == "gid-abc"
    assert row["status"] == "paused"
    assert row["error_code"] == "metadata_admission_paused"


@pytest.mark.asyncio
async def test_adapter_submit_torrent_calls_add_torrent(temp_db: str) -> None:
    """AC-1: torrent kind with base64-prefixed uri routes to add_torrent."""
    user = await create_user_v0(username="s2")
    gd = await create_global_download_v0(
        resource_key="torrent:abc",
        source_uri="base64:AAAA",
        resource_kind="torrent",
        status="queued",
        total_bytes=1024,
        size_known=True,
    )
    await create_user_task_v0(
        user_id=user["id"], global_download_id=gd["id"], status="queued"
    )
    client = _make_aria2_client()
    adapter = Aria2BackendAdapter(client)

    gid = await adapter.submit(tid=gd["id"], uri="base64:AAAA", options={})

    assert gid == "gid-tor"
    client.add_torrent.assert_awaited_once()
    call_args = client.add_torrent.await_args
    assert call_args.args[0] == "AAAA"
    assert call_args.args[1] == []
    opts = call_args.args[2]
    assert opts["seed-time"] == "0"
    assert opts.get("pause") == "true"
    assert "dir" in opts
    row = await _get_global(gd["id"])
    assert row is not None
    assert row["aria2_gid"] == "gid-tor"
    assert row["status"] == "paused"
    assert row["error_code"] == "admission_paused"


@pytest.mark.asyncio
async def test_submit_tid_returns_gid_after_register(temp_db: str) -> None:
    """submit_tid reads the row and submits via BackendPort."""
    user = await create_user_v0(username="s3")
    gd = await create_global_download_v0(
        resource_key="http://example.com/a.bin",
        source_uri="http://example.com/a.bin",
        resource_kind="http",
        status="queued",
        total_bytes=0,
        size_known=False,
    )
    await create_user_task_v0(
        user_id=user["id"], global_download_id=gd["id"], status="queued"
    )
    backend = AsyncMock(spec=BackendPort)
    backend.submit = AsyncMock(return_value="gid-xyz")

    gid = await submit_tid(backend=backend, tid=gd["id"])

    assert gid == "gid-xyz"
    backend.submit.assert_awaited_once_with(
        tid=gd["id"], uri="http://example.com/a.bin", options={}
    )


@pytest.mark.asyncio
async def test_submit_tid_skips_already_submitted(temp_db: str) -> None:
    """submit_tid returns existing gid without calling backend again."""
    user = await create_user_v0(username="s4")
    gd = await create_global_download_v0(
        resource_key="http://example.com/b.bin",
        source_uri="http://example.com/b.bin",
        resource_kind="http",
        status="active",
        aria2_gid="gid-existing",
        total_bytes=100,
        size_known=True,
    )
    await create_user_task_v0(
        user_id=user["id"], global_download_id=gd["id"], status="active"
    )
    backend = AsyncMock(spec=BackendPort)

    gid = await submit_tid(backend=backend, tid=gd["id"])

    assert gid == "gid-existing"
    backend.submit.assert_not_called()


@pytest.mark.asyncio
async def test_sync_once_updates_completed_bytes_and_status(temp_db: str) -> None:
    """AC-8: sync_once batch tell_many writes completed_bytes/status back."""
    user = await create_user_v0(username="s5")
    gd = await create_global_download_v0(
        resource_key="http://example.com/c.bin",
        source_uri="http://example.com/c.bin",
        resource_kind="http",
        status="active",
        aria2_gid="gid-sync",
        total_bytes=1024,
        completed_bytes=0,
        size_known=True,
    )
    await create_user_task_v0(
        user_id=user["id"], global_download_id=gd["id"], status="active"
    )
    backend = AsyncMock(spec=BackendPort)
    backend.tell_many = AsyncMock(
        return_value=[
            Snapshot(
                tid=gd["id"],
                status="active",
                raw={"completedLength": "512", "totalLength": "1024"},
            )
        ]
    )

    report = await sync_once(backend)

    assert report.fetched == 1
    assert report.updated == 1
    backend.tell_many.assert_awaited_once_with([gd["id"]])
    row = await _get_global(gd["id"])
    assert row is not None
    assert row["completed_bytes"] == 512
    assert row["status"] == "active"


@pytest.mark.asyncio
async def test_sync_once_maps_terminal_status(temp_db: str) -> None:
    """AC-8: backend terminal statuses are mapped to DB strings."""
    user = await create_user_v0(username="s6")
    gd = await create_global_download_v0(
        resource_key="http://example.com/d.bin",
        source_uri="http://example.com/d.bin",
        resource_kind="http",
        status="active",
        aria2_gid="gid-done",
        total_bytes=100,
        completed_bytes=50,
        size_known=True,
    )
    await create_user_task_v0(
        user_id=user["id"], global_download_id=gd["id"], status="active"
    )
    backend = AsyncMock(spec=BackendPort)
    backend.tell_many = AsyncMock(
        return_value=[
            Snapshot(
                tid=gd["id"],
                status="complete",
                raw={"completedLength": "100", "totalLength": "100"},
            )
        ]
    )

    report = await sync_once(backend)

    assert report.updated == 1
    row = await _get_global(gd["id"])
    assert row is not None
    assert row["completed_bytes"] == 100
    assert row["status"] == "completed"


@pytest.mark.asyncio
async def test_adapter_remove_calls_client_remove(temp_db: str) -> None:
    """AC-1: adapter.remove resolves gid from DB and calls client.remove."""
    user = await create_user_v0(username="s7")
    gd = await create_global_download_v0(
        resource_key="http://example.com/e.bin",
        source_uri="http://example.com/e.bin",
        resource_kind="http",
        status="active",
        aria2_gid="gid-rm",
        total_bytes=10,
        size_known=True,
    )
    await create_user_task_v0(
        user_id=user["id"], global_download_id=gd["id"], status="active"
    )
    client = _make_aria2_client()
    adapter = Aria2BackendAdapter(client)

    await adapter.remove(gd["id"])

    client.remove.assert_awaited_once_with("gid-rm")
