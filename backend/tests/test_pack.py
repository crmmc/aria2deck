from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import insert, select, text

from app.core.config import settings
from app.db.engine import transaction
from app.db.schema import (
    global_downloads,
    pack_task_sources,
    pack_tasks,
    stored_files,
    user_files,
    user_storage_usage,
    user_tasks,
)
from app.services.pack import PackTaskManager, calculate_folder_size, get_reserved_space
from app.repositories.pack import (
    PackAdmissionError,
    create_pending_pack_with_reservation,
    persist_pack_prepared,
)
from app.domain.errors import BadRequestError, ForbiddenError
from app.services.task_broadcast import clear_connections, set_connections_for_user
import app.services.file_service as file_service
import app.services.pack as pack_service
from tests.helpers_v0 import create_user_file_v0, create_user_v0, now_ms


def _v2_file_key(content: bytes) -> str:
    raw_digest = hashlib.sha256(content).digest()
    digest = hashlib.sha256(b"aria2deck-content-v2\x00file\x00" + raw_digest).hexdigest()
    return f"v2:file:{digest}"


async def _insert_pack_task(
    *,
    user_id: int,
    source_ids: list[int],
    source_size_bytes: int,
    reserved_bytes: int,
    status: str = "pending",
    progress: int = 0,
    output_name: str | None = None,
    delete_source: bool = False,
) -> dict:
    timestamp = now_ms()
    async with transaction() as conn:
        row = (
            (
                await conn.execute(
                    insert(pack_tasks)
                    .values(
                        user_id=user_id,
                        source_user_file_ids_json=str(source_ids),
                        source_size_bytes=source_size_bytes,
                        reserved_bytes=reserved_bytes,
                        output_name=output_name,
                        delete_source=int(delete_source),
                        status=status,
                        progress=progress,
                        created_at_ms=timestamp,
                        updated_at_ms=timestamp,
                    )
                    .returning(pack_tasks)
                )
            )
            .mappings()
            .one()
        )
        identities = (
            await conn.execute(
                select(
                    user_files.c.id,
                    user_files.c.stored_file_id,
                    user_files.c.created_at_ms,
                    stored_files.c.content_hash,
                )
                .select_from(user_files.join(stored_files))
                .where(
                    user_files.c.user_id == user_id,
                    user_files.c.id.in_(source_ids),
                )
            )
        ).mappings().all()
        by_id = {int(identity["id"]): identity for identity in identities}
        source_values = [
            {
                "task_id": row["id"], "ordinal": ordinal,
                "original_user_file_id": source_id,
                "stored_file_id": by_id[source_id]["stored_file_id"],
                "user_file_created_at_ms": by_id[source_id]["created_at_ms"],
                "content_hash": by_id[source_id]["content_hash"],
                "cleanup_state": "pending" if delete_source else "retained",
            }
            for ordinal, source_id in enumerate(source_ids)
            if source_id in by_id
        ]
        if source_values:
            await conn.execute(insert(pack_task_sources), source_values)
    return dict(row)


def _setup_disk_space(
    monkeypatch: pytest.MonkeyPatch, *, free_bytes: int, min_free: int
) -> None:
    """统一设置磁盘空间 mock。"""
    disk = type("DiskUsage", (), {"free": free_bytes})()
    monkeypatch.setattr(pack_service.shutil, "disk_usage", lambda _path: disk)
    monkeypatch.setattr(pack_service, "get_min_free_disk", lambda: min_free)


def _mock_write_archive(
    monkeypatch: pytest.MonkeyPatch, content: bytes
) -> None:
    """模拟归档写入，将指定内容写入输出路径。"""
    monkeypatch.setattr(
        PackTaskManager,
        "_write_archive_sync",
        lambda output_path, *_args: output_path.write_bytes(content),
    )


@pytest.mark.asyncio
async def test_get_reserved_space_sums_active_v0_pack_tasks(temp_db: str) -> None:
    user = await create_user_v0(username="pack_reserved")
    await _insert_pack_task(
        user_id=user["id"],
        source_ids=[1],
        source_size_bytes=10,
        reserved_bytes=10,
        status="pending",
    )
    await _insert_pack_task(
        user_id=user["id"],
        source_ids=[2],
        source_size_bytes=20,
        reserved_bytes=20,
        status="packing",
    )
    await _insert_pack_task(
        user_id=user["id"],
        source_ids=[3],
        source_size_bytes=30,
        reserved_bytes=30,
        status="completed",
    )

    assert await get_reserved_space() == 30


