from __future__ import annotations

import pytest

from aria2deck.domain.downloads import (
    NormalizedResource,
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


def test_tracker_merge_skips_blank_entries() -> None:
    assert merge_trackers((" ", "udp://a.example:80")) == ("udp://a.example:80",)
