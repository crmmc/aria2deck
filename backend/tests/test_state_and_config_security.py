"""Tests for state cache semantics and secret key safety checks."""
from types import SimpleNamespace

import pytest

from app.aria2.client import Aria2Client
from app.aria2.gateway import get_aria2_client, update_cached_aria2_config
from app.core.config import (
    CREDENTIAL_PEPPER_ENV,
    DEFAULT_SECRET_KEY,
    INITIAL_ADMIN_PASSWORD_ENV,
    INTERNAL_BASE_URL_ENV,
    LEGACY_SHARE_JWT_SECRET_ENV,
    MIN_SECRET_KEY_BYTES,
    PREVIOUS_CREDENTIAL_PEPPER_ENV,
    SHARE_JWT_SECRET_ENV,
    Settings,
    check_secret_key,
    get_internal_base_url,
    settings,
)
from app.services.settings_service import refresh_aria2_config


def test_check_secret_key_raises_on_default_in_non_debug(monkeypatch):
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "secret_key", DEFAULT_SECRET_KEY)

    with pytest.raises(RuntimeError):
        check_secret_key()


def test_check_secret_key_raises_on_empty_value_in_non_debug(monkeypatch):
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "secret_key", "")

    with pytest.raises(RuntimeError):
        check_secret_key()


def test_check_secret_key_raises_on_whitespace_in_non_debug(monkeypatch):
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "secret_key", " " * MIN_SECRET_KEY_BYTES)

    with pytest.raises(RuntimeError):
        check_secret_key()


def test_check_secret_key_raises_below_minimum_bytes(monkeypatch):
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "secret_key", "x" * (MIN_SECRET_KEY_BYTES - 1))

    with pytest.raises(RuntimeError):
        check_secret_key()


def test_check_secret_key_accepts_minimum_bytes(monkeypatch):
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "secret_key", "x" * MIN_SECRET_KEY_BYTES)
    monkeypatch.setattr(settings, "credential_pepper", "p" * MIN_SECRET_KEY_BYTES)
    monkeypatch.setattr(settings, "dev_reset_admin_password", False)

    check_secret_key()


def test_check_secret_key_counts_utf8_bytes(monkeypatch):
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "secret_key", "密" * 11)
    monkeypatch.setattr(settings, "credential_pepper", "p" * MIN_SECRET_KEY_BYTES)
    monkeypatch.setattr(settings, "dev_reset_admin_password", False)

    check_secret_key()


def test_check_secret_key_requires_credential_pepper_in_non_debug(monkeypatch):
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "secret_key", "x" * MIN_SECRET_KEY_BYTES)
    monkeypatch.setattr(settings, "credential_pepper", "")

    with pytest.raises(RuntimeError, match=CREDENTIAL_PEPPER_ENV):
        check_secret_key()


def test_check_secret_key_rejects_short_previous_credential_pepper(monkeypatch):
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "secret_key", "s" * MIN_SECRET_KEY_BYTES)
    monkeypatch.setattr(settings, "credential_pepper", "c" * MIN_SECRET_KEY_BYTES)
    monkeypatch.setattr(settings, "previous_credential_pepper", "p" * 31)

    with pytest.raises(RuntimeError, match=PREVIOUS_CREDENTIAL_PEPPER_ENV):
        check_secret_key()


def test_check_secret_key_rejects_dev_password_reset_in_non_debug(monkeypatch):
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "secret_key", "x" * MIN_SECRET_KEY_BYTES)
    monkeypatch.setattr(settings, "credential_pepper", "p" * MIN_SECRET_KEY_BYTES)
    monkeypatch.setattr(settings, "dev_reset_admin_password", True)

    with pytest.raises(RuntimeError, match="仅允许在调试模式"):
        check_secret_key()


def test_check_secret_key_allows_default_in_debug(monkeypatch):
    monkeypatch.setattr(settings, "debug", True)
    monkeypatch.setattr(settings, "secret_key", DEFAULT_SECRET_KEY)

    check_secret_key()


def test_settings_reads_share_jwt_secret_from_new_env(monkeypatch):
    monkeypatch.delenv(SHARE_JWT_SECRET_ENV, raising=False)
    monkeypatch.delenv(LEGACY_SHARE_JWT_SECRET_ENV, raising=False)
    monkeypatch.setenv(SHARE_JWT_SECRET_ENV, "new-secret")

    configured = Settings()

    assert configured.secret_key == "new-secret"


