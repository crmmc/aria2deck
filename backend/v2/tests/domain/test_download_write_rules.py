from __future__ import annotations

import pytest

from aria2deck.domain.downloads import (
    NormalizedResource,
    adopted_display_name,
    magnet_missing_name_fallback,
    normalize_download_options,
    normalize_resource,
    resource_display_name,
    submission_pause_options,
)
from aria2deck.domain.errors import DomainError
from aria2deck.domain.settings import merge_trackers, parse_tracker_text


def test_http_identity_includes_headers_and_mirrors_but_display_is_safe() -> None:
    resource = normalize_resource("https://example.com/file.iso?password=secret")
    first, first_key = normalize_download_options(
        resource,
        {"headers": {"X-Auth": "one"}, "mirrors": ["https://mirror.example/file.iso", "https://mirror.example/file.iso"]},
    )
    second, second_key = normalize_download_options(
        resource, {"headers": {"X-Auth": "two"}}
    )
    assert first == {"headers": {"x-auth": "one"}, "mirrors": ["https://mirror.example/file.iso"]}
    assert first_key != second_key != resource.resource_key
    assert resource_display_name(resource, resource.canonical_uri) == "file.iso"
    assert resource_display_name(normalize_resource("https://example.com/"), "https://example.com/") == "download"
    magnet = normalize_resource("magnet:?xt=urn:btih:" + "ab" * 20 + "&dn=My+File")
    assert resource_display_name(magnet, "magnet:?xt=urn:btih:" + "ab" * 20 + "&dn=My+File") == "My File"


@pytest.mark.parametrize(
    "uri,options",
    [
        ("https://example.com", {"unknown": True}),
        ("https://example.com", {"headers": []}),
        ("https://example.com", {"mirrors": {}}),
        ("https://example.com", {"headers": {"Host": "other"}}),
        ("https://example.com", {"headers": {"Bad Header": "x"}}),
        ("https://example.com", {"headers": {"X-Test": "line\nbreak"}}),
        ("https://example.com", {"headers": {1: "x"}}),
        ("https://example.com", {"mirrors": [1]}),
        ("https://example.com", {"mirrors": ["magnet:?xt=urn:btih:" + "ab" * 20]}),
        ("magnet:?xt=urn:btih:" + "ab" * 20, {"headers": {"X": "y"}}),
        ("https://example.com", {"headers": {str(index): "x" for index in range(51)}}),
        ("https://example.com", {"mirrors": ["https://example.com"] * 11}),
        ("https://user@example.com/file", {}),
    ],
)
def test_write_options_reject_ambiguous_or_unsafe_values(uri: str, options: object) -> None:
    with pytest.raises(DomainError):
        normalize_download_options(normalize_resource(uri), options)


def test_pause_policy_is_kind_specific_and_closed() -> None:
    assert submission_pause_options("http") == {"pause": "true", "pause-metadata": "true"}
    assert submission_pause_options("magnet") == {"pause-metadata": "true"}
    assert submission_pause_options("torrent") == {"pause": "true"}
    with pytest.raises(DomainError):
        submission_pause_options("ftp")


def test_tracker_merge_parses_remote_text_deduplicates_and_caps() -> None:
    text = "# comment\n\nudp://b.example:80\ninvalid\nudp://a.example:80\n"
    assert parse_tracker_text(text) == ("udp://b.example:80", "udp://a.example:80")
    assert merge_trackers(
        ("udp://a.example:80",), ("udp://a.example:80", "udp://b.example:80")
    ) == ("udp://a.example:80", "udp://b.example:80")
    assert len(merge_trackers(tuple(f"udp://{index}.example" for index in range(5001)))) == 5000
    with pytest.raises(DomainError):
        merge_trackers(("not a tracker",))


def test_display_name_fallback_and_validation_cover_non_uri_sources() -> None:
    torrent = NormalizedResource("torrent", "torrent", "torrent:key", "torrent")
    assert resource_display_name(torrent, "ignored") == "download"
    with pytest.raises(DomainError):
        resource_display_name(normalize_resource("https://example.com/bad%0Aname"), "ignored")


INFO_HASH = "ab" * 20
MAGNET_URI = f"magnet:?xt=urn:btih:{INFO_HASH}"


