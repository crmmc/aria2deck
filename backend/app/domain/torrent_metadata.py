from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
from dataclasses import dataclass
from typing import Any

MAX_TORRENT_BASE64_LENGTH = 14 * 1024 * 1024
MAX_TORRENT_RAW_LENGTH = 10 * 1024 * 1024
MAX_TORRENT_FILE_COUNT = 5000
MAX_BENCODE_DEPTH = 64
MAX_BENCODE_NODES = 100_000
MAX_BENCODE_CONTAINER_WIDTH = 10_000
MAX_BENCODE_NUMBER_DIGITS = 20
MAX_PATH_DEPTH = 32
MAX_PATH_COMPONENT_LENGTH = 255
MAX_RELATIVE_PATH_LENGTH = 4096
MAX_TORRENT_NETWORK_ENDPOINTS = 64


class TorrentMetadataError(ValueError):
    pass


@dataclass
class _BencodeBudget:
    decoded_raw_bytes: int
    node_count: int = 0

    def __post_init__(self) -> None:
        if self.decoded_raw_bytes > MAX_TORRENT_RAW_LENGTH:
            raise TorrentMetadataError("torrent bytes too large")

    def consume_node(self) -> None:
        self.node_count += 1
        if self.node_count > MAX_BENCODE_NODES:
            raise TorrentMetadataError("too many bencode nodes")

    @staticmethod
    def check_container_width(width: int) -> None:
        if width > MAX_BENCODE_CONTAINER_WIDTH:
            raise TorrentMetadataError("bencode container too wide")


@dataclass(frozen=True)
class TorrentFile:
    index: int
    path: tuple[str, ...]
    size: int


@dataclass(frozen=True)
class TorrentMetadata:
    info_hash: str
    name: str
    files: tuple[TorrentFile, ...]
    tree: list[dict[str, Any]]
    tracker_urls: tuple[str, ...]
    webseed_urls: tuple[str, ...]

    @property
    def file_count(self) -> int:
        return len(self.files)

    @property
    def total_size(self) -> int:
        return sum(file.size for file in self.files)


