from __future__ import annotations

import asyncio

import pytest

import app.services.usage_service as usage_service
from app.services.usage_service import (
    get_usage,
    release_reserved,
    reserve_bytes,
    update_used_bytes,
)
from tests.helpers_v0 import create_user_v0


@pytest.mark.asyncio
async def test_reserve_and_release_bytes(temp_db: str) -> None:
    user = await create_user_v0(username="usage_user", quota_bytes=1000)

    usage = await reserve_bytes(user["id"], 400)
    assert usage["reserved_bytes"] == 400
    assert usage["available_bytes"] == 600

    usage = await release_reserved(user["id"], 150)
    assert usage["reserved_bytes"] == 250
    assert usage["available_bytes"] == 750


@pytest.mark.asyncio
async def test_reserve_rejects_quota_exceeded(temp_db: str) -> None:
    user = await create_user_v0(username="quota_user", quota_bytes=100)

    with pytest.raises(ValueError, match="quota exceeded"):
        await reserve_bytes(user["id"], 101)


@pytest.mark.asyncio
async def test_concurrent_reserve_enforces_quota_atomically(
    temp_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await create_user_v0(username="quota_race_user", quota_bytes=1000)
    original_get_usage_row = usage_service.get_usage_row
    lock = asyncio.Lock()
    both_read = asyncio.Event()
    read_count = 0

    async def synchronized_get_usage_row(user_id: int) -> dict:
        nonlocal read_count
        row = await original_get_usage_row(user_id)
        async with lock:
            read_count += 1
            if read_count == 2:
                both_read.set()
        await both_read.wait()
        return row

    monkeypatch.setattr(usage_service, "get_usage_row", synchronized_get_usage_row)

    results = await asyncio.gather(
        reserve_bytes(user["id"], 600),
        reserve_bytes(user["id"], 600),
        return_exceptions=True,
    )
    monkeypatch.setattr(usage_service, "get_usage_row", original_get_usage_row)

    successes = [result for result in results if not isinstance(result, Exception)]
    failures = [result for result in results if isinstance(result, ValueError)]
    usage = await get_usage(user["id"], quota_bytes=1000)

    assert len(successes) == 1
    assert len(failures) == 1
    assert usage["reserved_bytes"] == 600
    assert usage["available_bytes"] == 400


@pytest.mark.asyncio
async def test_used_bytes_reduce_available(temp_db: str) -> None:
    user = await create_user_v0(username="used_user", quota_bytes=1000)

    await update_used_bytes(user["id"], 300)
    usage = await get_usage(user["id"], quota_bytes=1000)

    assert usage["used_bytes"] == 300
    assert usage["reserved_bytes"] == 0
    assert usage["available_bytes"] == 700


@pytest.mark.asyncio
async def test_negative_reserve_and_release_are_rejected(temp_db: str) -> None:
    user = await create_user_v0(username="negative_usage_user", quota_bytes=1000)

    with pytest.raises(ValueError, match="amount must be non-negative"):
        await reserve_bytes(user["id"], -1)

    with pytest.raises(ValueError, match="amount must be non-negative"):
        await release_reserved(user["id"], -1)


@pytest.mark.asyncio
async def test_release_reserved_does_not_go_below_zero(temp_db: str) -> None:
    user = await create_user_v0(username="release_floor_user", quota_bytes=1000)

    await reserve_bytes(user["id"], 100)
    usage = await release_reserved(user["id"], 250)

    assert usage["reserved_bytes"] == 0
    assert usage["available_bytes"] == 1000


@pytest.mark.asyncio
async def test_used_bytes_delta_does_not_go_below_zero(temp_db: str) -> None:
    user = await create_user_v0(username="used_floor_user", quota_bytes=1000)

    await update_used_bytes(user["id"], 100)
    usage = await update_used_bytes(user["id"], -250)

    assert usage["used_bytes"] == 0
    assert usage["available_bytes"] == 1000
