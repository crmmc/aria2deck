"""Tests for state cache semantics and secret key safety checks."""
from types import SimpleNamespace

import pytest

from app.aria2.client import Aria2Client
from app.aria2.gateway import get_aria2_client, update_cached_aria2_config
from app.core.config import (
    DEFAULT_SECRET_KEY,
    LEGACY_SHARE_JWT_SECRET_ENV,
    SHARE_JWT_SECRET_ENV,
    Settings,
    check_secret_key,
    settings,
)
from app.services.settings_service import refresh_aria2_config


def test_check_secret_key_raises_on_default_in_non_debug(monkeypatch):
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "secret_key", DEFAULT_SECRET_KEY)

    with pytest.raises(RuntimeError):
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
