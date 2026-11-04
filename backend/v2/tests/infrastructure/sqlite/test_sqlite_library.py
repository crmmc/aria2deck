from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text

from aria2deck.domain.content import ContentDeletionState, ContentKind
from aria2deck.domain.errors import DomainError, ErrorCode
from aria2deck.domain.packs import PackSourceSnapshot
from aria2deck.infrastructure.sqlite.engine import SQLiteDatabase
from aria2deck.infrastructure.sqlite.repositories.content import (
    DELETE_LEASE_OWNER,
    DELETE_LEASE_TTL_MS,
    ContentRepository,
)
from aria2deck.infrastructure.sqlite.repositories.packs import PackRepository
from aria2deck.infrastructure.sqlite.repositories.shares import ShareRepository
from aria2deck.infrastructure.sqlite.unit_of_work import SQLiteUnitOfWork

from .test_accounts_quota import create_user


def _digest(index: int) -> str:
    return f"{index:064x}"


async def make_object(
    database: SQLiteDatabase,
    index: int,
    *,
    kind: str = "file",
    size: int = 10,
    now_ms: int = 1,
) -> int:
    async with SQLiteUnitOfWork(database) as uow:
        return await ContentRepository(uow.connection).create_object(
            kind=kind,
            digest=_digest(index),
            size=size,
            storage_path=f"objects/{index}",
            now_ms=now_ms,
        )


async def test_user_file_listing_pages_with_real_total_and_directories_first(
    database: SQLiteDatabase,
) -> None:
    user_id = await create_user(database, "admin")
    other_id = await create_user(database, "other", admin=False)
    directory_id = await make_object(database, 1, kind="directory", size=5)
    file_id = await make_object(database, 2)
    await make_object(database, 3)
    async with SQLiteUnitOfWork(database) as uow:
        repo = ContentRepository(uow.connection)
        await repo.add_user_file(user_id=user_id, content_id=directory_id, display_name="zz-dir", now_ms=1)
        await repo.add_user_file(user_id=user_id, content_id=file_id, display_name="a-file", now_ms=2)
        await repo.add_user_file(user_id=other_id, content_id=file_id, display_name="b-file", now_ms=3)
        page_one, total = await repo.list_user_files(user_id=user_id, offset=0, limit=1)
        beyond, beyond_total = await repo.list_user_files(user_id=user_id, offset=10, limit=5)
    assert total == 2
    assert beyond_total == 2
    assert beyond == []
    assert [item.display_name for item in page_one] == ["zz-dir"]
    assert page_one[0].kind is ContentKind.DIRECTORY
    assert page_one[0].size == 5
    assert page_one[0].created_at_ms == 1


async def test_get_user_file_only_sees_owned_references(database: SQLiteDatabase) -> None:
    owner = await create_user(database, "admin")
    other = await create_user(database, "other", admin=False)
    content_id = await make_object(database, 1)
    async with SQLiteUnitOfWork(database) as uow:
        repo = ContentRepository(uow.connection)
        owned = await repo.add_user_file(user_id=owner, content_id=content_id, display_name="mine", now_ms=1)
        foreign = await repo.add_user_file(user_id=other, content_id=content_id, display_name="theirs", now_ms=1)
        record = await repo.get_user_file(user_id=owner, file_id=owned)
    assert record.display_name == "mine"
    assert record.content_id == content_id
    assert record.storage_path == "objects/1"
    assert record.digest == _digest(1)
    for user, file_id in ((other, owned), (owner, foreign)):
        async with SQLiteUnitOfWork(database) as uow:
            with pytest.raises(DomainError) as caught:
                await ContentRepository(uow.connection).get_user_file(user_id=user, file_id=file_id)
        assert caught.value.code is ErrorCode.NOT_FOUND


async def test_rename_user_file_validates_conflicts_and_ownership(database: SQLiteDatabase) -> None:
    user_id = await create_user(database, "admin")
    first = await make_object(database, 1)
    second = await make_object(database, 2)
    async with SQLiteUnitOfWork(database) as uow:
        repo = ContentRepository(uow.connection)
        await repo.add_user_file(user_id=user_id, content_id=first, display_name="one", now_ms=1)
        second_file = await repo.add_user_file(user_id=user_id, content_id=second, display_name="two", now_ms=1)
        with pytest.raises(DomainError) as caught:
            await repo.rename_user_file(user_id=user_id, file_id=second_file, new_name="one")
        assert caught.value.code is ErrorCode.DUPLICATE_RESOURCE
        await repo.rename_user_file(user_id=user_id, file_id=second_file, new_name="two-renamed")
        assert (await repo.get_user_file(user_id=user_id, file_id=second_file)).display_name == "two-renamed"
        with pytest.raises(DomainError):
            await repo.rename_user_file(user_id=user_id, file_id=second_file, new_name="a/b")
    async with SQLiteUnitOfWork(database) as uow:
        with pytest.raises(DomainError) as caught:
            await ContentRepository(uow.connection).rename_user_file(user_id=user_id, file_id=999, new_name="x")
        assert caught.value.code is ErrorCode.NOT_FOUND


