"""Tests for hash utility functions."""

import base64
import hashlib
import pytest
from pathlib import Path

from app.services.hash import (
    extract_info_hash_from_magnet,
    extract_info_hash_from_torrent,
    extract_info_hash_from_torrent_base64,
    _find_bencode_end,
    calculate_url_hash,
    calculate_file_content_hash,
    calculate_directory_content_hash,
    calculate_content_hash,
    get_uri_hash,
    is_magnet_link,
    is_http_url,
)


class TestExtractInfoHashFromMagnet:
    """Tests for extract_info_hash_from_magnet()"""

    def test_valid_hex_hash(self):
        """Test extraction of 40-char hex hash"""
        magnet = "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=test"
        result = extract_info_hash_from_magnet(magnet)
        assert result == "0123456789abcdef0123456789abcdef01234567"

    def test_valid_hex_hash_uppercase(self):
        """Test extraction of uppercase hex hash (should return lowercase)"""
        magnet = "magnet:?xt=urn:btih:0123456789ABCDEF0123456789ABCDEF01234567&dn=test"
        result = extract_info_hash_from_magnet(magnet)
        assert result == "0123456789abcdef0123456789abcdef01234567"

    def test_valid_base32_hash(self):
        """Test extraction of 32-char base32 hash"""
        # Base32 encode a 20-byte hash
        raw_hash = bytes.fromhex("0123456789abcdef0123456789abcdef01234567")
        base32_hash = base64.b32encode(raw_hash).decode("ascii")
        magnet = f"magnet:?xt=urn:btih:{base32_hash}&dn=test"
        result = extract_info_hash_from_magnet(magnet)
        assert result == "0123456789abcdef0123456789abcdef01234567"

    def test_invalid_magnet_no_xt(self):
        """Test magnet link without xt parameter"""
        magnet = "magnet:?dn=test&tr=http://tracker.example.com"
        result = extract_info_hash_from_magnet(magnet)
        assert result is None

    def test_invalid_magnet_wrong_urn(self):
        """Test magnet link with wrong URN type"""
        magnet = "magnet:?xt=urn:sha1:0123456789abcdef0123456789abcdef01234567&dn=test"
        result = extract_info_hash_from_magnet(magnet)
        assert result is None

    def test_non_magnet_uri(self):
        """Test non-magnet URI returns None"""
        result = extract_info_hash_from_magnet("https://example.com/file.torrent")
        assert result is None

    def test_empty_string(self):
        """Test empty string returns None"""
        result = extract_info_hash_from_magnet("")
        assert result is None

    def test_magnet_with_multiple_xt(self):
        """Test magnet with multiple xt parameters"""
        magnet = "magnet:?xt=urn:sha1:invalid&xt=urn:btih:0123456789abcdef0123456789abcdef01234567"
        result = extract_info_hash_from_magnet(magnet)
        assert result == "0123456789abcdef0123456789abcdef01234567"


class TestExtractInfoHashFromTorrent:
    """Tests for extract_info_hash_from_torrent()"""

    def test_valid_torrent(self):
        """Test extraction from valid torrent data"""
        # Create a minimal valid torrent structure
        # d8:announce...4:infod4:name4:test12:piece lengthi16384e6:pieces20:...ee
        info_dict = (
            b"d4:name4:test12:piece lengthi16384e6:pieces20:01234567890123456789e"
        )
        torrent = b"d8:announce25:http://tracker.example.com4:info" + info_dict + b"e"

        result = extract_info_hash_from_torrent(torrent)
        expected = hashlib.sha1(info_dict).hexdigest().lower()
        assert result == expected

    def test_invalid_torrent_no_info(self):
        """Test torrent without info dict"""
        torrent = b"d8:announce25:http://tracker.example.come"
        result = extract_info_hash_from_torrent(torrent)
        assert result is None

    def test_corrupted_torrent(self):
        """Test corrupted torrent data"""
        result = extract_info_hash_from_torrent(b"not a torrent")
        assert result is None

    def test_empty_data(self):
        """Test empty data"""
        result = extract_info_hash_from_torrent(b"")
        assert result is None