def parse_torrent_base64(torrent_base64: str) -> TorrentMetadata:
    if not torrent_base64 or len(torrent_base64) > MAX_TORRENT_BASE64_LENGTH:
        raise TorrentMetadataError("invalid torrent size")
    try:
        raw = base64.b64decode(torrent_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise TorrentMetadataError("invalid base64 torrent") from exc
    return parse_torrent_bytes(raw)


async def parse_torrent_base64_async(torrent_base64: str) -> TorrentMetadata:
    return await asyncio.to_thread(parse_torrent_base64, torrent_base64)


def parse_torrent_bytes(raw: bytes) -> TorrentMetadata:
    budget = _BencodeBudget(len(raw))
    value, end, info_span = _parse_value(raw, 0, 0, budget)
    if end != len(raw):
        raise TorrentMetadataError("trailing bencode data")
    if not isinstance(value, dict):
        raise TorrentMetadataError("torrent root must be a dictionary")
    info = value.get(b"info")
    if not isinstance(info, dict) or info_span is None:
        raise TorrentMetadataError("missing info dictionary")

    info_bytes = raw[info_span[0] : info_span[1]]
    info_hash = hashlib.sha1(info_bytes).hexdigest().lower()
    name = _decode_component(info.get(b"name"), field="name")

    files = _extract_files(name, info)
    if not files:
        raise TorrentMetadataError("torrent has no files")
    if len(files) > MAX_TORRENT_FILE_COUNT:
        raise TorrentMetadataError("too many files")

    tracker_urls, webseed_urls = _extract_network_endpoints(value)
    return TorrentMetadata(
        info_hash=info_hash,
        name=name,
        files=tuple(files),
        tree=_build_tree(files),
        tracker_urls=tracker_urls,
        webseed_urls=webseed_urls,
    )


def validate_selected_indexes(
    metadata: TorrentMetadata,
    selected_file_indexes: list[object] | tuple[object, ...] | None,
) -> tuple[int, ...]:
    if selected_file_indexes is None:
        return tuple(file.index for file in metadata.files)
    if not selected_file_indexes:
        raise TorrentMetadataError("empty selection")

    seen: set[int] = set()
    selected: list[int] = []
    max_index = metadata.file_count
    for value in selected_file_indexes:
        if not isinstance(value, int) or isinstance(value, bool):
            raise TorrentMetadataError("selected indexes must be integers")
        if value in seen:
            raise TorrentMetadataError("duplicate selected index")
        if value < 1 or value > max_index:
            raise TorrentMetadataError("selected index out of range")
        seen.add(value)
        selected.append(value)
    return tuple(sorted(selected))


def selected_total_size(metadata: TorrentMetadata, selected_indexes: tuple[int, ...]) -> int:
    by_index = {file.index: file.size for file in metadata.files}
    return sum(by_index[index] for index in selected_indexes)


def build_select_file_option(selected_indexes: tuple[int, ...], total_file_count: int) -> str | None:
    if len(selected_indexes) == total_file_count:
        return None
    return ",".join(str(index) for index in selected_indexes)


def build_selection_resource_key(
    info_hash: str, selected_indexes: tuple[int, ...], *, total_file_count: int
) -> str:
    normalized = tuple(sorted(selected_indexes))
    if len(normalized) == total_file_count:
        return info_hash
    digest = hashlib.sha256(",".join(str(index) for index in normalized).encode("ascii")).hexdigest()[
        :32
    ]
    return f"{info_hash}:files:{digest}"


def _decode_network_url(value: Any, *, field: str) -> str:
    if not isinstance(value, bytes):
        raise TorrentMetadataError(f"invalid {field} URL")
    try:
        url = value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TorrentMetadataError(f"invalid {field} URL encoding") from exc
    if not url:
        raise TorrentMetadataError(f"empty {field} URL")
    return url


def _extract_network_endpoints(
    root: dict[bytes, Any],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    trackers: list[str] = []
    webseeds: list[str] = []

    if b"announce" in root:
        trackers.append(_decode_network_url(root[b"announce"], field="announce"))

    announce_list = root.get(b"announce-list")
    if announce_list is not None:
        if not isinstance(announce_list, list):
            raise TorrentMetadataError("announce-list must be a list")
        for tier in announce_list:
            if not isinstance(tier, list):
                raise TorrentMetadataError("announce-list tier must be a list")
            trackers.extend(
                _decode_network_url(item, field="announce-list") for item in tier
            )

    for key in (b"url-list", b"httpseeds"):
        value = root.get(key)
        if value is None:
            continue
        field = key.decode("ascii")
        if isinstance(value, bytes):
            webseeds.append(_decode_network_url(value, field=field))
        elif isinstance(value, list):
            webseeds.extend(
                _decode_network_url(item, field=field) for item in value
            )
        else:
            raise TorrentMetadataError(f"{field} must be a string or list")

    if len(trackers) + len(webseeds) > MAX_TORRENT_NETWORK_ENDPOINTS:
        raise TorrentMetadataError("too many torrent network endpoints")
    return tuple(trackers), tuple(webseeds)


def _extract_files(root_name: str, info: dict[bytes, Any]) -> list[TorrentFile]:
    if b"files" not in info:
        length = _decode_length(info.get(b"length"))
        return [TorrentFile(index=1, path=(root_name,), size=length)]

    files_value = info[b"files"]
    if not isinstance(files_value, list):
        raise TorrentMetadataError("files must be a list")

    files: list[TorrentFile] = []
    for idx, entry in enumerate(files_value, start=1):
        if not isinstance(entry, dict):
            raise TorrentMetadataError("file entry must be a dictionary")
        length = _decode_length(entry.get(b"length"))
        path_value = entry.get(b"path")
        if not isinstance(path_value, list):
            raise TorrentMetadataError("file path must be a list")
        components = [root_name]
        components.extend(_decode_component(component, field="path") for component in path_value)
        _validate_path(components)
        files.append(TorrentFile(index=idx, path=tuple(components), size=length))
    return files


def _decode_length(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise TorrentMetadataError("invalid file length")
    return value


def _decode_component(value: Any, *, field: str) -> str:
    if not isinstance(value, bytes):
        raise TorrentMetadataError(f"invalid {field}")
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError:
        text = value.decode("utf-8", errors="replace")
    if not text:
        raise TorrentMetadataError("empty path component")
    if text in {".", ".."} or "/" in text or "\\" in text or text.startswith("/"):
        raise TorrentMetadataError("unsafe path component")
    if len(text) > MAX_PATH_COMPONENT_LENGTH:
        raise TorrentMetadataError("path component too long")
    return text


def _validate_path(components: list[str]) -> None:
    if len(components) > MAX_PATH_DEPTH:
        raise TorrentMetadataError("path too deep")
    relative = "/".join(components)
    if len(relative) > MAX_RELATIVE_PATH_LENGTH:
        raise TorrentMetadataError("path too long")


def _build_tree(files: list[TorrentFile]) -> list[dict[str, Any]]:
    root_nodes: dict[str, dict[str, Any]] = {}
    for file in files:
        current = root_nodes
        for depth, component in enumerate(file.path):
            is_leaf = depth == len(file.path) - 1
            if component not in current:
                current[component] = {
                    "type": "file" if is_leaf else "directory",
                    "name": component,
                    "path": list(file.path[: depth + 1]),
                    "size": file.size if is_leaf else 0,
                    "index": file.index if is_leaf else None,
                    "children": {},
                }
            node = current[component]
            if is_leaf:
                node["size"] = file.size
                node["index"] = file.index
            else:
                node["size"] = int(node["size"]) + file.size
                current = node["children"]
    return [_serialize_node(node) for node in root_nodes.values()]


def _serialize_node(node: dict[str, Any]) -> dict[str, Any]:
    result = {
        "type": node["type"],
        "name": node["name"],
        "path": node["path"],
        "size": node["size"],
    }
    if node["type"] == "file":
        result["index"] = node["index"]
    else:
        result["children"] = [_serialize_node(child) for child in node["children"].values()]
    return result


def _parse_value(
    data: bytes,
    pos: int,
    depth: int,
    budget: _BencodeBudget,
) -> tuple[Any, int, tuple[int, int] | None]:
    if depth > MAX_BENCODE_DEPTH:
        raise TorrentMetadataError("bencode too deep")
    if pos >= len(data):
        raise TorrentMetadataError("unexpected end of bencode")
    budget.consume_node()
    token = data[pos : pos + 1]
    if token == b"i":
        integer, end = _parse_int(data, pos)
        return integer, end, None
    if token == b"l":
        values: list[Any] = []
        pos += 1
        while pos < len(data) and data[pos : pos + 1] != b"e":
            budget.check_container_width(len(values) + 1)
            value, pos, _ = _parse_value(data, pos, depth + 1, budget)
            values.append(value)
        if pos >= len(data):
            raise TorrentMetadataError("unterminated list")
        return values, pos + 1, None
    if token == b"d":
        result: dict[bytes, Any] = {}
        info_span: tuple[int, int] | None = None
        pos += 1
        while pos < len(data) and data[pos : pos + 1] != b"e":
            budget.check_container_width(len(result) + 1)
            key, pos, _ = _parse_value(data, pos, depth + 1, budget)
            if not isinstance(key, bytes):
                raise TorrentMetadataError("dictionary key must be bytes")
            if key in result:
                raise TorrentMetadataError("duplicate dictionary key")
            value_start = pos
            value, pos, _ = _parse_value(data, pos, depth + 1, budget)
            result[key] = value
            if depth == 0 and key == b"info":
                info_span = (value_start, pos)
        if pos >= len(data):
            raise TorrentMetadataError("unterminated dictionary")
        return result, pos + 1, info_span
    if token.isdigit():
        value, end = _parse_bytes(data, pos)
        return value, end, None
    raise TorrentMetadataError("invalid bencode token")


def _find_number_end(data: bytes, start: int, terminator: bytes, label: str) -> int:
    end = data.find(terminator, start, start + MAX_BENCODE_NUMBER_DIGITS + 1)
    if end != -1:
        return end
    if start + MAX_BENCODE_NUMBER_DIGITS < len(data):
        raise TorrentMetadataError(f"{label} too long")
    raise TorrentMetadataError(f"unterminated {label}")


def _parse_int(data: bytes, pos: int) -> tuple[int, int]:
    end = _find_number_end(data, pos + 1, b"e", "integer")
    raw = data[pos + 1 : end]
    if not raw or raw in {b"-0", b"+0"}:
        raise TorrentMetadataError("invalid integer")
    try:
        return int(raw), end + 1
    except ValueError as exc:
        raise TorrentMetadataError("invalid integer") from exc


def _parse_bytes(data: bytes, pos: int) -> tuple[bytes, int]:
    colon = _find_number_end(data, pos, b":", "string length")
    raw_length = data[pos:colon]
    if not raw_length or (len(raw_length) > 1 and raw_length.startswith(b"0")):
        raise TorrentMetadataError("invalid string length")
    try:
        length = int(raw_length)
    except ValueError as exc:
        raise TorrentMetadataError("invalid string length") from exc
    start = colon + 1
    end = start + length
    if end > len(data):
        raise TorrentMetadataError("string exceeds input")
    return data[start:end], end