async def test_remove_user_file_returns_reference_count_before_delete(database: SQLiteDatabase) -> None:
    owner = await create_user(database, "admin")
    other = await create_user(database, "other", admin=False)
    content_id = await make_object(database, 1)
    async with SQLiteUnitOfWork(database) as uow:
        repo = ContentRepository(uow.connection)
        owner_file = await repo.add_user_file(user_id=owner, content_id=content_id, display_name="a", now_ms=1)
        other_file = await repo.add_user_file(user_id=other, content_id=content_id, display_name="b", now_ms=1)
        assert await repo.count_references(content_id) == 2
        assert await repo.remove_user_file(user_id=owner, file_id=owner_file) == 2
        assert await repo.remove_user_file(user_id=other, file_id=other_file) == 1
        assert await repo.count_references(content_id) == 0
        with pytest.raises(DomainError) as caught:
            await repo.remove_user_file(user_id=owner, file_id=owner_file)
        assert caught.value.code is ErrorCode.NOT_FOUND


async def test_pending_pack_sources_are_detected_per_content(database: SQLiteDatabase) -> None:
    user_id = await create_user(database, "admin")
    blocked = await make_object(database, 1)
    free = await make_object(database, 2)
    async with SQLiteUnitOfWork(database) as uow:
        repo = ContentRepository(uow.connection)
        blocked_file = await repo.add_user_file(user_id=user_id, content_id=blocked, display_name="b", now_ms=1)
        await repo.add_user_file(user_id=user_id, content_id=free, display_name="f", now_ms=1)
        pack_id = await PackRepository(uow.connection).create_job(
            owner_id=user_id,
            output_name="a.zip",
            pack_format="zip",
            source_size=10,
            reserved_bytes=10,
            physical_reserved_bytes=10,
            delete_source=True,
            now_ms=1,
        )
        await PackRepository(uow.connection).add_source(
            pack_id, PackSourceSnapshot(blocked_file, blocked, "file:" + _digest(1), "b", 1, 10)
        )
        assert await repo.has_pending_pack_sources(blocked) is True
        assert await repo.has_pending_pack_sources(free) is False


async def test_revoke_shares_only_touches_unrevoked_rows(database: SQLiteDatabase) -> None:
    user_id = await create_user(database, "admin")
    content_id = await make_object(database, 1)
    async with SQLiteUnitOfWork(database) as uow:
        repo = ContentRepository(uow.connection)
        file_id = await repo.add_user_file(user_id=user_id, content_id=content_id, display_name="a", now_ms=1)
        shares = ShareRepository(uow.connection)
        await shares.create_share(
            user_id=user_id,
            user_file_id=file_id,
            code="c1",
            password_hash=None,
            expires_at_ms=None,
            max_downloads=None,
            now_ms=10,
        )
        second = await shares.create_share(
            user_id=user_id,
            user_file_id=file_id,
            code="c2",
            password_hash=None,
            expires_at_ms=None,
            max_downloads=None,
            now_ms=10,
        )
        assert await repo.revoke_shares_for_user_file(user_file_id=file_id, now_ms=20) == 2
        assert await repo.revoke_shares_for_user_file(user_file_id=file_id, now_ms=21) == 0
        revoked = await shares.get_share(second)
    assert revoked.revoked_at_ms == 20


