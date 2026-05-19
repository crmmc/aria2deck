from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import insert, select

from app.core.config import settings
from app.core.state import AppState
from app.db.engine import transaction
from app.db.schema import (
    global_downloads,
    pack_tasks,
    user_files,
    user_storage_usage,
    user_tasks,
)
from app.services.pack import PackTaskManager, calculate_folder_size, get_reserved_space
import app.services.pack as pack_service
from tests.helpers_v0 import create_user_file_v0, create_user_v0, now_ms


async def _insert_pack_task(
    *,
    user_id: int,
    source_ids: list[int],
    source_size_bytes: int,
    reserved_bytes: int,
    status: str = "pending",
    progress: int = 0,
    output_name: str | None = None,
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
                        delete_source=0,
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
    return dict(row)


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


def test_calculate_folder_size_recurses_files(tmp_path: Path) -> None:
    (tmp_path / "nested").mkdir()
    (tmp_path / "a.txt").write_bytes(b"abc")
    (tmp_path / "nested" / "b.txt").write_bytes(b"de")

    assert calculate_folder_size(tmp_path) == 5


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
            .values(used_bytes=5, reserved_bytes=5, updated_at_ms=now_ms())
        )
    task = await _insert_pack_task(
        user_id=user["id"],
        source_ids=[user_file["id"]],
        source_size_bytes=5,
        reserved_bytes=5,
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
            .values(used_bytes=5, reserved_bytes=5, updated_at_ms=timestamp)
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
        reserved_bytes=5,
        status="pending",
        output_name="packed-broadcast",
    )
    state = AppState()
    ws = AsyncMock()
    state.ws_connections[user["id"]] = {ws}

    await PackTaskManager.start_pack(
        task_id=pack_task["id"],
        user_id=user["id"],
        abs_paths=[str(source)],
        file_ids=[source_ref["id"]],
        output_name="packed-broadcast",
        delete_source=True,
        source_names=["broadcast-source.txt"],
        app_state=state,
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
    output_ref = await create_user_file_v0(
        user_id=user["id"],
        real_path=output_file,
        content_hash="pack_existing_output_hash",
        display_name="existing-output.zip",
        size_bytes=8,
    )
    async with transaction() as conn:
        await conn.execute(
            user_storage_usage.update()
            .where(user_storage_usage.c.user_id == user["id"])
            .values(used_bytes=13, reserved_bytes=5, updated_at_ms=now_ms())
        )
    task = await _insert_pack_task(
        user_id=user["id"],
        source_ids=[source_ref["id"]],
        source_size_bytes=5,
        reserved_bytes=5,
        status="pending",
        output_name="packed",
    )

    async def existing_register(**kwargs: object) -> tuple[int, int | None]:
        return int(output_ref["stored_file_id"]), None

    monkeypatch.setattr(pack_service, "_register_pack_output_v0", existing_register)

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
    assert usage["used_bytes"] == 13


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
            .values(used_bytes=5, reserved_bytes=5, updated_at_ms=now_ms())
        )
    task = await _insert_pack_task(
        user_id=user["id"],
        source_ids=[source_ref["id"]],
        source_size_bytes=5,
        reserved_bytes=5,
        status="pending",
        output_name="cancel-race-packed",
    )

    real_is_task_status = PackTaskManager._is_task_status
    status_checks = 0

    async def racing_is_task_status(task_id: int, expected_status: str) -> bool:
        nonlocal status_checks
        status_checks += 1
        if status_checks == 2:
            async with transaction() as conn:
                await conn.execute(
                    pack_tasks.update()
                    .where(pack_tasks.c.id == task["id"])
                    .values(
                        status="cancelled",
                        reserved_bytes=0,
                        updated_at_ms=now_ms(),
                        finished_at_ms=now_ms(),
                    )
                )
                await conn.execute(
                    user_storage_usage.update()
                    .where(user_storage_usage.c.user_id == user["id"])
                    .values(reserved_bytes=0, updated_at_ms=now_ms())
                )
            return True
        return await real_is_task_status(task_id, expected_status)

    monkeypatch.setattr(PackTaskManager, "_is_task_status", racing_is_task_status)

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
    assert output_refs == []
    assert source_after_race is not None
    assert usage["reserved_bytes"] == 0
    assert usage["used_bytes"] == 5


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

    def write_fixed_archive(
        output_path: Path, *_args: object, **_kwargs: object
    ) -> None:
        output_path.write_bytes(b"12345")

    original_release_reserved = pack_service.release_reserved

    async def racing_release_reserved(
        user_id: int, amount: int, **kwargs: object
    ) -> dict[str, int]:
        result = await original_release_reserved(user_id, amount, **kwargs)
        await pack_service.reserve_bytes(user_id, 10, quota_bytes=quota_bytes)
        return result

    monkeypatch.setattr(PackTaskManager, "_write_archive_sync", write_fixed_archive)
    monkeypatch.setattr(pack_service, "release_reserved", racing_release_reserved)

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
