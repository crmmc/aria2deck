"""Coverage gaps for app/services/rpc/_shared.py (pure helpers + error paths)."""

from __future__ import annotations

import pytest

from app.domain.errors import BadRequestError, ConflictError, ForbiddenError
from app.domain.torrent_metadata import TorrentFile, TorrentMetadata
from app.services.rpc import _shared
from app.services.rpc._shared import (
    RpcError,
    RpcErrorCode,
    _apply_status_keys,
    _apply_status_keys_to_list,
    _extract_name_from_uri,
    _extract_status_keys,
    _normalize_pagination,
    _parse_history_gid,
    _raise_create_download_error,
    _resource_kind_for_uri,
    _resource_key_for_uri,
    _selected_torrent_indexes,
    _slice_with_offset,
    _validate_submit_options,
    _validate_uri_list,
    _with_rpc_mirrors,
)


def _metadata(file_count: int = 3) -> TorrentMetadata:
    files = tuple(
        TorrentFile(index=i + 1, path=(f"f{i}",), size=10)
        for i in range(file_count)
    )
    return TorrentMetadata(
        info_hash="a" * 40,
        name="t",
        files=files,
        tree=[],
        tracker_urls=(),
        webseed_urls=(),
    )


class TestRpcErrorToDict:
    def test_with_data(self):
        err = RpcError(1, "msg", data={"x": 1})
        assert err.to_dict() == {"code": 1, "message": "msg", "data": {"x": 1}}

    def test_without_data(self):
        assert RpcError(1, "msg").to_dict() == {"code": 1, "message": "msg"}


class TestParseHistoryGid:
    def test_task_gid(self):
        assert _parse_history_gid("task-42") == (None, 42, None)

    def test_hist_gid(self):
        assert _parse_history_gid("hist-7") == (None, None, 7)

    def test_raw_gid(self):
        assert _parse_history_gid("abc123") == ("abc123", None, None)

    def test_invalid_suffix(self):
        assert _parse_history_gid("task-x") == ("task-x", None, None)


class TestRaiseCreateDownloadError:
    def test_passthrough_rpc_error(self):
        err = RpcError(RpcErrorCode.TASK_EXISTS, "x")
        with pytest.raises(RpcError) as exc:
            _raise_create_download_error(1, err)
        assert exc.value.code == RpcErrorCode.TASK_EXISTS

    def test_conflict(self):
        with pytest.raises(RpcError) as exc:
            _raise_create_download_error(1, ConflictError("dup"))
        assert exc.value.code == RpcErrorCode.TASK_EXISTS

    def test_forbidden(self):
        with pytest.raises(RpcError) as exc:
            _raise_create_download_error(1, ForbiddenError("quota"))
        assert exc.value.code == RpcErrorCode.QUOTA_EXCEEDED

    def test_domain_error(self):
        with pytest.raises(RpcError) as exc:
            _raise_create_download_error(1, BadRequestError("bad"))
        assert exc.value.code == RpcErrorCode.INVALID_PARAMS

    def test_value_error(self):
        with pytest.raises(RpcError) as exc:
            _raise_create_download_error(1, ValueError("bad"))
        assert exc.value.code == RpcErrorCode.INVALID_PARAMS

    def test_internal(self):
        with pytest.raises(RpcError) as exc:
            _raise_create_download_error(1, RuntimeError("boom"))
        assert exc.value.code == RpcErrorCode.INTERNAL_ERROR
        assert exc.value.message == _shared.SAFE_INTERNAL_ERROR_MESSAGE


class TestValidateUriList:
    @pytest.mark.asyncio
    async def test_not_a_list(self):
        with pytest.raises(RpcError):
            await _validate_uri_list(
                "x", name="uris", allowed_schemes=frozenset({"http"}), allow_empty=False
            )

    @pytest.mark.asyncio
    async def test_empty_not_allowed(self):
        with pytest.raises(RpcError):
            await _validate_uri_list(
                [], name="uris", allowed_schemes=frozenset({"http"}), allow_empty=False
            )

    @pytest.mark.asyncio
    async def test_too_many(self):
        with pytest.raises(RpcError):
            await _validate_uri_list(
                ["http://a.example/f"] * 100,
                name="uris",
                allowed_schemes=frozenset({"http"}),
                allow_empty=False,
            )

    @pytest.mark.asyncio
    async def test_non_string_item(self):
        with pytest.raises(RpcError):
            await _validate_uri_list(
                [1], name="uris", allowed_schemes=frozenset({"http"}), allow_empty=False
            )

    @pytest.mark.asyncio
    async def test_invalid_magnet(self):
        with pytest.raises(RpcError):
            await _validate_uri_list(
                ["magnet:?xt=urn:btih:zz"],
                name="uris",
                allowed_schemes=frozenset({"http", "magnet"}),
                allow_empty=False,
            )