async def test_entry_listing_pages_sorts_directories_first_and_finds_by_path(
    database: SQLiteDatabase,
) -> None:
    content_id = await make_object(database, 1, kind="directory")
    async with SQLiteUnitOfWork(database) as uow:
        repo = ContentRepository(uow.connection)
        for parent, name, kind, size in [
            ("", "z-dir", "directory", 0),
            ("", "a-file", "file", 3),
            ("", "b-dir", "directory", 0),
            ("b-dir", "inner", "file", 4),
        ]:
            await repo.add_entry(
                content_id=content_id, parent_path=parent, name=name,
                entry_kind=kind, size=size, now_ms=1,
            )
        page, total = await repo.list_entries(
            content_id=content_id, parent_path="", offset=0, limit=2
        )
        empty, beyond_total = await repo.list_entries(
            content_id=content_id, parent_path="", offset=9, limit=5
        )
        inner, inner_total = await repo.list_entries(
            content_id=content_id, parent_path="b-dir", offset=0, limit=10
        )
        found = await repo.get_entry(content_id=content_id, path="b-dir/inner")
    assert total == 3
    assert beyond_total == 3
    assert empty == []
    assert [entry.name for entry in page] == ["b-dir", "z-dir"]
    assert page[0].entry_kind is ContentKind.DIRECTORY
    assert [entry.name for entry in inner] == ["inner"]
    assert inner_total == 1
    assert found.path == "b-dir/inner"
    assert found.size == 4
    async with SQLiteUnitOfWork(database) as uow:
        with pytest.raises(DomainError) as caught:
            await ContentRepository(uow.connection).get_entry(content_id=content_id, path="missing")
        assert caught.value.code is ErrorCode.NOT_FOUND


async def test_search_finds_display_names_and_entry_paths_case_insensitively(
    database: SQLiteDatabase,
) -> None:
    user_id = await create_user(database, "admin")
    other_id = await create_user(database, "other", admin=False)
    report_id = await make_object(database, 1)
    directory_id = await make_object(database, 2, kind="directory")
    other_content = await make_object(database, 3)
    async with SQLiteUnitOfWork(database) as uow:
        repo = ContentRepository(uow.connection)
        await repo.add_user_file(user_id=user_id, content_id=report_id, display_name="Report.pdf", now_ms=1)
        directory_file = await repo.add_user_file(user_id=user_id, content_id=directory_id, display_name="data", now_ms=1)
        await repo.add_user_file(user_id=other_id, content_id=other_content, display_name="other-report", now_ms=1)
        for parent, name in [("", "docs"), ("docs", "REPORT-q1.txt"), ("docs", "notes.txt")]:
            await repo.add_entry(
                content_id=directory_id,
                parent_path=parent,
                name=name,
                entry_kind="directory" if name == "docs" else "file",
                size=2,
                now_ms=1,
            )
        hits, truncated = await repo.search_user_files(user_id=user_id, term="rePoRt")
        scoped, scoped_truncated = await repo.search_user_files(
            user_id=user_id, term="report", content_id=directory_id
        )
        foreign, _ = await repo.search_user_files(user_id=other_id, term="report")
    assert truncated is False
    assert [(hit.user_file_id, hit.display_name, hit.path) for hit in hits] == [
        (1, "Report.pdf", ""),
        (2, "data", "docs/REPORT-q1.txt"),
    ]
    assert hits[0].kind is ContentKind.FILE
    assert scoped_truncated is False
    assert [(hit.user_file_id, hit.path) for hit in scoped] == [(2, "docs/REPORT-q1.txt")]
    assert [(hit.display_name, hit.path) for hit in foreign] == [("other-report", "")]
    assert directory_file > 0


async def test_search_escapes_like_wildcards(database: SQLiteDatabase) -> None:
    user_id = await create_user(database, "admin")
    async with SQLiteUnitOfWork(database) as uow:
        repo = ContentRepository(uow.connection)
        for index, name in enumerate(["50%off.bin", "a_b.bin", "axb.bin", "plain.txt"]):
            content_id = await repo.create_object(
                kind="file",
                digest=_digest(index),
                size=1,
                storage_path=f"objects/{index}",
                now_ms=1,
            )
            await repo.add_user_file(
                user_id=user_id, content_id=content_id, display_name=name, now_ms=1
            )
        percent, _ = await repo.search_user_files(user_id=user_id, term="50%off")
        underscore, _ = await repo.search_user_files(user_id=user_id, term="a_b")
        backslash, _ = await repo.search_user_files(user_id=user_id, term="a\\b")
        wildcard, _ = await repo.search_user_files(user_id=user_id, term="%")
    assert [hit.display_name for hit in percent] == ["50%off.bin"]
    assert [hit.display_name for hit in underscore] == ["a_b.bin"]
    assert backslash == []  # 文件名不允许反斜杠，转义后不应误当通配符。
    assert [hit.display_name for hit in wildcard] == ["50%off.bin"]  # 裸 % 按字面匹配。


