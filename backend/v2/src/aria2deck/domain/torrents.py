from __future__ import annotations

import base64
import binascii
import hashlib
from dataclasses import dataclass

from .content import validate_display_name
from .downloads import torrent_resource_key
from .errors import DomainError, ErrorCode

MAX_TORRENT_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class TorrentFile:
    index: int
    path: str
    size: int


@dataclass(frozen=True, slots=True)
class TorrentMetadata:
    info_hash: str
    name: str
    files: tuple[TorrentFile, ...]
    total_size: int
    selected_file_indexes: tuple[int, ...] | None
    selected_size: int
    resource_key: str
    payload_base64: str



def _encode(value: object) -> bytes:
    if isinstance(value, bytes):
        return str(len(value)).encode("ascii") + b":" + value
    if isinstance(value, int) and not isinstance(value, bool):
        return b"i" + str(value).encode("ascii") + b"e"
    if isinstance(value, list):
        return b"l" + b"".join(_encode(item) for item in value) + b"e"
    if isinstance(value, dict):
        return b"d" + b"".join(
            _encode(key) + _encode(item) for key, item in value.items()
        ) + b"e"
    raise _invalid("种子包含不支持的数据类型")

def _invalid(message: str = "种子文件无效") -> DomainError:
    return DomainError(ErrorCode.INVALID_INPUT, message)


class _Parser:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.info_span: tuple[int, int] | None = None

    def parse(self) -> dict[bytes, object]:
        value, position = self.value(0, 0, top_level=True)
        if position != len(self.data) or not isinstance(value, dict):
            raise _invalid()
        return value

    def value(
        self, position: int, depth: int, *, top_level: bool = False
    ) -> tuple[object, int]:
        if depth > 32 or position >= len(self.data):
            raise _invalid()
        marker = self.data[position : position + 1]
        if marker == b"i":
            end = self.data.find(b"e", position + 1)
            if end < 0:
                raise _invalid()
            raw = self.data[position + 1 : end]
            if not raw or raw in {b"-0", b"+0"} or raw.startswith(b"+") or (
                raw.startswith(b"0") and raw != b"0"
            ) or (raw.startswith(b"-0") and raw != b"-0"):
                raise _invalid()
            try:
                return int(raw), end + 1
            except ValueError:
                raise _invalid() from None
        if marker == b"l":
            return self.sequence(position + 1, depth)
        if marker == b"d":
            return self.mapping(position + 1, depth, top_level=top_level)
        if marker.isdigit():
            return self.byte_string(position)
        raise _invalid()

    def byte_string(self, position: int) -> tuple[bytes, int]:
        colon = self.data.find(b":", position)
        if colon < 0:
            raise _invalid()
        raw_length = self.data[position:colon]
        if not raw_length.isdigit() or (len(raw_length) > 1 and raw_length.startswith(b"0")):
            raise _invalid()
        length = int(raw_length)
        start = colon + 1
        end = start + length
        if end > len(self.data):
            raise _invalid()
        return self.data[start:end], end

    def sequence(self, position: int, depth: int) -> tuple[list[object], int]:
        values: list[object] = []
        while position < len(self.data) and self.data[position : position + 1] != b"e":
            value, position = self.value(position, depth + 1)
            values.append(value)
        if position >= len(self.data):
            raise _invalid()
        return values, position + 1

    def mapping(
        self, position: int, depth: int, *, top_level: bool = False
    ) -> tuple[dict[bytes, object], int]:
        values: dict[bytes, object] = {}
        previous: bytes | None = None
        while position < len(self.data) and self.data[position : position + 1] != b"e":
            key, position = self.byte_string(position)
            if previous is not None and key <= previous:
                raise _invalid("种子字典键必须严格递增")
            previous = key
            start = position
            value, position = self.value(position, depth + 1)
            if top_level and key == b"info":
                self.info_span = (start, position)
            values[key] = value
        if position >= len(self.data):
            raise _invalid()
        return values, position + 1


def _text(value: object, message: str) -> str:
    if not isinstance(value, bytes):
        raise _invalid(message)
    try:
        return validate_display_name(value.decode("utf-8"))
    except (UnicodeDecodeError, DomainError):
        raise _invalid(message) from None


