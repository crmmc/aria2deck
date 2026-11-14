from __future__ import annotations

import base64

import pytest

from aria2deck.domain.errors import DomainError
from aria2deck.domain.torrents import _encode, _Parser, parse_torrent, rebuild_torrent_peer_payload
from tests.download_fakes import bencode, make_torrent


def test_rebuild_keeps_announce_tracker_in_announce_list_tier() -> None:
    """回归：重建不得把 announce-list 第 0 层的主 tracker 去重丢掉。

    announce 与其在 announce-list 的副本并存是 BEP 12 常态；aria2 等 BEP 12
    客户端只认 announce-list，丢掉后主 tracker 在 aria2 侧整体不可见，曾导致
    slackware 实测 swarm 只剩备用 tracker、下载因 bt-stop-timeout 失败。
    """
    original = bencode({
        "info": {"length": 1, "name": "file"},
        "announce": "udp://primary.example:1337/announce",
        "announce-list": [
            [
                "udp://primary.example:1337/announce",
                "udp://backup.example:6969/announce",
            ]
        ],
    })
    payload = base64.b64encode(original).decode()

    rebuilt = base64.b64decode(
        rebuild_torrent_peer_payload(
            payload,
            trackers=(
                "udp://primary.example:1337/announce",
                "udp://backup.example:6969/announce",
            ),
            nodes=(),
        )
    )
    root = _Parser(rebuilt).parse()
    assert root[b"announce"] == b"udp://primary.example:1337/announce"
    assert root[b"announce-list"] == [
        [
            b"udp://primary.example:1337/announce",
            b"udp://backup.example:6969/announce",
        ]
    ]

    filtered = base64.b64decode(
        rebuild_torrent_peer_payload(
            payload,
            trackers=("udp://backup.example:6969/announce",),
            nodes=(),
        )
    )
    root_filtered = _Parser(filtered).parse()
    assert b"announce" not in root_filtered
    assert root_filtered[b"announce-list"] == [[b"udp://backup.example:6969/announce"]]


def test_multi_file_preview_selection_and_identity() -> None:
    payload = make_torrent([("a.bin", 3), ("folder/b.bin", 5)], name="Bundle")
    metadata = parse_torrent(payload, [2])
    assert metadata.name == "Bundle"
    assert metadata.total_size == 8
    assert metadata.selected_size == 5
    assert metadata.selected_file_indexes == (2,)
    assert [(file.index, file.path, file.size) for file in metadata.files] == [
        (1, "Bundle/a.bin", 3),
        (2, "Bundle/folder/b.bin", 5),
    ]
    assert metadata.resource_key != parse_torrent(payload).resource_key
    assert parse_torrent(payload, [2, 1]).selected_file_indexes == (1, 2)


@pytest.mark.parametrize(
    "payload",
    [
        "",
        "not-base64!",
        base64.b64encode(b"").decode(),
        base64.b64encode(b"not-bencode").decode(),
        base64.b64encode(bencode({"info": {"name": "x"}}) + b"trailing").decode(),
        base64.b64encode(bencode({"name": "missing-info"})).decode(),
        make_torrent(webseed=True),
        base64.b64encode(
            bencode({"info": {"length": 1, "name": "x"}, "httpseeds": ["https://example.com"]})
        ).decode(),
        make_torrent([("a", -1)]),
        make_torrent([("a", 1)], name="bad/name"),
        make_torrent([("a", 1), ("../b", 2)]),
    ],
)
def test_torrent_parser_rejects_malformed_or_unsafe_payloads(payload: str) -> None:
    with pytest.raises(DomainError):
        parse_torrent(payload)


@pytest.mark.parametrize("selection", [[], [0], [2], [1, 1], [True]])
def test_torrent_selection_is_strict(selection: list[int]) -> None:
    with pytest.raises(DomainError):
        parse_torrent(make_torrent(), selection)


def test_single_file_cannot_also_have_a_file_list() -> None:
    raw = b"d4:infod5:filesle6:lengthi1e4:name1:xee"
    with pytest.raises(DomainError):
        parse_torrent(base64.b64encode(raw).decode())


