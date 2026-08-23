"""Coverage gaps for app/services/share_service.py."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.domain.errors import BadRequestError, GoneError, NotFoundError
from app.repositories.errors import RepositoryConflictError
from app.services import share_service as svc
from app.services.share_service import (
    bulk_delete_shares,
    consume_share_download,
    create_access_token,
    delete_share,
    record_shared_download,
    resolve_shared_download_target,
    revoke_all_shares,
    revoke_share,
    verify_access_token,
)


def test_verify_access_token_invalid():
    token = create_access_token("abc12345")
    assert verify_access_token("abc12345", token) is True
    assert verify_access_token("other123", token) is False
    assert verify_access_token("abc12345", "not-a-jwt") is False


@pytest.mark.asyncio
async def test_create_share_target_inactive(monkeypatch, tmp_path):
    async def inactive(*args, **kwargs):
        raise svc.shares_repo.ShareTargetInactiveError

    monkeypatch.setattr(svc.shares_repo, "get_owned_file", _async_row({"display_name": "f", "size_bytes": 1}))
    monkeypatch.setattr(svc.shares_repo, "create_share_with_retry", inactive)
    with pytest.raises(NotFoundError):
        await svc.create_share(
            user_id=1, user_file_id=1, password=None, expires_in=None, max_downloads=None
        )


@pytest.mark.asyncio
async def test_create_share_conflict(monkeypatch):
    async def conflict(*args, **kwargs):
        raise RepositoryConflictError

    monkeypatch.setattr(svc.shares_repo, "get_owned_file", _async_row({"display_name": "f", "size_bytes": 1}))
    monkeypatch.setattr(svc.shares_repo, "create_share_with_retry", conflict)
    from app.domain.errors import InternalDomainError

    with pytest.raises(InternalDomainError):
        await svc.create_share(
            user_id=1, user_file_id=1, password=None, expires_in=None, max_downloads=None
        )


def _async_row(row):
    async def inner(*args, **kwargs):
        return row

    return inner


@pytest.mark.asyncio
async def test_revoke_share_gone(monkeypatch):
    async def none_row(*args, **kwargs):
        return None

    monkeypatch.setattr(svc.shares_repo, "get_share_status_for_owner", none_row)
    with pytest.raises(NotFoundError):
        await revoke_share(1, 1)


@pytest.mark.asyncio
async def test_delete_share_not_found(monkeypatch):
    monkeypatch.setattr(svc.shares_repo, "delete_share", _async_row(False))
    with pytest.raises(NotFoundError):
        await delete_share(1, 1)


@pytest.mark.asyncio
async def test_bulk_delete_generic_failure(monkeypatch):
    async def boom(share_id, user_id):
        raise RuntimeError("db down")

    monkeypatch.setattr(svc.shares_repo, "delete_share", boom)
    result = await bulk_delete_shares(1, [1, 1])
    assert result["accepted_count"] == 0
    assert result["failed_count"] == 1
    assert result["results"][0]["error"] == "删除分享失败"


@pytest.mark.asyncio
async def test_revoke_all_shares(monkeypatch):
    monkeypatch.setattr(svc.shares_repo, "revoke_all_shares", _async_row(3))
    assert await revoke_all_shares(1) == {"ok": True, "count": 3}


@pytest.mark.asyncio
async def test_get_share_with_file_gone(monkeypatch):
    monkeypatch.setattr(
        svc.shares_repo, "get_share_with_file", _async_row((None, True))
    )
    with pytest.raises(GoneError):
        await svc.get_share_with_file("code")


@pytest.mark.asyncio
async def test_access_share_no_password(monkeypatch):
    share = {
        "status": "active",
        "expires_at_ms": None,
        "max_downloads": None,
        "download_count": 0,
        "password_hash": None,
    }
    monkeypatch.setattr(svc, "get_share_with_file", _async_row(share))
    with pytest.raises(BadRequestError):
        await svc.access_share("code", "pw")


class TestResolveSharedDownloadTarget:
    @pytest.mark.asyncio
    async def test_missing_file(self, tmp_path):
        with pytest.raises(NotFoundError):
            await resolve_shared_download_target(
                {"real_path": str(tmp_path / "nope")}, subpath=None
            )

    @pytest.mark.asyncio
    async def test_subpath_on_plain_file(self, tmp_path):
        f = tmp_path / "f.bin"
        f.write_bytes(b"x")
        with pytest.raises(BadRequestError):
            await resolve_shared_download_target(
                {"real_path": str(f), "is_directory": False}, subpath="a"
            )

    @pytest.mark.asyncio
    async def test_subpath_missing(self, tmp_path):
        d = tmp_path / "d"
        d.mkdir()
        with pytest.raises(NotFoundError):
            await resolve_shared_download_target(
                {"real_path": str(d), "is_directory": True}, subpath="missing.txt"
            )

    @pytest.mark.asyncio
    async def test_subpath_is_dir(self, tmp_path):
        d = tmp_path / "d"
        d.mkdir()
        (d / "sub").mkdir()
        with pytest.raises(BadRequestError):
            await resolve_shared_download_target(
                {"real_path": str(d), "is_directory": True}, subpath="sub"
            )

    @pytest.mark.asyncio
    async def test_subpath_ok(self, tmp_path):
        d = tmp_path / "d"
        d.mkdir()
        (d / "a.bin").write_bytes(b"x")
        target, name = await resolve_shared_download_target(
            {"real_path": str(d), "is_directory": True}, subpath="a.bin"
        )
        assert name == "a.bin"
        assert Path(target).is_file()

    @pytest.mark.asyncio
    async def test_directory_requires_subpath(self, tmp_path):
        d = tmp_path / "d"
        d.mkdir()
        with pytest.raises(BadRequestError):
            await resolve_shared_download_target(
                {"real_path": str(d), "is_directory": True, "file_name": None},
                subpath=None,
            )


@pytest.mark.asyncio
async def test_consume_share_download_exhausted(monkeypatch):
    monkeypatch.setattr(svc.shares_repo, "consume_share_download", _async_row(False))
    with pytest.raises(GoneError):
        await consume_share_download(1)


@pytest.mark.asyncio
async def test_record_shared_download_exhausted(monkeypatch):
    monkeypatch.setattr(
        svc.shares_repo, "touch_and_maybe_count_download", _async_row(False)
    )
    with pytest.raises(GoneError):
        await record_shared_download({"id": 1}, should_count_download=True)


@pytest.mark.asyncio
async def test_browse_shared_directory_not_dir(monkeypatch):
    share = {
        "id": 1,
        "stored_file_id": 1,
        "is_directory": False,
        "status": "active",
        "expires_at_ms": None,
        "max_downloads": None,
        "download_count": 0,
        "password_hash": None,
    }
    monkeypatch.setattr(svc, "get_share_with_file", _async_row(share))
    with pytest.raises(BadRequestError):
        await svc.browse_shared_directory("code", None, "")
