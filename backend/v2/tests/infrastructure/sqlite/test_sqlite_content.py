from __future__ import annotations

import pytest
from sqlalchemy import text

from aria2deck.domain.content import ContentKind
from aria2deck.domain.errors import DomainError, ErrorCode
from aria2deck.infrastructure.sqlite.engine import SQLiteDatabase
from aria2deck.infrastructure.sqlite.repositories.content import ContentRepository
from aria2deck.infrastructure.sqlite.repositories.packs import PackRepository
from aria2deck.infrastructure.sqlite.unit_of_work import SQLiteUnitOfWork

from .test_accounts_quota import HASH, create_user


async def object_id(database: SQLiteDatabase, digest: str = HASH) -> int:
    async with SQLiteUnitOfWork(database) as uow:
        return await ContentRepository(uow.connection).create_object(kind="file", digest=digest, size=10, storage_path="objects/" + digest[:4], now_ms=1)


async def test_content_key_and_canonical_path_are_unique(database: SQLiteDatabase) -> None:
    await object_id(database)
    async with SQLiteUnitOfWork(database) as uow:
        repo = ContentRepository(uow.connection)
        with pytest.raises(DomainError) as caught:
            await repo.create_object(kind="file", digest=HASH, size=10, storage_path="objects/other", now_ms=1)
        assert caught.value.code is ErrorCode.DUPLICATE_RESOURCE
        with pytest.raises(DomainError):
            await repo.create_object(kind="directory", digest=HASH, size=10, storage_path="objects/abab", now_ms=1)
        directory_id = await repo.create_object(kind="directory", digest=HASH, size=10, storage_path="objects/directory", now_ms=1)
        assert (await repo.get_object(directory_id)).kind.value == "directory"


async def test_find_by_storage_path_and_list_all_storage_paths_match_written_rows(
    database: SQLiteDatabase,
) -> None:
    async with SQLiteUnitOfWork(database) as uow:
        repo = ContentRepository(uow.connection)
        assert await repo.find_by_storage_path("objects/file-" + "a" * 64) is None
        assert await repo.list_all_storage_paths() == set()

    first = f"objects/file-{'a' * 64}"
    second = f"objects/directory-{'b' * 64}"
    async with SQLiteUnitOfWork(database) as uow:
        repo = ContentRepository(uow.connection)
        first_id = await repo.create_object(
            kind="file", digest="a" * 64, size=10, storage_path=first, now_ms=1
        )
        await repo.create_object(
            kind="directory", digest="b" * 64, size=20, storage_path=second, now_ms=1
        )
        found = await repo.find_by_storage_path(first)
        assert found is not None
        assert (found.id, found.kind, found.size, found.storage_path) == (
            first_id,
            ContentKind.FILE,
            10,
            first,
        )
        assert await repo.find_by_storage_path(second + "-missing") is None
        assert await repo.list_all_storage_paths() == {first, second}


async def test_content_path_validation_runs_before_persistence(database: SQLiteDatabase) -> None:
    async with SQLiteUnitOfWork(database) as uow:
        repo = ContentRepository(uow.connection)
        with pytest.raises(DomainError):
            await repo.create_object(kind="file", digest="bad", size=1, storage_path="objects/a", now_ms=1)
        with pytest.raises(DomainError):
            await repo.create_object(kind="file", digest=HASH, size=1, storage_path="/absolute", now_ms=1)


async def test_user_file_references_have_independent_ownership_and_unique_names(database: SQLiteDatabase) -> None:
    owner = await create_user(database)
    other = await create_user(database, "other", admin=False)
    content_id = await object_id(database)
    async with SQLiteUnitOfWork(database) as uow:
        repo = ContentRepository(uow.connection)
        first = await repo.add_user_file(user_id=owner, content_id=content_id, display_name="same", now_ms=1)
        second = await repo.add_user_file(user_id=other, content_id=content_id, display_name="same", now_ms=1)
        assert first != second
        with pytest.raises(DomainError):
            await repo.add_user_file(user_id=owner, content_id=content_id, display_name="another", now_ms=1)
        other_content = await repo.create_object(kind="file", digest="cd" * 32, size=5, storage_path="objects/cd", now_ms=1)
        with pytest.raises(DomainError):
            await repo.add_user_file(user_id=owner, content_id=other_content, display_name="same", now_ms=1)


async def test_directory_entries_are_unique_by_full_path(database: SQLiteDatabase) -> None:
    content_id = await object_id(database)
    async with SQLiteUnitOfWork(database) as uow:
        repo = ContentRepository(uow.connection)
        entry_id = await repo.add_entry(content_id=content_id, parent_path="", name="a", entry_kind="file", size=10, now_ms=1)
        assert entry_id > 0
        with pytest.raises(DomainError):
            await repo.add_entry(content_id=content_id, parent_path="", name="a", entry_kind="file", size=10, now_ms=1)
        with pytest.raises(DomainError):
            await repo.add_entry(content_id=content_id, parent_path="../", name="a", entry_kind="file", size=10, now_ms=1)