def _nonnegative(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise _invalid("种子包含无效文件大小")
    return value


def _files(info: dict[bytes, object], name: str) -> tuple[TorrentFile, ...]:
    if b"length" in info:
        if b"files" in info:
            raise _invalid("种子不能同时包含单文件和多文件描述")
        return (TorrentFile(1, name, _nonnegative(info[b"length"])),)
    raw_files = info.get(b"files")
    if not isinstance(raw_files, list) or not raw_files:
        raise _invalid("种子缺少文件列表")
    files: list[TorrentFile] = []
    for index, raw in enumerate(raw_files, 1):
        if not isinstance(raw, dict) or not isinstance(raw.get(b"path"), list):
            raise _invalid("种子文件路径无效")
        parts = [_text(part, "种子文件路径无效") for part in raw[b"path"]]
        if not parts or any(part in {".", ".."} for part in parts):
            raise _invalid("种子文件路径无效")
        files.append(
            TorrentFile(index, "/".join((name, *parts)), _nonnegative(raw.get(b"length")))
        )
    return tuple(files)


def _selected_indexes(
    selected: list[int] | tuple[int, ...] | None, file_count: int
) -> tuple[int, ...] | None:
    if selected is None:
        return None
    if not selected or len(set(selected)) != len(selected) or any(
        not isinstance(index, int) or isinstance(index, bool) or not 1 <= index <= file_count
        for index in selected
    ):
        raise _invalid("种子文件选择无效")
    return tuple(sorted(selected))


def _parse_payload(
    payload_base64: str,
) -> tuple[bytes, dict[bytes, object], dict[bytes, object], tuple[int, int]]:
    if not payload_base64 or len(payload_base64) > 14_000_000:
        raise _invalid("种子内容为空或过大")
    try:
        data = base64.b64decode(payload_base64, validate=True)
    except (binascii.Error, ValueError):
        raise _invalid("种子 Base64 编码无效") from None
    if not data or len(data) > MAX_TORRENT_BYTES:
        raise _invalid("种子内容为空或过大")
    parser = _Parser(data)
    root = parser.parse()
    info = root.get(b"info")
    if not isinstance(info, dict) or parser.info_span is None:
        raise _invalid("种子缺少 info 字典")
    if any(key in root or key in info for key in (b"url-list", b"httpseeds")):
        raise _invalid("不支持种子 webseed")
    return data, root, info, parser.info_span


def _encode_shell(
    data: bytes, root: dict[bytes, object], info_span: tuple[int, int]
) -> str:
    start, end = info_span
    encoded = [b"d"]
    for key, value in root.items():
        encoded_value = data[start:end] if key == b"info" else _encode(value)
        encoded.extend((_encode(key), encoded_value))
    encoded.append(b"e")
    return base64.b64encode(b"".join(encoded)).decode("ascii")


def _peer_text(value: object) -> str | None:
    if not isinstance(value, bytes):
        return None
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _peer_node(value: object) -> tuple[str, int] | None:
    if not isinstance(value, list) or len(value) != 2:
        return None
    host = _peer_text(value[0])
    port = value[1]
    if host is None or not isinstance(port, int) or isinstance(port, bool):
        return None
    return host, port


def parse_torrent(
    payload_base64: str, selected_file_indexes: list[int] | tuple[int, ...] | None = None
) -> TorrentMetadata:
    data, root, info, info_span = _parse_payload(payload_base64)
    name = _text(info.get(b"name"), "种子名称无效")
    files = _files(info, name)
    selected = _selected_indexes(selected_file_indexes, len(files))
    chosen = files if selected is None else tuple(files[index - 1] for index in selected)
    start, end = info_span
    info_hash = hashlib.sha1(data[start:end], usedforsecurity=False).hexdigest()
    # Peer candidates are validated asynchronously at the aria2 submission boundary.
    safe_payload = _encode_shell(data, root, info_span)
    return TorrentMetadata(
        info_hash=info_hash,
        name=name,
        files=files,
        total_size=sum(file.size for file in files),
        selected_file_indexes=selected,
        selected_size=sum(file.size for file in chosen),
        resource_key=torrent_resource_key(info_hash, selected),
        payload_base64=safe_payload,
    )


def torrent_peer_candidates(
    payload_base64: str,
) -> tuple[tuple[str, ...], tuple[tuple[str, int], ...]]:
    _data, root, _info, _info_span = _parse_payload(payload_base64)
    trackers: list[str] = []
    announce = _peer_text(root.get(b"announce"))
    if announce is not None:
        trackers.append(announce)
    tiers = root.get(b"announce-list")
    if isinstance(tiers, list):
        for tier in tiers:
            if not isinstance(tier, list):
                continue
            for value in tier:
                tracker = _peer_text(value)
                if tracker is not None:
                    trackers.append(tracker)
    raw_nodes = root.get(b"nodes")
    nodes = (
        tuple(node for value in raw_nodes if (node := _peer_node(value)) is not None)
        if isinstance(raw_nodes, list)
        else ()
    )
    return tuple(trackers), nodes


def rebuild_torrent_peer_payload(
    payload_base64: str,
    *,
    trackers: tuple[str, ...],
    nodes: tuple[tuple[str, int], ...],
) -> str:
    data, root, _info, info_span = _parse_payload(payload_base64)
    allowed_trackers = set(trackers)
    allowed_nodes = set(nodes)
    seen_trackers: set[str] = set()
    rebuilt: dict[bytes, object] = {}
    for key, value in root.items():
        if key == b"announce":
            tracker = _peer_text(value)
            if tracker is None or tracker not in allowed_trackers:
                continue
            seen_trackers.add(tracker)
        elif key == b"announce-list":
            if not isinstance(value, list):
                continue
            tiers: list[object] = []
            for tier in value:
                if not isinstance(tier, list):
                    continue
                kept: list[object] = []
                for raw in tier:
                    tracker = _peer_text(raw)
                    if (
                        tracker is None
                        or tracker not in allowed_trackers
                        or tracker in seen_trackers
                    ):
                        continue
                    seen_trackers.add(tracker)
                    kept.append(raw)
                if kept:
                    tiers.append(kept)
            if not tiers:
                continue
            value = tiers
        elif key == b"nodes":
            if not isinstance(value, list):
                continue
            seen_nodes: set[tuple[str, int]] = set()
            kept_nodes: list[object] = []
            for raw in value:
                node = _peer_node(raw)
                if node is None or node not in allowed_nodes or node in seen_nodes:
                    continue
                seen_nodes.add(node)
                kept_nodes.append(raw)
            if not kept_nodes:
                continue
            value = kept_nodes
        rebuilt[key] = value
    return _encode_shell(data, rebuilt, info_span)