class TestExtractInfoHashFromTorrentBase64:
    """Tests for extract_info_hash_from_torrent_base64()"""

    def test_valid_base64_torrent(self):
        """Test extraction from base64-encoded torrent"""
        info_dict = (
            b"d4:name4:test12:piece lengthi16384e6:pieces20:01234567890123456789e"
        )
        torrent = b"d8:announce25:http://tracker.example.com4:info" + info_dict + b"e"
        torrent_b64 = base64.b64encode(torrent).decode("ascii")

        result = extract_info_hash_from_torrent_base64(torrent_b64)
        expected = hashlib.sha1(info_dict).hexdigest().lower()
        assert result == expected

    def test_invalid_base64(self):
        """Test invalid base64 string"""
        result = extract_info_hash_from_torrent_base64("not valid base64!!!")
        assert result is None

    def test_empty_string(self):
        """Test empty string"""
        result = extract_info_hash_from_torrent_base64("")
        assert result is None


class TestFindBencodeEnd:
    """Tests for _find_bencode_end()"""

    def test_integer(self):
        """Test parsing integer: i123e"""
        data = b"i123e"
        result = _find_bencode_end(data, 0)
        assert result == 5

    def test_negative_integer(self):
        """Test parsing negative integer: i-456e"""
        data = b"i-456e"
        result = _find_bencode_end(data, 0)
        assert result == 6

    def test_string(self):
        """Test parsing string: 5:hello"""
        data = b"5:hello"
        result = _find_bencode_end(data, 0)
        assert result == 7

    def test_empty_string(self):
        """Test parsing empty string: 0:"""
        data = b"0:"
        result = _find_bencode_end(data, 0)
        assert result == 2

    def test_list(self):
        """Test parsing list: l5:helloi123ee"""
        data = b"l5:helloi123ee"
        result = _find_bencode_end(data, 0)
        assert result == 14

    def test_empty_list(self):
        """Test parsing empty list: le"""
        data = b"le"
        result = _find_bencode_end(data, 0)
        assert result == 2

    def test_dictionary(self):
        """Test parsing dictionary: d3:key5:valuee"""
        data = b"d3:key5:valuee"
        result = _find_bencode_end(data, 0)
        assert result == 14

    def test_empty_dictionary(self):
        """Test parsing empty dictionary: de"""
        data = b"de"
        result = _find_bencode_end(data, 0)
        assert result == 2

    def test_nested_structure(self):
        """Test parsing nested structure"""
        # d4:listl5:helloi123ee4:name4:teste
        data = b"d4:listl5:helloi123ee4:name4:teste"
        result = _find_bencode_end(data, 0)
        assert result == len(data)

    def test_start_beyond_data(self):
        """Test start position beyond data length"""
        data = b"i123e"
        result = _find_bencode_end(data, 100)
        assert result == -1

    def test_malformed_integer(self):
        """Test malformed integer without end marker"""
        data = b"i123"
        result = _find_bencode_end(data, 0)
        assert result == -1

    def test_malformed_string_no_colon(self):
        """Test malformed string without colon"""
        data = b"5hello"
        result = _find_bencode_end(data, 0)
        assert result == -1


class TestCalculateUrlHash:
    """Tests for calculate_url_hash()"""

    def test_basic_url(self):
        """Test hash of basic URL"""
        url = "https://example.com/file.zip"
        result = calculate_url_hash(url)
        expected = hashlib.sha256(url.encode("utf-8")).hexdigest().lower()
        assert result == expected
        assert len(result) == 64

    def test_same_url_same_hash(self):
        """Test same URL produces same hash"""
        url = "https://example.com/file.zip"
        result1 = calculate_url_hash(url)
        result2 = calculate_url_hash(url)
        assert result1 == result2

    def test_different_urls_different_hash(self):
        """Test different URLs produce different hashes"""
        url1 = "https://example.com/file1.zip"
        url2 = "https://example.com/file2.zip"
        result1 = calculate_url_hash(url1)
        result2 = calculate_url_hash(url2)
        assert result1 != result2

    def test_url_with_query_params(self):
        """Test URL with query parameters"""
        url = "https://example.com/file.zip?token=abc123&expire=12345"
        result = calculate_url_hash(url)
        assert len(result) == 64

    def test_url_with_unicode(self):
        """Test URL with unicode characters"""
        url = "https://example.com/文件.zip"
        result = calculate_url_hash(url)
        assert len(result) == 64