async def test_list_files_returns_only_real_entries_sorted_by_path(
    database: SQLiteDatabase,
) -> None:
    async with SQLiteUnitOfWork(database) as uow:
        repo = ContentRepository(uow.connection)
        content_id = await repo.create_object(
            kind="directory",
            digest=HASH,
            size=30,
            storage_path="objects/directory",
            now_ms=1,
        )
        for parent_path, name, entry_kind, size in (
            ("", "z.bin", "file", 20),
            ("", "folder", "directory", 0),
            ("folder", "a.bin", "file", 10),
        ):
            await repo.add_entry(
                content_id=content_id,
                parent_path=parent_path,
                name=name,
                entry_kind=entry_kind,
                size=size,
                now_ms=1,
            )

        files = await repo.list_files(content_id)

    assert [(entry.path, entry.size) for entry in files] == [
        ("folder/a.bin", 10),
        ("z.bin", 20),
    ]


async def test_pending_delete_content_rejects_new_references(database: SQLiteDatabase) -> None:
    user_id = await create_user(database)
    content_id = await object_id(database)
    async with SQLiteUnitOfWork(database) as uow:
        repo = ContentRepository(uow.connection)
        assert await repo.claim_orphan_delete(
            content_id, now_ms=1, lease_owner="content-deletion", lease_ttl_ms=1000
        )
        assert (await repo.get_object(content_id)).deletion_state.value == "pending_delete"
        with pytest.raises(DomainError) as caught:
            await repo.add_user_file(user_id=user_id, content_id=content_id, display_name="a", now_ms=1)
        assert caught.value.code is ErrorCode.CONTENT_PENDING_DELETE
        assert not await repo.claim_orphan_delete(
            content_id, now_ms=2, lease_owner="content-deletion", lease_ttl_ms=1000
        )


async def test_existing_reference_and_pending_pack_source_block_physical_delete_claim(database: SQLiteDatabase) -> None:
    user_id = await create_user(database)
    content_id = await object_id(database)
    async with SQLiteUnitOfWork(database) as uow:
        content = ContentRepository(uow.connection)
        file_id = await content.add_user_file(user_id=user_id, content_id=content_id, display_name="a", now_ms=1)
        assert not await content.claim_orphan_delete(
            content_id, now_ms=1, lease_owner="content-deletion", lease_ttl_ms=1000
        )
        pack_id = await PackRepository(uow.connection).create_job(owner_id=user_id, output_name="a.zip", pack_format="zip", source_size=10, reserved_bytes=10, physical_reserved_bytes=10, delete_source=True, now_ms=1)
        from aria2deck.domain.packs import PackSourceSnapshot
        await PackRepository(uow.connection).add_source(pack_id, PackSourceSnapshot(file_id, content_id, "file:" + HASH, "a", 1, 10))
        await uow.connection.execute(text("DELETE FROM user_files WHERE id = :id"), {"id": file_id})
        assert not await content.claim_orphan_delete(
            content_id, now_ms=2, lease_owner="content-deletion", lease_ttl_ms=1000
        )
        await uow.connection.execute(text("UPDATE pack_sources SET cleanup_status = 'kept'"))
        assert await content.claim_orphan_delete(
            content_id, now_ms=3, lease_owner="content-deletion", lease_ttl_ms=1000
        )


async def test_pack_sources_do_not_have_current_file_or_content_foreign_keys(database: SQLiteDatabase) -> None:
    async with database.connection() as connection:
        rows = (await connection.execute(text("SELECT \"table\" FROM pragma_foreign_key_list('pack_sources')"))).scalars().all()
    assert set(rows) == {"pack_jobs"}


async def test_content_repository_rejects_invalid_kind_size_and_missing_records(database: SQLiteDatabase) -> None:
    async with SQLiteUnitOfWork(database) as uow:
        repo = ContentRepository(uow.connection)
        with pytest.raises(DomainError):
            await repo.create_object(kind="link", digest=HASH, size=1, storage_path="objects/a", now_ms=1)
        with pytest.raises(DomainError):
            await repo.create_object(kind="file", digest=HASH, size=-1, storage_path="objects/a", now_ms=1)
        with pytest.raises(DomainError) as caught:
            await repo.get_object(999)
        assert caught.value.code is ErrorCode.NOT_FOUND
        content_id = await repo.create_object(kind="file", digest=HASH, size=1, storage_path="objects/a", now_ms=1)
        with pytest.raises(DomainError):
            await repo.add_entry(content_id=content_id, parent_path="", name="a", entry_kind="link", size=1, now_ms=1)
        with pytest.raises(DomainError):
            await repo.add_entry(content_id=content_id, parent_path="", name="a", entry_kind="file", size=-1, now_ms=1)


