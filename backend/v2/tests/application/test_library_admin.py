from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from aria2deck.application.library.admin import LibraryAdminService
from aria2deck.application.library.models import EntryDiff
from aria2deck.domain.content import ContentKind
from aria2deck.domain.errors import DomainError, ErrorCode
from aria2deck.infrastructure.content_store import DownloadContentStore
from aria2deck.infrastructure.sqlite.engine import SQLiteDatabase
from aria2deck.infrastructure.sqlite.repositories.content import ContentRepository
from aria2deck.infrastructure.sqlite.unit_of_work import SQLiteUnitOfWork

from .test_library_services import (
    _create_user,
    _make_services,
    _seed_directory,
    _seed_file,
    _setup,
)


def _admin_service(database: SQLiteDatabase, root: Path) -> LibraryAdminService:
    return LibraryAdminService(
        uow_factory=lambda: SQLiteUnitOfWork(database),
        repository_factory=lambda uow: ContentRepository(uow.connection),
        content_store=DownloadContentStore(root),
        clock_ms=lambda: 1,
    )


async def _sql(database: SQLiteDatabase, statement: str, params: dict[str, object]) -> None:
    async with SQLiteUnitOfWork(database) as uow:
        await uow.connection.execute(text(statement), params)


async def test_verify_reports_drift_and_rebuild_restores_read_model(tmp_path: Path) -> None:
    database, clock = await _setup(tmp_path)
    user_id = await _create_user(database, "admin")
    entries = [
        ("", "docs", "directory", 0),
        ("docs", "a.txt", "file", 3),
        ("docs", "b.txt", "file", 4),
        ("", "empty", "directory", 0),
    ]
    content_id, dir_file = await _seed_directory(database, user_id, 2, entries)
    root = tmp_path / "downloads"
    (root / "objects" / "directory-2" / "docs").mkdir(parents=True)
    (root / "objects" / "directory-2" / "empty").mkdir()
    (root / "objects" / "directory-2" / "docs" / "a.txt").write_bytes(b"abc")
    (root / "objects" / "directory-2" / "docs" / "b.txt").write_bytes(b"abcd")
    admin = _admin_service(database, root)

    assert await admin.verify_content_entries(content_id) == EntryDiff((), (), ())

    # 人为篡改读模型：删一行、改一行大小、多出一行。
    await _sql(database, "DELETE FROM content_entries WHERE path = 'docs/a.txt'", {})
    await _sql(
        database, "UPDATE content_entries SET size = 99 WHERE path = 'docs/b.txt'", {}
    )
    await _sql(
        database,
        "INSERT INTO content_entries (content_id, parent_path, name, path, entry_kind, size, "
        "modified_at_ms) VALUES (:content_id, 'docs', 'ghost.txt', 'docs/ghost.txt', 'file', 1, 1)",
        {"content_id": content_id},
    )
    drift = await admin.verify_content_entries(content_id)
    assert drift == EntryDiff(
        missing=("docs/a.txt",),
        unexpected=("docs/ghost.txt",),
        size_mismatch=("docs/b.txt",),
    )
    assert drift.clean is False

    await admin.rebuild_content_entries(content_id)
    rebuilt_diff = await admin.verify_content_entries(content_id)
    assert rebuilt_diff == EntryDiff((), (), ())
    assert rebuilt_diff.clean is True

    queries, _, _, _ = _make_services(database, clock, DownloadContentStore(root))  # type: ignore[arg-type]
    docs_entries, _, total = await queries.browse_directory(
        user_id=user_id, file_id=dir_file, path="docs", page=1, page_size=10
    )
    assert total == 2
    assert [(entry.name, entry.size) for entry in docs_entries] == [("a.txt", 3), ("b.txt", 4)]
    hits, truncated = await queries.search(user_id=user_id, term="a.txt", scope_file_id=dir_file)
    assert truncated is False
    assert [(hit.path, hit.kind) for hit in hits] == [("docs/a.txt", ContentKind.FILE)]


async def test_verify_and_rebuild_are_safe_for_file_objects(tmp_path: Path) -> None:
    database, _clock = await _setup(tmp_path)
    user_id = await _create_user(database, "admin")
    content_id, _file_id = await _seed_file(database, user_id, 1, payload=b"abc")
    root = tmp_path / "downloads"
    (root / "objects").mkdir(parents=True)
    (root / "objects" / "file-1").write_bytes(b"abc")
    admin = _admin_service(database, root)

    assert await admin.verify_content_entries(content_id) == EntryDiff((), (), ())
    await admin.rebuild_content_entries(content_id)
    assert await admin.verify_content_entries(content_id) == EntryDiff((), (), ())
    async with SQLiteUnitOfWork(database) as uow:
        count = (
            await uow.connection.execute(
                text("SELECT COUNT(*) FROM content_entries WHERE content_id = :id"),
                {"id": content_id},
            )
        ).scalar_one()
    assert count == 0, "文件对象不产生目录条目"


async def test_verify_missing_content_is_a_stable_error(tmp_path: Path) -> None:
    database, _clock = await _setup(tmp_path)
    admin = _admin_service(database, tmp_path)
    with pytest.raises(DomainError) as caught:
        await admin.verify_content_entries(999)
    assert caught.value.code is ErrorCode.NOT_FOUND
    with pytest.raises(DomainError) as caught:
        await admin.rebuild_content_entries(999)
    assert caught.value.code is ErrorCode.NOT_FOUND
