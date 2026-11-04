from __future__ import annotations

from dataclasses import replace

import pytest

from aria2deck.domain.errors import DomainError, ErrorCode
from aria2deck.domain.settings import (
    DEFAULT_SITE_TITLE,
    SECRET_MASK,
    SystemSettings,
    apply_settings_patch,
    normalize_extensions,
    normalize_site_title,
    normalize_string_list,
)

RPC_TEST_VALUE = "test-rpc-value"


@pytest.mark.parametrize(
    "field,value",
    [
        ("site_title", ""),
        ("site_title", "x" * 101),
        ("site_title", "bad\x00title"),
        ("max_task_size", -1),
        ("max_task_size", 2**63),
        ("min_free_disk", -1),
        ("min_free_disk", 2**63),
        ("aria2_rpc_secret", ""),
        ("aria2_rpc_secret", "x" * 4097),
        ("aria2_bt_stop_timeout_seconds", 0),
        ("aria2_bt_stop_timeout_seconds", 86401),
        ("pack_format", "7z"),
        ("pack_compression_level", -1),
        ("pack_compression_level", 23),
        ("ws_reconnect_max_delay_seconds", 0),
        ("ws_reconnect_max_delay_seconds", 301),
        ("ws_reconnect_jitter_seconds", -1),
        ("ws_reconnect_jitter_seconds", 31),
        ("ws_reconnect_factor", 0),
        ("ws_reconnect_factor", 11),
        ("history_retention_days", 0),
        ("history_retention_days", 3651),
        ("tracker_refresh_interval_minutes", 1),
        ("tracker_refresh_interval_minutes", 10081),
        ("updated_at_ms", -1),
    ],
)
def test_settings_reject_each_invalid_scalar(field: str, value: object) -> None:
    with pytest.raises(DomainError) as caught:
        replace(SystemSettings(), **{field: value})
    assert caught.value.code is ErrorCode.INVALID_INPUT


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/jsonrpc",
        "http:///jsonrpc",
        "http://user@example.com/jsonrpc",
        "http://:password@example.com/jsonrpc",
        "http://example.com/jsonrpc?secret=value",
        "http://example.com/jsonrpc#fragment",
        "http://example.com:invalid/jsonrpc",
        "http://[::1",
    ],
)
def test_rpc_url_rejects_ambiguous_or_unsafe_forms(url: str) -> None:
    with pytest.raises(DomainError) as caught:
        replace(SystemSettings(), aria2_rpc_url=url)
    assert caught.value.code is ErrorCode.INVALID_INPUT


@pytest.mark.parametrize(
    "field,value",
    [
        ("tracker_fixed_list", ["udp://tracker.example"] * 5001),
        ("tracker_fixed_list", [""]),
        ("tracker_fixed_list", ["udp://tracker.example/path with space"]),
        ("tracker_fixed_list", ["ftp://tracker.example"]),
        ("tracker_fixed_list", ["udp:///missing-host"]),
        ("tracker_fixed_list", ["udp://[::1"]),
        ("tracker_remote_urls", ["ftp://example.com/list"]),
        ("download_connection_limits", {"HTTP": 1}),
        ("download_connection_limits", {"http": 0}),
        ("download_connection_limits", {"http": 65}),
    ],
)
def test_settings_reject_invalid_collections(field: str, value: object) -> None:
    with pytest.raises(DomainError) as caught:
        replace(SystemSettings(), **{field: value})
    assert caught.value.code is ErrorCode.INVALID_INPUT


@pytest.mark.parametrize(
    "field",
    [
        "account_security_rate_limit",
        "authenticated_api_rate_limit",
        "public_api_rate_limit",
        "share_access_rate_limit",
        "create_task_rate_limit",
        "create_torrent_rate_limit",
        "create_pack_rate_limit",
        "aria2_test_rate_limit",
        "rpc_rate_limit",
        "file_search_rate_limit",
        "create_share_rate_limit",
    ],
)
def test_every_rate_limit_has_the_same_lower_boundary(field: str) -> None:
    with pytest.raises(DomainError):
        replace(SystemSettings(), **{field: 0})
    assert getattr(replace(SystemSettings(), **{field: 1_000_000}), field) == 1_000_000
    with pytest.raises(DomainError):
        replace(SystemSettings(), **{field: 1_000_001})


def test_valid_boundaries_and_urls_are_accepted() -> None:
    settings = SystemSettings(
        max_task_size=2**63 - 1,
        min_free_disk=2**63 - 1,
        aria2_rpc_url="https://[::1]:65535/jsonrpc",
        aria2_rpc_secret=RPC_TEST_VALUE,
        aria2_bt_stop_timeout_seconds=86400,
        tracker_fixed_list=("http://tracker.example:80", "https://tracker.example/announce"),
        tracker_remote_urls=("https://example.com/list",),
        tracker_refresh_interval_minutes=10080,
        download_connection_limits={"http": 64},
        updated_at_ms=1,
    )
    assert settings.tracker_refresh_interval_minutes == 10080
    assert replace(settings, tracker_refresh_interval_minutes=0).tracker_refresh_interval_minutes == 0
    assert replace(settings, tracker_refresh_interval_minutes=5).tracker_refresh_interval_minutes == 5


def test_normalizers_are_deterministic_and_preserve_order() -> None:
    assert normalize_site_title("  ") == DEFAULT_SITE_TITLE
    assert normalize_site_title("  Title  ") == "Title"
    assert normalize_extensions((" TMP ", ".part", ".tmp")) == (".tmp", ".part")
    assert normalize_string_list([" first ", "", "second", "first"]) == ("first", "second")
    assert normalize_string_list(("second", "first")) == ("second", "first")


@pytest.mark.parametrize("value", ["a/b", "a\\b", "a\x00b", ".", "x" * 33])
def test_extension_normalizer_rejects_path_like_values(value: str) -> None:
    with pytest.raises(DomainError):
        normalize_extensions([value])


def test_patch_normalizes_values_and_preserves_masked_secret() -> None:
    current = replace(SystemSettings(), aria2_rpc_secret=RPC_TEST_VALUE)

    updated = apply_settings_patch(
        current,
        {
            "site_title": "  ",
            "aria2_rpc_secret": SECRET_MASK,
            "hidden_file_extensions": ["TMP"],
            "tracker_fixed_list": [
                "udp://one.example",
                "udp://one.example",
                "udp://two.example",
            ],
            "tracker_remote_urls": [],
            "max_task_size": None,
        },
    )

    assert updated.site_title == DEFAULT_SITE_TITLE
    assert updated.aria2_rpc_secret == RPC_TEST_VALUE
    assert updated.hidden_file_extensions == (".tmp",)
    assert updated.tracker_fixed_list == ("udp://one.example", "udp://two.example")
    assert updated.max_task_size == current.max_task_size
    assert apply_settings_patch(current, {}) == current


@pytest.mark.parametrize(
    "changes",
    [
        {"unknown": 1},
        {"hidden_file_extensions": ".tmp"},
        {"tracker_fixed_list": "udp://tracker.example"},
        {"tracker_remote_urls": "https://example.com/list"},
    ],
)
def test_patch_rejects_unknown_fields_and_ambiguous_collection_types(
    changes: dict[str, object],
) -> None:
    with pytest.raises(DomainError) as caught:
        apply_settings_patch(SystemSettings(), changes)
    assert caught.value.code is ErrorCode.INVALID_INPUT