class TestValidateSubmitOptions:
    def test_none(self):
        _validate_submit_options(None)

    def test_no_out(self):
        _validate_submit_options({"dir": "/tmp"})

    def test_invalid_out(self):
        for bad in ("", ".", "..", "a/b", "a\\b"):
            with pytest.raises(RpcError):
                _validate_submit_options({"out": bad})

    def test_valid_out(self):
        _validate_submit_options({"out": "file.zip"})


def test_with_rpc_mirrors():
    assert _with_rpc_mirrors({"a": 1}, ["u1"]) == {"a": 1}
    assert _with_rpc_mirrors({"a": 1}, ["u1", "u2", "u3"]) == {
        "a": 1,
        "mirrors": ["u2", "u3"],
    }


def test_resource_kind_for_uri():
    assert _resource_kind_for_uri("MAGNET:?xt=x") == "magnet"
    assert _resource_kind_for_uri("https://x.example/f") == "http"
    assert _resource_kind_for_uri("ftp://x/f") == "other"


def test_resource_key_for_uri_invalid_magnet(monkeypatch):
    monkeypatch.setattr(_shared, "get_uri_hash", lambda uri: "")
    with pytest.raises(RpcError):
        _resource_key_for_uri("magnet:?xt=urn:btih:zz")
    assert len(_resource_key_for_uri("ftp://x/f")) == 64


def test_extract_name_from_uri():
    assert _extract_name_from_uri("") == ""
    assert _extract_name_from_uri("http://a.example") == ""
    assert _extract_name_from_uri("http://a.example/a%20b.zip") == "a b.zip"


class TestSelectedTorrentIndexes:
    def test_none_and_empty(self):
        meta = _metadata()
        assert _selected_torrent_indexes(meta, None) == (1, 2, 3)
        assert _selected_torrent_indexes(meta, "") == (1, 2, 3)

    def test_not_string(self):
        with pytest.raises(RpcError):
            _selected_torrent_indexes(_metadata(), 5)

    def test_single(self):
        assert _selected_torrent_indexes(_metadata(), "2") == (2,)

    def test_range(self):
        assert _selected_torrent_indexes(_metadata(), "1-2") == (1, 2)

    @pytest.mark.parametrize(
        "value",
        ["0", "4", "x", "1-", "-2", "3-1", "2-4", ",", "  ,", "1,1,1,1"],
    )
    def test_invalid(self, value):
        with pytest.raises(RpcError):
            _selected_torrent_indexes(_metadata(), value)


class TestExtractStatusKeys:
    def test_missing(self):
        assert _extract_status_keys([], 0) is None

    def test_none(self):
        assert _extract_status_keys([None], 0) is None

    def test_not_list(self):
        with pytest.raises(RpcError):
            _extract_status_keys(["x"], 0)

    def test_filters_non_strings_and_empty(self):
        assert _extract_status_keys([[1, "", "  ", "gid"]], 0) == ["gid"]


class TestNormalizePagination:
    def test_defaults(self):
        assert _normalize_pagination([]) == (0, 1000)

    def test_bad_offset_type(self):
        with pytest.raises(RpcError):
            _normalize_pagination(["0", 1])

    def test_negative_num(self):
        with pytest.raises(RpcError):
            _normalize_pagination([0, -1])


class TestSliceWithOffset:
    def test_num_zero(self):
        assert _slice_with_offset([1, 2, 3], 0, 0) == []

    def test_positive_offset(self):
        assert _slice_with_offset([1, 2, 3, 4], 1, 2) == [2, 3]

    def test_negative_offset(self):
        assert _slice_with_offset([1, 2, 3, 4], -1, 2) == [4, 3]

    def test_negative_offset_underflow(self):
        assert _slice_with_offset([1, 2], -5, 3) == []


def test_apply_status_keys():
    status = {"gid": "g", "status": "active"}
    assert _apply_status_keys(status, None) is status
    assert _apply_status_keys(status, ["gid", "missing"]) == {"gid": "g"}
    assert _apply_status_keys_to_list([status], ["gid"]) == [{"gid": "g"}]


