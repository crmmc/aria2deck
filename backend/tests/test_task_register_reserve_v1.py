"""Task C2 — register() usage reservation + adapter unknown-size pause alignment.

Covers:
- known-size create: user_storage_usage.reserved_bytes == size after register.
- known-size create + unref: reserved_bytes returns to 0 (no drift).
- known-size join: second user's reservation tracked independently.
- adapter: unknown-size http submit includes pause=true.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.db.engine import transaction
from app.db.schema import global_downloads, user_storage_usage, user_tasks
from app.modules.backend.aria2_adapter import Aria2BackendAdapter
from app.modules.task_core.register import ResourceSpec, register
from app.modules.task_core.unref import unref
from app.repositories.usage import get_usage_row
from tests.helpers_v0 import (
    create_global_download_v0,
    create_user_task_v0,
    create_user_v0,
)


async def _get_user_task(pid: int) -> dict | None:
    async with transaction() as conn:
        row = (
            await conn.execute(select(user_tasks).where(user_tasks.c.id == pid))
        ).mappings().first()
    return dict(row) if row else None


@pytest.mark.asyncio
async def test_known_size_create_reserves_usage(temp_db: str) -> None:
    """After register with known size, user_storage_usage.reserved_bytes == size."""
    user = await create_user_v0(username="reserve1", quota_bytes=10_000)
    spec = ResourceSpec(
        resource_key="magnet:?xt=urn:btih:res1",
        source_uri="magnet:?xt=urn:btih:res1",
        resource_kind="magnet",
        size_bytes=4096,
        size_known=True,
    )
    result = await register(user_id=user["id"], quota_bytes=user["quota_bytes"], resource=spec)

    assert result.outcome == "created"
    usage = await get_usage_row(user["id"])
    assert usage["reserved_bytes"] == 4096

    task = await _get_user_task(result.pid)
    assert task is not None
    assert task["reserved_bytes"] == 4096


@pytest.mark.asyncio
async def test_known_size_unref_releases_no_drift(temp_db: str) -> None:
    """unref after known-size register brings reserved_bytes back to 0."""
    user = await create_user_v0(username="reserve2", quota_bytes=10_000)
    spec = ResourceSpec(
        resource_key="magnet:?xt=urn:btih:res2",
        source_uri="magnet:?xt=urn:btih:res2",
        resource_kind="magnet",
        size_bytes=2048,
        size_known=True,
    )
    result = await register(user_id=user["id"], quota_bytes=user["quota_bytes"], resource=spec)
    assert result.outcome == "created"

    usage = await get_usage_row(user["id"])
    assert usage["reserved_bytes"] == 2048

    backend = AsyncMock()
    backend.remove = AsyncMock()
    await unref(user_id=user["id"], pid=result.pid, backend=backend)

    usage_after = await get_usage_row(user["id"])
    assert usage_after["reserved_bytes"] == 0


@pytest.mark.asyncio
async def test_join_known_size_each_user_reserved(temp_db: str) -> None:
    """Two users joining the same known-size tid each get their own reservation."""
    owner = await create_user_v0(username="join-owner", quota_bytes=10_000)
    other = await create_user_v0(username="join-other", quota_bytes=10_000)

    gd = await create_global_download_v0(
        resource_key="magnet:?xt=urn:btih:join1",
        source_uri="magnet:?xt=urn:btih:join1",
        resource_kind="magnet",
        status="active",
        total_bytes=1024,
        size_known=True,
    )
    # Owner already has a task (simulating first-user register).
    await create_user_task_v0(
        user_id=owner["id"], global_download_id=gd["id"], status="active"
    )

    spec = ResourceSpec(
        resource_key="magnet:?xt=urn:btih:join1",
        source_uri="magnet:?xt=urn:btih:join1",
        resource_kind="magnet",
        size_bytes=1024,
        size_known=True,
    )
    result = await register(user_id=other["id"], quota_bytes=other["quota_bytes"], resource=spec)

    assert result.outcome == "joined_live"

    usage_other = await get_usage_row(other["id"])
    assert usage_other["reserved_bytes"] == 1024

    # unref the joiner; reserved returns to 0 for other, owner unaffected.
    backend = AsyncMock()
    backend.remove = AsyncMock()
    await unref(user_id=other["id"], pid=result.pid, backend=backend)

    usage_other_after = await get_usage_row(other["id"])
    assert usage_other_after["reserved_bytes"] == 0


@pytest.mark.asyncio
async def test_known_size_create_rejected_by_usage_quota(temp_db: str) -> None:
    """Reserve fails when used+reserved+size exceeds quota — no pid created."""
    user = await create_user_v0(username="reserve3", quota_bytes=500)
    spec = ResourceSpec(
        resource_key="magnet:?xt=urn:btih:res3",
        source_uri="magnet:?xt=urn:btih:res3",
        resource_kind="magnet",
        size_bytes=600,
        size_known=True,
    )
    from app.modules.task_core.register import RegisterError
    from app.modules.task_core.states import ERROR_QUOTA_EXCEEDED

    with pytest.raises(RegisterError) as excinfo:
        await register(user_id=user["id"], quota_bytes=user["quota_bytes"], resource=spec)
    assert excinfo.value.code == ERROR_QUOTA_EXCEEDED

    # No reservation leaked.
    usage = await get_usage_row(user["id"])
    assert usage["reserved_bytes"] == 0


@pytest.mark.asyncio
async def test_adapter_http_unknown_size_adds_pause(temp_db: str) -> None:
    """Unknown-size http submit includes pause=true (aligned with old path)."""
    gd = await create_global_download_v0(
        resource_key="http://example.com/unknown",
        source_uri="http://example.com/unknown",
        resource_kind="http",
        status="queued",
        total_bytes=0,
        size_known=False,
    )
    client = AsyncMock()
    client.add_uri = AsyncMock(return_value="gid-http")
    adapter = Aria2BackendAdapter(client)

    gid = await adapter.submit(
        tid=gd["id"], uri="http://example.com/unknown", options={}
    )

    assert gid == "gid-http"
    client.add_uri.assert_awaited_once()
    _, opts = client.add_uri.await_args.args
    assert opts.get("pause") == "true"

    async with transaction() as conn:
        row = (
            await conn.execute(
                select(global_downloads).where(global_downloads.c.id == gd["id"])
            )
        ).mappings().first()
    assert row is not None
    assert row["status"] == "paused"
    assert row["error_code"] == "admission_paused"


@pytest.mark.asyncio
async def test_adapter_http_known_size_no_pause(temp_db: str) -> None:
    """Known-size http submit does NOT include pause (downloads immediately)."""
    gd = await create_global_download_v0(
        resource_key="http://example.com/known",
        source_uri="http://example.com/known",
        resource_kind="http",
        status="queued",
        total_bytes=1024,
        size_known=True,
    )
    client = AsyncMock()
    client.add_uri = AsyncMock(return_value="gid-http2")
    adapter = Aria2BackendAdapter(client)

    gid = await adapter.submit(
        tid=gd["id"], uri="http://example.com/known", options={}
    )

    assert gid == "gid-http2"
    _, opts = client.add_uri.await_args.args
    assert "pause" not in opts

    async with transaction() as conn:
        row = (
            await conn.execute(
                select(global_downloads).where(global_downloads.c.id == gd["id"])
            )
        ).mappings().first()
    assert row is not None
    assert row["status"] == "active"
