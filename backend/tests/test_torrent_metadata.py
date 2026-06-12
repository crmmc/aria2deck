from __future__ import annotations

import base64
import hashlib

import pytest

from app.domain.torrent_metadata import (
    MAX_TORRENT_FILE_COUNT,
    TorrentMetadataError,
    build_selection_resource_key,
    parse_torrent_base64,
    validate_selected_indexes,
)


def bstr(value: bytes) -> bytes:
    return str(len(value)).encode("ascii") + b":" + value


def bint(value: int) -> bytes:
    return b"i" + str(value).encode("ascii") + b"e"


def bdict(items: list[tuple[bytes, bytes]]) -> bytes:
    return b"d" + b"".join(bstr(key) + value for key, value in items) + b"e"


def blist(values: list[bytes]) -> bytes:
    return b"l" + b"".join(values) + b"e"


def torrent_b64(info: bytes) -> str:
    torrent = bdict([(b"announce", bstr(b"http://tracker.example.com")), (b"info", info)])
    return base64.b64encode(torrent).decode("ascii")


def single_file_info() -> bytes:
    return bdict(
        [
            (b"name", bstr(b"ubuntu.iso")),
            (b"length", bint(1024)),
            (b"piece length", bint(16384)),
            (b"pieces", bstr(b"0" * 20)),
        ]
    )


def multi_file_info() -> bytes:
    return bdict(
        [
            (b"name", bstr(b"Fedora Workstation")),
            (
                b"files",
                blist(
                    [
                        bdict([(b"length", bint(4096)), (b"path", blist([bstr(b"iso.bin")]))]),
                        bdict(
                            [
                                (b"length", bint(48)),
                                (b"path", blist([bstr(b"docs"), bstr(b"release-notes.pdf")])),
                            ]
                        ),
                        bdict(
                            [
                                (b"length", bint(90)),
                                (b"path", blist([bstr(b"docs"), bstr(b"install.pdf")])),
                            ]
                        ),
                    ]
                ),
            ),
            (b"piece length", bint(16384)),
            (b"pieces", bstr(b"1" * 20)),
        ]
    )


def test_parse_single_file_torrent_metadata() -> None:
    info = single_file_info()
    metadata = parse_torrent_base64(torrent_b64(info))

    assert metadata.info_hash == hashlib.sha1(info).hexdigest()
    assert metadata.name == "ubuntu.iso"
    assert metadata.file_count == 1
    assert metadata.total_size == 1024
    assert metadata.files[0].index == 1
    assert metadata.files[0].path == ("ubuntu.iso",)
    assert metadata.files[0].size == 1024
    assert metadata.tree[0]["name"] == "ubuntu.iso"


def test_parse_multi_file_torrent_metadata() -> None:
    info = multi_file_info()
    metadata = parse_torrent_base64(torrent_b64(info))

    assert metadata.info_hash == hashlib.sha1(info).hexdigest()
    assert metadata.name == "Fedora Workstation"
    assert metadata.file_count == 3
    assert metadata.total_size == 4234
    assert [file.index for file in metadata.files] == [1, 2, 3]
    assert metadata.files[1].path == ("Fedora Workstation", "docs", "release-notes.pdf")
    assert metadata.tree[0]["type"] == "directory"
    assert metadata.tree[0]["children"][1]["name"] == "docs"


@pytest.mark.parametrize(
    "payload",
    [
        "",
        "not valid base64!!!",
        base64.b64encode(b"not bencode").decode("ascii"),
        base64.b64encode(bdict([(b"announce", bstr(b"x"))])).decode("ascii"),
    ],
)
def test_parse_rejects_invalid_torrent(payload: str) -> None:
    with pytest.raises(TorrentMetadataError):
        parse_torrent_base64(payload)


@pytest.mark.parametrize(
    "path_component",
    [b"", b".", b"..", b"/absolute", b"dir/name", b"dir\\name"],
)
def test_parse_rejects_unsafe_path_components(path_component: bytes) -> None:
    info = bdict(
        [
            (b"name", bstr(b"root")),
            (
                b"files",
                blist(
                    [
                        bdict(
                            [
                                (b"length", bint(1)),
                                (b"path", blist([bstr(path_component)])),
                            ]
                        )
                    ]
                ),
            ),
            (b"piece length", bint(16384)),
            (b"pieces", bstr(b"2" * 20)),
        ]
    )

    with pytest.raises(TorrentMetadataError):
        parse_torrent_base64(torrent_b64(info))


def test_parse_rejects_too_many_files() -> None:
    file_entry = bdict([(b"length", bint(1)), (b"path", blist([bstr(b"x.bin")]))])
    info = bdict(
        [
            (b"name", bstr(b"root")),
            (b"files", blist([file_entry] * (MAX_TORRENT_FILE_COUNT + 1))),
            (b"piece length", bint(16384)),
            (b"pieces", bstr(b"3" * 20)),
        ]
    )

    with pytest.raises(TorrentMetadataError, match="too many files"):
        parse_torrent_base64(torrent_b64(info))


def test_validate_selected_indexes() -> None:
    metadata = parse_torrent_base64(torrent_b64(multi_file_info()))

    assert validate_selected_indexes(metadata, None) == (1, 2, 3)
    assert validate_selected_indexes(metadata, [3, 1]) == (1, 3)

    with pytest.raises(TorrentMetadataError, match="empty selection"):
        validate_selected_indexes(metadata, [])
    with pytest.raises(TorrentMetadataError, match="duplicate"):
        validate_selected_indexes(metadata, [1, 1])
    with pytest.raises(TorrentMetadataError, match="out of range"):
        validate_selected_indexes(metadata, [4])


def test_build_selection_resource_key() -> None:
    info_hash = "0123456789abcdef0123456789abcdef01234567"

    assert build_selection_resource_key(info_hash, (1, 2, 3), total_file_count=3) == info_hash
    partial = build_selection_resource_key(info_hash, (1, 3), total_file_count=3)

    assert partial.startswith(f"{info_hash}:files:")
    assert partial != info_hash
    assert partial == build_selection_resource_key(info_hash, (3, 1), total_file_count=3)
