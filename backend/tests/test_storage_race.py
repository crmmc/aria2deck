from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import insert, select

from app.auth import user_from_row
from app.core.config import settings
from app.db.engine import transaction
from app.db.schema import (
    global_downloads,
    pack_tasks,
    stored_files,
    user_tasks,
    user_files,
    user_storage_usage,
)
from app.routers.files import _delete_user_file_reference_v0
from app.routers.storage import BulkDeleteRequest, bulk_delete_files
from tests.helpers_v0 import create_user_v0, now_ms


async def _seed_shared_file(user_ids: list[int]) -> dict:
    path = Path(settings.download_dir) / "store" / "shared.bin"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"shared")
    timestamp = now_ms()
    async with transaction() as conn:
        stored = (
            (
                await conn.execute(
                    insert(stored_files)
                    .values(
                        content_hash="shared_hash",
                        real_path=str(path),
                        size_bytes=6,
                        is_directory=0,
                        original_name="shared.bin",
                        created_at_ms=timestamp,
                    )
                    .returning(stored_files)
                )
            )
            .mappings()
            .one()
        )
        user_file_ids: list[int] = []
        for user_id in user_ids:
            row = (
                await conn.execute(
                    insert(user_files)
                    .values(
                        user_id=user_id,
                        stored_file_id=stored["id"],
                        display_name=f"shared-{user_id}.bin",
                        created_at_ms=timestamp,
                        updated_at_ms=timestamp,
                    )
                    .returning(user_files.c.id)
                )
            ).one()
            user_file_ids.append(int(row[0]))
    return {
        "stored_file_id": stored["id"],
        "path": path,
        "user_file_ids": user_file_ids,
    }


@pytest.mark.asyncio
async def test_delete_user_file_keeps_shared_storage_until_last_reference(
    temp_db: str,
) -> None:
    user_a = await create_user_v0(username="storage_ref_a")
    user_b = await create_user_v0(username="storage_ref_b")
    seeded = await _seed_shared_file([user_a["id"], user_b["id"]])

    assert await _delete_user_file_reference_v0(
        user_a["id"],
        seeded["user_file_ids"][0],
    )

    async with transaction() as conn:
        stored_after_first = (
            (
                await conn.execute(
                    select(stored_files).where(
                        stored_files.c.id == seeded["stored_file_id"]
                    )
                )
            )
            .mappings()
            .first()
        )
        refs_after_first = (
            (
                await conn.execute(
                    select(user_files).where(
                        user_files.c.stored_file_id == seeded["stored_file_id"]
                    )
                )
            )
            .mappings()
            .all()
        )

    assert stored_after_first is not None
    assert len(refs_after_first) == 1
    assert seeded["path"].exists()

    assert await _delete_user_file_reference_v0(
        user_b["id"],
        seeded["user_file_ids"][1],
    )

    async with transaction() as conn:
        stored_after_last = (
            (
                await conn.execute(
                    select(stored_files).where(
                        stored_files.c.id == seeded["stored_file_id"]
                    )
                )
            )
            .mappings()
            .first()
        )

    assert stored_after_last is None
    assert not seeded["path"].exists()


@pytest.mark.asyncio
async def test_concurrent_delete_same_user_file_deletes_once(temp_db: str) -> None:
    user = await create_user_v0(username="storage_race_user")
    seeded = await _seed_shared_file([user["id"]])

    results = await asyncio.gather(
        _delete_user_file_reference_v0(user["id"], seeded["user_file_ids"][0]),
        _delete_user_file_reference_v0(user["id"], seeded["user_file_ids"][0]),
    )

    async with transaction() as conn:
        refs = (
            (
                await conn.execute(
                    select(user_files).where(
                        user_files.c.stored_file_id == seeded["stored_file_id"]
                    )
                )
            )
            .mappings()
            .all()
        )
        stored = (
            (
                await conn.execute(
                    select(stored_files).where(
                        stored_files.c.id == seeded["stored_file_id"]
                    )
                )
            )
            .mappings()
            .first()
        )

    assert sorted(results) == [False, True]
    assert refs == []
    assert stored is None