class TestCalculateFileContentHash:
    """Tests for calculate_file_content_hash()"""

    def test_basic_file(self, tmp_path: Path):
        """Test hash of basic file"""
        file_path = tmp_path / "test.txt"
        content = b"Hello, World!"
        file_path.write_bytes(content)

        result = calculate_file_content_hash(file_path)
        expected = hashlib.sha256(content).hexdigest().lower()
        assert result == expected

    def test_empty_file(self, tmp_path: Path):
        """Test hash of empty file"""
        file_path = tmp_path / "empty.txt"
        file_path.write_bytes(b"")

        result = calculate_file_content_hash(file_path)
        expected = hashlib.sha256(b"").hexdigest().lower()
        assert result == expected

    def test_large_file(self, tmp_path: Path):
        """Test hash of file larger than chunk size"""
        file_path = tmp_path / "large.bin"
        # Create file larger than 8192 bytes (chunk size)
        content = b"x" * 20000
        file_path.write_bytes(content)

        result = calculate_file_content_hash(file_path)
        expected = hashlib.sha256(content).hexdigest().lower()
        assert result == expected

    def test_same_content_same_hash(self, tmp_path: Path):
        """Test same content produces same hash"""
        file1 = tmp_path / "file1.txt"
        file2 = tmp_path / "file2.txt"
        content = b"Same content"
        file1.write_bytes(content)
        file2.write_bytes(content)

        result1 = calculate_file_content_hash(file1)
        result2 = calculate_file_content_hash(file2)
        assert result1 == result2


class TestCalculateDirectoryContentHash:
    """Tests for calculate_directory_content_hash()"""

    def test_directory_with_files(self, tmp_path: Path):
        """Test hash of directory with files"""
        dir_path = tmp_path / "testdir"
        dir_path.mkdir()
        (dir_path / "file1.txt").write_bytes(b"content1")
        (dir_path / "file2.txt").write_bytes(b"content2")

        result = calculate_directory_content_hash(dir_path)
        assert len(result) == 64

    def test_empty_directory(self, tmp_path: Path):
        """Test hash of empty directory"""
        dir_path = tmp_path / "emptydir"
        dir_path.mkdir()

        result = calculate_directory_content_hash(dir_path)
        # Empty directory should produce hash of empty input
        expected = hashlib.sha256(b"").hexdigest().lower()
        assert result == expected

    def test_nested_directory(self, tmp_path: Path):
        """Test hash of nested directory structure"""
        dir_path = tmp_path / "nested"
        dir_path.mkdir()
        subdir = dir_path / "subdir"
        subdir.mkdir()
        (dir_path / "file1.txt").write_bytes(b"content1")
        (subdir / "file2.txt").write_bytes(b"content2")

        result = calculate_directory_content_hash(dir_path)
        assert len(result) == 64

    def test_same_content_same_hash(self, tmp_path: Path):
        """Test directories with same content produce same hash"""
        dir1 = tmp_path / "dir1"
        dir2 = tmp_path / "dir2"
        dir1.mkdir()
        dir2.mkdir()
        (dir1 / "file.txt").write_bytes(b"content")
        (dir2 / "file.txt").write_bytes(b"content")

        result1 = calculate_directory_content_hash(dir1)
        result2 = calculate_directory_content_hash(dir2)
        assert result1 == result2

    def test_different_content_different_hash(self, tmp_path: Path):
        """Test directories with different content produce different hashes"""
        dir1 = tmp_path / "dir1"
        dir2 = tmp_path / "dir2"
        dir1.mkdir()
        dir2.mkdir()
        (dir1 / "file.txt").write_bytes(b"content1")
        (dir2 / "file.txt").write_bytes(b"content2")

        result1 = calculate_directory_content_hash(dir1)
        result2 = calculate_directory_content_hash(dir2)
        assert result1 != result2


