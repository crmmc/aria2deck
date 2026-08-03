from __future__ import annotations

import asyncio
import gc
import weakref
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import Response
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
from app.services.deletion_cleanup import DeletionCleanupManager
from app.services.file_service import delete_user_file_reference_v0
from app.services.task_broadcast import (
    clear_connections,
    remove_connections_for_user,
    set_connections_for_user,
)
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
async def test_content_hash_lock_registry_releases_unused_locks(temp_db: str) -> None:
    from app.services import storage_locks

    locks = await asyncio.gather(
        *(storage_locks.get_content_hash_lock("weak-lock") for _ in range(20))
    )
    assert all(lock is locks[0] for lock in locks)
    key = (id(asyncio.get_running_loop()), "weak-lock")
    assert key in storage_locks._content_hash_locks
    lock_ref = weakref.ref(locks[0])
    del locks
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    gc.collect()
    assert lock_ref() is None
    assert key not in storage_locks._content_hash_locks


@pytest.mark.asyncio
async def test_delete_user_file_keeps_shared_storage_until_last_reference(
    temp_db: str,
) -> None:
    user_a = await create_user_v0(username="storage_ref_a")
    user_b = await create_user_v0(username="storage_ref_b")
    seeded = await _seed_shared_file([user_a["id"], user_b["id"]])

    assert await delete_user_file_reference_v0(
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

    assert await delete_user_file_reference_v0(
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

    assert stored_after_last is not None
    assert stored_after_last["pending_delete"] == 1
    assert seeded["path"].exists()

    await DeletionCleanupManager.run_once()
    async with transaction() as conn:
        stored_after_cleanup = (
            await conn.execute(
                select(stored_files.c.id).where(
                    stored_files.c.id == seeded["stored_file_id"]
                )
            )
        ).first()

    assert stored_after_cleanup is None
    assert not seeded["path"].exists()


@pytest.mark.asyncio
async def test_concurrent_delete_same_user_file_deletes_once(temp_db: str) -> None:
    user = await create_user_v0(username="storage_race_user")
    seeded = await _seed_shared_file([user["id"]])

    results = await asyncio.gather(
        delete_user_file_reference_v0(user["id"], seeded["user_file_ids"][0]),
        delete_user_file_reference_v0(user["id"], seeded["user_file_ids"][0]),
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
    assert stored is not None
    assert stored["pending_delete"] == 1

    await DeletionCleanupManager.run_once()
    async with transaction() as conn:
        stored_after_cleanup = (
            await conn.execute(
                select(stored_files.c.id).where(
                    stored_files.c.id == seeded["stored_file_id"]
                )
            )
        ).first()
    assert stored_after_cleanup is None
    assert not seeded["path"].exists()


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

    assert await delete_user_file_reference_v0(
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
    assert seeded["path"].exists()

    await DeletionCleanupManager.run_once()
    assert not seeded["path"].exists()


@pytest.mark.asyncio
async def test_delete_user_file_reference_result_reports_affected_downloads(
    temp_db: str,
) -> None:
    from app.services.file_service import delete_user_file_reference_v0_result

    user = await create_user_v0(username="storage_broadcast_user")
    seeded = await _seed_shared_file([user["id"]])
    timestamp = now_ms()
    async with transaction() as conn:
        global_download = (
            (
                await conn.execute(
                    insert(global_downloads)
                    .values(
                        resource_key="http:storage-broadcast",
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

    result = await delete_user_file_reference_v0_result(
        user["id"],
        seeded["user_file_ids"][0],
    )

    assert result.deleted is True
    assert result.affected_download_ids == [global_download["id"]]


@pytest.mark.asyncio
async def test_delete_shared_non_final_reference_reports_no_affected_downloads(
    temp_db: str,
) -> None:
    from app.services.file_service import delete_user_file_reference_v0_result

    user_a = await create_user_v0(username="storage_broadcast_a")
    user_b = await create_user_v0(username="storage_broadcast_b")
    seeded = await _seed_shared_file([user_a["id"], user_b["id"]])

    result = await delete_user_file_reference_v0_result(
        user_a["id"],
        seeded["user_file_ids"][0],
    )

    assert result.deleted is True
    assert result.affected_download_ids == []


def test_delete_file_endpoint_broadcasts_affected_task_update(
    authenticated_client,
    test_user: dict,
) -> None:
    import asyncio

    seeded = asyncio.run(_seed_shared_file([test_user["id"]]))
    timestamp = now_ms()
    async_mock = AsyncMock()
    asyncio.run(clear_connections())
    asyncio.run(set_connections_for_user(test_user["id"], {async_mock}))

    async def _seed_download() -> int:
        async with transaction() as conn:
            global_download = (
                (
                    await conn.execute(
                        insert(global_downloads)
                        .values(
                            resource_key="http:storage-endpoint-broadcast",
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
            task = (
                (
                    await conn.execute(
                        insert(user_tasks)
                        .values(
                            user_id=test_user["id"],
                            global_download_id=global_download["id"],
                            status="completed",
                            reserved_bytes=0,
                            display_name="shared.bin",
                            created_at_ms=timestamp,
                            updated_at_ms=timestamp,
                            finished_at_ms=timestamp,
                        )
                        .returning(user_tasks)
                    )
                )
                .mappings()
                .one()
            )
        return int(task["id"])

    task_id = asyncio.run(_seed_download())

    try:
        response = authenticated_client.delete("/api/files/shared_hash")
    finally:
        asyncio.run(remove_connections_for_user(test_user["id"]))

    assert response.status_code == 202
    assert response.json() == {
        "ok": True,
        "state": "pending",
        "accepted": True,
    }
    async_mock.send_json.assert_awaited_once()
    payload = async_mock.send_json.await_args.args[0]
    assert payload["type"] == "task_update"
    assert payload["task"]["id"] == task_id
    assert payload["task"]["status"] == "error"


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

    raw_response = Response()
    response = await bulk_delete_files(
        BulkDeleteRequest(file_ids=[stored["id"]]),
        raw_response,
        admin=user_from_row(admin),
    )

    async with transaction() as conn:
        pending = (
            await conn.execute(
                select(stored_files).where(stored_files.c.id == stored["id"])
            )
        ).mappings().one()
        download = (await conn.execute(select(global_downloads))).mappings().one()
        pack_file_id = (
            await conn.execute(select(pack_tasks.c.output_stored_file_id))
        ).scalar_one()

    assert raw_response.status_code == 202
    assert response.deleted_count == 0
    assert response.accepted_count == 1
    assert response.failed_ids == []
    assert pending["pending_delete"] == 1
    assert download["completed_file_id"] is None
    assert download["status"] == "cancelled"
    assert pack_file_id is None
    assert path.exists()

    await DeletionCleanupManager.run_once()
    async with transaction() as conn:
        stored_after_delete = (
            await conn.execute(
                select(stored_files.c.id).where(stored_files.c.id == stored["id"])
            )
        ).first()
    assert stored_after_delete is None
    assert not path.exists()


@pytest.mark.asyncio
async def test_admin_bulk_delete_physical_cleanup_failure_still_broadcasts_committed_state(
    temp_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin = await create_user_v0(username="storage_admin_cleanup_fail", is_admin=True)
    path = Path(settings.download_dir) / "store" / "admin-cleanup-fail.bin"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"orphan")
    timestamp = now_ms()
    async with transaction() as conn:
        stored = (
            (
                await conn.execute(
                    insert(stored_files)
                    .values(
                        content_hash="admin_cleanup_fail_hash",
                        real_path=str(path),
                        size_bytes=6,
                        is_directory=0,
                        original_name="admin-cleanup-fail.bin",
                        created_at_ms=timestamp,
                    )
                    .returning(stored_files)
                )
            )
            .mappings()
            .one()
        )
        global_download = (
            (
                await conn.execute(
                    insert(global_downloads)
                    .values(
                        resource_key="http:admin-cleanup-fail",
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
                    .returning(global_downloads)
                )
            )
            .mappings()
            .one()
        )
        task = (
            (
                await conn.execute(
                    insert(user_tasks)
                    .values(
                        user_id=admin["id"],
                        global_download_id=global_download["id"],
                        status="completed",
                        reserved_bytes=0,
                        display_name="admin-cleanup-fail.bin",
                        created_at_ms=timestamp,
                        updated_at_ms=timestamp,
                        finished_at_ms=timestamp,
                    )
                    .returning(user_tasks)
                )
            )
            .mappings()
            .one()
        )

    def fail_delete_path(*_: object) -> None:
        raise OSError("simulated cleanup failure")

    monkeypatch.setattr(
        "app.services.deletion_cleanup._delete_stored_path", fail_delete_path
    )
    ws = AsyncMock()
    await clear_connections()
    await set_connections_for_user(admin["id"], {ws})

    raw_response = Response()
    response = await bulk_delete_files(
        BulkDeleteRequest(file_ids=[stored["id"]]),
        raw_response,
        admin=user_from_row(admin),
    )
    await DeletionCleanupManager.run_once()

    async with transaction() as conn:
        pending = (
            await conn.execute(
                select(stored_files).where(stored_files.c.id == stored["id"])
            )
        ).mappings().one()

    assert raw_response.status_code == 202
    assert response.deleted_count == 0
    assert response.accepted_count == 1
    assert response.failed_ids == []
    assert response.errors == []
    assert pending["pending_delete"] == 1
    assert pending["delete_attempts"] == 1
    assert pending["delete_lease_token"] is None
    assert pending["delete_error"].startswith("物理清理失败：OSError")
    assert path.exists()
    ws.send_json.assert_awaited_once()
    payload = ws.send_json.await_args.args[0]
    assert payload["type"] == "task_update"
    assert payload["task"]["id"] == task["id"]
    assert payload["task"]["status"] == "error"


def test_admin_bulk_delete_endpoint_broadcasts_affected_task_update(
    client,
    test_admin: dict,
    admin_session: str,
) -> None:
    import asyncio

    path = Path(settings.download_dir) / "store" / "admin-broadcast.bin"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"orphan")
    timestamp = now_ms()
    async_mock = AsyncMock()
    asyncio.run(clear_connections())
    asyncio.run(set_connections_for_user(test_admin["id"], {async_mock}))

    async def _seed_download() -> tuple[int, int]:
        async with transaction() as conn:
            stored = (
                (
                    await conn.execute(
                        insert(stored_files)
                        .values(
                            content_hash="admin_broadcast_hash",
                            real_path=str(path),
                            size_bytes=6,
                            is_directory=0,
                            original_name="admin-broadcast.bin",
                            created_at_ms=timestamp,
                        )
                        .returning(stored_files)
                    )
                )
                .mappings()
                .one()
            )
            global_download = (
                (
                    await conn.execute(
                        insert(global_downloads)
                        .values(
                            resource_key="http:admin-broadcast",
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
                        .returning(global_downloads)
                    )
                )
                .mappings()
                .one()
            )
            task = (
                (
                    await conn.execute(
                        insert(user_tasks)
                        .values(
                            user_id=test_admin["id"],
                            global_download_id=global_download["id"],
                            status="completed",
                            reserved_bytes=0,
                            display_name="admin-broadcast.bin",
                            created_at_ms=timestamp,
                            updated_at_ms=timestamp,
                            finished_at_ms=timestamp,
                        )
                        .returning(user_tasks)
                    )
                )
                .mappings()
                .one()
            )
        return int(stored["id"]), int(task["id"])

    stored_id, task_id = asyncio.run(_seed_download())
    client.cookies.set(settings.session_cookie_name, admin_session)

    try:
        response = client.request(
            "DELETE",
            "/api/admin/storage/files",
            json={"file_ids": [stored_id]},
        )
    finally:
        asyncio.run(remove_connections_for_user(test_admin["id"]))

    assert response.status_code == 202
    assert response.json()["deleted_count"] == 0
    assert response.json()["accepted_count"] == 1
    assert response.json()["results"] == [
        {
            "file_id": stored_id,
            "ok": True,
            "state": "pending",
            "accepted": True,
            "error": None,
        }
    ]
    async_mock.send_json.assert_awaited_once()
    payload = async_mock.send_json.await_args.args[0]
    assert payload["type"] == "task_update"
    assert payload["task"]["id"] == task_id
    assert payload["task"]["status"] == "error"
