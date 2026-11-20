from __future__ import annotations

import base64

import pytest

from aria2deck.domain.errors import DomainError, ErrorCode
from aria2deck.domain.torrents import (
    _encode,
    _Parser,
    parse_torrent,
    rebuild_torrent_peer_payload,
    torrent_peer_candidates,
)
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
            webseeds=(),
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
            webseeds=(),
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
    ],
)
def test_invalid_info_shapes_are_rejected(info: dict[str, object]) -> None:
    payload = base64.b64encode(bencode({"info": info})).decode()
    with pytest.raises(DomainError):
        parse_torrent(payload)


@pytest.mark.parametrize(
    "inside",
    [
        {"url-list": ["https://example.com"]},
        {"url-list": "https://example.com"},
        {"httpseeds": ["https://example.com"]},
    ],
)
def test_webseeds_inside_info_do_not_block_parsing(inside: dict[str, object]) -> None:
    """info 内同名键不阻碍解析：它们逐字节参与 info hash，剔除会改变内容身份。"""
    payload = base64.b64encode(bencode({"info": {"length": 1, "name": "x", **inside}})).decode()
    metadata = parse_torrent(payload)
    preserved = _Parser(base64.b64decode(metadata.payload_base64)).parse()
    assert preserved[b"info"] == _Parser(base64.b64decode(payload)).parse()[b"info"]


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


@pytest.mark.parametrize(
    "root_extra,expected",
    [
        ({"url-list": "https://seed.example/a"}, ("https://seed.example/a",)),
        ({"url-list": ["https://seed.example/a", "https://seed.example/b"]}, ("https://seed.example/a", "https://seed.example/b")),
        ({"httpseeds": ["https://seed.example/c"]}, ("https://seed.example/c",)),
        ({"url-list": [], "httpseeds": []}, ()),
        ({"url-list": [1, b"https://seed.example/d"]}, ("https://seed.example/d",)),
        ({"url-list": 42}, ()),
        ({}, ()),
    ],
)
def test_peer_candidates_extract_root_webseeds(
    root_extra: dict[str, object], expected: tuple[str, ...]
) -> None:
    payload = base64.b64encode(
        bencode({"info": {"length": 1, "name": "x"}, **root_extra})
    ).decode()
    trackers, webseeds, nodes = torrent_peer_candidates(payload)
    assert trackers == ()
    assert webseeds == expected
    assert nodes == ()


def test_peer_candidates_extract_webseeds_inside_info_for_validation() -> None:
    """info 内同名键 aria2 不读取，但仍纳入白名单校验范围。"""
    payload = base64.b64encode(
        bencode({
            "info": {
                "length": 1,
                "name": "x",
                "url-list": ["https://seed.example/inside"],
                "httpseeds": ["https://seed.example/info-httpseeds"],
            },
        })
    ).decode()
    _trackers, webseeds, _nodes = torrent_peer_candidates(payload)
    assert webseeds == (
        "https://seed.example/inside",
        "https://seed.example/info-httpseeds",
    )


def test_rebuild_keeps_only_whitelisted_root_webseeds() -> None:
    payload = base64.b64encode(
        bencode({
            "info": {"length": 1, "name": "x"},
            "url-list": ["https://good.example/a", "http://127.0.0.1/b"],
            "httpseeds": ["ftp://bad.example/c", "https://good.example/d"],
        })
    ).decode()
    root = _Parser(
        base64.b64decode(
            rebuild_torrent_peer_payload(
                payload,
                trackers=(),
                webseeds=("https://good.example/a", "https://good.example/d"),
                nodes=(),
            )
        )
    ).parse()
    assert root[b"url-list"] == [b"https://good.example/a"]
    assert root[b"httpseeds"] == [b"https://good.example/d"]


def test_rebuild_preserves_single_string_url_list_shape() -> None:
    payload = base64.b64encode(
        bencode({"info": {"length": 1, "name": "x"}, "url-list": "https://good.example/a"})
    ).decode()
    root = _Parser(
        base64.b64decode(
            rebuild_torrent_peer_payload(
                payload, trackers=(), webseeds=("https://good.example/a",), nodes=()
            )
        )
    ).parse()
    assert root[b"url-list"] == b"https://good.example/a"


def test_rebuild_drops_webseed_keys_when_nothing_survives() -> None:
    payload = base64.b64encode(
        bencode({
            "info": {"length": 1, "name": "x"},
            "url-list": ["http://127.0.0.1/a"],
            "httpseeds": ["ftp://bad.example/b"],
        })
    ).decode()
    metadata = parse_torrent(payload)
    root = _Parser(
        base64.b64decode(
            rebuild_torrent_peer_payload(
                metadata.payload_base64, trackers=(), webseeds=(), nodes=()
            )
        )
    ).parse()
    assert b"url-list" not in root
    assert b"httpseeds" not in root
    assert b"info" in root


def test_rebuild_rejects_torrent_when_info_webseed_fails_validation() -> None:
    """info 内非法 webseed 无法剔除（会改 info hash），只能整种子拒绝。"""
    payload = base64.b64encode(
        bencode({
            "info": {"length": 1, "name": "x", "url-list": ["http://127.0.0.1/a"]},
        })
    ).decode()
    with pytest.raises(DomainError) as caught:
        rebuild_torrent_peer_payload(payload, trackers=(), webseeds=(), nodes=())
    assert caught.value.code is ErrorCode.INVALID_INPUT


def test_rebuild_accepts_torrent_when_info_webseed_is_validated() -> None:
    payload = base64.b64encode(
        bencode({
            "info": {"length": 1, "name": "x", "url-list": ["https://good.example/a"]},
        })
    ).decode()
    root = _Parser(
        base64.b64decode(
            rebuild_torrent_peer_payload(
                payload, trackers=(), webseeds=("https://good.example/a",), nodes=()
            )
        )
    ).parse()
    assert root[b"info"][b"url-list"] == [b"https://good.example/a"]


def test_rebuild_drops_single_string_webseed_when_not_whitelisted() -> None:
    """单字符串形态的 url-list 未通过校验时整键去除（不留下空字符串占位）。"""
    payload = base64.b64encode(
        bencode({"info": {"length": 1, "name": "x"}, "url-list": "http://127.0.0.1/a"})
    ).decode()
    root = _Parser(
        base64.b64decode(
            rebuild_torrent_peer_payload(payload, trackers=(), webseeds=(), nodes=())
        )
    ).parse()
    assert b"url-list" not in root


@pytest.mark.parametrize("value", [1, {"nested": "x"}])
def test_rebuild_ignores_non_string_non_list_webseed_values(value: object) -> None:
    """非字符串、非列表形态的 url-list 无法解释，直接去掉该键而不是崩溃。"""
    payload = base64.b64encode(
        bencode({"info": {"length": 1, "name": "x"}, "url-list": value})
    ).decode()
    root = _Parser(
        base64.b64decode(
            rebuild_torrent_peer_payload(payload, trackers=(), webseeds=(), nodes=())
        )
    ).parse()
    assert b"url-list" not in root
