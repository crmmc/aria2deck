"""Coverage gaps for app/services/settings_service.py."""

from __future__ import annotations

import pytest

from app.services import settings_service as ss


@pytest.fixture(autouse=True)
def _clear_cache():
    ss.clear_config_cache()
    yield
    ss.clear_config_cache()


class TestCoerce:
    def test_ws_reconnect_max_delay_float_string(self):
        assert ss.coerce_raw_config_value("ws_reconnect_max_delay", "60.5") == 60

    def test_int_column(self):
        assert ss.coerce_raw_config_value("history_retention_days", "30") == 30

    def test_string_column(self):
        assert ss.coerce_raw_config_value("site_title", "x") == "x"


class TestIntFloatConfigFallbacks:
    def test_int_default_on_garbage(self):
        ss._config_cache["max_task_size"] = ("abc", 0.0)
        assert ss.get_max_task_size() == 10 * 1024 * 1024 * 1024

    def test_int_min_max(self):
        ss._config_cache["pack_compression_level"] = ("99", 0.0)
        assert ss.get_pack_compression_level() == 9
        ss._config_cache["pack_compression_level"] = ("-3", 0.0)
        assert ss.get_pack_compression_level() == 0

    def test_float_default_on_garbage(self):
        ss._config_cache["ws_reconnect_jitter"] = ("zz", 0.0)
        assert ss.get_ws_reconnect_jitter() == 0.2

    def test_float_bounds(self):
        ss._config_cache["ws_reconnect_factor"] = ("1.0", 0.0)
        assert ss.get_ws_reconnect_factor() == 1.1
        ss._config_cache["ws_reconnect_factor"] = ("99", 0.0)
        assert ss.get_ws_reconnect_factor() == 10.0

    def test_site_title_default(self):
        assert ss.get_site_title() == "Aria2 控制器"
        ss._config_cache["site_title"] = ("", 0.0)
        assert ss.get_site_title() == "Aria2 控制器"


def test_masked_secret():
    assert ss._masked_secret(None) == ""
    assert ss._masked_secret("short") == "*****"
    assert ss._masked_secret("a-very-long-secret") == "*" * 8


def test_decode_hidden_extensions_invalid():
    assert ss._decode_hidden_extensions("not-json") == []
    assert ss._decode_hidden_extensions('{"a":1}') == []
    assert ss._decode_hidden_extensions(None) == []


def test_normalize_hidden_extensions():
    assert ss._normalize_hidden_extensions([" ZIP ", ".Tar", "", "zip"]) == [
        ".zip",
        ".tar",
    ]


def test_api_pack_format():
    assert ss._api_pack_format("7z") == "tar.zst"
    assert ss._api_pack_format("rar") == "zip"


def test_get_config_value_sync_uses_default():
    assert ss.get_config_value_sync("site_title") == "Aria2 控制器"


class TestPayloadToDbColumns:
    def test_skips_none_unknown_masked(self):
        values, changed = ss.payload_to_db_columns(
            {
                "site_title": None,
                "unknown_key": 1,
                "aria2_rpc_secret": "****",
            }
        )
        assert values == {}
        assert changed == []

    def test_normalizes_pack_format(self):
        values, changed = ss.payload_to_db_columns({"pack_format": "7z"})
        assert values == {"pack_format": "tar.zst"}
        assert changed == ["pack_format"]

    def test_rejects_bad_pack_format(self):
        values, changed = ss.payload_to_db_columns({"pack_format": "rar"})
        assert values == {}

    def test_float_and_int_coercion(self):
        values, changed = ss.payload_to_db_columns(
            {
                "ws_reconnect_jitter": "0.3",
                "ws_reconnect_max_delay": "90",
            }
        )
        assert values == {
            "ws_reconnect_jitter": "0.3",
            "ws_reconnect_max_delay": 90,
        }
        assert changed == ["ws_reconnect_jitter", "ws_reconnect_max_delay"]

    def test_hidden_extensions_serialized(self):
        values, _ = ss.payload_to_db_columns({"hidden_file_extensions": ["zip"]})
        assert values == {"hidden_file_extensions_json": '[".zip"]'}


@pytest.mark.asyncio
async def test_get_api_settings_missing_row(monkeypatch):
    async def none_row():
        return None

    monkeypatch.setattr(ss.settings_repo, "get_settings_row", none_row)
    with pytest.raises(RuntimeError):
        await ss.get_api_settings()


@pytest.mark.asyncio
async def test_update_api_settings_missing_row(monkeypatch):
    async def none_row(values):
        return None

    monkeypatch.setattr(ss.settings_repo, "update_settings_row", none_row)
    with pytest.raises(RuntimeError):
        await ss.update_api_settings({"site_title": "x"})


@pytest.mark.asyncio
async def test_validate_download_settings_zero_total_ok():
    ss.validate_download_settings({"download_total_connections": 0})