async def test_search_truncates_at_two_hundred_hits(database: SQLiteDatabase) -> None:
    user_id = await create_user(database, "admin")
    async with SQLiteUnitOfWork(database) as uow:
        repo = ContentRepository(uow.connection)
        for index in range(201):
            content_id = await repo.create_object(
                kind="file",
                digest=_digest(index),
                size=1,
                storage_path=f"objects/{index}",
                now_ms=1,
            )
            await repo.add_user_file(
                user_id=user_id, content_id=content_id, display_name=f"match-{index:03d}", now_ms=1
            )
        hits, truncated = await repo.search_user_files(user_id=user_id, term="match-")
    assert truncated is True
    assert len(hits) == 200
    assert hits[0].display_name == "match-000"
    assert hits[-1].display_name == "match-199"


async def test_read_leases_are_conditional_floored_and_resettable(database: SQLiteDatabase) -> None:
    content_id = await make_object(database, 1)
    claimed_id = await make_object(database, 2)
    async with SQLiteUnitOfWork(database) as uow:
        repo = ContentRepository(uow.connection)
        await repo.acquire_read_lease(content_id)
        await repo.acquire_read_lease(content_id)
        assert (await repo.get_object(content_id)).read_leases == 2
        await repo.release_read_lease(content_id)
        await repo.release_read_lease(content_id)
        await repo.release_read_lease(content_id)
        assert (await repo.get_object(content_id)).read_leases == 0
        await repo.claim_orphan_delete(
            claimed_id, now_ms=1, lease_owner="w1", lease_ttl_ms=1000
        )
    async with SQLiteUnitOfWork(database) as uow:
        repo = ContentRepository(uow.connection)
        with pytest.raises(DomainError) as caught:
            await repo.acquire_read_lease(claimed_id)
        assert caught.value.code is ErrorCode.CONTENT_PENDING_DELETE
        with pytest.raises(DomainError) as caught:
            await repo.acquire_read_lease(999)
        assert caught.value.code is ErrorCode.NOT_FOUND
        await repo.acquire_read_lease(content_id)
        assert await repo.reset_all_read_leases() == 1
        assert (await repo.get_object(content_id)).read_leases == 0
        assert await repo.reset_all_read_leases() == 0


async def test_concurrent_claims_allow_exactly_one_winner(database: SQLiteDatabase) -> None:
    content_id = await make_object(database, 1)

    async def claim(owner: str) -> bool:
        async with SQLiteUnitOfWork(database) as uow:
            return await ContentRepository(uow.connection).claim_orphan_delete(
                content_id, now_ms=1, lease_owner=owner, lease_ttl_ms=1000
            )

    results = await asyncio.gather(claim("w1"), claim("w2"))
    assert sorted(results) == [False, True]
    async with SQLiteUnitOfWork(database) as uow:
        record = await ContentRepository(uow.connection).get_object(content_id)
    assert record.deletion_state is ContentDeletionState.PENDING_DELETE
    assert record.delete_lease_owner in {"w1", "w2"}
    assert record.delete_lease_expires_at_ms == 1001


async def test_negative_lease_ttl_is_rejected(database: SQLiteDatabase) -> None:
    content_id = await make_object(database, 1)
    async with SQLiteUnitOfWork(database) as uow:
        repo = ContentRepository(uow.connection)
        for operation in (repo.claim_orphan_delete, repo.acquire_or_reclaim):
            with pytest.raises(DomainError) as caught:
                await operation(
                    content_id, now_ms=1, lease_owner="w1", lease_ttl_ms=-1
                )
            assert caught.value.code is ErrorCode.INVALID_INPUT


async def test_list_deletion_candidates_requires_lease_expiry_or_ownership(
    database: SQLiteDatabase,
) -> None:
    pending_id = await make_object(database, 1)
    tombstoned_id = await make_object(database, 2)
    active_id = await make_object(database, 3)
    async with SQLiteUnitOfWork(database) as uow:
        repo = ContentRepository(uow.connection)
        assert await repo.claim_orphan_delete(
            pending_id, now_ms=100, lease_owner="w1", lease_ttl_ms=1000
        )
        assert await repo.claim_orphan_delete(
            tombstoned_id, now_ms=100, lease_owner="w1", lease_ttl_ms=1000
        )
        assert await repo.write_tombstone(tombstoned_id, owner="w1", now_ms=200)
        assert [item.content_id for item in await repo.list_deletion_candidates(owner="w2", now_ms=500)] == [
            tombstoned_id
        ]
        assert [item.content_id for item in await repo.list_deletion_candidates(owner="w1", now_ms=500)] == [
            pending_id,
            tombstoned_id,
        ]
        expired = await repo.list_deletion_candidates(owner="w2", now_ms=2000)
    assert sorted(item.content_id for item in expired) == [pending_id, tombstoned_id]
    candidate = next(item for item in expired if item.content_id == pending_id)
    assert candidate.reference_count == 0
    assert candidate.read_leases == 0
    assert candidate.storage_path == "objects/1"
    assert candidate.tombstone_path is None
    assert active_id > 0


