from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

import app.services.download_service as download_service
from app.core.config import settings
from app.db.engine import transaction
from app.db.schema import stored_file_entries, stored_files, user_files
from app.repositories.downloads import get_global_by_resource_key, get_user_task
from app.services.download_service import (
    complete_global_download,
    create_user_download,
)
from app.services.usage_service import get_usage
from tests.helpers_v0 import create_user_v0


@pytest.mark.asyncio
async def test_complete_global_download_indexes_stored_files_and_user_files(
    temp_db: str,
) -> None:
    user = await create_user_v0(username="complete_v0", quota_bytes=1000)
    client = AsyncMock()
    client.add_uri.return_value = "gid-complete"
    total_bytes = len(b"alpha") + len(b"beta")

    task = await create_user_download(
        user_id=user["id"],
        quota_bytes=user["quota_bytes"],
        uri="https://example.com/archive",
        resource_key="http:complete-v0",
        resource_kind="http",
        display_name="archive",
        total_bytes=total_bytes,
        aria2_client=client,
    )

    source_path = Path(settings.download_dir) / "downloading" / str(
        task["global_download_id"]
    )
    nested_path = source_path / "nested"
    nested_path.mkdir(parents=True)
    (source_path / "a.txt").write_bytes(b"alpha")
    (nested_path / "b.txt").write_bytes(b"beta")

    result = await complete_global_download(
        global_download_id=task["global_download_id"],
        source_path=source_path,
        original_name="archive",
    )

    global_download = await get_global_by_resource_key("http:complete-v0")
    user_task = await get_user_task(user["id"], task["global_download_id"])
    usage = await get_usage(user["id"], quota_bytes=user["quota_bytes"])
    async with transaction() as conn:
        stored = (
            await conn.execute(
                select(stored_files).where(
                    stored_files.c.id == global_download["completed_file_id"]
                )
            )
        ).mappings().one()
        entries = (
            await conn.execute(
                select(stored_file_entries)
                .where(stored_file_entries.c.stored_file_id == stored["id"])
                .order_by(stored_file_entries.c.relative_path)
            )
        ).mappings().all()
        user_file = (
            await conn.execute(
                select(user_files).where(
                    user_files.c.user_id == user["id"],
                    user_files.c.stored_file_id == stored["id"],
                )
            )
        ).mappings().one()

    assert result == {
        "status": "completed",
        "entries_created": 4,
        "user_files_created": 1,
    }
    assert global_download is not None
    assert global_download["status"] == "completed"
    assert global_download["completed_at_ms"] is not None
    assert user_task is not None
    assert user_task["status"] == "completed"
    assert user_task["reserved_bytes"] == 0
    assert user_task["finished_at_ms"] is not None
    assert usage["reserved_bytes"] == 0
    assert usage["used_bytes"] == total_bytes
    assert stored["size_bytes"] == total_bytes
    assert stored["is_directory"] == 1
    assert user_file["display_name"] == "archive"
    assert [entry["relative_path"] for entry in entries] == [
        ".",
        "a.txt",
        "nested",
        "nested/b.txt",
    ]
    assert {
        entry["relative_path"]: entry["size_bytes"]
        for entry in entries
        if entry["is_dir"]
    } == {
        ".": 0,
        "nested": 0,
    }


@pytest.mark.asyncio
async def test_complete_global_download_reuses_existing_stored_file_for_same_content(
    temp_db: str,
) -> None:
    user_a = await create_user_v0(username="complete_reuse_a", quota_bytes=1000)
    user_b = await create_user_v0(username="complete_reuse_b", quota_bytes=1000)
    client = AsyncMock()
    client.add_uri.side_effect = ["gid-reuse-a", "gid-reuse-b"]
    total_bytes = len(b"same")

    first_task = await create_user_download(
        user_id=user_a["id"],
        quota_bytes=user_a["quota_bytes"],
        uri="https://example.com/one",
        resource_key="http:reuse-one",
        resource_kind="http",
        display_name="one",
        total_bytes=total_bytes,
        aria2_client=client,
    )
    second_task = await create_user_download(
        user_id=user_b["id"],
        quota_bytes=user_b["quota_bytes"],
        uri="https://example.com/two",
        resource_key="http:reuse-two",
        resource_kind="http",
        display_name="two",
        total_bytes=total_bytes,
        aria2_client=client,
    )

    first_source = Path(settings.download_dir) / "downloading" / str(
        first_task["global_download_id"]
    )
    second_source = Path(settings.download_dir) / "downloading" / str(
        second_task["global_download_id"]
    )
    first_source.mkdir(parents=True, exist_ok=True)
    second_source.mkdir(parents=True, exist_ok=True)
    (first_source / "same.txt").write_bytes(b"same")
    (second_source / "same.txt").write_bytes(b"same")

    first_result = await complete_global_download(
        global_download_id=first_task["global_download_id"],
        source_path=first_source,
        original_name="one",
    )
    second_result = await complete_global_download(
        global_download_id=second_task["global_download_id"],
        source_path=second_source,
        original_name="two",
    )

    first_global = await get_global_by_resource_key("http:reuse-one")
    second_global = await get_global_by_resource_key("http:reuse-two")
    usage_a = await get_usage(user_a["id"], quota_bytes=user_a["quota_bytes"])
    usage_b = await get_usage(user_b["id"], quota_bytes=user_b["quota_bytes"])
    async with transaction() as conn:
        stored_count = (
            await conn.execute(select(func.count()).select_from(stored_files))
        ).scalar_one()
        user_file_count = (
            await conn.execute(select(func.count()).select_from(user_files))
        ).scalar_one()

    assert first_result["entries_created"] == 2
    assert second_result["entries_created"] == 0
    assert second_result["user_files_created"] == 1
    assert first_global is not None
    assert second_global is not None
    assert first_global["completed_file_id"] == second_global["completed_file_id"]
    assert stored_count == 1
    assert user_file_count == 2
    assert not second_source.exists()
    assert usage_a["used_bytes"] == total_bytes
    assert usage_b["used_bytes"] == total_bytes


