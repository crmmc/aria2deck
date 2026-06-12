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
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from app.domain.torrent_metadata import TorrentMetadataError, parse_torrent_bytes

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
    except Exception as e:
        logger.warning(f"Failed to decode base64 torrent: {e}")
        return None
    return extract_info_hash_from_torrent(torrent_data)


def _extract_info_dict_bytes(data: bytes) -> bytes | None:
    """Extract the raw bytes of the 'info' dictionary from torrent data.

    This is a minimal bencode parser that only extracts what we need.
    """
    # Find the info dictionary in the torrent
    # Torrent structure: d...4:info<info_dict>...e
    info_key = b"4:info"
    info_start = data.find(info_key)
    if info_start == -1:
        return None

    # Move past the key to the value
    dict_start = info_start + len(info_key)

    # Parse the dictionary to find its end
    end_pos = _find_bencode_end(data, dict_start)
    if end_pos == -1:
        return None

    return data[dict_start:end_pos]


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


def calculate_file_content_hash(file_path: Path) -> str:
    """Calculate content hash for a single file.

    Uses SHA256 for content hashing.

    Args:
        file_path: Path to the file

    Returns:
        Lowercase hex SHA256 hash (64 chars)
    """
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha256.update(chunk)
    return sha256.hexdigest().lower()


def calculate_directory_content_hash(dir_path: Path) -> str:
    """Calculate content hash for a directory.

    Creates a deterministic hash based on:
    - Sorted list of relative file paths
    - Each file's content hash

    Args:
        dir_path: Path to the directory

    Returns:
        Lowercase hex SHA256 hash (64 chars)
    """
    sha256 = hashlib.sha256()

    # Get all files sorted by relative path
    files = sorted(dir_path.rglob("*"))

    for file_path in files:
        if file_path.is_file():
            # Include relative path in hash
            rel_path = file_path.relative_to(dir_path)
            sha256.update(str(rel_path).encode("utf-8"))

            # Include file content hash
            file_hash = calculate_file_content_hash(file_path)
            sha256.update(file_hash.encode("utf-8"))

    return sha256.hexdigest().lower()


def calculate_content_hash(path: Path) -> str:
    """Calculate content hash for a file or directory.

    Args:
        path: Path to file or directory

    Returns:
        Lowercase hex SHA256 hash (64 chars)
    """
    if path.is_file():
        return calculate_file_content_hash(path)
    elif path.is_dir():
        return calculate_directory_content_hash(path)
    else:
        raise ValueError(f"Path does not exist or is not a file/directory: {path}")


async def calculate_content_hash_async(path: Path) -> str:
    """异步计算文件或目录的内容哈希。

    使用 asyncio.to_thread 避免阻塞事件循环，适用于大文件。

    Args:
        path: Path to file or directory

    Returns:
        Lowercase hex SHA256 hash (64 chars)
    """
    import asyncio
    return await asyncio.to_thread(calculate_content_hash, path)


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
    if uri_lower.startswith("http://") or uri_lower.startswith("https://"):
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
    return lower.startswith("http://") or lower.startswith("https://")