@pytest.mark.asyncio
async def test_validate_download_settings_over_allocated():
    from app.domain.errors import BadRequestError

    with pytest.raises(BadRequestError):
        ss.validate_download_settings(
            {
                "download_total_connections": 5,
                "download_authenticated_reserved_connections": 4,
                "download_anonymous_base_connections": 2,
                "download_anonymous_borrow_connections": 1,
            }
        )


@pytest.mark.asyncio
async def test_validate_tracker_settings_interval(monkeypatch):
    from app.domain.errors import BadRequestError

    with pytest.raises(BadRequestError):
        await ss.validate_tracker_settings({"tracker_refresh_interval_minutes": 3})


@pytest.mark.asyncio
async def test_validate_tracker_settings_remote_url_invalid(monkeypatch):
    from app.domain.errors import BadRequestError

    async def reject(url, *, allowed_schemes):
        return "bad url"

    monkeypatch.setattr(ss, "check_url_ssrf", reject)
    with pytest.raises(BadRequestError):
        await ss.validate_tracker_settings({"tracker_remote_urls": "ftp://x/y"})


@pytest.mark.asyncio
async def test_validate_tracker_settings_fixed_list_bad_scheme():
    from app.domain.errors import BadRequestError

    with pytest.raises(BadRequestError):
        await ss.validate_tracker_settings({"tracker_fixed_list": "ftp://x/ann"})


@pytest.mark.asyncio
async def test_validate_tracker_settings_fixed_list_too_long():
    from app.domain.errors import BadRequestError
    from app.services import tracker_list_service

    with pytest.raises(BadRequestError):
        await ss.validate_tracker_settings(
            {
                "tracker_fixed_list": (
                    "http://x.example/ann?".ljust(tracker_list_service.MAX_TRACKER_ENTRY_LENGTH + 1, "a")
                )
            }
        )


@pytest.mark.asyncio
async def test_update_api_settings_with_runtime_refresh_no_keys(monkeypatch):
    async def fake_update(payload):
        return ss.SettingsUpdateResult(settings={}, changed_keys=["site_title"])

    async def fake_load():
        return None

    monkeypatch.setattr(ss, "update_api_settings", fake_update)
    monkeypatch.setattr(ss, "load_runtime_config", fake_load)
    result = await ss.update_api_settings_with_runtime_refresh({"site_title": "x"})
    assert result.changed_keys == ["site_title"]


@pytest.mark.asyncio
async def test_update_api_settings_with_runtime_refresh_tracker(monkeypatch):
    async def fake_update(payload):
        return ss.SettingsUpdateResult(
            settings={"tracker_fixed_list": ""}, changed_keys=["tracker_fixed_list"]
        )

    async def fake_load():
        return None

    async def fake_apply(fixed_list):
        raise RuntimeError("merge failed")

    monkeypatch.setattr(ss, "update_api_settings", fake_update)
    monkeypatch.setattr(ss, "load_runtime_config", fake_load)
    monkeypatch.setattr(ss.tracker_list_service, "apply_fixed_list", fake_apply)

    async def fail_refresh():
        raise RuntimeError("refresh failed")

    monkeypatch.setattr(
        ss.tracker_list_service, "refresh_remote_trackers", fail_refresh
    )
    result = await ss.update_api_settings_with_runtime_refresh(
        {"tracker_fixed_list": ""}
    )
    assert result.changed_keys == ["tracker_fixed_list"]


@pytest.mark.asyncio
async def test_get_config_value_unknown_key():
    assert await ss.get_config_value("not_a_key") is None


@pytest.mark.asyncio
async def test_get_config_value_missing_row(monkeypatch):
    ss.clear_config_cache()
    async def none_row():
        return None

    monkeypatch.setattr(ss.settings_repo, "get_settings_row", none_row)
    assert await ss.get_config_value("site_title") is None


@pytest.mark.asyncio
async def test_get_config_value_cached(monkeypatch):
    ss._config_cache["site_title"] = ("cached", ss.time())
    assert await ss.get_config_value("site_title") == "cached"


@pytest.mark.asyncio
async def test_get_config_value_expired_reload(monkeypatch):
    ss._config_cache["site_title"] = ("stale", 0.0)

    class Row:
        def get(self, key):
            return "fresh"

    async def row_row():
        return Row()

    monkeypatch.setattr(ss.settings_repo, "get_settings_row", row_row)
    assert await ss.get_config_value("site_title") == "fresh"


@pytest.mark.asyncio
async def test_set_config_value_unknown_key():
    await ss.set_config_value("not_a_key", "v")
    assert ss._config_cache["not_a_key"][0] is None


@pytest.mark.asyncio
async def test_set_config_value_missing_row(monkeypatch):
    async def none_row(values):
        return None

    monkeypatch.setattr(ss.settings_repo, "update_settings_row", none_row)
    await ss.set_config_value("site_title", "x")
    assert ss._config_cache["site_title"][0] == "x"