class TestQuotaAndDisk:
    @pytest.mark.asyncio
    async def test_user_available_space_no_user(self, monkeypatch, tmp_path):
        async def fake_get(user_id):
            return None

        monkeypatch.setattr(_shared.auth_repo, "get_user_by_id", fake_get)
        monkeypatch.setattr(_shared.settings, "download_dir", str(tmp_path))
        assert await _shared._get_user_available_space(1) == 0

    @pytest.mark.asyncio
    async def test_user_available_space_zero_quota(self, monkeypatch, tmp_path):
        async def fake_get(user_id):
            return {"quota_bytes": 0}

        monkeypatch.setattr(_shared.auth_repo, "get_user_by_id", fake_get)
        monkeypatch.setattr(_shared.settings, "download_dir", str(tmp_path))
        assert await _shared._get_user_available_space(1) == 0

    @pytest.mark.asyncio
    async def test_check_quota_disk_low_disk(self, monkeypatch, tmp_path):
        monkeypatch.setattr(_shared.settings, "download_dir", str(tmp_path))
        monkeypatch.setattr(_shared.shutil, "disk_usage", lambda p: type("D", (), {"free": 10})())
        monkeypatch.setattr(_shared, "get_min_free_disk", lambda: 100)
        with pytest.raises(RpcError) as exc:
            await _shared._check_quota_and_disk(1)
        assert exc.value.code == RpcErrorCode.QUOTA_EXCEEDED

    @pytest.mark.asyncio
    async def test_check_quota_disk_no_space(self, monkeypatch, tmp_path):
        monkeypatch.setattr(_shared.settings, "download_dir", str(tmp_path))
        monkeypatch.setattr(_shared.shutil, "disk_usage", lambda p: type("D", (), {"free": 10**12})())
        monkeypatch.setattr(_shared, "get_min_free_disk", lambda: 100)
        monkeypatch.setattr(_shared, "_get_user_available_space", _async_const(0))
        with pytest.raises(RpcError) as exc:
            await _shared._check_quota_and_disk(1)
        assert exc.value.code == RpcErrorCode.QUOTA_EXCEEDED


def _async_const(value):
    async def inner(*args, **kwargs):
        return value

    return inner


class TestResolveOwnedRow:
    @pytest.mark.asyncio
    async def test_history_gid(self):
        assert await _shared._resolve_owned_row(1, "hist-9") is None

    @pytest.mark.asyncio
    async def test_task_gid(self, monkeypatch):
        async def fake_get(user_id, task_id):
            return {"id": task_id}

        monkeypatch.setattr(_shared, "get_user_task_by_id", fake_get)
        assert await _shared._resolve_owned_row(1, "task-5") == {"id": 5}

    @pytest.mark.asyncio
    async def test_raw_gid(self, monkeypatch):
        async def fake_get(user_id, gid):
            return {"gid": gid}

        monkeypatch.setattr(_shared, "get_user_task_by_gid", fake_get)
        assert await _shared._resolve_owned_row(1, "abc") == {"gid": "abc"}


@pytest.mark.asyncio
async def test_get_user_available_space_with_usage(monkeypatch, tmp_path):
    async def fake_get(user_id):
        return {"quota_bytes": 100}

    async def fake_usage(user_id, quota):
        return {"available_bytes": 50}

    monkeypatch.setattr(_shared.auth_repo, "get_user_by_id", fake_get)
    monkeypatch.setattr(_shared, "get_usage", fake_usage)
    monkeypatch.setattr(_shared.settings, "download_dir", str(tmp_path))
    monkeypatch.setattr(
        _shared.shutil, "disk_usage", lambda p: type("D", (), {"free": 10**9})()
    )
    assert await _shared._get_user_available_space(1) == 50


@pytest.mark.asyncio
async def test_get_user_quota(monkeypatch):
    async def fake_get(user_id):
        return {"quota_bytes": 77}

    monkeypatch.setattr(_shared.auth_repo, "get_user_by_id", fake_get)
    assert await _shared._get_user_quota(1) == 77

    async def none_get(user_id):
        return None

    monkeypatch.setattr(_shared.auth_repo, "get_user_by_id", none_get)
    assert await _shared._get_user_quota(1) == 0


@pytest.mark.asyncio
async def test_gid_for_created_task():
    assert await _shared._gid_for_created_task({"id": 3}, "k") == "task-3"


@pytest.mark.asyncio
async def test_validate_uri_list_success_and_magnet(monkeypatch):
    async def no_error(uri, *, allowed_schemes):
        return None

    monkeypatch.setattr(_shared, "check_url_ssrf", no_error)
    uris = await _validate_uri_list(
        [
            "http://a.example/f.zip",
            "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=x",
        ],
        name="uris",
        allowed_schemes=frozenset({"http", "https", "magnet"}),
        allow_empty=False,
    )
    assert uris[0] == "http://a.example/f.zip"
    assert uris[1] == "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567"


@pytest.mark.asyncio
async def test_validate_uri_list_ssrf_reject():
    with pytest.raises(RpcError):
        await _validate_uri_list(
            ["http://a.example/f.zip"],
            name="uris",
            allowed_schemes=frozenset({"https"}),
            allow_empty=False,
        )


def test_selected_indexes_range_too_wide():
    with pytest.raises(RpcError):
        _selected_torrent_indexes(_metadata(3), "2,1-3")


def test_resource_key_for_uri_real_hash():
    key = _resource_key_for_uri("magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567")
    assert key == "0123456789abcdef0123456789abcdef01234567"


def test_extract_status_keys_all_filtered():
    assert _extract_status_keys([[1, ""]], 0) is None