async def test_acquire_or_reclaim_requires_expired_or_own_lease(database: SQLiteDatabase) -> None:
    content_id = await make_object(database, 1)
    async with SQLiteUnitOfWork(database) as uow:
        repo = ContentRepository(uow.connection)
        assert await repo.claim_orphan_delete(content_id, now_ms=100, lease_owner="w1", lease_ttl_ms=1000)
        assert not await repo.acquire_or_reclaim(content_id, now_ms=200, lease_owner="w2", lease_ttl_ms=1000)
        assert await repo.acquire_or_reclaim(content_id, now_ms=300, lease_owner="w1", lease_ttl_ms=5000)
        record = await repo.get_object(content_id)
        assert record.delete_lease_owner == "w1"
        assert record.delete_lease_expires_at_ms == 5300
        assert await repo.acquire_or_reclaim(content_id, now_ms=5400, lease_owner="w2", lease_ttl_ms=1000)
        record = await repo.get_object(content_id)
    assert record.delete_lease_owner == "w2"
    assert record.delete_lease_expires_at_ms == 6400


async def test_write_tombstone_requires_pending_zero_references_and_valid_lease(
    database: SQLiteDatabase,
) -> None:
    user_id = await create_user(database, "admin")
    content_id = await make_object(database, 1)
    reclaimed_id = await make_object(database, 2)
    async with SQLiteUnitOfWork(database) as uow:
        repo = ContentRepository(uow.connection)
        await repo.claim_orphan_delete(content_id, now_ms=100, lease_owner="w1", lease_ttl_ms=1000)
        await repo.claim_orphan_delete(reclaimed_id, now_ms=100, lease_owner="w1", lease_ttl_ms=1000)
        assert not await repo.write_tombstone(content_id, owner="w2", now_ms=200)
        assert not await repo.write_tombstone(reclaimed_id, owner="w1", now_ms=5000)
        await uow.connection.execute(
            text(
                "INSERT INTO user_files (user_id, content_id, display_name, created_at_ms) "
                "VALUES (:user_id, :content_id, 'ghost', 1)"
            ),
            {"user_id": user_id, "content_id": content_id},
        )
        assert not await repo.write_tombstone(content_id, owner="w1", now_ms=300)
        await uow.connection.execute(text("DELETE FROM user_files WHERE content_id = :id"), {"id": content_id})
        assert await repo.write_tombstone(content_id, owner="w1", now_ms=300)
        record = await repo.get_object(content_id)
        assert record.deletion_state is ContentDeletionState.TOMBSTONED
        assert record.tombstone_path == "objects/1"
        assert not await repo.write_tombstone(content_id, owner="w1", now_ms=400)


async def test_finalize_tombstoned_is_idempotent_and_cascades_entries(database: SQLiteDatabase) -> None:
    content_id = await make_object(database, 1, kind="directory")
    async with SQLiteUnitOfWork(database) as uow:
        repo = ContentRepository(uow.connection)
        await repo.add_entry(
            content_id=content_id, parent_path="", name="child",
            entry_kind="file", size=2, now_ms=1,
        )
        await repo.claim_orphan_delete(content_id, now_ms=100, lease_owner="w1", lease_ttl_ms=1000)
        assert not await repo.finalize_tombstoned(content_id)
        assert await repo.write_tombstone(content_id, owner="w1", now_ms=200)
        assert await repo.finalize_tombstoned(content_id)
        assert not await repo.finalize_tombstoned(content_id)
    async with database.connection() as connection:
        objects = (
            await connection.execute(
                text("SELECT COUNT(*) FROM content_objects WHERE id = :id"), {"id": content_id}
            )
        ).scalar_one()
        entries = (
            await connection.execute(
                text("SELECT COUNT(*) FROM content_entries WHERE content_id = :id"), {"id": content_id}
            )
        ).scalar_one()
    assert objects == 0
    assert entries == 0