@pytest.mark.asyncio
async def test_update_api_settings_with_runtime_refresh_aria2(monkeypatch):
    called = {}

    async def fake_update(payload):
        return ss.SettingsUpdateResult(
            settings={}, changed_keys=["aria2_rpc_url"]
        )

    async def fake_load():
        return None

    async def fake_refresh():
        called["refreshed"] = True

    monkeypatch.setattr(ss, "update_api_settings", fake_update)
    monkeypatch.setattr(ss, "load_runtime_config", fake_load)
    monkeypatch.setattr(ss, "refresh_aria2_config", fake_refresh)
    await ss.update_api_settings_with_runtime_refresh({"aria2_rpc_url": "http://x"})
    assert called.get("refreshed") is True


@pytest.mark.asyncio
async def test_clear_config_cache_async():
    ss._config_cache["site_title"] = ("x", 0.0)
    await ss.clear_config_cache_async()
    assert not ss._config_cache


def test_simple_getters():
    assert ss.get_min_free_disk() == 1024**3
    assert ss.get_aria2_bt_stop_timeout_seconds() == 7 * 24 * 60 * 60
    assert ss.get_pack_format() == "zip"
    assert ss.get_ws_reconnect_max_delay() == 60.0


def test_merged_download_settings_defaults_and_values():
    defaults = ss.merged_download_settings({})
    assert defaults["download_total_connections"] == ss.download_config.total_connections
    values = ss.merged_download_settings(
        {
            "download_total_connections": 3,
            "download_authenticated_reserved_connections": 1,
            "download_authenticated_per_user_connections": 1,
            "download_authenticated_per_file_connections": 1,
            "download_anonymous_base_connections": 1,
            "download_anonymous_borrow_connections": 1,
            "download_anonymous_per_ip_connections": 1,
            "download_anonymous_per_file_connections": 1,
        }
    )
    assert values["download_total_connections"] == 3


@pytest.mark.asyncio
async def test_update_with_runtime_refresh_download_keys(monkeypatch):
    async def fake_update(payload):
        return ss.SettingsUpdateResult(
            settings={}, changed_keys=["download_total_connections"]
        )

    async def fake_load():
        return None

    monkeypatch.setattr(ss, "update_api_settings", fake_update)
    monkeypatch.setattr(ss, "load_runtime_config", fake_load)
    await ss.update_api_settings_with_runtime_refresh(
        {"download_total_connections": 1000}
    )


@pytest.mark.asyncio
async def test_update_with_runtime_refresh_tracker_ok(monkeypatch):
    applied = {}
    refreshed = {}

    async def fake_update(payload):
        return ss.SettingsUpdateResult(
            settings={"tracker_fixed_list": "http://t/ann"},
            changed_keys=["tracker_fixed_list"],
        )

    async def fake_load():
        return None

    async def fake_apply(fixed_list):
        applied["fixed"] = fixed_list

    async def fake_refresh():
        refreshed["done"] = True

    monkeypatch.setattr(ss, "update_api_settings", fake_update)
    monkeypatch.setattr(ss, "load_runtime_config", fake_load)
    monkeypatch.setattr(ss.tracker_list_service, "apply_fixed_list", fake_apply)
    monkeypatch.setattr(
        ss.tracker_list_service, "refresh_remote_trackers", fake_refresh
    )
    await ss.update_api_settings_with_runtime_refresh(
        {"tracker_fixed_list": "http://t/ann"}
    )
    assert applied["fixed"] == "http://t/ann"
    await __import__("asyncio").sleep(0)
    assert refreshed.get("done") is True


@pytest.mark.asyncio
async def test_load_runtime_config(temp_db, monkeypatch):
    monkeypatch.setattr(
        ss.download_config, "load_from_settings", lambda row: None
    )
    monkeypatch.setattr(
        ss.rate_limit_config, "load_from_settings", lambda row: None
    )
    await ss.load_runtime_config()
    assert "site_title" in ss._config_cache


@pytest.mark.asyncio
async def test_refresh_aria2_config(temp_db):
    await ss.refresh_aria2_config()


@pytest.mark.asyncio
async def test_set_config_value_real(temp_db):
    await ss.set_config_value("site_title", "T")
    ss.clear_config_cache()
    assert await ss.get_config_value("site_title") == "T"


@pytest.mark.asyncio
async def test_get_and_update_api_settings_real(temp_db):
    settings = await ss.get_api_settings()
    assert "tracker_status" in settings
    result = await ss.update_api_settings({"site_title": "New"})
    assert result.changed_keys == ["site_title"]
    assert "site_title" in ss._config_cache


def test_get_config_value_sync_unknown_key():
    assert ss.get_config_value_sync("no_such_key") is None


def test_cache_settings_row_none():
    ss._cache_settings_row(None)


def test_serialize_config_value():
    assert ss.serialize_config_value(None) is None
    assert ss.serialize_config_value(5) == "5"