@pytest.mark.parametrize(
    "raw",
    [
        b"le",
        b"d4:infod6:lengthi1ee4:name1:xee",
        b"d1x",
        b"d4:infod6:lengthi1",
        b"d4:infod6:lengthi01e4:name1:xee",
        b"d4:infod6:lengthi-0e4:name1:xee",
        b"d4:infod6:lengthi+1e4:name1:xee",
        b"d4:infod6:lengthiXe4:name1:xee",
        b"d4:infod6:lengthi1e4:name01:xee",
        b"d4:infod6:lengthi1e4:name5:xe",
        b"d4:infod6:lengthi1e4:name1:xe1:xl",
        b"d4:infod6:lengthi1e4:name1:x",
        b"d1:x0:4:infod6:lengthi1e4:name1:xee",
    ],
)
def test_malformed_bencode_structure_is_rejected(raw: bytes) -> None:
    with pytest.raises(DomainError):
        parse_torrent(base64.b64encode(raw).decode())


def test_excessive_nesting_is_rejected() -> None:
    raw = b"d4:infod6:lengthi1e4:name1:xe1:x" + b"l" * 33 + b"e" * 33 + b"e"
    with pytest.raises(DomainError):
        parse_torrent(base64.b64encode(raw).decode())


@pytest.mark.parametrize(
    "info",
    [
        {"length": 1, "name": 1},
        {"length": 1, "name": b"\xff"},
        {"name": "missing-files"},
        {"files": [], "name": "empty"},
        {"files": [1], "name": "bad"},
        {"files": [{"length": 1, "path": 1}], "name": "bad"},
        {"files": [{"length": 1, "path": []}], "name": "bad"},
        {"files": [{"length": 1, "path": [".."]}], "name": "bad"},
        {"files": [{"length": "1", "path": ["a"]}], "name": "bad"},
        {"length": 1, "name": "x", "url-list": ["https://example.com"]},
        {"length": 1, "name": "x", "httpseeds": ["https://example.com"]},
    ],
)
def test_invalid_info_shapes_are_rejected(info: dict[str, object]) -> None:
    payload = base64.b64encode(bencode({"info": info})).decode()
    with pytest.raises(DomainError):
        parse_torrent(payload)


def test_decoded_torrent_size_limit_is_enforced_before_parsing() -> None:
    payload = base64.b64encode(b"0" * (10 * 1024 * 1024 + 1)).decode()
    with pytest.raises(DomainError):
        parse_torrent(payload)


def test_torrent_shell_preserves_peer_candidates_without_changing_info_identity() -> None:
    info = {"length": 3, "name": "file"}
    original = bencode({
        "info": info,
        "announce": "https://tracker.example/announce",
        "announce-list": [["udp://tracker.example:6969/announce"]],
        "nodes": [["dht.example", 6881]],
    })
    metadata = parse_torrent(base64.b64encode(original).decode(), [1])
    preserved = base64.b64decode(metadata.payload_base64)
    parser = _Parser(preserved)
    root = parser.parse()
    assert root[b"announce"] == b"https://tracker.example/announce"
    assert root[b"announce-list"] == [[b"udp://tracker.example:6969/announce"]]
    assert root[b"nodes"] == [[b"dht.example", 6881]]
    assert metadata.info_hash == parse_torrent(base64.b64encode(bencode({"info": info})).decode()).info_hash
    assert parser.info_span is not None
    start, end = parser.info_span
    assert preserved[start:end] == bencode(info)
    reparsed = parse_torrent(metadata.payload_base64, [1])
    assert reparsed.resource_key == metadata.resource_key
    assert reparsed.files == metadata.files
    assert reparsed.selected_file_indexes == (1,)
    assert reparsed.selected_size == metadata.selected_size


def test_torrent_shell_encoder_retains_safe_metadata_types() -> None:
    original = bencode({
        "info": {"length": 1, "name": "file"},
        "comment": "safe",
        "creation date": 1,
        "extra-list": ["x", 2],
        "extra-map": {"key": "value"},
    })
    metadata = parse_torrent(base64.b64encode(original).decode())
    sanitized = base64.b64decode(metadata.payload_base64)
    assert b"safe" in sanitized
    assert b"extra-list" in sanitized
    assert b"extra-map" in sanitized
    with pytest.raises(DomainError):
        _encode(object())
