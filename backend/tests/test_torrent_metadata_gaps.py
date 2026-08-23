"""Coverage gaps for app/domain/torrent_metadata.py error branches."""

from __future__ import annotations

import base64

import pytest

from app.domain.torrent_metadata import (
    TorrentMetadataError,
    parse_torrent_base64,
    parse_torrent_bytes,
)


def bstr(value: bytes) -> bytes:
    return str(len(value)).encode("ascii") + b":" + value


def bint(value: int) -> bytes:
    return b"i" + str(value).encode("ascii") + b"e"


def bdict(items) -> bytes:
    return b"d" + b"".join(bstr(k) + v for k, v in items) + b"e"


def blist(values) -> bytes:
    return b"l" + b"".join(values) + b"e"


def single_file_info(**overrides) -> bytes:
    items = {
        b"name": bstr(b"ubuntu.iso"),
        b"length": bint(1024),
        b"piece length": bint(16384),
        b"pieces": bstr(b"0" * 20),
    }
    items.update({k.encode(): v for k, v in overrides.items()})
    return bdict(sorted(items.items(), key=lambda kv: kv[0]))


def wrap(info: bytes, **extra) -> bytes:
    items = {k.encode(): v for k, v in extra.items()}
    items[b"info"] = info
    return bdict(sorted(items.items(), key=lambda kv: kv[0]))


def test_root_not_dict():
    with pytest.raises(TorrentMetadataError, match="dictionary"):
        parse_torrent_bytes(blist([bstr(b"x")]))


def test_trailing_data():
    valid = wrap(single_file_info())
    with pytest.raises(TorrentMetadataError, match="trailing"):
        parse_torrent_bytes(valid + b"i1e")


def test_no_files():
    info = single_file_info(files=blist([]))
    with pytest.raises(TorrentMetadataError, match="no files"):
        parse_torrent_bytes(wrap(info))


def test_bad_announce_url():
    # announce 值不是字符串（bencode 整数）
    with pytest.raises(TorrentMetadataError, match="announce"):
        parse_torrent_bytes(wrap(single_file_info(), announce=bint(1)))


def test_non_ascii_announce():
    with pytest.raises(TorrentMetadataError, match="encoding|URL"):
        parse_torrent_bytes(
            wrap(single_file_info(), announce=bstr("http://x/\udc80".encode("utf-8", "surrogateescape") if False else bytes([0xff])))
        )


def test_empty_announce():
    with pytest.raises(TorrentMetadataError, match="empty"):
        parse_torrent_bytes(wrap(single_file_info(), announce=bstr(b"")))


def test_announce_list_not_list():
    with pytest.raises(TorrentMetadataError, match="announce-list"):
        parse_torrent_bytes(
            wrap(single_file_info(), **{"announce-list": bint(1)})
        )


def test_announce_list_tier_not_list():
    with pytest.raises(TorrentMetadataError, match="tier"):
        parse_torrent_bytes(
            wrap(single_file_info(), **{"announce-list": blist([bstr(b"http://t.example/a")])})
        )


def test_webseed_string_and_list_values():
    # 字符串 / 列表两种 url-list 均能解析（安全策略在解析之后才拦截）
    torrent = wrap(
        single_file_info(), announce=bstr(b"http://tracker.example.com")
    )
    metadata = parse_torrent_bytes(torrent)
    assert metadata.webseed_urls == ()

    for url_list in (bstr(b"http://w.example/f"), blist([bstr(b"http://w.example/f")])):
        torrent = wrap(
            single_file_info(),
            announce=bstr(b"http://tracker.example.com"),
            **{"url-list": url_list},
        )
        with pytest.raises(TorrentMetadataError):
            parse_torrent_bytes(torrent)


def test_webseed_invalid_type():
    # 本版本直接拒绝 webseed，先于类型分支
    with pytest.raises(TorrentMetadataError):
        parse_torrent_bytes(
            wrap(
                single_file_info(),
                announce=bstr(b"http://tracker.example.com"),
                **{"url-list": bint(3)},
            )
        )