def test_settings_prefers_new_share_jwt_secret_over_legacy(monkeypatch):
    monkeypatch.setenv(LEGACY_SHARE_JWT_SECRET_ENV, "legacy-secret")
    monkeypatch.setenv(SHARE_JWT_SECRET_ENV, "new-secret")

    configured = Settings()

    assert configured.secret_key == "new-secret"


def test_settings_accepts_legacy_share_jwt_secret_env(monkeypatch):
    monkeypatch.delenv(SHARE_JWT_SECRET_ENV, raising=False)
    monkeypatch.setenv(LEGACY_SHARE_JWT_SECRET_ENV, "legacy-secret")

    configured = Settings()

    assert configured.secret_key == "legacy-secret"


def test_settings_reads_initial_admin_password_from_explicit_env(monkeypatch):
    monkeypatch.setenv(INITIAL_ADMIN_PASSWORD_ENV, "configured admin passphrase")

    configured = Settings()

    assert configured.initial_admin_password == "configured admin passphrase"


def test_internal_base_url_defaults_to_loopback_running_port(monkeypatch):
    monkeypatch.setattr(settings, "internal_base_url", "")
    monkeypatch.setattr(settings, "port", 8123)

    assert get_internal_base_url() == "http://127.0.0.1:8123"


def test_settings_reads_explicit_internal_base_url(monkeypatch):
    monkeypatch.setenv(INTERNAL_BASE_URL_ENV, "http://app:8001")

    configured = Settings()

    assert configured.internal_base_url == "http://app:8001"


@pytest.mark.parametrize(
    "value",
    [
        "ftp://app:8001",
        "http://user:password@app:8001",
        "http://public.example.com:8001",
        "http://0.0.0.0:8001",
        "http://app:0",
        "http://app:8001/prefix",
        "http://app:8001?token=secret",
    ],
)
def test_internal_base_url_rejects_unsafe_values(monkeypatch, value: str):
    monkeypatch.setattr(settings, "internal_base_url", value)

    with pytest.raises(RuntimeError, match=INTERNAL_BASE_URL_ENV):
        get_internal_base_url()


def test_get_aria2_client_preserves_empty_cached_secret(monkeypatch):
    update_cached_aria2_config(
        rpc_url="http://cached:6800/jsonrpc",
        rpc_secret="",
    )

    monkeypatch.setattr(settings, "aria2_rpc_url", "http://env:6800/jsonrpc")
    monkeypatch.setattr(settings, "aria2_rpc_secret", "ENV_SECRET_SHOULD_NOT_APPLY")

    client = get_aria2_client()

    assert client._rpc_url == "http://cached:6800/jsonrpc"
    assert client._secret == ""


def test_get_aria2_client_refreshes_request_client_when_cache_changed():
    update_cached_aria2_config(
        rpc_url="http://new-rpc:6800/jsonrpc",
        rpc_secret="new-secret",
    )

    state_namespace = SimpleNamespace(
        aria2_client=Aria2Client("http://old-rpc:6800/jsonrpc", "old-secret"),
    )
    request = SimpleNamespace(app=SimpleNamespace(state=state_namespace))

    client = get_aria2_client(request=request)

    assert client._rpc_url == "http://new-rpc:6800/jsonrpc"
    assert client._secret == "new-secret"
    assert request.app.state.aria2_client is client


@pytest.mark.asyncio
async def test_refresh_aria2_config_preserves_empty_secret(monkeypatch):
    async def fake_get_config_value(key: str):
        if key == "aria2_rpc_url":
            return "http://db:6800/jsonrpc"
        if key == "aria2_rpc_secret":
            return ""
        return None

    from app.services import settings_service

    monkeypatch.setattr(settings_service, "get_config_value", fake_get_config_value)
    monkeypatch.setattr(settings, "aria2_rpc_secret", "ENV_SECRET_SHOULD_NOT_APPLY")

    await refresh_aria2_config()
    client = get_aria2_client()

    assert client._rpc_url == "http://db:6800/jsonrpc"
    assert client._secret == ""