@pytest.mark.asyncio
async def test_create_user_download_attaches_late_subscriber_to_completed_file(
    temp_db: str,
) -> None:
    user_a = await create_user_v0(username="late_done_a", quota_bytes=1000)
    user_b = await create_user_v0(username="late_done_b", quota_bytes=1000)
    client = AsyncMock()
    client.add_uri.return_value = "gid-late"
    total_bytes = len(b"done")

    first_task = await create_user_download(
        user_id=user_a["id"],
        quota_bytes=user_a["quota_bytes"],
        uri="https://example.com/done",
        resource_key="http:late-done",
        resource_kind="http",
        display_name="done-a",
        total_bytes=total_bytes,
        aria2_client=client,
    )
    source_path = Path(settings.download_dir) / "downloading" / str(
        first_task["global_download_id"]
    )
    source_path.mkdir(parents=True, exist_ok=True)
    (source_path / "done.txt").write_bytes(b"done")
    await complete_global_download(
        global_download_id=first_task["global_download_id"],
        source_path=source_path,
        original_name="done-a",
    )

    second_task = await create_user_download(
        user_id=user_b["id"],
        quota_bytes=user_b["quota_bytes"],
        uri="https://example.com/done",
        resource_key="http:late-done",
        resource_kind="http",
        display_name="done-b",
        total_bytes=total_bytes,
        aria2_client=client,
    )

    global_download = await get_global_by_resource_key("http:late-done")
    usage_b = await get_usage(user_b["id"], quota_bytes=user_b["quota_bytes"])
    async with transaction() as conn:
        user_file = (
            await conn.execute(
                select(user_files).where(
                    user_files.c.user_id == user_b["id"],
                    user_files.c.stored_file_id == global_download["completed_file_id"],
                )
            )
        ).mappings().one()

    assert second_task["status"] == "completed"
    assert second_task["reserved_bytes"] == 0
    assert second_task["finished_at_ms"] is not None
    assert usage_b["reserved_bytes"] == 0
    assert usage_b["used_bytes"] == total_bytes
    assert user_file["display_name"] == "done-b"
    client.add_uri.assert_awaited_once()


@pytest.mark.asyncio
async def test_complete_global_download_restores_source_when_index_registration_fails(
    temp_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await create_user_v0(username="complete_fail", quota_bytes=1000)
    client = AsyncMock()
    client.add_uri.return_value = "gid-complete-fail"
    total_bytes = len(b"rollback")

    task = await create_user_download(
        user_id=user["id"],
        quota_bytes=user["quota_bytes"],
        uri="https://example.com/fail",
        resource_key="http:complete-fail",
        resource_kind="http",
        display_name="fail",
        total_bytes=total_bytes,
        aria2_client=client,
    )
    source_path = Path(settings.download_dir) / "downloading" / str(
        task["global_download_id"]
    )
    source_path.mkdir(parents=True, exist_ok=True)
    (source_path / "file.txt").write_bytes(b"rollback")

    async def fail_registration(*args: object, **kwargs: object) -> tuple[dict, int]:
        raise RuntimeError("index registration failed")

    monkeypatch.setattr(
        download_service,
        "create_stored_file_with_entries",
        fail_registration,
    )

    with pytest.raises(RuntimeError, match="index registration failed"):
        await complete_global_download(
            global_download_id=task["global_download_id"],
            source_path=source_path,
            original_name="fail",
        )

    user_task = await get_user_task(user["id"], task["global_download_id"])
    usage = await get_usage(user["id"], quota_bytes=user["quota_bytes"])
    async with transaction() as conn:
        stored_count = (
            await conn.execute(select(func.count()).select_from(stored_files))
        ).scalar_one()
        entry_count = (
            await conn.execute(select(func.count()).select_from(stored_file_entries))
        ).scalar_one()

    assert source_path.exists()
    assert (source_path / "file.txt").read_bytes() == b"rollback"
    assert stored_count == 0
    assert entry_count == 0
    assert user_task is not None
    assert user_task["status"] == "active"
    assert user_task["reserved_bytes"] == total_bytes
    assert usage["reserved_bytes"] == total_bytes
    assert usage["used_bytes"] == 0
