from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import settings
from app.db.migrations import migrate_v7
from app.services.storage import get_store_path_for_hash
from app.services.storage_index import scan_storage_path


@pytest.mark.asyncio
async def test_v6_to_v7_backfills_and_validates_identity() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE schema_meta (id INTEGER PRIMARY KEY, version INTEGER, created_at_ms INTEGER)"))
        await conn.execute(text("INSERT INTO schema_meta VALUES (1, 6, 1)"))
        await conn.execute(text("CREATE TABLE stored_files (id INTEGER PRIMARY KEY, content_hash TEXT UNIQUE)"))
        await conn.execute(text("INSERT INTO stored_files VALUES (1, 'legacy-key')"))
        await migrate_v7(conn)
        row = (await conn.execute(text("SELECT content_hash_version, content_object_kind, content_digest FROM stored_files"))).one()
        assert row == ("v1", "legacy", "legacy-key")
        digest = "a" * 64
        key = f"v2:file:{digest}"
        await conn.execute(text("INSERT INTO stored_files (id, content_hash, content_hash_version, content_object_kind, content_digest) VALUES (2, :key, 'v2', 'file', :digest)"), {"key": key, "digest": digest})
        await conn.execute(text("INSERT INTO stored_files (id, content_hash, content_hash_version, content_object_kind, content_digest) VALUES (3, :key, 'v2', 'directory', :digest)"), {"key": f"v2:directory:{digest}", "digest": digest})
        with pytest.raises(IntegrityError):
            await conn.execute(text("INSERT INTO stored_files (id, content_hash, content_hash_version, content_object_kind, content_digest) VALUES (4, 'wrong', 'v2', 'file', :digest)"), {"digest": "b" * 64})
    await engine.dispose()


def test_v2_file_and_directories_are_domain_separated(tmp_path: Path) -> None:
    empty_file = tmp_path / "empty"
    empty_file.write_bytes(b"")
    empty_directory = tmp_path / "directory"
    empty_directory.mkdir()
    nested_empty_directory = empty_directory / "nested"
    nested_empty_directory.mkdir()

    file_scan = scan_storage_path(empty_file)
    directory_scan = scan_storage_path(empty_directory)
    nested_scan = scan_storage_path(nested_empty_directory)

    assert file_scan.content_hash.startswith("v2:file:")
    assert directory_scan.content_hash.startswith("v2:directory:")
    assert file_scan.content_hash != directory_scan.content_hash
    assert directory_scan.content_hash != nested_scan.content_hash


def test_v2_store_path_is_separate_from_legacy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "download_dir", str(tmp_path))
    digest = "c" * 64

    legacy = get_store_path_for_hash(digest)
    v2_file = get_store_path_for_hash(f"v2:file:{digest}")
    v2_directory = get_store_path_for_hash(f"v2:directory:{digest}")

    assert legacy == tmp_path / "store" / "cc" / digest
    assert v2_file == tmp_path / "store" / "v2" / "file" / "cc" / digest
    assert v2_directory == tmp_path / "store" / "v2" / "directory" / "cc" / digest