def test_too_many_endpoints():
    tiers = blist([blist([bstr(b"http://t.example/a")]) for _ in range(100)])
    with pytest.raises(TorrentMetadataError, match="too many"):
        parse_torrent_bytes(wrap(single_file_info(), **{"announce-list": tiers}))


def test_files_not_list():
    with pytest.raises(TorrentMetadataError, match="files must be a list"):
        parse_torrent_bytes(wrap(single_file_info(files=bint(1))))


def test_file_entry_not_dict():
    with pytest.raises(TorrentMetadataError, match="file entry"):
        parse_torrent_bytes(wrap(single_file_info(files=blist([bstr(b"x")]))))


def test_file_path_not_list():
    with pytest.raises(TorrentMetadataError, match="path must be a list"):
        parse_torrent_bytes(
            wrap(single_file_info(files=blist([bdict([(b"path", bstr(b"a")), (b"length", bint(1))])])))
        )


def test_file_bad_length():
    with pytest.raises(TorrentMetadataError, match="length"):
        parse_torrent_bytes(
            wrap(
                single_file_info(
                    files=blist([bdict([(b"path", blist([bstr(b"a")])), (b"length", bstr(b"x"))])])
                )
            )
        )


def test_path_component_too_long():
    with pytest.raises(TorrentMetadataError, match="component too long"):
        parse_torrent_bytes(
            wrap(
                single_file_info(
                    files=blist(
                        [bdict([(b"path", blist([bstr(b"a" * 300)])), (b"length", bint(1))])]
                    )
                )
            )
        )


def test_path_too_deep():
    with pytest.raises(TorrentMetadataError, match="too deep"):
        parse_torrent_bytes(
            wrap(
                single_file_info(
                    files=blist(
                        [
                            bdict(
                                [
                                    (b"path", blist([bstr(b"a")] * 40)),
                                    (b"length", bint(1)),
                                ]
                            )
                        ]
                    )
                )
            )
        )


def test_path_too_long():
    component = b"a" * 200
    with pytest.raises(TorrentMetadataError, match="too long"):
        parse_torrent_bytes(
            wrap(
                single_file_info(
                    files=blist(
                        [
                            bdict(
                                [
                                    (b"path", blist([bstr(component)] * 25)),
                                    (b"length", bint(1)),
                                ]
                            )
                        ]
                    )
                )
            )
        )


def test_bencode_too_deep():
    data = b"d" + b"l" * 100 + b"e" * 100 + b"e"
    with pytest.raises(TorrentMetadataError):
        parse_torrent_bytes(data)


def test_unterminated_list():
    with pytest.raises(TorrentMetadataError):
        parse_torrent_bytes(b"l" + bint(1))


def test_dict_key_not_bytes():
    with pytest.raises(TorrentMetadataError, match="key must be bytes"):
        parse_torrent_bytes(b"d" + bint(1) + bint(1) + b"e")


def test_duplicate_dict_key():
    with pytest.raises(TorrentMetadataError, match="duplicate"):
        parse_torrent_bytes(b"d" + bstr(b"a") + bint(1) + bstr(b"a") + bint(2) + b"e")


def test_unterminated_dict():
    with pytest.raises(TorrentMetadataError):
        parse_torrent_bytes(b"d" + bstr(b"a"))


def test_invalid_int():
    with pytest.raises(TorrentMetadataError, match="integer"):
        parse_torrent_bytes(b"ixe")
    with pytest.raises(TorrentMetadataError, match="integer"):
        parse_torrent_bytes(b"i12x3e")


def test_invalid_string_length():
    with pytest.raises(TorrentMetadataError, match="string length"):
        parse_torrent_bytes(b"3x:abc")
    with pytest.raises(TorrentMetadataError, match="string length"):
        parse_torrent_bytes(b"999999999999999999999999:abc")


def test_string_exceeds_input():
    with pytest.raises(TorrentMetadataError, match="exceeds input"):
        parse_torrent_bytes(b"10:abc")


def test_base64_roundtrip_ok():
    torrent = wrap(
        single_file_info(), announce=bstr(b"http://tracker.example.com")
    )
    b64 = base64.b64encode(torrent).decode()
    metadata = parse_torrent_base64(b64)
    assert metadata.file_count == 1
