"""Coverage tests for app/services/storage.py and storage_admin_service.py."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.config import settings
from app.domain.errors import NotFoundError
from app.repositories import storage as storage_repo
from app.services import storage, storage_admin_service
from app.services.storage import (
    cleanup_task_download_dir,
    get_store_path_for_hash,
    get_task_download_dir,
    is_canonical_store_path,
    is_path_within_base,
    safe_delete_path,
    verify_download_dir_writable,
)


# ---------------------------------------------------------------------------
# storage.py helpers
# ---------------------------------------------------------------------------


def test_store_and_downloading_dirs(temp_db: str) -> None:
    store = storage.get_store_dir()
    downloading = storage.get_downloading_dir()
    assert store.is_dir() and store.name == "store"
    assert downloading.is_dir() and downloading.name == "downloading"
    task_dir = get_task_download_dir(12345)
    assert task_dir == downloading / "12345"
    assert task_dir.is_dir()


def test_verify_download_dir_writable_rejects_unwritable_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocked = tmp_path / "ro"
    blocked.mkdir()
    blocked.chmod(0o500)
    try:
        monkeypatch.setattr(settings, "download_dir", str(blocked / "child"))
        with pytest.raises(RuntimeError, match="not writable"):
            verify_download_dir_writable()
    finally:
        blocked.chmod(0o700)


def test_is_path_within_base(tmp_path: Path) -> None:
    base = tmp_path / "base"
    base.mkdir()
    assert is_path_within_base(base, base / "a.txt") is True
    assert is_path_within_base(base, Path("a.txt")) is True
    assert is_path_within_base(base, tmp_path / "other.txt") is False
    assert is_path_within_base(base, base / ".." / "escape.txt") is False


def test_safe_delete_rejects_empty_target(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Empty target"):
        safe_delete_path(base_dir=tmp_path, target="  ")


def test_safe_delete_rejects_outside_base(tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"x")
    with pytest.raises(ValueError, match="outside allowed base"):
        safe_delete_path(base_dir=tmp_path / "base", target=outside)
    (tmp_path / "base").mkdir()
    with pytest.raises(ValueError, match="outside allowed base"):
        safe_delete_path(base_dir=tmp_path / "base", target=tmp_path / "base" / ".." / "outside.txt")


def test_safe_delete_rejects_filesystem_root() -> None:
    with pytest.raises(ValueError, match="filesystem root"):
        safe_delete_path(base_dir=Path("/"), target=Path("/"))


def test_verify_download_dir_writable_succeeds(temp_db: str) -> None:
    verify_download_dir_writable()


def test_safe_delete_rejects_base_without_permission(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="base directory"):
        safe_delete_path(base_dir=tmp_path, target=tmp_path)
    assert safe_delete_path(base_dir=tmp_path, target=tmp_path, allow_delete_base=True) is True


def test_safe_delete_missing_target(tmp_path: Path) -> None:
    assert safe_delete_path(base_dir=tmp_path, target=tmp_path / "missing.txt") is False
    with pytest.raises(FileNotFoundError):
        safe_delete_path(base_dir=tmp_path, target=tmp_path / "missing.txt", allow_missing=False)


def test_safe_delete_symlink(tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"x")
    link = tmp_path / "link"
    link.symlink_to(outside)
    assert safe_delete_path(base_dir=tmp_path, target=link) is True
    assert link.exists() is False and outside.exists() is True


def test_safe_delete_symlink_already_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    link = tmp_path / "link"
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"x")
    link.symlink_to(outside)

    real_unlink = Path.unlink

    def failing_unlink(self, *args, **kwargs):
        if self.name == "link":
            raise FileNotFoundError("gone")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", failing_unlink)
    assert safe_delete_path(base_dir=tmp_path, target=link) is False
    with pytest.raises(FileNotFoundError):
        safe_delete_path(base_dir=tmp_path, target=link, allow_missing=False)


def test_safe_delete_directory_recursive_and_plain(tmp_path: Path) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "child.txt").write_bytes(b"x")
    assert safe_delete_path(base_dir=tmp_path, target=nested, recursive=True) is True
    assert nested.exists() is False

    empty = tmp_path / "empty"
    empty.mkdir()
    assert safe_delete_path(base_dir=tmp_path, target=empty, recursive=False) is True
    assert empty.exists() is False


def test_safe_delete_directory_missing_during_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "dir"
    target.mkdir()

    def failing_rmtree(path, *args, **kwargs):
        raise FileNotFoundError("gone")

    with patch.object(storage.shutil, "rmtree", failing_rmtree):
        assert safe_delete_path(base_dir=tmp_path, target=target, recursive=True) is False
        with pytest.raises(FileNotFoundError):
            safe_delete_path(base_dir=tmp_path, target=target, recursive=True, allow_missing=False)


def test_safe_delete_file_missing_during_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "file.txt"
    target.write_bytes(b"x")

    real_unlink = Path.unlink

    def failing_unlink(self, *args, **kwargs):
        if self.name == "file.txt":
            raise FileNotFoundError("gone")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", failing_unlink)
    assert safe_delete_path(base_dir=tmp_path, target=target) is False
    with pytest.raises(FileNotFoundError):
        safe_delete_path(base_dir=tmp_path, target=target, allow_missing=False)
    monkeypatch.setattr(Path, "unlink", real_unlink)


def test_safe_delete_plain_file(tmp_path: Path) -> None:
    target = tmp_path / "file.txt"
    target.write_bytes(b"x")
    assert safe_delete_path(base_dir=tmp_path, target=target) is True
    assert target.exists() is False


def test_get_store_path_for_hash_versions(temp_db: str) -> None:
    legacy = get_store_path_for_hash("abcdef1234")
    assert legacy == storage.get_store_dir() / "ab" / "abcdef1234"
    with pytest.raises(ValueError, match="invalid legacy content key"):
        get_store_path_for_hash("../escape")
    with pytest.raises(ValueError, match="invalid legacy content key"):
        get_store_path_for_hash("")

    v2 = get_store_path_for_hash("v2:file:" + "0" * 64)
    assert v2 == storage.get_store_dir() / "v2" / "file" / "00" / ("0" * 64)
    assert is_canonical_store_path(v2, "v2:file:" + "0" * 64) is True
    assert is_canonical_store_path(v2.parent / "other", "v2:file:" + "0" * 64) is False


def test_cleanup_task_download_dir_success(temp_db: str) -> None:
    task_dir = get_task_download_dir(98765)
    (task_dir / "part.bin").write_bytes(b"x")
    asyncio.run(cleanup_task_download_dir(98765))
    assert task_dir.exists() is False


def test_cleanup_task_download_dir_wraps_failure(
    temp_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing_delete(**kwargs):
        raise ValueError("boom")

    monkeypatch.setattr(storage, "safe_delete_path", failing_delete)
    with pytest.raises(RuntimeError, match="Failed to clean up task directory"):
        asyncio.run(cleanup_task_download_dir(55555))


# ---------------------------------------------------------------------------
# storage_admin_service
# ---------------------------------------------------------------------------


def test_get_file_users_not_found(temp_db: str) -> None:
    with pytest.raises(NotFoundError, match="存储文件不存在"):
        asyncio.run(storage_admin_service.get_file_users(999999))


def test_get_file_users_returns_owners(user_file: dict, test_user: dict) -> None:
    result = asyncio.run(storage_admin_service.get_file_users(user_file["stored_file_id"]))
    assert result["file_id"] == user_file["stored_file_id"]
    assert [entry["username"] for entry in result["users"]] == [test_user["username"]]


def test_list_stored_files_pagination(user_file: dict) -> None:
    result = asyncio.run(
        storage_admin_service.list_stored_files("", False, page=1, page_size=5)
    )
    assert result["total"] >= 1
    entry = result["files"][0]
    assert entry["exists_on_disk"] is True
    assert entry["cleanup_state"] == "active"
    clamped = asyncio.run(
        storage_admin_service.list_stored_files("", False, page=0, page_size=500)
    )
    assert clamped["page"] == 1 and clamped["page_size"] == 100


def test_bulk_delete_reports_missing_and_referenced(user_file: dict) -> None:
    result = asyncio.run(storage_admin_service.bulk_delete_files([999999, user_file["stored_file_id"]]))
    assert result["accepted_count"] == 0
    assert result["failed_ids"] == [999999, user_file["stored_file_id"]]
    states = {item["file_id"]: item["state"] for item in result["results"]}
    assert states[999999] == "not_found"
    assert states[user_file["stored_file_id"]] == "referenced"


def test_bulk_delete_conflict_when_state_changes(user_file: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        storage_repo, "delete_orphan_stored_file", AsyncMock(return_value=None)
    )
    result = asyncio.run(
        storage_admin_service.bulk_delete_files([user_file["stored_file_id"]])
    )
    assert result["results"][0]["state"] == "conflict"


def test_bulk_delete_accepts_orphan_and_wakes_cleanup(
    user_file: dict, test_user: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.db.engine import transaction
    from app.db.schema import user_files

    async def detach() -> None:
        async with transaction() as conn:
            await conn.execute(user_files.delete())

    asyncio.run(detach())

    wake = MagicMock()
    monkeypatch.setattr(
        "app.services.deletion_cleanup.DeletionCleanupManager.wake", wake
    )
    result = asyncio.run(
        storage_admin_service.bulk_delete_files([user_file["stored_file_id"]])
    )
    assert result["accepted_count"] == 1
    assert result["results"][0]["state"] == "pending"
    wake.assert_called_once()


def test_bulk_delete_generic_failure(user_file: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args, **kwargs):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(storage_repo, "get_stored_file", boom)
    result = asyncio.run(storage_admin_service.bulk_delete_files([user_file["stored_file_id"]]))
    assert result["results"][0]["state"] == "failed"
    assert result["errors"] == [f"删除受理失败: {user_file['stored_file_id']}"]


def test_bulk_delete_broadcasts_affected_downloads(
    user_file: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_delete_orphan(file_id, expected_content_hash=None):
        return ("hash", "/tmp/x", [42, 43])

    monkeypatch.setattr(storage_repo, "delete_orphan_stored_file", fake_delete_orphan)
    broadcast = AsyncMock()
    monkeypatch.setattr(storage_admin_service, "broadcast_task_update_to_subscribers", broadcast)
    monkeypatch.setattr(
        "app.services.deletion_cleanup.DeletionCleanupManager.wake", MagicMock()
    )
    result = asyncio.run(
        storage_admin_service.bulk_delete_files([user_file["stored_file_id"]])
    )
    assert result["accepted_count"] == 1
    assert broadcast.await_count == 2