@pytest.mark.asyncio
async def test_pack_available_space_subtracts_download_and_pack_commitments(
    temp_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await create_user_v0(username="pack_available_budget")
    await _insert_pack_task(
        user_id=user["id"], source_ids=[1], source_size_bytes=1,
        reserved_bytes=300, status="packing",
    )
    async with transaction() as conn:
        await conn.execute(
            insert(global_downloads).values(
                resource_key="pack:available-budget", resource_kind="http",
                source_uri="https://example.com/budget.bin", status="active",
                total_bytes=200, completed_bytes=50, size_known=1,
                size_limit_bytes=200, disk_reserved_bytes=200,
                created_at_ms=now_ms(), updated_at_ms=now_ms(),
            )
        )
    _setup_disk_space(monkeypatch, free_bytes=1000, min_free=100)

    assert await pack_service.get_server_available_space() == 450


def test_calculate_folder_size_recurses_files(tmp_path: Path) -> None:
    (tmp_path / "nested").mkdir()
    (tmp_path / "a.txt").write_bytes(b"abc")
    (tmp_path / "nested" / "b.txt").write_bytes(b"de")

    assert calculate_folder_size(tmp_path) == 5


@pytest.mark.asyncio
async def test_pack_materialized_bytes_are_not_double_committed(
    temp_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await create_user_v0(username="pack_materialized_budget")
    task = await _insert_pack_task(
        user_id=user["id"], source_ids=[1], source_size_bytes=1,
        reserved_bytes=300, status="packing",
    )
    async with transaction() as conn:
        await conn.execute(pack_tasks.update().where(
            pack_tasks.c.id == task["id"]
        ).values(materialized_bytes=120, install_reserved_bytes=30))
        await conn.execute(insert(global_downloads).values(
            resource_key="pack:materialized-budget", resource_kind="http",
            source_uri="https://example.com/materialized.bin", status="active",
            total_bytes=200, completed_bytes=50, size_known=1,
            size_limit_bytes=200, disk_reserved_bytes=200,
            created_at_ms=now_ms(), updated_at_ms=now_ms(),
        ))
    _setup_disk_space(monkeypatch, free_bytes=1000, min_free=100)

    assert await pack_service.get_server_available_space() == 540


@pytest.mark.asyncio
async def test_update_task_error_marks_failed_and_releases_reserved(
    temp_db: str,
) -> None:
    user = await create_user_v0(username="pack_error", quota_bytes=1000)
    async with transaction() as conn:
        await conn.execute(
            user_storage_usage.update()
            .where(user_storage_usage.c.user_id == user["id"])
            .values(reserved_bytes=50, updated_at_ms=now_ms())
        )
    task = await _insert_pack_task(
        user_id=user["id"],
        source_ids=[1],
        source_size_bytes=50,
        reserved_bytes=50,
        status="packing",
    )

    await PackTaskManager._update_task_error(task["id"], "failed")

    async with transaction() as conn:
        stored_task = (
            (
                await conn.execute(
                    select(pack_tasks).where(pack_tasks.c.id == task["id"])
                )
            )
            .mappings()
            .one()
        )
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

    assert stored_task["status"] == "failed"
    assert stored_task["reserved_bytes"] == 0
    assert stored_task["error_message"] == "failed"
    assert usage["reserved_bytes"] == 0


@pytest.mark.asyncio
async def test_pack_task_writes_archive_and_registers_output(temp_db: str) -> None:
    user = await create_user_v0(username="pack_complete", quota_bytes=10_000)
    source = Path(settings.download_dir) / "store" / "source.txt"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"hello")
    user_file = await create_user_file_v0(
        user_id=user["id"],
        real_path=source,
        content_hash="pack_source_hash",
        display_name="source.txt",
        size_bytes=5,
    )
    async with transaction() as conn:
        await conn.execute(
            user_storage_usage.update()
            .where(user_storage_usage.c.user_id == user["id"])
            .values(used_bytes=5, reserved_bytes=1000, updated_at_ms=now_ms())
        )
    task = await _insert_pack_task(
        user_id=user["id"],
        source_ids=[user_file["id"]],
        source_size_bytes=5,
        reserved_bytes=1000,
        status="pending",
        output_name="packed",
    )

    await PackTaskManager.start_pack(
        task_id=task["id"],
        user_id=user["id"],
        abs_paths=[str(source)],
        file_ids=[user_file["id"]],
        output_name="packed",
        delete_source=False,
        source_names=["source.txt"],
    )

    async with transaction() as conn:
        stored_task = (
            (
                await conn.execute(
                    select(pack_tasks).where(pack_tasks.c.id == task["id"])
                )
            )
            .mappings()
            .one()
        )
        output_ref = (
            (
                await conn.execute(
                    select(user_files).where(
                        user_files.c.user_id == user["id"],
                        user_files.c.stored_file_id
                        == stored_task["output_stored_file_id"],
                    )
                )
            )
            .mappings()
            .one()
        )
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

    assert stored_task["status"] == "completed"
    assert stored_task["progress"] == 100
    assert stored_task["reserved_bytes"] == 0
    assert stored_task["output_stored_file_id"] == output_ref["stored_file_id"]
    assert usage["reserved_bytes"] == 0
    assert usage["used_bytes"] > 0


@pytest.mark.asyncio
async def test_pack_fsyncs_prepared_canonical_and_parent_directories(
    temp_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await create_user_v0(username="pack_fsync", quota_bytes=1000)
    source = Path(settings.download_dir) / "store" / "fsync-source.bin"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"source")
    source_ref = await create_user_file_v0(
        user_id=user["id"], real_path=source, content_hash="fsync_source_hash",
        display_name=source.name, size_bytes=6,
    )
    async with transaction() as conn:
        await conn.execute(user_storage_usage.update().where(
            user_storage_usage.c.user_id == user["id"]
        ).values(reserved_bytes=100, updated_at_ms=now_ms()))
    task = await _insert_pack_task(
        user_id=user["id"], source_ids=[source_ref["id"]], source_size_bytes=6,
        reserved_bytes=100, status="pending", output_name="fsync-output",
    )
    _mock_write_archive(monkeypatch, b"archive")
    fsync_calls: list[int] = []
    monkeypatch.setattr(pack_service.os, "fsync", fsync_calls.append)

    await PackTaskManager.start_pack(task["id"], user["id"], [], [])

    current = await pack_service.get_pack_task_row(task["id"])
    assert current is not None
    assert current["status"] == "completed"
    assert len(fsync_calls) >= 4


@pytest.mark.asyncio
async def test_pack_delete_source_broadcasts_cancelled_download_update(
    temp_db: str,
) -> None:
    user = await create_user_v0(
        username="pack_delete_source_broadcast", quota_bytes=10_000
    )
    source = Path(settings.download_dir) / "store" / "broadcast-source.txt"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"hello")
    source_ref = await create_user_file_v0(
        user_id=user["id"],
        real_path=source,
        content_hash="pack_broadcast_source_hash",
        display_name="broadcast-source.txt",
        size_bytes=5,
    )
    timestamp = now_ms()
    async with transaction() as conn:
        await conn.execute(
            user_storage_usage.update()
            .where(user_storage_usage.c.user_id == user["id"])
            .values(used_bytes=5, reserved_bytes=1000, updated_at_ms=timestamp)
        )
        download = (
            (
                await conn.execute(
                    insert(global_downloads)
                    .values(
                        resource_key="pack:broadcast-source",
                        resource_kind="http",
                        source_uri="https://example.com/broadcast-source.txt",
                        status="completed",
                        total_bytes=5,
                        completed_bytes=5,
                        completed_file_id=source_ref["stored_file_id"],
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
                        user_id=user["id"],
                        global_download_id=download["id"],
                        status="completed",
                        reserved_bytes=0,
                        display_name="broadcast-source.txt",
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
    pack_task = await _insert_pack_task(
        user_id=user["id"],
        source_ids=[source_ref["id"]],
        source_size_bytes=5,
        reserved_bytes=1000,
        status="pending",
        output_name="packed-broadcast",
        delete_source=True,
    )
    ws = AsyncMock()
    await clear_connections()
    await set_connections_for_user(user["id"], {ws})

    await PackTaskManager.start_pack(
        task_id=pack_task["id"],
        user_id=user["id"],
        abs_paths=[str(source)],
        file_ids=[source_ref["id"]],
        output_name="packed-broadcast",
        delete_source=True,
        source_names=["broadcast-source.txt"],
    )

    ws.send_json.assert_awaited_once()
    payload = ws.send_json.await_args.args[0]
    assert payload["type"] == "task_update"
    assert payload["task"]["id"] == task["id"]
    assert payload["task"]["status"] == "error"


@pytest.mark.asyncio
async def test_pack_completion_does_not_charge_used_bytes_for_existing_output_ref(
    temp_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await create_user_v0(username="pack_existing_output", quota_bytes=10_000)
    source = Path(settings.download_dir) / "store" / "existing-source.txt"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"hello")
    source_ref = await create_user_file_v0(
        user_id=user["id"],
        real_path=source,
        content_hash="pack_existing_source_hash",
        display_name="existing-source.txt",
        size_bytes=5,
    )
    output_file = Path(settings.download_dir) / "store" / "existing-output.zip"
    output_file.write_bytes(b"existing")
    await create_user_file_v0(
        user_id=user["id"],
        real_path=output_file,
        content_hash=hashlib.sha256(b"existing").hexdigest(),
        display_name="existing-output.zip",
        size_bytes=8,
    )
    async with transaction() as conn:
        await conn.execute(
            user_storage_usage.update()
            .where(user_storage_usage.c.user_id == user["id"])
            .values(used_bytes=13, reserved_bytes=1000, updated_at_ms=now_ms())
        )
    task = await _insert_pack_task(
        user_id=user["id"],
        source_ids=[source_ref["id"]],
        source_size_bytes=5,
        reserved_bytes=1000,
        status="pending",
        output_name="packed",
    )

    _mock_write_archive(monkeypatch, b"existing")

    await PackTaskManager.start_pack(
        task_id=task["id"],
        user_id=user["id"],
        abs_paths=[str(source)],
        file_ids=[source_ref["id"]],
        output_name="packed",
        delete_source=False,
        source_names=["existing-source.txt"],
    )

    async with transaction() as conn:
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

    assert usage["reserved_bytes"] == 0
    assert usage["used_bytes"] == 21


@pytest.mark.asyncio
async def test_delete_source_preserves_deduplicated_output_reference(
    temp_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await create_user_v0(username="pack_self_dedup", quota_bytes=1000)
    content = b"same-content"
    content_hash = hashlib.sha256(content).hexdigest()
    source = Path(settings.download_dir) / "store" / "same-content.bin"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(content)
    source_ref = await create_user_file_v0(
        user_id=user["id"], real_path=source, content_hash=content_hash,
        display_name=source.name, size_bytes=len(content),
    )
    async with transaction() as conn:
        await conn.execute(
            user_storage_usage.update()
            .where(user_storage_usage.c.user_id == user["id"])
            .values(
                used_bytes=len(content), reserved_bytes=100, updated_at_ms=now_ms()
            )
        )
    task = await _insert_pack_task(
        user_id=user["id"], source_ids=[source_ref["id"]],
        source_size_bytes=len(content), reserved_bytes=100,
        status="pending", output_name="same-output", delete_source=True,
    )

    _mock_write_archive(monkeypatch, content)
    await PackTaskManager.start_pack(task["id"], user["id"], [], [])

    async with transaction() as conn:
        completed = (
            await conn.execute(select(pack_tasks).where(pack_tasks.c.id == task["id"]))
        ).mappings().one()
        source_after = (
            await conn.execute(select(user_files).where(
                user_files.c.id == source_ref["id"]
            ))
        ).mappings().one_or_none()
        usage = (
            await conn.execute(select(user_storage_usage).where(
                user_storage_usage.c.user_id == user["id"]
            ))
        ).mappings().one()
    assert completed["status"] == "completed"
    assert completed["output_stored_file_id"] != source_ref["stored_file_id"]
    assert completed["source_cleanup_pending"] == 0
    assert source_after is None
    assert usage["used_bytes"] == len(content)
    assert usage["reserved_bytes"] == 0


@pytest.mark.asyncio
async def test_source_cleanup_never_deletes_reused_user_file_id(temp_db: str) -> None:
    user = await create_user_v0(username="pack_source_id_reuse")
    old_path = Path(settings.download_dir) / "store" / "old-source.bin"
    old_path.parent.mkdir(parents=True, exist_ok=True)
    old_path.write_bytes(b"old")
    old_ref = await create_user_file_v0(
        user_id=user["id"], real_path=old_path, content_hash="old_source_identity",
        display_name=old_path.name, size_bytes=3,
    )
    task = await _insert_pack_task(
        user_id=user["id"], source_ids=[old_ref["id"]], source_size_bytes=3,
        reserved_bytes=0, status="completed", delete_source=True,
    )
    new_path = Path(settings.download_dir) / "store" / "new-source.bin"
    new_path.write_bytes(b"new")
    async with transaction() as conn:
        await conn.execute(user_files.delete().where(user_files.c.id == old_ref["id"]))
        new_stored = (
            await conn.execute(
                insert(stored_files).values(
                    content_hash="new_source_identity", real_path=str(new_path),
                    size_bytes=3, is_directory=0, original_name=new_path.name,
                    created_at_ms=now_ms(),
                ).returning(stored_files.c.id)
            )
        ).scalar_one()
        await conn.execute(insert(user_files).values(
            id=old_ref["id"], user_id=user["id"], stored_file_id=new_stored,
            display_name=new_path.name, created_at_ms=now_ms(), updated_at_ms=now_ms(),
        ))
        await conn.execute(pack_tasks.update().where(
            pack_tasks.c.id == task["id"]
        ).values(source_cleanup_pending=1))
    current = await pack_service.get_pack_task_row(task["id"])
    assert current is not None

    assert await PackTaskManager._replay_source_cleanup(current)

    async with transaction() as conn:
        reused = (
            await conn.execute(select(user_files).where(
                user_files.c.id == old_ref["id"],
                user_files.c.stored_file_id == new_stored,
            ))
        ).mappings().one_or_none()
        source_state = (
            await conn.execute(select(pack_task_sources.c.cleanup_state).where(
                pack_task_sources.c.task_id == task["id"]
            ))
        ).scalar_one()
        pending = (
            await conn.execute(select(pack_tasks.c.source_cleanup_pending).where(
                pack_tasks.c.id == task["id"]
            ))
        ).scalar_one()
    assert reused is not None
    assert source_state == "identity_mismatch"
    assert pending == 0
    assert new_path.read_bytes() == b"new"


@pytest.mark.asyncio
async def test_deleting_shared_pack_output_clears_only_owner_pointer(
    temp_db: str,
) -> None:
    owner = await create_user_v0(username="pack_output_owner")
    peer = await create_user_v0(username="pack_output_peer")
    output = Path(settings.download_dir) / "store" / "shared-pack-output.bin"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"shared")
    owner_ref = await create_user_file_v0(
        user_id=owner["id"], real_path=output,
        content_hash="shared_pack_output", display_name=output.name, size_bytes=6,
    )
    task = await _insert_pack_task(
        user_id=owner["id"], source_ids=[1], source_size_bytes=1,
        reserved_bytes=0, status="completed", output_name="shared",
    )
    async with transaction() as conn:
        peer_ref = (
            await conn.execute(
                insert(user_files).values(
                    user_id=peer["id"], stored_file_id=owner_ref["stored_file_id"],
                    display_name=output.name,
                    created_at_ms=now_ms(), updated_at_ms=now_ms(),
                ).returning(user_files.c.id)
            )
        ).scalar_one()
        await conn.execute(
            pack_tasks.update().where(pack_tasks.c.id == task["id"]).values(
                output_stored_file_id=owner_ref["stored_file_id"]
            )
        )

    result = await file_service.delete_user_file_reference_v0_result(
        owner["id"], owner_ref["id"]
    )

    async with transaction() as conn:
        output_pointer = (
            await conn.execute(select(pack_tasks.c.output_stored_file_id).where(
                pack_tasks.c.id == task["id"]
            ))
        ).scalar_one_or_none()
        peer_exists = (
            await conn.execute(select(user_files.c.id).where(
                user_files.c.id == peer_ref
            ))
        ).scalar_one_or_none()
        stored_exists = (
            await conn.execute(select(stored_files.c.id).where(
                stored_files.c.id == owner_ref["stored_file_id"]
            ))
        ).scalar_one_or_none()
    assert result.deleted is True
    assert output_pointer is None
    assert peer_exists == peer_ref
    assert stored_exists == owner_ref["stored_file_id"]
    assert output.exists()


@pytest.mark.asyncio
async def test_last_reference_delete_waits_for_pack_finalize_content_lock(
    temp_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.storage import get_store_path_for_hash

    owner = await create_user_v0(username="pack_lock_owner")
    pack_user = await create_user_v0(username="pack_lock_writer", quota_bytes=1000)
    content = b"shared-finalize"
    content_hash = hashlib.sha256(content).hexdigest()
    canonical = get_store_path_for_hash(content_hash)
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_bytes(content)
    owner_ref = await create_user_file_v0(
        user_id=owner["id"], real_path=canonical, content_hash=content_hash,
        display_name="shared.bin", size_bytes=len(content),
    )
    async with transaction() as conn:
        await conn.execute(user_storage_usage.update().where(
            user_storage_usage.c.user_id == pack_user["id"]
        ).values(reserved_bytes=len(content), updated_at_ms=now_ms()))
    task = await _insert_pack_task(
        user_id=pack_user["id"], source_ids=[999],
        source_size_bytes=1, reserved_bytes=len(content), status="packing",
        output_name="barrier",
    )
    filename = "barrier.tar.zst"
    prepared = Path(settings.download_dir) / "downloading" / f"pack_{task['id']}" / filename
    prepared.parent.mkdir(parents=True, exist_ok=True)
    prepared.write_bytes(content)
    async with transaction() as conn:
        await conn.execute(pack_tasks.update().where(
            pack_tasks.c.id == task["id"]
        ).values(
            prepared_content_hash=content_hash,
            prepared_size_bytes=len(content), prepared_filename=filename,
            materialized_bytes=len(content),
        ))
    prepared_task = await pack_service.get_pack_task_row(task["id"])
    assert prepared_task is not None
    entered = asyncio.Event()
    release = asyncio.Event()
    real_finalize = pack_service.finalize_prepared_pack_task

    async def blocked_finalize(
        task_id: int,
        *,
        content_hash: str,
        size_bytes: int,
        filename: str,
        real_path: str,
    ) -> dict[str, Any] | None:
        entered.set()
        await release.wait()
        return await real_finalize(
            task_id,
            content_hash=content_hash,
            size_bytes=size_bytes,
            filename=filename,
            real_path=real_path,
        )

    monkeypatch.setattr(pack_service, "finalize_prepared_pack_task", blocked_finalize)
    finalize = asyncio.create_task(
        PackTaskManager._finalize_prepared(prepared_task, threading.Event())
    )
    await entered.wait()
    deletion = asyncio.create_task(
        file_service.delete_user_file_reference_v0_result(owner["id"], owner_ref["id"])
    )
    await asyncio.sleep(0.05)
    assert not deletion.done()
    release.set()
    await finalize
    assert (await deletion).deleted

    async with transaction() as conn:
        output_task = (
            await conn.execute(select(pack_tasks).where(pack_tasks.c.id == task["id"]))
        ).mappings().one()
        refs = (
            await conn.execute(select(user_files.c.user_id).where(
                user_files.c.stored_file_id == owner_ref["stored_file_id"]
            ))
        ).scalars().all()
        stored = (
            await conn.execute(select(stored_files.c.id).where(
                stored_files.c.id == owner_ref["stored_file_id"]
            ))
        ).scalar_one_or_none()
    assert output_task["status"] == "completed"
    assert refs == [pack_user["id"]]
    assert stored == owner_ref["stored_file_id"]
    assert canonical.read_bytes() == content


@pytest.mark.asyncio
async def test_cancelled_finalize_barrier_removes_unowned_canonical(
    temp_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.storage import get_downloading_dir, get_store_path_for_hash

    user = await create_user_v0(username="pack_finalize_cancel", quota_bytes=1000)
    content = b"cancel-finalize"
    content_hash = hashlib.sha256(content).hexdigest()
    task = await _insert_pack_task(
        user_id=user["id"], source_ids=[1], source_size_bytes=1,
        reserved_bytes=100, status="packing", output_name="cancel-finalize",
    )
    filename = "cancel-finalize.tar.zst"
    prepared = get_downloading_dir() / f"pack_{task['id']}" / filename
    prepared.parent.mkdir(parents=True, exist_ok=True)
    prepared.write_bytes(content)
    async with transaction() as conn:
        await conn.execute(pack_tasks.update().where(
            pack_tasks.c.id == task["id"]
        ).values(
            prepared_content_hash=content_hash,
            prepared_size_bytes=len(content), prepared_filename=filename,
            materialized_bytes=len(content),
        ))
    current = await pack_service.get_pack_task_row(task["id"])
    assert current is not None
    entered = asyncio.Event()

    async def block_finalize(*_args: object, **_kwargs: object) -> None:
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(pack_service, "finalize_prepared_pack_task", block_finalize)
    finalize = asyncio.create_task(
        PackTaskManager._finalize_prepared(current, threading.Event())
    )
    await entered.wait()
    canonical = get_store_path_for_hash(content_hash)
    assert canonical.exists()
    finalize.cancel()
    with pytest.raises(asyncio.CancelledError):
        await finalize

    current = await pack_service.get_pack_task_row(task["id"])
    assert current is not None
    assert current["status"] == "packing"
    assert current["prepared_content_hash"] == content_hash
    assert prepared.exists()
    assert not canonical.exists()


@pytest.mark.asyncio
async def test_pack_refuses_canonical_path_owned_by_different_hash(
    temp_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.storage import get_store_path_for_hash

    user = await create_user_v0(username="pack_target_owner", quota_bytes=1000)
    source = Path(settings.download_dir) / "store" / "target-owner-source.bin"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"source")
    source_ref = await create_user_file_v0(
        user_id=user["id"], real_path=source, content_hash="target_owner_source",
        display_name=source.name, size_bytes=6,
    )
    output = b"expected-output"
    output_hash = _v2_file_key(output)
    foreign = b"foreign-output"
    foreign_hash = hashlib.sha256(foreign).hexdigest()
    target = get_store_path_for_hash(output_hash)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(foreign)
    await create_user_file_v0(
        user_id=user["id"], real_path=target, content_hash=foreign_hash,
        display_name="foreign.bin", size_bytes=len(foreign),
    )
    async with transaction() as conn:
        await conn.execute(user_storage_usage.update().where(
            user_storage_usage.c.user_id == user["id"]
        ).values(reserved_bytes=100, updated_at_ms=now_ms()))
    task = await _insert_pack_task(
        user_id=user["id"], source_ids=[source_ref["id"]], source_size_bytes=6,
        reserved_bytes=100, status="pending", output_name="owned-target",
    )
    _mock_write_archive(monkeypatch, output)

    await PackTaskManager.start_pack(task["id"], user["id"], [], [])

    current = await pack_service.get_pack_task_row(task["id"])
    assert current is not None
    assert current["status"] == "failed"
    assert "属于其他内容" in current["error_message"]
    assert target.read_bytes() == foreign


@pytest.mark.asyncio
async def test_pack_completion_rolls_back_new_output_ref_when_final_update_loses_race(
    temp_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await create_user_v0(username="pack_cancel_race", quota_bytes=10_000)
    source = Path(settings.download_dir) / "store" / "cancel-race-source.txt"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"hello")
    source_ref = await create_user_file_v0(
        user_id=user["id"],
        real_path=source,
        content_hash="pack_cancel_race_source_hash",
        display_name="cancel-race-source.txt",
        size_bytes=5,
    )
    async with transaction() as conn:
        await conn.execute(
            user_storage_usage.update()
            .where(user_storage_usage.c.user_id == user["id"])
            .values(used_bytes=5, reserved_bytes=1000, updated_at_ms=now_ms())
        )
    task = await _insert_pack_task(
        user_id=user["id"],
        source_ids=[source_ref["id"]],
        source_size_bytes=5,
        reserved_bytes=1000,
        status="pending",
        output_name="cancel-race-packed",
    )

    packed = b"cancel-race-output"
    packed_hash = hashlib.sha256(packed).hexdigest()
    _mock_write_archive(monkeypatch, packed)
    real_finalize = pack_service.finalize_prepared_pack_task

    async def racing_finalize(
        task_id: int,
        *,
        content_hash: str,
        size_bytes: int,
        filename: str,
        real_path: str,
    ) -> dict[str, Any] | None:
        await pack_service.cancel_active_pack_task(user["id"], task_id)
        return await real_finalize(
            task_id,
            content_hash=content_hash,
            size_bytes=size_bytes,
            filename=filename,
            real_path=real_path,
        )

    monkeypatch.setattr(
        pack_service,
        "finalize_prepared_pack_task",
        racing_finalize,
    )

    await PackTaskManager.start_pack(
        task_id=task["id"],
        user_id=user["id"],
        abs_paths=[str(source)],
        file_ids=[source_ref["id"]],
        output_name="cancel-race-packed",
        delete_source=True,
        source_names=["cancel-race-source.txt"],
    )

    async with transaction() as conn:
        stored_task = (
            (
                await conn.execute(
                    select(pack_tasks).where(pack_tasks.c.id == task["id"])
                )
            )
            .mappings()
            .one()
        )
        output_refs = (
            (
                await conn.execute(
                    select(user_files).where(
                        user_files.c.user_id == user["id"],
                        user_files.c.display_name.like("cancel-race-packed.%"),
                    )
                )
            )
            .mappings()
            .all()
        )
        source_after_race = (
            (
                await conn.execute(
                    select(user_files).where(user_files.c.id == source_ref["id"])
                )
            )
            .mappings()
            .first()
        )
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

    assert stored_task["status"] == "cancelled"
    assert stored_task["output_stored_file_id"] is None
    assert stored_task["prepared_content_hash"] is None
    assert stored_task["prepared_size_bytes"] is None
    assert output_refs == []
    assert source_after_race is not None
    assert usage["reserved_bytes"] == 0
    assert usage["used_bytes"] == 5
    from app.services.storage import get_downloading_dir, get_store_path_for_hash

    assert not (get_downloading_dir() / f"pack_{task['id']}").exists()
    assert not get_store_path_for_hash(packed_hash).exists()


@pytest.mark.asyncio
async def test_pack_completion_fails_when_archive_exceeds_reserved_quota(
    temp_db: str,
) -> None:
    user = await create_user_v0(username="pack_output_quota", quota_bytes=10)
    source = Path(settings.download_dir) / "store" / "quota-source.txt"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"hello")
    source_ref = await create_user_file_v0(
        user_id=user["id"],
        real_path=source,
        content_hash="pack_quota_source_hash",
        display_name="quota-source.txt",
        size_bytes=5,
    )
    async with transaction() as conn:
        await conn.execute(
            user_storage_usage.update()
            .where(user_storage_usage.c.user_id == user["id"])
            .values(used_bytes=5, reserved_bytes=5, updated_at_ms=now_ms())
        )
    task = await _insert_pack_task(
        user_id=user["id"],
        source_ids=[source_ref["id"]],
        source_size_bytes=5,
        reserved_bytes=5,
        status="pending",
        output_name="quota-packed",
    )

    await PackTaskManager.start_pack(
        task_id=task["id"],
        user_id=user["id"],
        abs_paths=[str(source)],
        file_ids=[source_ref["id"]],
        output_name="quota-packed",
        delete_source=False,
        source_names=["quota-source.txt"],
    )

    async with transaction() as conn:
        stored_task = (
            (
                await conn.execute(
                    select(pack_tasks).where(pack_tasks.c.id == task["id"])
                )
            )
            .mappings()
            .one()
        )
        output_refs = (
            (
                await conn.execute(
                    select(user_files).where(
                        user_files.c.user_id == user["id"],
                        user_files.c.display_name.like("quota-packed.%"),
                    )
                )
            )
            .mappings()
            .all()
        )
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

    assert stored_task["status"] == "failed"
    assert output_refs == []
    assert usage["reserved_bytes"] == 0
    assert usage["used_bytes"] == 5


@pytest.mark.asyncio
async def test_pack_completion_converts_reserved_to_used_without_release_window(
    temp_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    quota_bytes = 15
    user = await create_user_v0(username="pack_atomic_usage", quota_bytes=quota_bytes)
    source = Path(settings.download_dir) / "store" / "atomic-source.txt"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"hello")
    source_ref = await create_user_file_v0(
        user_id=user["id"],
        real_path=source,
        content_hash="pack_atomic_source_hash",
        display_name="atomic-source.txt",
        size_bytes=5,
    )
    async with transaction() as conn:
        await conn.execute(
            user_storage_usage.update()
            .where(user_storage_usage.c.user_id == user["id"])
            .values(used_bytes=5, reserved_bytes=5, updated_at_ms=now_ms())
        )
    task = await _insert_pack_task(
        user_id=user["id"],
        source_ids=[source_ref["id"]],
        source_size_bytes=5,
        reserved_bytes=5,
        status="pending",
        output_name="atomic-packed",
    )

    _mock_write_archive(monkeypatch, b"12345")

    await PackTaskManager.start_pack(
        task_id=task["id"],
        user_id=user["id"],
        abs_paths=[str(source)],
        file_ids=[source_ref["id"]],
        output_name="atomic-packed",
        delete_source=False,
        source_names=["atomic-source.txt"],
    )

    async with transaction() as conn:
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

    assert usage["used_bytes"] + usage["reserved_bytes"] <= quota_bytes
    assert usage["used_bytes"] == 10


@pytest.mark.asyncio
async def test_cancel_pack_sets_running_job_cancel_event(temp_db: str) -> None:
    event_seen = asyncio.Event()

    async def sleeper() -> None:
        event_seen.set()
        await asyncio.sleep(10)

    task = asyncio.create_task(sleeper())
    await event_seen.wait()
    try:
        from app.services.pack import _RunningPackJob

        cancel_event = __import__("threading").Event()
        PackTaskManager._running_tasks[12345] = _RunningPackJob(
            task=task,
            cancel_event=cancel_event,
        )

        assert await PackTaskManager.cancel_pack(12345) is True
        assert cancel_event.is_set()
    finally:
        PackTaskManager._running_tasks.pop(12345, None)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_pack_source_markers_block_file_and_task_deletion(temp_db: str) -> None:
    user = await create_user_v0(username="pack_marker_delete")
    path = Path(settings.download_dir) / "store" / "marker-source.bin"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"marker")
    source = await create_user_file_v0(
        user_id=user["id"], real_path=path, content_hash="marker_source_hash",
        display_name=path.name, size_bytes=6,
    )
    task = await _insert_pack_task(
        user_id=user["id"], source_ids=[source["id"]], source_size_bytes=6,
        reserved_bytes=1, status="pending",
    )

    with pytest.raises(ForbiddenError, match="正在被打包"):
        await file_service.delete_user_file_reference_v0_result(
            user["id"], source["id"]
        )

    async with transaction() as conn:
        await conn.execute(pack_tasks.update().where(
            pack_tasks.c.id == task["id"]
        ).values(status="failed", reserved_bytes=0, source_cleanup_pending=0))
        await conn.execute(pack_task_sources.update().where(
            pack_task_sources.c.task_id == task["id"]
        ).values(cleanup_state="unknown", cleanup_error="identity unknown"))
    cleared = await pack_service.clear_finished_pack_tasks(user["id"])
    assert cleared == {"ok": True, "count": 0}
    with pytest.raises(BadRequestError, match="清理记录尚未完成"):
        await pack_service.cancel_or_delete_pack_task(user["id"], task["id"])
    async with transaction() as conn:
        task_exists = (
            await conn.execute(select(pack_tasks.c.id).where(
                pack_tasks.c.id == task["id"]
            ))
        ).scalar_one_or_none()
        source_exists = (
            await conn.execute(select(user_files.c.id).where(
                user_files.c.id == source["id"]
            ))
        ).scalar_one_or_none()
    assert task_exists == task["id"]
    assert source_exists == source["id"]


@pytest.mark.asyncio
async def test_cancel_user_jobs_waits_for_tracked_executor_thread(
    temp_db: str,
) -> None:
    from app.services.pack import _RunningPackJob

    entered = threading.Event()
    exited = threading.Event()
    cancel_event = threading.Event()
    job: _RunningPackJob

    def blocking_work() -> None:
        entered.set()
        cancel_event.wait(2)
        exited.set()

    async def run() -> None:
        await asyncio.sleep(0)
        await PackTaskManager._run_thread(job, blocking_work)

    task = asyncio.create_task(run())
    job = _RunningPackJob(task, cancel_event, user_id=123)
    PackTaskManager._running_tasks[9876] = job
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        await PackTaskManager.cancel_user_jobs(123)
        assert exited.is_set()
        assert 123 in PackTaskManager._blocked_user_ids
    finally:
        PackTaskManager._running_tasks.pop(9876, None)
        await PackTaskManager.unblock_user(123)


@pytest.mark.asyncio
async def test_pack_manager_bounds_active_coroutines(
    temp_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocking_start(*_args: object, **_kwargs: object) -> None:
        entered.set()
        await release.wait()

    monkeypatch.setattr(PackTaskManager, "start_pack", blocking_start)
    results = [await PackTaskManager.submit(task_id) for task_id in range(9000, 9100)]
    await entered.wait()
    assert results.count(True) == 1
    assert len(PackTaskManager._running_tasks) == 1
    release.set()
    await PackTaskManager.shutdown()
    assert PackTaskManager._running_tasks == {}


@pytest.mark.asyncio
async def test_completed_thread_tasks_are_removed_from_job_set(temp_db: str) -> None:
    current = asyncio.current_task()
    assert current is not None
    job = pack_service._RunningPackJob(current, threading.Event())
    tasks = [
        PackTaskManager._start_thread(job, lambda: None)
        for _ in range(100)
    ]
    await asyncio.gather(*tasks)
    await asyncio.sleep(0)
    assert job.thread_tasks == set()


@pytest.mark.asyncio
async def test_transient_pack_attempt_persists_retry_and_exits(
    temp_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await create_user_v0(username="pack_retry_state")
    task = await _insert_pack_task(
        user_id=user["id"], source_ids=[1], source_size_bytes=1,
        reserved_bytes=100, status="pending",
    )
    monkeypatch.setattr(
        PackTaskManager,
        "_run_persistent_pack",
        AsyncMock(side_effect=OSError("temporary")),
    )

    await PackTaskManager.start_pack(task["id"], user["id"], [], [])

    current = await pack_service.get_pack_task_row(task["id"])
    assert current is not None
    assert current["status"] == "pending"
    assert current["retry_count"] == 1
    assert current["next_retry_at_ms"] > now_ms()
    assert task["id"] not in PackTaskManager._running_tasks


@pytest.mark.asyncio
async def test_pack_duplicate_insert_rolls_back_second_reservation(
    temp_db: str,
) -> None:
    user = await create_user_v0(username="pack_atomic_duplicate", quota_bytes=1000)
    source = Path(settings.download_dir) / "store" / "duplicate.bin"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"x")
    source_ref = await create_user_file_v0(
        user_id=user["id"],
        real_path=source,
        content_hash="pack_duplicate_source",
        display_name="duplicate.bin",
        size_bytes=1,
    )
    source_json = json.dumps([source_ref["id"]], separators=(",", ":"))
    await create_pending_pack_with_reservation(
        user_id=user["id"],
        source_user_file_ids_json=source_json,
        source_size_bytes=1,
        reserved_bytes=100,
        output_name="duplicate",
        delete_source=False,
        disk_available_bytes=1000,
    )

    with pytest.raises(PackAdmissionError, match="duplicate"):
        await create_pending_pack_with_reservation(
            user_id=user["id"],
            source_user_file_ids_json=f" {source_json} ",
            source_size_bytes=1,
            reserved_bytes=100,
            output_name="duplicate",
            delete_source=False,
            disk_available_bytes=1000,
        )

    async with transaction() as conn:
        usage = (
            await conn.execute(
                select(user_storage_usage.c.reserved_bytes).where(
                    user_storage_usage.c.user_id == user["id"]
                )
            )
        ).scalar_one()
        tasks = (
            await conn.execute(
                select(pack_tasks.c.id).where(pack_tasks.c.user_id == user["id"])
            )
        ).all()
    assert usage == 100
    assert len(tasks) == 1


@pytest.mark.asyncio
async def test_pack_disk_rejection_rolls_back_reservation(temp_db: str) -> None:
    user = await create_user_v0(username="pack_disk_rollback", quota_bytes=1000)
    source = Path(settings.download_dir) / "store" / "disk.bin"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"x")
    source_ref = await create_user_file_v0(
        user_id=user["id"],
        real_path=source,
        content_hash="pack_disk_source",
        display_name="disk.bin",
        size_bytes=1,
    )
    with pytest.raises(PackAdmissionError, match="disk"):
        await create_pending_pack_with_reservation(
            user_id=user["id"],
            source_user_file_ids_json=json.dumps([source_ref["id"]]),
            source_size_bytes=1,
            reserved_bytes=100,
            output_name="disk",
            delete_source=False,
            disk_available_bytes=99,
        )
    async with transaction() as conn:
        reserved = (
            await conn.execute(
                select(user_storage_usage.c.reserved_bytes).where(
                    user_storage_usage.c.user_id == user["id"]
                )
            )
        ).scalar_one()
    assert reserved == 0


@pytest.mark.asyncio
async def test_pack_admission_scans_sources_off_event_loop(
    temp_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await create_user_v0(username="pack_scan_thread", quota_bytes=100_000)
    source = Path(settings.download_dir) / "store" / "scan-source.bin"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"source")
    source_ref = await create_user_file_v0(
        user_id=user["id"], real_path=source,
        content_hash="pack_scan_thread_source",
        display_name=source.name, size_bytes=6,
    )
    caller_thread = threading.get_ident()
    scan_threads: list[int] = []
    real_scan = PackTaskManager._build_archive_items

    def record_scan(
        sources: list[Path],
        source_names: list[str] | None,
        cancel_event: threading.Event | None = None,
    ) -> list[pack_service._ArchiveItem]:
        scan_threads.append(threading.get_ident())
        return real_scan(sources, source_names, cancel_event)

    submit = AsyncMock(return_value=True)
    monkeypatch.setattr(PackTaskManager, "_build_archive_items", record_scan)
    monkeypatch.setattr(PackTaskManager, "submit", submit)

    task = await pack_service.create_pack_task_from_user_files(
        user_id=user["id"], quota_bytes=1,
        file_ids=[source_ref["id"]], output_name="scan", delete_source=False,
    )

    assert task["status"] == "pending"
    assert scan_threads and scan_threads[0] != caller_thread
    submit.assert_awaited_once_with(task["id"])


@pytest.mark.asyncio
async def test_cancelled_pack_admission_waits_for_scan_thread(
    temp_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await create_user_v0(username="pack_scan_cancel", quota_bytes=100_000)
    source = Path(settings.download_dir) / "store" / "scan-cancel.bin"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"source")
    source_ref = await create_user_file_v0(
        user_id=user["id"], real_path=source, content_hash="scan_cancel_source",
        display_name=source.name, size_bytes=6,
    )
    entered = threading.Event()
    exited = threading.Event()

    def blocking_scan(
        _sources: list[Path],
        _names: list[str] | None,
        cancel_event: threading.Event | None = None,
    ) -> list[pack_service._ArchiveItem]:
        assert cancel_event is not None
        entered.set()
        cancel_event.wait(2)
        exited.set()
        raise InterruptedError("pack cancelled")

    monkeypatch.setattr(PackTaskManager, "_build_archive_items", blocking_scan)
    admission = asyncio.create_task(pack_service.create_pack_task_from_user_files(
        user_id=user["id"], quota_bytes=user["quota_bytes"],
        file_ids=[source_ref["id"]], output_name="cancel", delete_source=False,
    ))
    assert await asyncio.to_thread(entered.wait, 2)
    admission.cancel()
    with pytest.raises(asyncio.CancelledError):
        await admission
    assert exited.is_set()


@pytest.mark.asyncio
async def test_pack_ignores_stale_request_quota(
    temp_db: str,
) -> None:
    user = await create_user_v0(username="pack_stale_quota", quota_bytes=100)
    source = Path(settings.download_dir) / "store" / "stale.bin"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"x")
    source_ref = await create_user_file_v0(
        user_id=user["id"],
        real_path=source,
        content_hash="pack_stale_quota_source",
        display_name="stale.bin",
        size_bytes=1,
    )

    with pytest.raises(ForbiddenError, match="空间不足"):
        await pack_service.create_pack_task_from_user_files(
            user_id=user["id"],
            quota_bytes=10_000_000,
            file_ids=[source_ref["id"]],
            output_name="stale",
            delete_source=False,
        )

    async with transaction() as conn:
        reserved = (
            await conn.execute(
                select(user_storage_usage.c.reserved_bytes).where(
                    user_storage_usage.c.user_id == user["id"]
                )
            )
        ).scalar_one()
        tasks = (
            await conn.execute(
                select(pack_tasks.c.id).where(pack_tasks.c.user_id == user["id"])
            )
        ).all()
    assert reserved == 0
    assert tasks == []


def test_archive_scan_enforces_entry_and_path_metadata_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.write_bytes(b"1")
    second.write_bytes(b"2")
    monkeypatch.setattr(pack_service, "_MAX_ARCHIVE_ENTRIES", 1)
    with pytest.raises(pack_service.PackBoundaryError, match="条目过多"):
        PackTaskManager._build_archive_items(
            [first, second],
            ["first", "second"],
        )

    monkeypatch.setattr(pack_service, "_MAX_ARCHIVE_ENTRIES", 10)
    monkeypatch.setattr(pack_service, "_MAX_ARCHIVE_PATH_BYTES", 3)
    with pytest.raises(pack_service.PackBoundaryError, match="路径过长"):
        PackTaskManager._build_archive_items([first], ["long-name"])
    monkeypatch.setattr(pack_service, "_MAX_ARCHIVE_PATH_BYTES", 100)
    monkeypatch.setattr(pack_service, "_MAX_TOTAL_ARCHIVE_PATH_BYTES", 6)
    with pytest.raises(pack_service.PackBoundaryError, match="路径元数据过大"):
        PackTaskManager._build_archive_items(
            [first, second],
            ["first", "second"],
        )


@pytest.mark.parametrize("pack_format", ["zip", "tar.zst"])
def test_archive_writers_enforce_output_extent(
    tmp_path: Path,
    pack_format: str,
) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(os.urandom(4096))
    items = PackTaskManager._build_archive_items([source], [source.name])
    output = tmp_path / f"archive.{pack_format}"

    with pytest.raises(pack_service.PackBoundaryError, match="超过预留空间"):
        PackTaskManager._write_archive_sync(
            output,
            pack_format,
            5,
            items,
            pack_service._ProgressTracker(source.stat().st_size),
            threading.Event(),
            1,
            0,
        )
    assert output.stat().st_size <= 1


def test_bounded_sink_uses_file_extent_not_cumulative_writes(tmp_path: Path) -> None:
    path = tmp_path / "extent.partial"
    with pack_service._BoundedSink(
        path,
        max_bytes=4,
        min_free_bytes=0,
        cancel_event=threading.Event(),
    ) as sink:
        assert sink.write(b"1234") == 4
        sink.seek(1)
        assert sink.write(b"ab") == 2
        sink.seek(4)
        with pytest.raises(pack_service.PackBoundaryError, match="超过预留空间"):
            sink.write(b"x")
    assert path.stat().st_size == 4


def test_bounded_sink_rejects_disk_floor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    disk = type("DiskUsage", (), {"free": 0})()
    monkeypatch.setattr(pack_service.shutil, "disk_usage", lambda _path: disk)
    sink = pack_service._BoundedSink(
        tmp_path / "archive.partial",
        max_bytes=100,
        min_free_bytes=1,
        cancel_event=threading.Event(),
    )
    try:
        with pytest.raises(pack_service.PackBoundaryError, match="磁盘可用空间不足"):
            sink.write(b"x")
    finally:
        sink.close()


@pytest.mark.asyncio
async def test_cancelled_startup_recovery_stops_and_waits_for_thread(
    temp_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await create_user_v0(username="pack_startup_cancel")
    content = b"startup-cancel"
    content_hash = hashlib.sha256(content).hexdigest()
    task = await _insert_pack_task(
        user_id=user["id"], source_ids=[1], source_size_bytes=1,
        reserved_bytes=100, status="packing", output_name="startup-cancel",
    )
    filename = "startup-cancel.tar.zst"
    async with transaction() as conn:
        await conn.execute(pack_tasks.update().where(
            pack_tasks.c.id == task["id"]
        ).values(
            prepared_content_hash=content_hash,
            prepared_size_bytes=len(content), prepared_filename=filename,
        ))
    entered = threading.Event()
    exited = threading.Event()

    def blocking_measure(
        _task: dict[str, object], cancel_event: threading.Event
    ) -> int:
        entered.set()
        cancel_event.wait(2)
        exited.set()
        raise InterruptedError("startup cancelled")

    monkeypatch.setattr(
        PackTaskManager, "_measure_pack_materialized_bytes", blocking_measure
    )
    recovery = asyncio.create_task(PackTaskManager.recover_startup())
    assert await asyncio.to_thread(entered.wait, 2)
    recovery.cancel()
    with pytest.raises(asyncio.CancelledError):
        await recovery
    assert exited.is_set()


@pytest.mark.asyncio
async def test_copy_fallback_persists_peak_commitment_until_install_finishes(
    temp_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.repositories.downloads import get_active_physical_commitment_bytes
    from app.services.storage import get_downloading_dir

    user = await create_user_v0(username="pack_copy_commitment", quota_bytes=1000)
    content = b"copy-fallback-data"
    content_hash = hashlib.sha256(content).hexdigest()
    task = await _insert_pack_task(
        user_id=user["id"], source_ids=[1], source_size_bytes=1,
        reserved_bytes=100, status="packing", output_name="copy-commitment",
    )
    filename = "copy-commitment.tar.zst"
    prepared = get_downloading_dir() / f"pack_{task['id']}" / filename
    prepared.parent.mkdir(parents=True, exist_ok=True)
    prepared.write_bytes(content)
    async with transaction() as conn:
        await conn.execute(pack_tasks.update().where(
            pack_tasks.c.id == task["id"]
        ).values(
            prepared_content_hash=content_hash,
            prepared_size_bytes=len(content), prepared_filename=filename,
            materialized_bytes=len(content),
        ))
    monkeypatch.setattr(
        pack_service.os,
        "link",
        lambda *_args: (_ for _ in ()).throw(OSError(errno.EXDEV, "cross-device")),
    )
    monkeypatch.setattr(pack_service, "get_min_free_disk", lambda: 0)
    entered = threading.Event()
    release = threading.Event()
    real_copy = pack_service._durable_copy_file

    def blocking_copy(
        source: Path,
        target: Path,
        temporary: Path,
        cancel_event: threading.Event,
        max_bytes: int,
        min_free_bytes: int,
    ) -> None:
        entered.set()
        release.wait(2)
        real_copy(source, target, temporary, cancel_event, max_bytes, min_free_bytes)

    monkeypatch.setattr(pack_service, "_durable_copy_file", blocking_copy)
    install = asyncio.create_task(PackTaskManager._install_prepared_file(
        task["id"], content_hash=content_hash, size_bytes=len(content),
        filename=filename, cancel_event=threading.Event(), job=None,
    ))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        current = await pack_service.get_pack_task_row(task["id"])
        assert current is not None
        assert current["install_reserved_bytes"] == len(content)
        assert await get_active_physical_commitment_bytes() == 100
    finally:
        release.set()
    installed = await install
    current = await pack_service.get_pack_task_row(task["id"])
    assert installed.created_by_this_attempt
    assert installed.path.read_bytes() == content
    assert prepared.read_bytes() == content
    assert current is not None
    assert current["install_reserved_bytes"] == 0


@pytest.mark.asyncio
async def test_copy_fallback_rejects_near_disk_floor_without_second_copy(
    temp_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.storage import get_downloading_dir, get_store_path_for_hash

    user = await create_user_v0(username="pack_copy_floor", quota_bytes=1000)
    content = b"x" * 16
    content_hash = hashlib.sha256(content).hexdigest()
    task = await _insert_pack_task(
        user_id=user["id"], source_ids=[1], source_size_bytes=1,
        reserved_bytes=100, status="packing", output_name="copy-floor",
    )
    filename = "copy-floor.tar.zst"
    prepared = get_downloading_dir() / f"pack_{task['id']}" / filename
    prepared.parent.mkdir(parents=True, exist_ok=True)
    prepared.write_bytes(content)
    async with transaction() as conn:
        await conn.execute(pack_tasks.update().where(
            pack_tasks.c.id == task["id"]
        ).values(
            prepared_content_hash=content_hash,
            prepared_size_bytes=len(content),
            prepared_filename=filename,
            materialized_bytes=len(content),
        ))
    monkeypatch.setattr(
        pack_service.os,
        "link",
        lambda *_args: (_ for _ in ()).throw(OSError(errno.EXDEV, "cross-device")),
    )
    _setup_disk_space(monkeypatch, free_bytes=199, min_free=100)

    with pytest.raises(pack_service.PackBoundaryError, match="磁盘可用空间不足"):
        await PackTaskManager._install_prepared_file(
            task["id"], content_hash=content_hash,
            size_bytes=len(content), filename=filename,
            cancel_event=threading.Event(), job=None,
        )

    current = await pack_service.get_pack_task_row(task["id"])
    assert current is not None
    assert current["install_reserved_bytes"] == 0
    assert prepared.read_bytes() == content
    assert not get_store_path_for_hash(content_hash).exists()


@pytest.mark.asyncio
async def test_corrupt_prepared_task_does_not_block_other_startup_recovery(
    temp_db: str,
) -> None:
    user = await create_user_v0(username="pack_corrupt_isolation", quota_bytes=1000)
    bad = await _insert_pack_task(
        user_id=user["id"], source_ids=[1], source_size_bytes=1,
        reserved_bytes=100, status="packing", output_name="bad",
    )
    good = await _insert_pack_task(
        user_id=user["id"], source_ids=[2], source_size_bytes=1,
        reserved_bytes=100, status="packing", output_name="good",
    )
    bad_content, good_content = b"expected", b"good"
    bad_hash = hashlib.sha256(bad_content).hexdigest()
    good_hash = hashlib.sha256(good_content).hexdigest()
    bad_name, good_name = "bad.tar.zst", "good.tar.zst"
    bad_path = Path(settings.download_dir) / "downloading" / f"pack_{bad['id']}" / bad_name
    good_path = Path(settings.download_dir) / "downloading" / f"pack_{good['id']}" / good_name
    bad_path.parent.mkdir(parents=True, exist_ok=True)
    good_path.parent.mkdir(parents=True, exist_ok=True)
    bad_path.write_bytes(b"corrupt!")
    good_path.write_bytes(good_content)
    async with transaction() as conn:
        await conn.execute(user_storage_usage.update().where(
            user_storage_usage.c.user_id == user["id"]
        ).values(reserved_bytes=200, updated_at_ms=now_ms()))
        await conn.execute(pack_tasks.update().where(
            pack_tasks.c.id == bad["id"]
        ).values(
            prepared_content_hash=bad_hash, prepared_size_bytes=len(bad_content),
            prepared_filename=bad_name,
        ))
        await conn.execute(pack_tasks.update().where(
            pack_tasks.c.id == good["id"]
        ).values(
            prepared_content_hash=good_hash, prepared_size_bytes=len(good_content),
            prepared_filename=good_name,
        ))

    await PackTaskManager.recover_startup()

    async with transaction() as conn:
        rows = (
            await conn.execute(select(
                pack_tasks.c.id, pack_tasks.c.status, pack_tasks.c.error_message
            ).where(pack_tasks.c.id.in_((bad["id"], good["id"]))).order_by(
                pack_tasks.c.id
            ))
        ).all()
    assert rows[0][1] == "failed"
    assert "校验失败" in rows[0][2]
    assert rows[1][1] == "completed"


@pytest.mark.asyncio
async def test_startup_rebuilds_materialized_bytes_before_transient_retry(
    temp_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await create_user_v0(username="pack_materialized_rebuild", quota_bytes=1000)
    task = await _insert_pack_task(
        user_id=user["id"], source_ids=[1], source_size_bytes=1,
        reserved_bytes=100, status="packing", output_name="materialized",
    )
    content = b"m" * 40
    content_hash = hashlib.sha256(content).hexdigest()
    filename = "materialized.tar.zst"
    prepared = Path(settings.download_dir) / "downloading" / f"pack_{task['id']}" / filename
    prepared.parent.mkdir(parents=True, exist_ok=True)
    prepared.write_bytes(content)
    async with transaction() as conn:
        await conn.execute(pack_tasks.update().where(
            pack_tasks.c.id == task["id"]
        ).values(
            prepared_content_hash=content_hash,
            prepared_size_bytes=len(content), prepared_filename=filename,
            materialized_bytes=0,
        ))
    monkeypatch.setattr(
        PackTaskManager, "_finalize_prepared",
        AsyncMock(side_effect=OSError("transient finalize failure")),
    )

    await PackTaskManager.recover_startup()

    current = await pack_service.get_pack_task_row(task["id"])
    assert current is not None
    assert current["status"] == "packing"
    assert current["prepared_content_hash"] == content_hash
    assert current["materialized_bytes"] == len(content)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "location",
    ["staging", "canonical", "corrupt-canonical", "stale-record"],
)
async def test_startup_recovers_prepared_pack(
    temp_db: str,
    location: str,
) -> None:
    from app.services.storage import get_downloading_dir, get_store_path_for_hash

    user = await create_user_v0(username=f"pack_recover_{location}")
    source = Path(settings.download_dir) / "store" / "recover-source.bin"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"s")
    source_ref = await create_user_file_v0(
        user_id=user["id"],
        real_path=source,
        content_hash=f"pack_recover_source_{location}",
        display_name="recover-source.bin",
        size_bytes=1,
    )
    async with transaction() as conn:
        await conn.execute(
            user_storage_usage.update()
            .where(user_storage_usage.c.user_id == user["id"])
            .values(used_bytes=1, reserved_bytes=100, updated_at_ms=now_ms())
        )
    task = await _insert_pack_task(
        user_id=user["id"], source_ids=[source_ref["id"]],
        source_size_bytes=1, reserved_bytes=100,
        status="packing", output_name="recover",
    )
    content = b"prepared-output"
    content_hash = hashlib.sha256(content).hexdigest()
    pack_dir = get_downloading_dir() / f"pack_{task['id']}"
    pack_dir.mkdir(parents=True)
    prepared = pack_dir / "recover.zip"
    prepared.write_bytes(content)
    assert await persist_pack_prepared(
        task["id"], content_hash=content_hash,
        size_bytes=len(content), filename=prepared.name,
    )
    target = get_store_path_for_hash(content_hash)
    target.parent.mkdir(parents=True, exist_ok=True)
    if location == "canonical":
        prepared.replace(target)
    elif location == "corrupt-canonical":
        target.write_bytes(b"x" * len(content))
    elif location == "stale-record":
        async with transaction() as conn:
            await conn.execute(
                insert(stored_files).values(
                    content_hash=content_hash,
                    real_path=str(target.with_name("missing")),
                    size_bytes=len(content), is_directory=0,
                    original_name=prepared.name, created_at_ms=now_ms(),
                )
            )

    await PackTaskManager.recover_startup()

    async with transaction() as conn:
        stored_task = (
            await conn.execute(select(pack_tasks).where(pack_tasks.c.id == task["id"]))
        ).mappings().one()
        usage = (
            await conn.execute(select(user_storage_usage).where(
                user_storage_usage.c.user_id == user["id"]
            ))
        ).mappings().one()
        stored_path = (
            await conn.execute(select(stored_files.c.real_path).where(
                stored_files.c.id == stored_task["output_stored_file_id"]
            ))
        ).scalar_one()
    assert stored_task["status"] == "completed"
    assert target.read_bytes() == content
    assert Path(stored_path) == target
    assert usage["reserved_bytes"] == 0
    assert usage["used_bytes"] == 1 + len(content)


@pytest.mark.asyncio
async def test_unclaimed_packing_task_is_not_run_by_second_manager(
    temp_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await create_user_v0(username="pack_second_manager")
    task = await _insert_pack_task(
        user_id=user["id"], source_ids=[1], source_size_bytes=1,
        reserved_bytes=100, status="packing",
    )
    resolve = AsyncMock()
    monkeypatch.setattr(PackTaskManager, "_resolve_task_sources", resolve)
    current = asyncio.current_task()
    assert current is not None
    job = pack_service._RunningPackJob(current, threading.Event())

    await PackTaskManager._run_persistent_pack(task["id"], job, None)

    resolve.assert_not_awaited()


@pytest.mark.asyncio
async def test_startup_requeues_interrupted_pack_and_resubmits(
    temp_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await create_user_v0(username="pack_interrupted", quota_bytes=1000)
    task = await _insert_pack_task(
        user_id=user["id"], source_ids=[1], source_size_bytes=1,
        reserved_bytes=100, status="packing", progress=80,
    )
    from app.services.storage import get_downloading_dir

    pack_dir = get_downloading_dir() / f"pack_{task['id']}"
    pack_dir.mkdir(parents=True)
    (pack_dir / "stale.partial").write_bytes(b"partial")
    submit = AsyncMock(return_value=True)
    monkeypatch.setattr(PackTaskManager, "submit", submit)

    await PackTaskManager.recover_startup()
    await PackTaskManager.submit_pending()

    async with transaction() as conn:
        recovered = (
            await conn.execute(select(pack_tasks).where(pack_tasks.c.id == task["id"]))
        ).mappings().one()
        usage = (
            await conn.execute(select(user_storage_usage).where(
                user_storage_usage.c.user_id == user["id"]
            ))
        ).mappings().one()
    assert recovered["status"] == "pending"
    assert recovered["progress"] == 0
    assert usage["reserved_bytes"] == 100
    assert not pack_dir.exists()
    submit.assert_awaited_once_with(task["id"])


@pytest.mark.asyncio
async def test_startup_cleans_numeric_and_malformed_stale_pack_dirs(
    temp_db: str,
) -> None:
    from app.services.storage import get_downloading_dir

    downloading = get_downloading_dir()
    stale_dirs = [downloading / "pack_999", downloading / "pack_invalid"]
    for path in stale_dirs:
        path.mkdir(parents=True)
        (path / "partial").write_bytes(b"stale")

    await PackTaskManager.recover_startup()

    assert all(not path.exists() for path in stale_dirs)


@pytest.mark.asyncio
async def test_partial_source_cleanup_replays_only_unfinished_source(
    temp_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await create_user_v0(username="pack_partial_cleanup")
    refs = []
    for index in range(2):
        path = Path(settings.download_dir) / "store" / f"partial-{index}.bin"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(bytes([index + 1]))
        refs.append(await create_user_file_v0(
            user_id=user["id"], real_path=path,
            content_hash=f"partial_cleanup_{index}", display_name=path.name,
            size_bytes=1,
        ))
    task = await _insert_pack_task(
        user_id=user["id"], source_ids=[ref["id"] for ref in refs],
        source_size_bytes=2, reserved_bytes=0, status="completed",
        delete_source=True,
    )
    async with transaction() as conn:
        await conn.execute(pack_tasks.update().where(
            pack_tasks.c.id == task["id"]
        ).values(source_cleanup_pending=1))
    real_cleanup = pack_service.cleanup_pack_source_reference

    async def fail_second(task_id: int, ordinal: int):
        if ordinal == 1:
            raise OSError("injected second source failure")
        return await real_cleanup(task_id, ordinal)

    monkeypatch.setattr(pack_service, "cleanup_pack_source_reference", fail_second)
    current = await pack_service.get_pack_task_row(task["id"])
    assert current is not None
    assert not await PackTaskManager._replay_source_cleanup(current)
    async with transaction() as conn:
        states = (
            await conn.execute(select(
                pack_task_sources.c.ordinal, pack_task_sources.c.cleanup_state
            ).where(pack_task_sources.c.task_id == task["id"]).order_by(
                pack_task_sources.c.ordinal
            ))
        ).all()
    assert states == [(0, "cleaned"), (1, "pending")]

    monkeypatch.setattr(pack_service, "cleanup_pack_source_reference", real_cleanup)
    current = await pack_service.get_pack_task_row(task["id"])
    assert current is not None
    assert await PackTaskManager._replay_source_cleanup(current)
    async with transaction() as conn:
        pending = (
            await conn.execute(select(pack_tasks.c.source_cleanup_pending).where(
                pack_tasks.c.id == task["id"]
            ))
        ).scalar_one()
        states = (
            await conn.execute(select(pack_task_sources.c.cleanup_state).where(
                pack_task_sources.c.task_id == task["id"]
            ).order_by(pack_task_sources.c.ordinal))
        ).scalars().all()
    assert pending == 0
    assert states == ["cleaned", "cleaned"]


@pytest.mark.asyncio
@pytest.mark.parametrize("is_directory", [False, True])
async def test_source_cleanup_waits_for_active_content_read_lease(
    temp_db: str,
    monkeypatch: pytest.MonkeyPatch,
    is_directory: bool,
) -> None:
    from app.services.storage_locks import (
        acquire_content_read_lease_locked,
        get_content_hash_lock,
    )

    user = await create_user_v0(username=f"pack_cleanup_lease_{is_directory}")
    source = Path(settings.download_dir) / "store" / f"lease-source-{is_directory}"
    source.parent.mkdir(parents=True, exist_ok=True)
    if is_directory:
        source.mkdir()
        (source / "nested.bin").write_bytes(b"source")
    else:
        source.write_bytes(b"source")
    content_hash = f"pack_cleanup_read_lease_{is_directory}"
    source_ref = await create_user_file_v0(
        user_id=user["id"], real_path=source, content_hash=content_hash,
        display_name=source.name, size_bytes=6,
    )
    task = await _insert_pack_task(
        user_id=user["id"], source_ids=[source_ref["id"]], source_size_bytes=6,
        reserved_bytes=0, status="completed", delete_source=True,
    )
    async with transaction() as conn:
        await conn.execute(pack_tasks.update().where(
            pack_tasks.c.id == task["id"]
        ).values(source_cleanup_pending=1))

    content_lock = await get_content_hash_lock(content_hash)
    async with content_lock:
        active_lease = acquire_content_read_lease_locked(content_hash)
    wait_started = asyncio.Event()
    real_wait = pack_service.wait_for_content_readers_locked

    async def record_wait(waited_hash: str) -> None:
        wait_started.set()
        await real_wait(waited_hash)

    monkeypatch.setattr(pack_service, "wait_for_content_readers_locked", record_wait)
    current = await pack_service.get_pack_task_row(task["id"])
    assert current is not None
    cleanup = asyncio.create_task(PackTaskManager._replay_source_cleanup(current))
    await asyncio.wait_for(wait_started.wait(), timeout=1)
    assert source.exists()

    reader_acquired = asyncio.Event()

    async def acquire_new_read_lease():
        async with content_lock:
            reader_acquired.set()
            return acquire_content_read_lease_locked(content_hash)

    next_reader = asyncio.create_task(acquire_new_read_lease())
    await asyncio.sleep(0)
    assert not reader_acquired.is_set()
    await active_lease.release()
    assert await asyncio.wait_for(cleanup, timeout=1)
    assert not source.exists()
    next_lease = await asyncio.wait_for(next_reader, timeout=1)
    await next_lease.release()

    async with transaction() as conn:
        cleanup_real_path, pending = (await conn.execute(
            select(
                pack_task_sources.c.cleanup_real_path,
                pack_tasks.c.source_cleanup_pending,
            ).select_from(pack_task_sources.join(pack_tasks)).where(
                pack_task_sources.c.task_id == task["id"]
            )
        )).one()
    assert cleanup_real_path is None
    assert pending == 0


@pytest.mark.asyncio
async def test_cancelled_source_cleanup_wait_keeps_replayable_source(
    temp_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.storage_locks import (
        acquire_content_read_lease_locked,
        get_content_hash_lock,
    )

    user = await create_user_v0(username="pack_cleanup_wait_cancel")
    source = Path(settings.download_dir) / "store" / "wait-cancel.bin"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"source")
    content_hash = "pack_cleanup_wait_cancel"
    source_ref = await create_user_file_v0(
        user_id=user["id"], real_path=source, content_hash=content_hash,
        display_name=source.name, size_bytes=6,
    )
    task = await _insert_pack_task(
        user_id=user["id"], source_ids=[source_ref["id"]], source_size_bytes=6,
        reserved_bytes=0, status="completed", delete_source=True,
    )
    async with transaction() as conn:
        await conn.execute(pack_tasks.update().where(
            pack_tasks.c.id == task["id"]
        ).values(source_cleanup_pending=1))

    content_lock = await get_content_hash_lock(content_hash)
    async with content_lock:
        active_lease = acquire_content_read_lease_locked(content_hash)
    wait_started = asyncio.Event()
    real_wait = pack_service.wait_for_content_readers_locked

    async def record_wait(waited_hash: str) -> None:
        wait_started.set()
        await real_wait(waited_hash)

    monkeypatch.setattr(pack_service, "wait_for_content_readers_locked", record_wait)
    current = await pack_service.get_pack_task_row(task["id"])
    assert current is not None
    cleanup = asyncio.create_task(PackTaskManager._replay_source_cleanup(current))
    await asyncio.wait_for(wait_started.wait(), timeout=1)
    cleanup.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cleanup
    assert source.exists()

    await active_lease.release()
    current = await pack_service.get_pack_task_row(task["id"])
    assert current is not None
    assert await PackTaskManager._replay_source_cleanup(current)
    assert not source.exists()


@pytest.mark.asyncio
async def test_directory_source_cleanup_replays_durable_tombstone_after_cancel(
    temp_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await create_user_v0(username="pack_directory_tombstone")
    source = Path(settings.download_dir) / "store" / "directory-source"
    source.mkdir(parents=True)
    (source / "nested.bin").write_bytes(b"nested")
    source_ref = await create_user_file_v0(
        user_id=user["id"], real_path=source,
        content_hash="directory_tombstone_hash",
        display_name=source.name, size_bytes=6,
    )
    task = await _insert_pack_task(
        user_id=user["id"], source_ids=[source_ref["id"]],
        source_size_bytes=6, reserved_bytes=0, status="completed",
        delete_source=True,
    )
    async with transaction() as conn:
        await conn.execute(pack_tasks.update().where(
            pack_tasks.c.id == task["id"]
        ).values(source_cleanup_pending=1))
    real_remove = pack_service._remove_tree_cancellable
    cancel_event = threading.Event()

    def interrupt_remove(path: Path, event: threading.Event) -> None:
        assert path.name.startswith(".aria2deck-pack-delete-")
        event.set()
        raise InterruptedError("cleanup cancelled")

    monkeypatch.setattr(pack_service, "_remove_tree_cancellable", interrupt_remove)
    current = await pack_service.get_pack_task_row(task["id"])
    assert current is not None
    with pytest.raises(InterruptedError, match="cleanup cancelled"):
        await PackTaskManager._replay_source_cleanup(
            current, cancel_event=cancel_event
        )

    async with transaction() as conn:
        marker = (
            await conn.execute(select(
                pack_task_sources.c.cleanup_real_path
            ).where(pack_task_sources.c.task_id == task["id"]))
        ).scalar_one()
    tombstone = Path(marker)
    assert not source.exists()
    assert tombstone.exists()

    monkeypatch.setattr(pack_service, "_remove_tree_cancellable", real_remove)
    current = await pack_service.get_pack_task_row(task["id"])
    assert current is not None
    assert await PackTaskManager._replay_source_cleanup(
        current, cancel_event=threading.Event()
    )
    assert not tombstone.exists()
    async with transaction() as conn:
        marker, pending = (
            await conn.execute(select(
                pack_task_sources.c.cleanup_real_path,
                pack_tasks.c.source_cleanup_pending,
            ).select_from(pack_task_sources.join(pack_tasks)).where(
                pack_task_sources.c.task_id == task["id"]
            ))
        ).one()
    assert marker is None
    assert pending == 0


@pytest.mark.asyncio
async def test_startup_replays_source_cleanup(
    temp_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await create_user_v0(username="pack_cleanup_replay")
    source = Path(settings.download_dir) / "store" / "cleanup-source.bin"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"source")
    source_ref = await create_user_file_v0(
        user_id=user["id"],
        real_path=source,
        content_hash="pack_cleanup_replay_source",
        display_name="cleanup-source.bin",
        size_bytes=6,
    )
    task = await _insert_pack_task(
        user_id=user["id"],
        source_ids=[source_ref["id"]],
        source_size_bytes=6,
        reserved_bytes=0,
        status="completed",
        output_name="cleanup",
        delete_source=True,
    )
    async with transaction() as conn:
        await conn.execute(
            pack_tasks.update()
            .where(pack_tasks.c.id == task["id"])
            .values(source_cleanup_pending=1)
        )

    real_cleanup = pack_service.cleanup_pack_source_reference
    monkeypatch.setattr(
        pack_service,
        "cleanup_pack_source_reference",
        AsyncMock(side_effect=OSError("injected cleanup failure")),
    )
    await PackTaskManager.recover_startup()
    async with transaction() as conn:
        pending_after_failure = (
            await conn.execute(
                select(pack_tasks.c.source_cleanup_pending).where(
                    pack_tasks.c.id == task["id"]
                )
            )
        ).scalar_one()
    assert pending_after_failure == 1
    assert source.exists()

    fsync_calls: list[int] = []
    monkeypatch.setattr(pack_service.os, "fsync", fsync_calls.append)
    monkeypatch.setattr(
        pack_service,
        "cleanup_pack_source_reference",
        real_cleanup,
    )
    await PackTaskManager.recover_startup()
    async with transaction() as conn:
        source_row = (
            await conn.execute(
                select(user_files.c.id).where(user_files.c.id == source_ref["id"])
            )
        ).first()
        cleanup_pending = (
            await conn.execute(
                select(pack_tasks.c.source_cleanup_pending).where(
                    pack_tasks.c.id == task["id"]
                )
            )
        ).scalar_one()
    assert source_row is None
    assert cleanup_pending == 0
    assert fsync_calls


@pytest.mark.asyncio
async def test_finalize_rollback_keeps_prepared_output_recoverable(
    temp_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.storage import get_store_path_for_hash

    user = await create_user_v0(username="pack_finalize_rollback", quota_bytes=1000)
    source = Path(settings.download_dir) / "store" / "finalize-source.bin"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"source")
    source_ref = await create_user_file_v0(
        user_id=user["id"], real_path=source,
        content_hash="pack_finalize_rollback_source",
        display_name="finalize-source.bin", size_bytes=6,
    )
    async with transaction() as conn:
        await conn.execute(
            user_storage_usage.update()
            .where(user_storage_usage.c.user_id == user["id"])
            .values(used_bytes=6, reserved_bytes=100, updated_at_ms=now_ms())
        )
    task = await _insert_pack_task(
        user_id=user["id"], source_ids=[source_ref["id"]],
        source_size_bytes=6, reserved_bytes=100,
        status="pending", output_name="rollback",
    )
    content = b"archive"
    content_hash = _v2_file_key(content)
    _mock_write_archive(monkeypatch, content)
    async with transaction() as conn:
        await conn.execute(text(
            "CREATE TRIGGER reject_pack_finalize BEFORE UPDATE OF status ON pack_tasks "
            "WHEN NEW.status = 'completed' BEGIN "
            "SELECT RAISE(ABORT, 'injected finalize failure'); END"
        ))

    await PackTaskManager.start_dispatcher()
    assert await PackTaskManager.submit(task["id"])
    canonical = get_store_path_for_hash(content_hash)
    interrupted = None
    output_row = None
    usage = None
    for _ in range(200):
        async with transaction() as conn:
            interrupted = (
                await conn.execute(select(pack_tasks).where(
                    pack_tasks.c.id == task["id"]
                ))
            ).mappings().one()
            output_row = (
                await conn.execute(select(stored_files.c.id).where(
                    stored_files.c.content_hash == content_hash
                ))
            ).first()
            usage = (
                await conn.execute(select(user_storage_usage).where(
                    user_storage_usage.c.user_id == user["id"]
                ))
            ).mappings().one()
        if (
            interrupted["prepared_content_hash"] == content_hash
            and canonical.exists()
        ):
            break
        await asyncio.sleep(0.01)
    assert interrupted is not None
    assert usage is not None
    assert interrupted["status"] == "packing"
    assert output_row is None
    assert usage["reserved_bytes"] == 100
    prepared = Path(settings.download_dir) / "downloading" / f"pack_{task['id']}" / "rollback.tar.zst"
    assert canonical.read_bytes() == content
    assert prepared.read_bytes() == content
    assert canonical.stat().st_ino == prepared.stat().st_ino

    async with transaction() as conn:
        await conn.execute(text("DROP TRIGGER reject_pack_finalize"))
    completed = None
    for _ in range(300):
        async with transaction() as conn:
            completed = (
                await conn.execute(select(pack_tasks).where(
                    pack_tasks.c.id == task["id"]
                ))
            ).mappings().one()
            usage = (
                await conn.execute(select(user_storage_usage).where(
                    user_storage_usage.c.user_id == user["id"]
                ))
            ).mappings().one()
        if completed["status"] == "completed":
            break
        await asyncio.sleep(0.01)
    assert completed is not None
    assert completed["status"] == "completed"
    assert usage["used_bytes"] == 6 + len(content)
    assert usage["reserved_bytes"] == 0
    assert not prepared.exists()
    await PackTaskManager.shutdown()


@pytest.mark.asyncio
async def test_shutdown_waits_for_pack_writer_thread(
    temp_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await create_user_v0(username="pack_shutdown", quota_bytes=10_000)
    source = Path(settings.download_dir) / "store" / "shutdown-source.bin"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"source")
    source_ref = await create_user_file_v0(
        user_id=user["id"],
        real_path=source,
        content_hash="pack_shutdown_source",
        display_name="shutdown-source.bin",
        size_bytes=6,
    )
    async with transaction() as conn:
        await conn.execute(
            user_storage_usage.update()
            .where(user_storage_usage.c.user_id == user["id"])
            .values(reserved_bytes=1000, updated_at_ms=now_ms())
        )
    task = await _insert_pack_task(
        user_id=user["id"],
        source_ids=[source_ref["id"]],
        source_size_bytes=6,
        reserved_bytes=1000,
        status="pending",
        output_name="shutdown",
    )
    entered = threading.Event()
    exited = threading.Event()

    def blocking_writer(
        _path: Path,
        _format: str,
        _level: int,
        _items: object,
        _tracker: object,
        cancel_event: threading.Event,
        _max_bytes: int,
        _min_free: int,
    ) -> None:
        entered.set()
        while not cancel_event.wait(0.01):
            pass
        exited.set()
        raise InterruptedError("pack cancelled")

    monkeypatch.setattr(PackTaskManager, "_write_archive_sync", blocking_writer)
    assert await PackTaskManager.submit(task["id"])
    assert await asyncio.to_thread(entered.wait, 2)
    await PackTaskManager.shutdown()
    assert exited.is_set()
    assert PackTaskManager._running_tasks == {}


@pytest.mark.asyncio
async def test_shutdown_waits_for_source_scan_thread(
    temp_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await create_user_v0(username="pack_shutdown_scan", quota_bytes=10_000)
    source = Path(settings.download_dir) / "store" / "shutdown-scan.bin"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"source")
    source_ref = await create_user_file_v0(
        user_id=user["id"], real_path=source,
        content_hash="pack_shutdown_scan_source",
        display_name=source.name, size_bytes=6,
    )
    async with transaction() as conn:
        await conn.execute(
            user_storage_usage.update()
            .where(user_storage_usage.c.user_id == user["id"])
            .values(reserved_bytes=1000, updated_at_ms=now_ms())
        )
    task = await _insert_pack_task(
        user_id=user["id"], source_ids=[source_ref["id"]],
        source_size_bytes=6, reserved_bytes=1000,
        status="pending", output_name="shutdown-scan",
    )
    entered = threading.Event()
    release = threading.Event()
    exited = threading.Event()
    real_scan = PackTaskManager._build_archive_items

    def blocking_scan(
        sources: list[Path],
        source_names: list[str] | None,
        cancel_event: threading.Event | None = None,
    ) -> list[pack_service._ArchiveItem]:
        entered.set()
        release.wait(2)
        try:
            return real_scan(sources, source_names, cancel_event)
        finally:
            exited.set()

    monkeypatch.setattr(PackTaskManager, "_build_archive_items", blocking_scan)
    assert await PackTaskManager.submit(task["id"])
    assert await asyncio.to_thread(entered.wait, 2)
    shutdown = asyncio.create_task(PackTaskManager.shutdown())
    await asyncio.sleep(0.05)
    assert not shutdown.done()
    release.set()
    await asyncio.wait_for(shutdown, 2)
    assert exited.is_set()
    assert PackTaskManager._running_tasks == {}


@pytest.mark.asyncio
async def test_done_callback_consumes_failure_and_releases_manager_slot(
    temp_db: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def fail_start(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("injected background failure")

    monkeypatch.setattr(PackTaskManager, "start_pack", fail_start)
    assert await PackTaskManager.submit(987654)
    for _ in range(3):
        await asyncio.sleep(0)

    assert 987654 not in PackTaskManager._running_tasks
    assert "打包后台任务异常退出" in caplog.text
