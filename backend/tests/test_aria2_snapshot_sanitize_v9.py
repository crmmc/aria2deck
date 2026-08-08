"""M3 T02：aria2 快照脱敏共享层测试

覆盖 ``app/services/aria2_snapshot_sanitize``：
- 敏感字段脱敏（dir 清空、errorMessage 归一、路径 basename、uri 脱敏）
- BT 字段保留规则与现状一致
"""

from __future__ import annotations

import pytest

from app.services.aria2_snapshot_sanitize import (
    sanitize_bittorrent,
    sanitize_files,
    sanitize_status,
    sanitize_uris,
)


class TestSanitizeStatus:
    def test_dir_cleared_and_error_message_normalized(self) -> None:
        raw = {
            "gid": "abc123",
            "status": "error",
            "dir": "/data/downloads/secret",
            "errorMessage": "Connection refused (private detail)",
        }
        result = sanitize_status(raw)
        assert result["dir"] == ""
        assert result["errorMessage"] == "aria2 下载失败"
        assert result["gid"] == "abc123"
        assert result["status"] == "error"

    def test_error_message_empty_when_not_provided(self) -> None:
        result = sanitize_status({"gid": "g", "status": "active"})
        assert result["errorMessage"] == ""

    def test_invalid_status_normalized_to_waiting(self) -> None:
        result = sanitize_status({"gid": "g", "status": "malicious-status"})
        assert result["status"] == "waiting"

    def test_numeric_fields_stringified(self) -> None:
        result = sanitize_status(
            {
                "gid": "g",
                "status": "active",
                "totalLength": 1024,
                "completedLength": 512,
                "downloadSpeed": 100,
                "uploadSpeed": 50,
            }
        )
        assert result["totalLength"] == "1024"
        assert result["completedLength"] == "512"
        assert result["downloadSpeed"] == "100"
        assert result["uploadSpeed"] == "50"


class TestSanitizeFiles:
    def test_file_path_reduced_to_basename(self) -> None:
        files = [
            {
                "index": "1",
                "path": "/data/downloads/secret/report.pdf",
                "length": "100",
                "completedLength": "40",
                "selected": True,
            }
        ]
        result = sanitize_files(files)
        assert result[0]["path"] == "report.pdf"
        assert result[0]["selected"] == "true"

    def test_uri_like_path_kept(self) -> None:
        files = [{"path": "magnet:?xt=urn:btih:abc", "length": "0"}]
        result = sanitize_files(files)
        assert result[0]["path"] == "magnet:?xt=urn:btih:abc"

    def test_non_list_returns_empty(self) -> None:
        assert sanitize_files(None) == []
        assert sanitize_files("not-a-list") == []
        assert sanitize_files({"a": 1}) == []

    def test_skips_non_dict_entries(self) -> None:
        result = sanitize_files(["bad", 42, {"index": "3", "path": "/x/f.txt"}])
        assert len(result) == 1
        assert result[0]["index"] == "3"


class TestSanitizeUris:
    def test_uri_cleared_status_kept(self) -> None:
        uris = [
            {"uri": "http://internal.host/secret", "status": "used"},
            {"uri": "https://other", "status": "waiting"},
        ]
        result = sanitize_uris(uris)
        assert result[0]["uri"] == ""
        assert result[0]["status"] == "used"
        assert result[1]["uri"] == ""
        assert result[1]["status"] == "waiting"

    def test_invalid_status_becomes_waiting(self) -> None:
        result = sanitize_uris([{"uri": "http://x", "status": "bad"}])
        assert result[0]["status"] == "waiting"

    def test_non_list_returns_empty(self) -> None:
        assert sanitize_uris(None) == []
        assert sanitize_uris("x") == []


class TestSanitizeBittorrent:
    def test_name_and_mode_preserved(self) -> None:
        result = sanitize_bittorrent({"info": {"name": "My Torrent"}, "mode": "multi"})
        assert result["info"]["name"] == "My Torrent"
        assert result["mode"] == "multi"

    def test_announce_list_placeholder(self) -> None:
        result = sanitize_bittorrent({"info": {"name": "x"}})
        assert result["announceList"] == [["http://aria2deck.invalid/announce"]]
        assert result["comment"] == ""
        assert result["creationDate"] == 0

    def test_non_dict_returns_defaults(self) -> None:
        result = sanitize_bittorrent(None)
        assert result["info"]["name"] == ""
        assert result["mode"] == "single"

    def test_empty_mode_defaults_to_single(self) -> None:
        result = sanitize_bittorrent({"info": {"name": "n"}, "mode": ""})
        assert result["mode"] == "single"


class TestSanitizeStatusBittorrentFields:
    def test_bt_fields_only_when_bt_evidence(self) -> None:
        # 无 BT 证据时不注入 BT 字段
        plain = sanitize_status({"gid": "g", "status": "active"})
        assert "infoHash" not in plain
        assert "bittorrent" not in plain

        # 有 infoHash 时注入
        bt = sanitize_status(
            {
                "gid": "g",
                "status": "active",
                "infoHash": "a" * 40,
                "bittorrent": {"info": {"name": "bt"}, "mode": "single"},
            }
        )
        assert bt["infoHash"] == "a" * 40
        assert bt["bittorrent"]["info"]["name"] == "bt"

    def test_followed_by_and_belongs_to_kept(self) -> None:
        result = sanitize_status(
            {
                "gid": "g",
                "status": "active",
                "infoHash": "a" * 40,
                "followedBy": ["gid1", "gid2"],
                "belongsTo": "parent",
                "following": "child",
            }
        )
        assert result["followedBy"] == ["gid1", "gid2"]
        assert result["belongsTo"] == "parent"
        assert result["following"] == "child"

    def test_bitfield_kept_when_string(self) -> None:
        result = sanitize_status(
            {
                "gid": "g",
                "status": "active",
                "infoHash": "a" * 40,
                "bitfield": "ff00",
            }
        )
        assert result["bitfield"] == "ff00"
