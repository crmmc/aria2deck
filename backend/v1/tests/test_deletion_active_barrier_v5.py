from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select, update

from app.db.engine import transaction
from app.db.schema import stored_files, user_files
from app.repositories.task import user_tasks as downloads_repo
from app.repositories import files as files_repo
from app.repositories import shares as shares_repo
from app.repositories.errors import RepositoryConflictError
from app.services import storage_admin_service
from app.services.storage_locks import get_content_hash_lock
from tests.helpers_v0 import create_user_v0, now_ms


@pytest.mark.asyncio
async def test_pending_file_is_hidden_from_all_user_paths(
    test_user: dict, user_file: dict
) -> None:
    async with transaction() as conn:
        await conn.execute(
            update(stored_files)
            .where(stored_files.c.id == user_file["stored_file_id"])
            .values(
                pending_delete=1,
                delete_next_retry_at_ms=now_ms(),
            )
        )

    assert await files_repo.get_stored_file_by_content_hash(
        user_file["content_hash"]
    ) is None
    assert await files_repo.get_user_file_by_hash(
        test_user["id"], user_file["content_hash"]
    ) is None
    assert await files_repo.resolve_user_file_ids(
        test_user["id"], [user_file["id"]]
    ) == []
    assert await shares_repo.get_owned_file(
        test_user["id"], user_file["id"]
    ) is None
    assert await files_repo.directory_entries_page(
        user_file["stored_file_id"], "", limit=10, offset=0
    ) == (None, [], 0)
    with pytest.raises(RepositoryConflictError, match="用户或存储文件正在删除"):
        await downloads_repo.attach_completed_file_to_user(
            user_id=test_user["id"],
            quota_bytes=test_user["quota_bytes"],
            global_download_id=999,
            stored_file_id=user_file["stored_file_id"],
            size_bytes=user_file["size"],
            display_name="late.bin",
            finished_at_ms=now_ms(),
        )


@pytest.mark.asyncio
async def test_admin_enqueue_and_new_reference_have_one_winner(
    user_file: dict,
) -> None:
    async with transaction() as conn:
        await conn.execute(
            user_files.delete().where(user_files.c.id == user_file["id"])
        )
    other = await create_user_v0(username="new-ref-racer")

    async def add_reference() -> tuple[int, int | None]:
        lock = await get_content_hash_lock(user_file["content_hash"])
        async with lock:
            return await files_repo.ensure_stored_file_with_user_ref(
                user_id=other["id"],
                content_hash=user_file["content_hash"],
                real_path=user_file["real_path"],
                size_bytes=user_file["size"],
                is_directory=False,
                original_name="raced.bin",
                entry_templates=[],
            )

    deleted, added = await asyncio.gather(
        storage_admin_service.bulk_delete_files([user_file["stored_file_id"]]),
        add_reference(),
        return_exceptions=True,
    )
    delete_won = isinstance(deleted, dict) and deleted["accepted_count"] == 1
    add_won = isinstance(added, tuple)
    assert delete_won != add_won

    async with transaction() as conn:
        row = (
            await conn.execute(
                select(stored_files.c.pending_delete).where(
                    stored_files.c.id == user_file["stored_file_id"]
                )
            )
        ).one()
        refs = (
            await conn.execute(
                select(user_files.c.id).where(
                    user_files.c.stored_file_id == user_file["stored_file_id"]
                )
            )
        ).all()
    assert (row[0] == 1 and refs == []) or (row[0] == 0 and len(refs) == 1)
