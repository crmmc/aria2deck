from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import insert, select

from app.core.config import settings
from app.db.engine import transaction
from app.db.schema import global_downloads, stored_files, user_files
from app.repositories.downloads import get_user_task_by_id
from app.services.repair import repair_task_associations
from app.services.usage_service import get_usage, reserve_bytes
from tests.helpers_v0 import (
    create_global_download_v0,
    create_user_task_v0,
    create_user_v0,
    now_ms,
)


async def _create_stored_file(name: str, size: int) -> dict:
    path = Path(settings.download_dir) / "store" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    timestamp = now_ms()
    async with transaction() as conn:
        row = (
            (
                await conn.execute(
                    insert(stored_files)
                    .values(
                        content_hash=f"repair_{name}",
                        real_path=str(path),
                        size_bytes=size,
                        is_directory=0,
                        original_name=name,
                        created_at_ms=timestamp,
                    )
                    .returning(stored_files)
                )
            )
            .mappings()
            .one()
        )
    return dict(row)


@pytest.mark.asyncio
async def test_repair_task_associations_completes_user_lifecycle(temp_db: str) -> None:
    user = await create_user_v0(username="repair_full", quota_bytes=1000)
    await reserve_bytes(user["id"], 7, quota_bytes=user["quota_bytes"])
    stored = await _create_stored_file("payload.bin", 7)
    download = await create_global_download_v0(
        resource_key="repair:payload",
        resource_kind="http",
        source_uri="https://example.com/payload.bin",
        status="completed",
        display_name="payload.bin",
        total_bytes=7,
        completed_bytes=7,
        completed_file_id=None,
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=7,
        display_name="payload.bin",
    )

    repaired = await repair_task_associations()

    async with transaction() as conn:
        updated_download = (
            (
                await conn.execute(
                    select(global_downloads).where(
                        global_downloads.c.id == download["id"]
                    )
                )
            )
            .mappings()
            .one()
        )
        user_file = (
            (
                await conn.execute(
                    select(user_files).where(
                        user_files.c.user_id == user["id"],
                        user_files.c.stored_file_id == stored["id"],
                    )
                )
            )
            .mappings()
            .first()
        )
    updated_task = await get_user_task_by_id(user["id"], task["id"])
    usage = await get_usage(user["id"], quota_bytes=user["quota_bytes"])

    assert repaired == 1
    assert updated_download["completed_file_id"] == stored["id"]
    assert updated_task is not None
    assert updated_task["status"] == "completed"
    assert updated_task["reserved_bytes"] == 0
    assert user_file is not None
    assert usage["reserved_bytes"] == 0
    assert usage["used_bytes"] == 7


@pytest.mark.asyncio
async def test_repair_task_associations_skips_unsafe_size_match(temp_db: str) -> None:
    user = await create_user_v0(username="repair_skip", quota_bytes=1000)
    await reserve_bytes(user["id"], 7, quota_bytes=user["quota_bytes"])
    await _create_stored_file("payload.bin", 8)
    download = await create_global_download_v0(
        resource_key="repair:skip",
        resource_kind="http",
        source_uri="https://example.com/payload.bin",
        status="completed",
        display_name="payload.bin",
        total_bytes=7,
        completed_bytes=7,
        completed_file_id=None,
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=7,
        display_name="payload.bin",
    )

    repaired = await repair_task_associations()
    updated_task = await get_user_task_by_id(user["id"], task["id"])
    usage = await get_usage(user["id"], quota_bytes=user["quota_bytes"])

    assert repaired == 0
    assert updated_task is not None
    assert updated_task["status"] == "active"
    assert updated_task["reserved_bytes"] == 7
    assert usage["reserved_bytes"] == 7


@pytest.mark.asyncio
async def test_repair_task_associations_skips_ambiguous_name_size_match(
    temp_db: str,
) -> None:
    user = await create_user_v0(username="repair_ambiguous", quota_bytes=1000)
    await reserve_bytes(user["id"], 7, quota_bytes=user["quota_bytes"])
    await _create_stored_file("payload.bin", 7)
    await _create_stored_file("payload-copy.bin", 7)
    timestamp = now_ms()
    second_path = Path(settings.download_dir) / "store" / "payload-copy.bin"
    async with transaction() as conn:
        await conn.execute(
            stored_files.update()
            .where(stored_files.c.real_path == str(second_path))
            .values(
                content_hash="repair_payload_duplicate",
                original_name="payload.bin",
                created_at_ms=timestamp,
            )
        )
    download = await create_global_download_v0(
        resource_key="repair:ambiguous",
        resource_kind="http",
        source_uri="https://example.com/payload.bin",
        status="completed",
        display_name="payload.bin",
        total_bytes=7,
        completed_bytes=7,
        completed_file_id=None,
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=7,
        display_name="payload.bin",
    )

    repaired = await repair_task_associations()
    updated_task = await get_user_task_by_id(user["id"], task["id"])
    usage = await get_usage(user["id"], quota_bytes=user["quota_bytes"])

    assert repaired == 0
    assert updated_task is not None
    assert updated_task["status"] == "active"
    assert updated_task["reserved_bytes"] == 7
    assert usage["reserved_bytes"] == 7