@pytest.mark.asyncio
async def test_delete_last_reference_clears_download_and_pack_fk_and_usage(
    temp_db: str,
) -> None:
    user = await create_user_v0(username="storage_fk_user")
    seeded = await _seed_shared_file([user["id"]])
    timestamp = now_ms()
    async with transaction() as conn:
        global_download = (
            (
                await conn.execute(
                    insert(global_downloads)
                    .values(
                        resource_key="http:storage-fk",
                        resource_kind="http",
                        source_uri="https://example.com/file",
                        status="completed",
                        total_bytes=6,
                        completed_bytes=6,
                        completed_file_id=seeded["stored_file_id"],
                        created_at_ms=timestamp,
                        updated_at_ms=timestamp,
                        completed_at_ms=timestamp,
                    )
                    .returning(global_downloads)
                )
            )
            .mappings()
            .one()
        )
        await conn.execute(
            insert(user_tasks).values(
                user_id=user["id"],
                global_download_id=global_download["id"],
                status="completed",
                reserved_bytes=0,
                display_name="shared.bin",
                created_at_ms=timestamp,
                updated_at_ms=timestamp,
                finished_at_ms=timestamp,
            )
        )
        await conn.execute(
            insert(pack_tasks).values(
                user_id=user["id"],
                source_user_file_ids_json="[]",
                source_size_bytes=6,
                reserved_bytes=0,
                output_stored_file_id=seeded["stored_file_id"],
                delete_source=0,
                status="completed",
                progress=100,
                created_at_ms=timestamp,
                updated_at_ms=timestamp,
                finished_at_ms=timestamp,
            )
        )
        await conn.execute(
            user_storage_usage.update()
            .where(user_storage_usage.c.user_id == user["id"])
            .values(used_bytes=6, updated_at_ms=timestamp)
        )

    assert await _delete_user_file_reference_v0(
        user["id"],
        seeded["user_file_ids"][0],
    )

    async with transaction() as conn:
        download = (await conn.execute(select(global_downloads))).mappings().one()
        user_task = (await conn.execute(select(user_tasks))).mappings().one()
        pack_file_id = (
            await conn.execute(select(pack_tasks.c.output_stored_file_id))
        ).scalar_one()
        usage = (
            (
                await conn.execute(
                    select(user_storage_usage).where(
                        user_storage_usage.c.user_id == user["id"]
                    )
                )
            )
            .mappings()
            .one()
        )

    assert download["completed_file_id"] is None
    assert download["status"] == "cancelled"
    assert download["completed_bytes"] == 0
    assert download["completed_at_ms"] is None
    assert user_task["status"] == "cancelled"
    assert pack_file_id is None
    assert usage["used_bytes"] == 0
    assert not seeded["path"].exists()


@pytest.mark.asyncio
async def test_admin_bulk_delete_orphan_clears_download_and_pack_fks(
    temp_db: str,
) -> None:
    admin = await create_user_v0(username="storage_admin", is_admin=True)
    path = Path(settings.download_dir) / "store" / "admin-orphan.bin"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"orphan")
    timestamp = now_ms()
    async with transaction() as conn:
        stored = (
            (
                await conn.execute(
                    insert(stored_files)
                    .values(
                        content_hash="admin_orphan_hash",
                        real_path=str(path),
                        size_bytes=6,
                        is_directory=0,
                        original_name="admin-orphan.bin",
                        created_at_ms=timestamp,
                    )
                    .returning(stored_files)
                )
            )
            .mappings()
            .one()
        )
        await conn.execute(
            insert(global_downloads).values(
                resource_key="http:admin-orphan",
                resource_kind="http",
                source_uri="https://example.com/orphan",
                status="completed",
                total_bytes=6,
                completed_bytes=6,
                completed_file_id=stored["id"],
                created_at_ms=timestamp,
                updated_at_ms=timestamp,
                completed_at_ms=timestamp,
            )
        )
        await conn.execute(
            insert(pack_tasks).values(
                user_id=admin["id"],
                source_user_file_ids_json="[]",
                source_size_bytes=6,
                reserved_bytes=0,
                output_stored_file_id=stored["id"],
                delete_source=0,
                status="completed",
                progress=100,
                created_at_ms=timestamp,
                updated_at_ms=timestamp,
                finished_at_ms=timestamp,
            )
        )

    response = await bulk_delete_files(
        BulkDeleteRequest(file_ids=[stored["id"]]),
        admin=user_from_row(admin),
    )

    async with transaction() as conn:
        stored_after_delete = (
            (
                await conn.execute(
                    select(stored_files).where(stored_files.c.id == stored["id"])
                )
            )
            .mappings()
            .first()
        )
        download = (await conn.execute(select(global_downloads))).mappings().one()
        pack_file_id = (
            await conn.execute(select(pack_tasks.c.output_stored_file_id))
        ).scalar_one()

    assert response.deleted_count == 1
    assert response.failed_ids == []
    assert stored_after_delete is None
    assert download["completed_file_id"] is None
    assert download["status"] == "cancelled"
    assert pack_file_id is None
    assert not path.exists()