def test_magnet_without_dn_uses_info_hash_and_shares_one_fallback_definition() -> None:
    """无 dn 的磁力任务显示名就是 canonical URI 尾段：创建与改名共用同一函数。"""
    resource = normalize_resource(MAGNET_URI)
    assert magnet_missing_name_fallback(resource.canonical_uri) == INFO_HASH
    assert resource_display_name(resource, MAGNET_URI) == INFO_HASH


@pytest.mark.parametrize(
    "kind,canonical_uri,observed_name,expected",
    [
        ("magnet", MAGNET_URI, "Big Buck Bunny", "Big Buck Bunny"),
        # 观测名就是兜底名：不改；空观测名：不改。
        ("magnet", MAGNET_URI, INFO_HASH, None),
        ("magnet", MAGNET_URI, None, None),
        ("magnet", MAGNET_URI, "", None),
        # 非 magnet 任务没有哈希兜底名，永不改名。
        ("http", MAGNET_URI, "file.bin", None),
        ("torrent", MAGNET_URI, "seed.bin", None),
        # 观测名不能当文件名用时拒绝改名（不落库非法名）。
        ("magnet", MAGNET_URI, "a/b.bin", None),
        ("magnet", MAGNET_URI, "x" * 201, None),
    ],
)
def test_adopted_display_name_only_replaces_magnet_hash_fallback(
    kind: str, canonical_uri: str, observed_name: str | None, expected: str | None
) -> None:
    assert (
        adopted_display_name(
            kind=kind, canonical_uri=canonical_uri, observed_name=observed_name
        )
        == expected
    )


def test_tracker_merge_skips_blank_entries() -> None:
    assert merge_trackers((" ", "udp://a.example:80")) == ("udp://a.example:80",)


def test_write_options_accept_v1_parity_whitelist_and_key_identity_rules() -> None:
    """A9：out / http-user / http-passwd / max-connection-per-server 进入白名单。

    身份口径与旧版一致：凭据参与资源身份（同 URI 不同凭据是不同下载），
    out 与连接数不参与（换名/换连接数仍是同一资源）。
    """
    resource = normalize_resource("https://example.com/file.iso")
    base, base_key = normalize_download_options(resource, {})
    with_out, out_key = normalize_download_options(resource, {"out": "renamed.bin"})
    with_credentials, credential_key = normalize_download_options(
        resource, {"http-user": "alice", "http-passwd": "secret"}
    )
    with_connections, connections_key = normalize_download_options(
        resource, {"max-connection-per-server": 4}
    )

    assert with_out["out"] == "renamed.bin"
    assert with_credentials["http-user"] == "alice"
    assert with_credentials["http-passwd"] == "secret"
    assert with_connections["max-connection-per-server"] == 4
    assert out_key == base_key == connections_key
    assert credential_key != base_key
    # 凭据任意变化（含仅用户名）都会改变身份；缺失与空串也不同。
    _, half_key = normalize_download_options(resource, {"http-user": "alice"})
    _, empty_password_key = normalize_download_options(
        resource, {"http-user": "alice", "http-passwd": ""}
    )
    assert half_key != credential_key != empty_password_key


@pytest.mark.parametrize(
    "options",
    [
        {"out": "a/b.bin"},  # 路径形 out：防逃逸
        {"out": "a\\b.bin"},
        {"out": ".."},
        {"out": "."},
        {"out": ""},
        {"out": 123},
        {"http-user": "line\nbreak"},
        {"http-passwd": "null\x00byte"},
        {"http-user": "x" * 4097},
        {"max-connection-per-server": 0},
        {"max-connection-per-server": 17},
        {"max-connection-per-server": "4"},
        {"max-connection-per-server": True},
    ],
)
def test_new_write_options_reject_unsafe_or_invalid_values(options: object) -> None:
    with pytest.raises(DomainError):
        normalize_download_options(normalize_resource("https://example.com/f"), options)


def test_magnet_accepts_out_and_connections_but_rejects_credentials() -> None:
    magnet = normalize_resource("magnet:?xt=urn:btih:" + "ab" * 20)
    normalized, key = normalize_download_options(
        magnet, {"out": "bt-name.bin", "max-connection-per-server": 8}
    )
    assert normalized["out"] == "bt-name.bin"
    assert normalized["max-connection-per-server"] == 8
    assert key == magnet.resource_key
    with pytest.raises(DomainError):
        normalize_download_options(magnet, {"http-user": "alice"})
    with pytest.raises(DomainError):
        normalize_download_options(magnet, {"http-passwd": "secret"})