async def test_storage_objects_listing_filters_orphans_and_searches_paths(
    database: SQLiteDatabase,
) -> None:
    user_id = await create_user(database, "admin")
    referenced = await make_object(database, 11)
    orphan = await make_object(database, 22)
    free = await make_object(database, 33)
    async with SQLiteUnitOfWork(database) as uow:
        repo = ContentRepository(uow.connection)
        await repo.add_user_file(user_id=user_id, content_id=referenced, display_name="a", now_ms=1)
        everything, total = await repo.list_storage_objects(offset=0, limit=10)
        orphans, orphan_total = await repo.list_storage_objects(offset=0, limit=10, orphans_only=True)
        searched, search_total = await repo.list_storage_objects(offset=0, limit=10, search="2")
        empty, empty_total = await repo.list_storage_objects(offset=0, limit=10, search="nothing")
        page, page_total = await repo.list_storage_objects(offset=0, limit=2, search="objects/")
        beyond, beyond_total = await repo.list_storage_objects(offset=9, limit=5, search="objects/")
    assert total == 3
    assert {item.content_id for item in everything} == {referenced, orphan, free}
    by_id = {item.content_id: item for item in everything}
    assert by_id[referenced].reference_count == 1
    assert by_id[orphan].reference_count == 0
    assert by_id[referenced].read_leases == 0
    assert by_id[orphan].storage_path == "objects/22"
    assert orphan_total == 2
    assert {item.content_id for item in orphans} == {orphan, free}
    assert search_total == 1
    assert [item.content_id for item in searched] == [orphan]
    assert empty == []
    assert empty_total == 0
    assert page_total == 3
    assert len(page) == 2
    assert beyond == []
    assert beyond_total == 3


async def test_list_referencing_users_returns_rows_and_empty_for_zero_references(
    database: SQLiteDatabase,
) -> None:
    first = await create_user(database, "admin")
    second = await create_user(database, "other", admin=False)
    content_id = await make_object(database, 1)
    orphan_id = await make_object(database, 2)
    async with SQLiteUnitOfWork(database) as uow:
        repo = ContentRepository(uow.connection)
        await repo.add_user_file(user_id=first, content_id=content_id, display_name="a", now_ms=7)
        await repo.add_user_file(user_id=second, content_id=content_id, display_name="b", now_ms=8)
        referencing = await repo.list_referencing_users(content_id)
        assert await repo.list_referencing_users(orphan_id) == []
    assert [(row.user_id, row.username, row.display_name) for row in referencing] == [
        (first, "admin", "a"),
        (second, "other", "b"),
    ]
    assert referencing[0].referenced_at_ms == 7


async def test_accept_orphan_deletes_reports_mixed_results(database: SQLiteDatabase) -> None:
    user_id = await create_user(database, "admin")
    orphan = await make_object(database, 1)
    referenced = await make_object(database, 2)
    async with SQLiteUnitOfWork(database) as uow:
        repo = ContentRepository(uow.connection)
        await repo.add_user_file(user_id=user_id, content_id=referenced, display_name="a", now_ms=1)
        results = await repo.accept_orphan_deletes(
            [orphan, referenced, 999],
            now_ms=10,
            lease_owner=DELETE_LEASE_OWNER,
            lease_ttl_ms=DELETE_LEASE_TTL_MS,
        )
        orphan_state = (await repo.get_object(orphan)).deletion_state
        referenced_state = (await repo.get_object(referenced)).deletion_state
    assert results == [(orphan, True), (referenced, False), (999, False)]
    assert orphan_state is ContentDeletionState.PENDING_DELETE
    assert referenced_state is ContentDeletionState.ACTIVE


async def test_cleanup_user_claims_orphans_with_delete_lease(database: SQLiteDatabase) -> None:
    user_id = await create_user(database, "admin")
    content_id = await make_object(database, 1)
    async with SQLiteUnitOfWork(database) as uow:
        repo = ContentRepository(uow.connection)
        await repo.add_user_file(user_id=user_id, content_id=content_id, display_name="a", now_ms=1)
        await repo.cleanup_user(user_id=user_id, now_ms=100)
        record = await repo.get_object(content_id)
    assert record.deletion_state is ContentDeletionState.PENDING_DELETE
    assert record.delete_lease_owner == DELETE_LEASE_OWNER
    assert record.delete_lease_expires_at_ms == 100 + DELETE_LEASE_TTL_MS
