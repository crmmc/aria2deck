"""Coverage gaps for app/services/file_service.py and task_service.py."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.domain.errors import BadRequestError, ForbiddenError, NotFoundError
from app.services import file_service as fs
from app.services import task_service as ts


class TestPureHelpers:
    def test_validate_subpath_empty(self, tmp_path):
        assert fs.validate_subpath(tmp_path, "") == tmp_path.resolve()

    def test_normalize_entry_parent(self):
        assert fs.normalize_entry_parent("") == ""
        with pytest.raises(ForbiddenError):
            fs.normalize_entry_parent("../x")

    def test_validate_display_name(self):
        with pytest.raises(BadRequestError):
            fs.validate_display_name("  ")
        with pytest.raises(BadRequestError):
            fs.validate_display_name("a/b")
        with pytest.raises(BadRequestError):
            fs.validate_display_name(".")


@pytest.mark.asyncio
async def test_list_files_invalid_pagination(temp_db, test_user):
    result = await fs.list_files(
        test_user["id"], 10**12, page=-1, page_size=999
    )
    assert result["files"] == []
    assert result["total"] == 0


@pytest.mark.asyncio
async def test_resolve_download_target_missing_row(temp_db, test_user, monkeypatch):
    async def none_row(user_id, file_hash):
        return None

    monkeypatch.setattr(fs, "get_user_file_by_hash", none_row)
    with pytest.raises(NotFoundError):
        await fs.resolve_download_target(test_user["id"], "nohash")


@pytest.mark.asyncio
async def test_resolve_download_target_missing_file(temp_db, test_user, monkeypatch, tmp_path):
    async def fake_row(user_id, file_hash):
        return {
            "user_file_id": 1,
            "real_path": str(tmp_path / "missing"),
            "is_directory": False,
            "display_name": "f",
        }

    monkeypatch.setattr(fs, "get_user_file_by_hash", fake_row)
    with pytest.raises(NotFoundError):
        await fs.resolve_download_target(test_user["id"], "h")


@pytest.mark.asyncio
async def test_resolve_download_target_plain_file_with_path(temp_db, test_user, monkeypatch, tmp_path):
    f = tmp_path / "f.bin"
    f.write_bytes(b"x")

    async def fake_row(user_id, file_hash):
        return {
            "user_file_id": 1,
            "real_path": str(f),
            "is_directory": False,
            "display_name": "f.bin",
        }

    monkeypatch.setattr(fs, "get_user_file_by_hash", fake_row)
    with pytest.raises(BadRequestError):
        await fs.resolve_download_target(test_user["id"], "h", path="sub")
    target, name = await fs.resolve_download_target(test_user["id"], "h")
    assert name == "f.bin"


@pytest.mark.asyncio
async def test_resolve_file_ids_empty(temp_db, test_user):
    with pytest.raises(BadRequestError):
        await fs.resolve_file_ids(test_user["id"], [])


@pytest.mark.asyncio
async def test_delete_file_by_hash_not_found(temp_db, test_user, monkeypatch):
    async def none_row(user_id, file_hash):
        return None

    monkeypatch.setattr(fs, "get_user_file_by_hash", none_row)
    with pytest.raises(NotFoundError):
        await fs.delete_file_by_hash(test_user["id"], "nohash")


@pytest.mark.asyncio
async def test_delete_user_file_reference_identity_missing(temp_db, monkeypatch):
    from app.repositories import files as files_repo

    async def none_identity(user_id, user_file_id):
        return None

    monkeypatch.setattr(files_repo, "get_user_file_delete_identity", none_identity)
    result = await fs.delete_user_file_reference_v0_result(1, 5)
    assert result.deleted is False


@pytest.mark.asyncio
async def test_bulk_delete_generic_failure(temp_db, test_user, monkeypatch):
    async def boom(user_id, file_hash):
        raise RuntimeError("db down")

    monkeypatch.setattr(fs, "delete_file_by_hash", boom)
    result = await fs.bulk_delete_files_by_hashes(test_user["id"], ["h1"])
    assert result["failed_count"] == 1
    assert result["results"][0]["error"] == "删除受理失败"


class TestTaskServiceGaps:
    @pytest.mark.asyncio
    async def test_list_bad_status_filter(self, temp_db, test_user):
        with pytest.raises(BadRequestError):
            await ts.list_tasks(user_id=test_user["id"], status_filter="weird")

    @pytest.mark.asyncio
    async def test_list_bad_status_filter_page(self, temp_db, test_user):
        with pytest.raises(BadRequestError):
            await ts.list_tasks_page(
                user_id=test_user["id"],
                status_filter="weird",
                page=1,
                page_size=10,
            )

    @pytest.mark.asyncio
    async def test_cancel_task_gateway_failure(self, temp_db, test_user, failed_task, monkeypatch):
        async def boom(**kwargs):
            raise RuntimeError("aria2 down")

        monkeypatch.setattr(ts, "unref", boom)
        from app.domain.errors import BadGatewayError

        with pytest.raises(BadGatewayError):
            await ts.cancel_task(
                user_id=test_user["id"],
                user_task_id=failed_task["id"],
                quota_bytes=10**9,
            )

    @pytest.mark.asyncio
    async def test_bulk_cancel_generic_failure(self, temp_db, test_user, monkeypatch):
        async def boom(**kwargs):
            raise RuntimeError("db down")

        monkeypatch.setattr(ts, "cancel_task", boom)
        result = await ts.bulk_cancel_tasks(
            user_id=test_user["id"], task_ids=[1], quota_bytes=10**9
        )
        assert result["results"][0]["ok"] is False


@pytest.mark.asyncio
async def test_delete_file_by_hash_not_deleted(temp_db, test_user, monkeypatch):
    async def fake_row(user_id, file_hash):
        return {"user_file_id": 7, "real_path": "/x", "display_name": "f"}

    async def fake_result(user_id, user_file_id):
        return fs.DeleteUserFileReferenceResult(False, [])

    monkeypatch.setattr(fs, "get_user_file_by_hash", fake_row)
    monkeypatch.setattr(fs, "delete_user_file_reference_v0_result", fake_result)
    with pytest.raises(NotFoundError):
        await fs.delete_file_by_hash(test_user["id"], "h")
