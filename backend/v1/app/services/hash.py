"""Hash calculation services for download deduplication.

Provides functions to:
1. Extract info_hash from magnet links
2. Parse torrent files to get info_hash
3. Calculate SHA256 hash for HTTP URLs
4. Calculate content_hash for files/directories
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import re
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from app.domain.torrent_metadata import TorrentMetadataError, parse_torrent_bytes
from app.services.storage_index import scan_storage_path, scan_storage_path_async

logger = logging.getLogger(__name__)

# Magnet link info_hash pattern (btih = BitTorrent Info Hash)
MAGNET_BTIH_PATTERN = re.compile(
    r"urn:btih:([a-fA-F0-9]{40}|[a-zA-Z2-7]{32})",
    re.IGNORECASE
)


def extract_info_hash_from_magnet(magnet_uri: str) -> str | None:
    """Extract info_hash from a magnet link.

    Supports both hex (40 chars) and base32 (32 chars) encoded info_hash.

    Args:
        magnet_uri: Magnet link starting with "magnet:?"

    Returns:
        Lowercase hex info_hash (40 chars) or None if not found
    """
    if not magnet_uri.lower().startswith("magnet:"):
        return None

    # Parse the magnet URI
    parsed = urlparse(magnet_uri)
    params = parse_qs(parsed.query)

    # Look for xt (exact topic) parameter
    xt_values = params.get("xt", [])
    for xt in xt_values:
        match = MAGNET_BTIH_PATTERN.search(xt)
        if match:
            hash_value = match.group(1)
            # Convert base32 to hex if needed
            if len(hash_value) == 32:
                try:
                    decoded = base64.b32decode(hash_value.upper())
                    return decoded.hex().lower()
                except (ValueError, binascii.Error) as e:
                    logger.debug(f"Failed to decode base32 hash {hash_value}: {e}")
                    continue
            elif len(hash_value) == 40:
                return hash_value.lower()

    return None


def extract_info_hash_from_torrent(torrent_data: bytes) -> str | None:
    """Extract info_hash from torrent file data.

    The info_hash is the SHA1 hash of the bencoded "info" dictionary.

    Args:
        torrent_data: Raw torrent file bytes

    Returns:
        Lowercase hex info_hash (40 chars) or None if parsing fails
    """
    try:
        return parse_torrent_bytes(torrent_data).info_hash
    except TorrentMetadataError as e:
        logger.warning(f"Failed to parse torrent file: {e}")
        return None


def extract_info_hash_from_torrent_base64(torrent_base64: str) -> str | None:
    """Extract info_hash from base64-encoded torrent data.

    Args:
        torrent_base64: Base64-encoded torrent file

    Returns:
        Lowercase hex info_hash (40 chars) or None if parsing fails
    """
    try:
        torrent_data = base64.b64decode(torrent_base64, validate=True)
    except Exception as e:  # noqa: BLE001  # external boundary preserves failure isolation
        logger.warning(f"Failed to decode base64 torrent: {e}")
        return None
    return extract_info_hash_from_torrent(torrent_data)


def _find_bencode_end(data: bytes, start: int, depth: int = 0) -> int:
    """Find the end position of a bencoded value starting at 'start'.

    Returns the position after the last byte of the value, or -1 on error.
    """
    if depth > 100:
        return -1

    if start >= len(data):
        return -1

    char = data[start:start + 1]

    if char == b"i":
        # Integer: i<number>e
        end = data.find(b"e", start + 1)
        return end + 1 if end != -1 else -1

    elif char == b"l" or char == b"d":
        # List or Dictionary: l...e or d...e
        pos = start + 1
        while pos < len(data) and data[pos:pos + 1] != b"e":
            if char == b"d":
                # Dictionary has key-value pairs, skip the key first
                key_end = _find_bencode_end(data, pos, depth + 1)
                if key_end == -1:
                    return -1
                pos = key_end

            # Find end of value
            value_end = _find_bencode_end(data, pos, depth + 1)
            if value_end == -1:
                return -1
            pos = value_end

        return pos + 1 if pos < len(data) else -1

    elif char.isdigit():
        # String: <length>:<data>
        colon = data.find(b":", start)
        if colon == -1:
            return -1
        try:
            length = int(data[start:colon])
            return colon + 1 + length
        except ValueError:
            return -1

    return -1


def calculate_url_hash(url: str) -> str:
    """Calculate SHA256 hash of a URL for deduplication.

    Args:
        url: The URL to hash (should be the final URL after redirects)

    Returns:
        Lowercase hex SHA256 hash (64 chars)
    """
    return hashlib.sha256(url.encode("utf-8")).hexdigest().lower()


def calculate_file_content_hash(
    file_path: Path, cancel_event: threading.Event | None = None
) -> str:
    """Calculate the SHA256 hash of one regular file."""
    scan = scan_storage_path(file_path, cancel_event)
    if scan.is_directory:
        raise ValueError(f"Path is not a file: {file_path}")
    return scan.content_hash


def calculate_directory_content_hash(
    dir_path: Path, cancel_event: threading.Event | None = None
) -> str:
    """Calculate the content hash of a directory under storage boundaries."""
    scan = scan_storage_path(dir_path, cancel_event)
    if not scan.is_directory:
        raise ValueError(f"Path is not a directory: {dir_path}")
    return scan.content_hash


def calculate_content_hash(
    path: Path, cancel_event: threading.Event | None = None
) -> str:
    """Calculate a file or directory hash using the storage scan contract."""
    return scan_storage_path(path, cancel_event).content_hash


async def calculate_content_hash_async(
    path: Path, cancel_event: threading.Event | None = None
) -> str:
    """异步计算文件或目录内容哈希，并在取消时等待扫描线程退出。"""
    return (await scan_storage_path_async(path, cancel_event)).content_hash


def get_uri_hash(uri: str, torrent_base64: str | None = None) -> str | None:
    """Get the appropriate hash for a URI based on its type.

    Args:
        uri: The download URI (magnet link, HTTP URL, or "[torrent]")
        torrent_base64: Base64-encoded torrent data (required if uri is "[torrent]")

    Returns:
        The uri_hash for deduplication, or None if unable to determine
    """
    uri_lower = uri.lower()

    # Magnet link
    if uri_lower.startswith("magnet:"):
        return extract_info_hash_from_magnet(uri)

    # Torrent file
    if uri == "[torrent]" and torrent_base64:
        return extract_info_hash_from_torrent_base64(torrent_base64)

    # HTTP(S) URL
    if uri_lower.startswith(("http://", "https://")):
        return calculate_url_hash(uri)

    # FTP URL
    if uri_lower.startswith("ftp://"):
        return calculate_url_hash(uri)

    return None


def is_magnet_link(uri: str) -> bool:
    """Check if a URI is a magnet link."""
    return uri.lower().startswith("magnet:")


def is_http_url(uri: str) -> bool:
    """Check if a URI is an HTTP(S) URL."""
    lower = uri.lower()
    return lower.startswith(("http://", "https://"))
