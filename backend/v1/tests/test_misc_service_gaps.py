"""Coverage gaps across small service/helper modules."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest as _pytest

# --- download_ops ---


def test_safe_int_gaps():
    from app.services.download_ops import safe_int

    assert safe_int(None, default=7) == 7
    assert safe_int("abc") == 0


def test_first_followed_gid_gaps():
    from app.services.download_ops import first_followed_gid

    assert first_followed_gid({}) is None
    assert first_followed_gid({"followedBy": "x"}) is None
    assert first_followed_gid({"followedBy": ["", 5]}) == "5"


def test_is_metadata_handoff_pending_gaps():
    from app.services.download_ops import is_metadata_handoff_pending

    download = {"resource_kind": "magnet"}
    assert not is_metadata_handoff_pending(download, {"status": "active"})
    assert not is_metadata_handoff_pending(
        {"resource_kind": "http"}, {"status": "complete"}
    )
    assert not is_metadata_handoff_pending(
        download, {"status": "complete", "followedBy": ["g1"]}
    )
    assert not is_metadata_handoff_pending(
        download, {"status": "complete", "following": "g0"}
    )
    assert is_metadata_handoff_pending(download, {"status": "complete"})
    # magnet via source_uri fallback
    assert not is_metadata_handoff_pending(
        {"source_uri": "https://x/f"}, {"status": "complete", "following": "g"}
    )


# --- hash ---


def test_extract_info_hash_base32_failure():
    from app.services.hash import extract_info_hash_from_magnet

    # 32 chars matching pattern charset but not decodable base32
    assert extract_info_hash_from_magnet("magnet:?xt=urn:btih:0123456789abcdefghijklmnopqrstuv") is None


def test_find_bencode_end_gaps():
    from app.services.hash import _find_bencode_end

    assert _find_bencode_end(b"", 0) == -1
    assert _find_bencode_end(b"i12e", 0, depth=101) == -1
    assert _find_bencode_end(b"i12", 0) == -1
    assert _find_bencode_end(b"x:ab", 0) == -1
    assert _find_bencode_end(b"3:abc", 0) == 5
    assert _find_bencode_end(b"le", 0) == 2
    assert _find_bencode_end(b"d3:abc", 0) == -1
    assert _find_bencode_end(b"x", 0) == -1
    assert _find_bencode_end(b"d1:ax", 0) == -1
    assert _find_bencode_end(b"1x:abc", 0) == -1
    assert _find_bencode_end(b"dz", 0) == -1


def test_calculate_file_hash_on_dir_raises(tmp_path):
    from app.services.hash import calculate_directory_content_hash, calculate_file_content_hash

    d = tmp_path / "d"
    d.mkdir()
    with pytest.raises(ValueError):
        calculate_file_content_hash(d)
    f = tmp_path / "f"
    f.write_bytes(b"x")
    with pytest.raises(ValueError):
        calculate_directory_content_hash(f)


# --- http_probe ---


def test_parse_content_disposition_bad_charset():
    from app.services.http_probe import _parse_content_disposition

    assert _parse_content_disposition("") is None
    assert _parse_content_disposition("inline") is None
    assert (
        _parse_content_disposition("attachment; filename*=no-such-charset''a%20b.txt")
        is None
    )
    assert (
        _parse_content_disposition("attachment; filename*=utf-8''a%20b.txt") == "a b.txt"
    )
    assert _parse_content_disposition('attachment; filename="x.zip"') == "x.zip"
    assert _parse_content_disposition("attachment; filename=x.zip") == "x.zip"


def test_extract_filename_from_url_gaps():
    from app.services.http_probe import _extract_filename_from_url

    assert _extract_filename_from_url("http://x.example/") is None
    assert _extract_filename_from_url("http://x.example/noext") is None
    assert _extract_filename_from_url("http://x.example/a.zip") == "a.zip"


@pytest.mark.asyncio
async def test_probe_request_unsafe_target():
    from app.services.http_probe import _probe_request, ProbeResult

    result = await _probe_request(None, "HEAD", "ftp://not-http/x", 10)
    assert isinstance(result, ProbeResult)
    assert result.success is False


# --- task_projection ---


def test_display_name_fallbacks():
    from app.services.task_projection import display_name

    row = {"id": 9, "source_uri": "", "display_name": None}
    assert display_name(row) == "task-9"
    row = {"id": 9, "source_uri": "magnet:?xt=urn:btih:" + "a" * 40, "display_name": None}
    assert display_name(row) == "magnet:?xt=urn:btih:" + "a" * 40


def test_display_total_gaps():
    from app.services.task_projection import _display_total

    assert _display_total(db_total=5, size_known=False, live_total=None, active_like=True) == 5
    assert _display_total(db_total=5, size_known=False, live_total=0, active_like=True) == 5
    assert _display_total(db_total=5, size_known=True, live_total=99, active_like=True) == 5


def test_bt_helpers_gaps():
    from app.services.task_projection import (
        _hex_info_hash_parts,
        has_bittorrent_payload,
        has_live_bt_evidence,
        is_metadata_phase_status,
    )

    assert has_bittorrent_payload(None) is False
    assert has_bittorrent_payload({"bittorrent": "x"}) is False
    assert has_bittorrent_payload({"bittorrent": {"mode": "x"}}) is True
    assert _hex_info_hash_parts(None) == []
    assert _hex_info_hash_parts("zz no hash") == []
    assert has_live_bt_evidence(None) is False
    assert has_live_bt_evidence({}) is False
    assert is_metadata_phase_status({}) is False
    assert is_metadata_phase_status({"files": "no"}) is False
    assert is_metadata_phase_status({"files": ["x"]}) is False
    assert is_metadata_phase_status({"files": [{"path": "[METADATA]x"}]}) is True


# --- auth_service ---


@pytest.mark.asyncio
async def test_change_password_user_deleted_midway(temp_db, test_user, monkeypatch):
    from app.auth import AuthUser
    from app.repositories import auth as auth_repo
    from app.services import auth_service

    async def no_change(**kwargs):
        return None

    async def none_user(user_id):
        return None

    monkeypatch.setattr(auth_repo, "change_password_and_replace_session", no_change)
    monkeypatch.setattr(auth_repo, "get_user_by_id", none_user)
    user = AuthUser(
        id=test_user["id"],
        username=test_user["username"],
        password_hash="x",
        is_admin=False,
        quota=1,
        quota_bytes=1,
        is_initial_password=True,
    )
    from app.domain.errors import NotFoundError

    with pytest.raises(NotFoundError):
        await auth_service.change_password(
            user=user,
            old_password="a",
            new_password="newpass123",
            request_id="r",
        )


# --- rate_limit_service ---


@pytest.mark.asyncio
async def test_ensure_account_security_disabled(monkeypatch):
    from app.core import rate_limit_config
    from app.services import rate_limit_service as rls

    monkeypatch.setattr(rls.rate_limit_config, "limit_for", lambda scope: 0)
    await rls.ensure_account_security_allowed("1.2.3.4")


# --- history_retention ---


@pytest.mark.asyncio
async def test_expire_history_bad_days():
    from app.domain.errors import BadRequestError
    from app.services.history_retention import purge_by_cutoff

    with pytest.raises(BadRequestError):
        await purge_by_cutoff(older_than_days=0)


@pytest.mark.asyncio
async def test_history_retention_worker_swallows(monkeypatch):
    from app.services import history_retention as hr

    calls = {"n": 0}

    async def failing(**kwargs):
        calls["n"] += 1
        raise RuntimeError("db down")

    async def cancel_soon(*args, **kwargs):
        raise __import__("asyncio").CancelledError

    monkeypatch.setattr(hr, "soft_expire_due_history", failing)
    monkeypatch.setattr(hr.asyncio, "sleep", cancel_soon)
    with pytest.raises(__import__("asyncio").CancelledError):
        await hr.history_retention_worker(interval_seconds=0.01)
    assert calls["n"] >= 1


# --- history_service bulk generic failure ---


@pytest.mark.asyncio
async def test_bulk_delete_history_generic_failure(monkeypatch, temp_db):
    from app.services import history_service as hs

    async def boom(user_id, history_id):
        raise RuntimeError("db down")

    monkeypatch.setattr(hs, "delete_history", boom)
    result = await hs.bulk_delete_history(1, [1])
    assert result["failed_count"] == 1
    assert result["results"][0]["error"] == "删除历史记录失败"


# --- tracker_list_service ---


def test_decode_json_list_gaps():
    from app.services.tracker_list_service import _decode_json_list, get_bt_tracker_option

    assert _decode_json_list(None) == []
    assert _decode_json_list("{bad json") == []
    assert _decode_json_list('{"a":1}') == []
    assert _decode_json_list("[1,2]") == [1, 2]
    assert get_bt_tracker_option() is None


@pytest.mark.asyncio
async def test_fetch_source_warns_invalid(monkeypatch, caplog):
    from app.services import tracker_list_service as tls

    async def fake_fetch(url):
        return "http://ok/ann\nftp://bad/ann"

    monkeypatch.setattr(tls, "_fetch_url", fake_fetch)
    with caplog.at_level("WARNING"):
        assert await tls._fetch_source("http://ok") == ["http://ok/ann"]


@pytest.mark.asyncio
async def test_fetch_source_failure_reraises(monkeypatch):
    from app.services import tracker_list_service as tls

    async def fake_fetch(url):
        raise RuntimeError("network down")

    monkeypatch.setattr(tls, "_fetch_url", fake_fetch)
    with pytest.raises(RuntimeError):
        await tls._fetch_source("http://ok")


@pytest.mark.asyncio
async def test_refresher_iteration_refresh_failure(monkeypatch):
    from app.services import tracker_list_service as tls

    from app.services import settings_service

    monkeypatch.setattr(
        settings_service, "get_config_value_sync", lambda key: "10"
    )

    async def failing():
        raise RuntimeError("refresh failed")

    monkeypatch.setattr(tls, "refresh_remote_trackers", failing)
    assert await tls._refresher_iteration() == 600


# --- token_service ---


@pytest.mark.asyncio
async def test_create_token_loop_exhausted(temp_db, test_user, monkeypatch):
    from app.repositories import auth as auth_repo
    from app.domain.errors import BadRequestError
    from app.services import token_service

    async def dup(*args, **kwargs):
        raise auth_repo.DuplicateCredentialError

    monkeypatch.setattr(auth_repo, "create_api_token", dup)
    with pytest.raises(BadRequestError):
        await token_service.create_token(test_user["id"], "t")


# --- schemas ---


def test_user_create_validator_gaps():
    import pytest as _pytest

    from app.schemas import UserCreate

    with _pytest.raises(ValueError):
        UserCreate(username="", password="pass1234")
    with _pytest.raises(ValueError):
        UserCreate(username="bad name!", password="pass1234")
    assert UserCreate(username="合法用户_1", password="pass1234").username == "合法用户_1"


# --- domain: content_identity / quota / task_policy ---


def test_content_identity_gaps():
    from app.domain.content_identity import (
        content_identity_from_content_hash,
        content_identity_from_row,
    )

    identity = content_identity_from_content_hash("v2:file:" + "a" * 64)
    assert identity.content_hash == "v2:file:" + "a" * 64
    assert content_identity_from_content_hash("plainhash").digest == "plainhash"

    row = {"content_hash": "plainhash"}
    assert content_identity_from_row(row).digest == "plainhash"
    bad = dict(row, content_hash_version="v9")
    with _pytest.raises(ValueError):
        content_identity_from_row(bad)
    with _pytest.raises(ValueError):
        content_identity_from_content_hash("v2:file")


def test_quota_gaps():
    from app.domain.quota import candidate_size_from_status

    status = {"files": ["not-a-mapping"], "totalLength": "0"}
    assert candidate_size_from_status(status) is None
    assert candidate_size_from_status({}) is None
    assert candidate_size_from_status({"completedLength": "5"}) == (5, 5)
    files = [{"selected": "false", "length": "10"}]
    assert candidate_size_from_status(
        {"files": files, "totalLength": "0", "completedLength": "0"}
    ) is None


def test_task_policy_gaps():
    from app.domain.task_policy import filter_rows_for_status

    rows = [{"status": "waiting"}]
    assert filter_rows_for_status(rows, None) == rows
    from app.domain.task_policy import InvalidTaskStatusFilter

    with _pytest.raises(InvalidTaskStatusFilter):
        filter_rows_for_status(rows, "waiting")