# --- 回归（D5）：认领必须保护其他未完成 consumption 的同内容 attempt ---
async def test_claim_orphan_delete_is_blocked_by_other_unconsumed_attempt(
    database: SQLiteDatabase,
) -> None:
    from aria2deck.infrastructure.sqlite.repositories.downloads import DownloadRepository

    content_id = await object_id(database)
    async with SQLiteUnitOfWork(database) as uow:
        downloads = DownloadRepository(uow.connection)
        repo = ContentRepository(uow.connection)
        first_source = await downloads.create_source(
            kind="http", resource_key="http:d5-a", payload_json="{}", options_json="{}",
            selected_file_indexes_json=None, display_name="a", display_uri=None, now_ms=1,
        )
        second_source = await downloads.create_source(
            kind="http", resource_key="http:d5-b", payload_json="{}", options_json="{}",
            selected_file_indexes_json=None, display_name="b", display_uri=None, now_ms=1,
        )
        current = await downloads.create_attempt(
            source_id=first_source, resource_key="http:d5-a", gid="a" * 16, now_ms=1
        )
        other = await downloads.create_attempt(
            source_id=second_source, resource_key="http:d5-b", gid="b" * 16, now_ms=1
        )
        for attempt_id, gid in ((current, "a" * 16), (other, "b" * 16)):
            await downloads.begin_completion(
                attempt_id, expected_gid=gid, content_id=content_id, now_ms=2
            )
        await downloads.fail_attempt(current, error_code="failed", now_ms=3)

        # 其他 attempt 的完成意图尚未消费（result_consumed IS NULL）：认领必须失败。
        assert not await repo.claim_orphan_delete(
            content_id,
            now_ms=4,
            lease_owner="content-deletion",
            lease_ttl_ms=1000,
            exclude_attempt_id=current,
        )
        # 其他 attempt 退出完成意图后：认领恢复。
        await uow.connection.execute(
            text("UPDATE download_attempts SET completed_content_id = NULL WHERE id = :id"),
            {"id": other},
        )
        assert await repo.claim_orphan_delete(
            content_id,
            now_ms=5,
            lease_owner="content-deletion",
            lease_ttl_ms=1000,
            exclude_attempt_id=current,
        )


async def _seed_live_subscription(database: SQLiteDatabase, user_id: int, display_name: str) -> None:
    async with SQLiteUnitOfWork(database) as uow:
        await uow.connection.execute(
            text(
                "INSERT INTO download_sources (kind, resource_key, payload_json, options_json, display_name, created_at_ms)"
                " VALUES ('http', :key, '{}', '{}', :name, 1)"
            ),
            {"key": f"key-{display_name}", "name": display_name},
        )
        source_id = (await uow.connection.execute(text("SELECT id FROM download_sources WHERE resource_key = :key"), {"key": f"key-{display_name}"})).scalar_one()
        await uow.connection.execute(
            text(
                "INSERT INTO download_attempts (source_id, resource_key, gid, status, submission_state, completed_length, physical_reserved_bytes, created_at_ms, updated_at_ms)"
                " VALUES (:sid, :key, :gid, 'active', 'submitted', 0, 0, 1, 1)"
            ),
            {"sid": source_id, "key": f"key-{display_name}", "gid": f"gid-{display_name}"},
        )
        attempt_id = (await uow.connection.execute(text("SELECT id FROM download_attempts WHERE gid = :gid"), {"gid": f"gid-{display_name}"})).scalar_one()
        await uow.connection.execute(
            text(
                "INSERT INTO task_subscriptions (attempt_id, user_id, status, reserved_bytes, display_name, created_at_ms, updated_at_ms)"
                " VALUES (:aid, :uid, 'active', 0, :name, 1, 1)"
            ),
            {"aid": attempt_id, "uid": user_id, "name": display_name},
        )


# --- 回归（G2）：改名撞上同用户 live 订阅名被拒绝；与当前名相同是幂等成功。 ---
async def test_rename_user_file_live_subscription_guard_and_idempotent_same_name(
    database: SQLiteDatabase,
) -> None:
    user_id = await create_user(database)
    content_id = await object_id(database)
    async with SQLiteUnitOfWork(database) as uow:
        repo = ContentRepository(uow.connection)
        file_id = await repo.add_user_file(
            user_id=user_id, content_id=content_id, display_name="keep.bin", now_ms=1
        )
    await _seed_live_subscription(database, user_id, "busy.bin")
    await _seed_live_subscription(database, user_id, "keep.bin")

    async with SQLiteUnitOfWork(database) as uow:
        repo = ContentRepository(uow.connection)
        with pytest.raises(DomainError) as caught:
            await repo.rename_user_file(user_id=user_id, file_id=file_id, new_name="busy.bin")
        assert caught.value.code is ErrorCode.DUPLICATE_RESOURCE
        # 与当前名相同：幂等成功，不被自身 live 订阅误判为冲突。
        await repo.rename_user_file(user_id=user_id, file_id=file_id, new_name="keep.bin")
        assert (await repo.get_user_file(user_id=user_id, file_id=file_id)).display_name == "keep.bin"