class TestCalculateContentHash:
    """Tests for calculate_content_hash()"""

    def test_file(self, tmp_path: Path):
        """Test hash of file"""
        file_path = tmp_path / "test.txt"
        file_path.write_bytes(b"content")

        result = calculate_content_hash(file_path)
        expected = calculate_file_content_hash(file_path)
        assert result == expected

    def test_directory(self, tmp_path: Path):
        """Test hash of directory"""
        dir_path = tmp_path / "testdir"
        dir_path.mkdir()
        (dir_path / "file.txt").write_bytes(b"content")

        result = calculate_content_hash(dir_path)
        expected = calculate_directory_content_hash(dir_path)
        assert result == expected

    def test_nonexistent_path(self, tmp_path: Path):
        """Test nonexistent path raises ValueError"""
        nonexistent = tmp_path / "nonexistent"

        with pytest.raises(ValueError, match="does not exist"):
            calculate_content_hash(nonexistent)


class TestGetUriHash:
    """Tests for get_uri_hash()"""

    def test_magnet_link(self):
        """Test hash extraction from magnet link"""
        magnet = "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=test"
        result = get_uri_hash(magnet)
        assert result == "0123456789abcdef0123456789abcdef01234567"

    def test_http_url(self):
        """Test hash calculation for HTTP URL"""
        url = "https://example.com/file.zip"
        result = get_uri_hash(url)
        expected = calculate_url_hash(url)
        assert result == expected

    def test_https_url(self):
        """Test hash calculation for HTTPS URL"""
        url = "https://example.com/file.zip"
        result = get_uri_hash(url)
        expected = calculate_url_hash(url)
        assert result == expected

    def test_ftp_url(self):
        """Test hash calculation for FTP URL"""
        url = "ftp://example.com/file.zip"
        result = get_uri_hash(url)
        expected = calculate_url_hash(url)
        assert result == expected

    def test_torrent_with_base64(self):
        """Test hash extraction from torrent with base64 data"""
        info_dict = (
            b"d4:name4:test12:piece lengthi16384e6:pieces20:01234567890123456789e"
        )
        torrent = b"d8:announce25:http://tracker.example.com4:info" + info_dict + b"e"
        torrent_b64 = base64.b64encode(torrent).decode("ascii")

        result = get_uri_hash("[torrent]", torrent_b64)
        expected = hashlib.sha1(info_dict).hexdigest().lower()
        assert result == expected

    def test_torrent_without_base64(self):
        """Test torrent URI without base64 data returns None"""
        result = get_uri_hash("[torrent]")
        assert result is None

    def test_unknown_uri_type(self):
        """Test unknown URI type returns None"""
        result = get_uri_hash("unknown://example.com/file")
        assert result is None


class TestUriTypeChecks:
    """Tests for is_magnet_link() and is_http_url()"""

    def test_is_magnet_link_true(self):
        """Test is_magnet_link returns True for magnet links"""
        assert is_magnet_link("magnet:?xt=urn:btih:abc123") is True
        assert is_magnet_link("MAGNET:?xt=urn:btih:abc123") is True

    def test_is_magnet_link_false(self):
        """Test is_magnet_link returns False for non-magnet URIs"""
        assert is_magnet_link("https://example.com") is False
        assert is_magnet_link("ftp://example.com") is False
        assert is_magnet_link("") is False

    def test_is_http_url_true(self):
        """Test is_http_url returns True for HTTP(S) URLs"""
        assert is_http_url("http://example.com") is True
        assert is_http_url("https://example.com") is True
        assert is_http_url("HTTP://example.com") is True
        assert is_http_url("HTTPS://example.com") is True

    def test_is_http_url_false(self):
        """Test is_http_url returns False for non-HTTP URLs"""
        assert is_http_url("ftp://example.com") is False
        assert is_http_url("magnet:?xt=urn:btih:abc") is False
        assert is_http_url("") is False
